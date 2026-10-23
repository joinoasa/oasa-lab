"""The lab policy: what a job may do on this machine. Standard library only.

One root-owned file, `/etc/oasa-lab/lab-policy.toml`, read by two programs:

  - `oasa-lab serve` (user oasalab) to refuse a request early, with a clear message;
  - `oasa-lab job-run` (user oasalab-job, installed code) which decides again, before any suite code runs.

The service can read the file and cannot change it. So even a service that has been taken over cannot make
a job transmit: the job side applies the owner's rules itself. The rules (`interlock`) live here, once.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field

# A job id is a full random uuid4 in lowercase hex. The polkit rule (deploy/polkit/50-oasa-lab.rules) uses the
# same pattern; a test compares the two texts.
JOB_ID_PATTERN = r"[0-9a-f]{32}"
# \Z, not $: Python's $ also matches before a trailing newline, and a job id becomes a file name and a unit name
JOB_ID_RE = re.compile(rf"^{JOB_ID_PATTERN}\Z")
SUITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
MAX_SEED = 2**31 - 1

JOB_UNIT = "oasa-lab-job@{}.service"
MIRROR_UNIT = "oasa-lab-mirror.service"

RF_SAFE = ("cabled", "shielded")  # the only setups where a transmitting suite may run
RF_SETUPS = ("unset", "cabled", "shielded", "open")

DEFAULT_POLICY = "/etc/oasa-lab/lab-policy.toml"
DEFAULT_SPOOL = "/var/lib/oasa-lab/spool"
DEFAULT_RESULTS = "/var/lib/oasa-lab/results"
DEFAULT_MIRROR = "/var/lib/oasa-lab/mirror.git"
DEFAULT_GIT_CONFIG = "/etc/oasa-lab.d/gitconfig"
DEFAULT_PAUSE = "/etc/oasa-lab/PAUSE"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    rf_setup: str = "unset"
    simulated: bool = False                 # run on the simulated bench (tests, trying it out)
    devices_file: str = ""                  # the hardware suites' device file; jobs get it as OASA_LAB_INVENTORY
    branch: str = "main"
    min_commit: str = ""                    # the oldest commit a job may run (full id); "" = any commit on the branch
    job_timeout_seconds: float = 1800.0     # the service stops a job at this limit; job-run itself cuts it 30 s later
    stop_grace_seconds: float = 10.0        # SIGTERM to SIGKILL
    sec_bin_dir: str = ""                   # fixed folders of the oasa-sec / oasa-radio programs; never from a caller
    radio_bin_dir: str = ""
    confirm_wiring: dict[str, str] = field(default_factory=dict)   # suite -> the wiring the owner says is connected
    spool_dir: str = DEFAULT_SPOOL
    results_dir: str = DEFAULT_RESULTS
    mirror: str = DEFAULT_MIRROR
    git_config: str = DEFAULT_GIT_CONFIG    # root-owned git config that only trusts the mirror (safe.directory)
    pause_file: str = DEFAULT_PAUSE         # the owner's pause switch: while anything exists here no job starts


_SECTIONS = {
    "lab": {"rf_setup", "simulated", "devices_file", "branch", "min_commit", "job_timeout_seconds", "stop_grace_seconds"},
    "bins": {"sec", "radio"},
    "confirm_wiring": None,   # free keys: suite names
    "paths": {"spool", "results", "mirror", "git_config", "pause"},
}


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise PolicyError(msg)


def _abs(value: object, what: str, allow_empty: bool = True) -> str:
    _need(isinstance(value, str), f"{what} must be text")
    value = str(value)
    if not value:
        _need(allow_empty, f"{what} must not be empty")
        return value
    _need(os.path.isabs(value) and "\0" not in value and ".." not in value.split("/") and len(value) < 300,
          f"{what} must be a plain absolute path")
    return value


def parse_policy(data: dict) -> Policy:
    for name, body in data.items():
        _need(name in _SECTIONS and isinstance(body, dict), f"unknown section or entry {name!r}")
        if _SECTIONS[name] is not None:
            extra = set(body) - _SECTIONS[name]
            _need(not extra, f"[{name}]: unknown key(s) {', '.join(sorted(extra))}")
    lab, bins, paths = data.get("lab", {}), data.get("bins", {}), data.get("paths", {})
    p = Policy()
    rf = lab.get("rf_setup", p.rf_setup)
    _need(rf in RF_SETUPS, f"lab.rf_setup must be one of {', '.join(RF_SETUPS)}")
    sim = lab.get("simulated", False)
    _need(isinstance(sim, bool), "lab.simulated must be true or false")
    branch = lab.get("branch", p.branch)
    _need(isinstance(branch, str) and bool(re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch)) and not branch.startswith("-"),
          "bad lab.branch")
    floor = lab.get("min_commit", "")
    _need(isinstance(floor, str) and (not floor or bool(SHA_RE.match(floor))),
          "lab.min_commit must be a full 40-character commit id")
    timeout = lab.get("job_timeout_seconds", p.job_timeout_seconds)
    _need(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 1 <= timeout <= 3600,
          "lab.job_timeout_seconds must be 1 to 3600 (the job unit's RuntimeMaxSec is 3900)")
    grace = lab.get("stop_grace_seconds", p.stop_grace_seconds)
    _need(isinstance(grace, (int, float)) and not isinstance(grace, bool) and 0.1 <= grace <= 60,
          "lab.stop_grace_seconds must be 0.1 to 60")
    wiring: dict[str, str] = {}
    for suite, w in data.get("confirm_wiring", {}).items():
        _need(bool(SUITE_RE.match(suite)) and isinstance(w, str) and bool(NAME_RE.match(w)),
              f"confirm_wiring: bad entry {suite!r} = {w!r}")
        wiring[suite] = w
    return Policy(
        rf_setup=rf, simulated=sim, devices_file=_abs(lab.get("devices_file", ""), "lab.devices_file"),
        branch=branch, min_commit=floor, job_timeout_seconds=float(timeout), stop_grace_seconds=float(grace),
        sec_bin_dir=_abs(bins.get("sec", ""), "bins.sec"), radio_bin_dir=_abs(bins.get("radio", ""), "bins.radio"),
        confirm_wiring=wiring,
        spool_dir=_abs(paths.get("spool", DEFAULT_SPOOL), "paths.spool", False),
        results_dir=_abs(paths.get("results", DEFAULT_RESULTS), "paths.results", False),
        mirror=_abs(paths.get("mirror", DEFAULT_MIRROR), "paths.mirror", False),
        git_config=_abs(paths.get("git_config", DEFAULT_GIT_CONFIG), "paths.git_config"),
        pause_file=_abs(paths.get("pause", DEFAULT_PAUSE), "paths.pause", False))


def load_policy(path: str = DEFAULT_POLICY, owner_uid: int = 0) -> Policy:
    """Read the policy file. It must be a plain file (no link), owned by `owner_uid` (root) and writable by
    nobody else. Production never passes `owner_uid`; tests do."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise PolicyError(f"cannot read the lab policy {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        _need(stat.S_ISREG(st.st_mode), f"{path} is not a plain file")
        _need(st.st_uid == owner_uid, f"{path} must be owned by uid {owner_uid}, not {st.st_uid}")
        _need(not st.st_mode & 0o022, f"{path} must not be writable by group or others")
        _need(st.st_size <= 65536, f"{path} is too big")
        raw = fh.read(65537)
    try:
        return parse_policy(tomllib.loads(raw.decode("utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PolicyError(f"{path}: {exc}") from None


# --- the transmit lock ----------------------------------------------------------------------------------------
def wiring_to_confirm(confirm_wiring: dict[str, str], suite: dict) -> str | None:
    """The wiring name to pass as --confirm-wiring, or None. Only when the suite names a wiring and the
    owner's policy confirms exactly that one."""
    w = suite.get("wiring") or ""
    return w if w and confirm_wiring.get(suite["name"]) == w else None


def interlock(suite: dict, rf_setup: str, simulated: bool, confirm_wiring: dict[str, str]) -> str | None:
    """Why this suite may not run now, or None. 1) A suite that needs hardware must say whether it transmits.
    2) A suite that transmits needs rf_setup cabled or shielded. 3) On a real bench the owner must also have
    confirmed the suite's wiring in the policy. `suite`: {name, requires, transmits, transmits_declared, wiring}."""
    if not simulated and suite["requires"] and not suite["transmits_declared"]:
        return (f"refused: suite {suite['name']!r} needs hardware but its suite.toml does not say transmits = true "
                "or transmits = false")
    if not suite["transmits"]:
        return None
    if rf_setup not in RF_SAFE:
        return (f"refused: suite {suite['name']!r} transmits, and this lab's rf_setup is "
                f"{rf_setup!r} (must be one of: {', '.join(RF_SAFE)})")
    if not simulated and not wiring_to_confirm(confirm_wiring, suite):
        return (f"refused: suite {suite['name']!r} transmits on real hardware. Connect wiring "
                f"{suite['wiring'] or '(the suite names none)'!r} as in docs/hardware-suites.md, then set "
                f"[confirm_wiring] {suite['name']} = {suite['wiring']!r} in the lab policy file and restart the service")
    return None


def confirm_for_run(suite: dict, simulated: bool, confirm_wiring: dict[str, str]) -> str | None:
    """The wiring to pass as --confirm-wiring for this run: only a suite that transmits, on real hardware."""
    return wiring_to_confirm(confirm_wiring, suite) if suite["transmits"] and not simulated else None
