"""The one small program that runs as root: it starts a job as the unprivileged job user.

    sudo -n /opt/oasa-lab/venv/bin/python -I -m oasa_lab.service.jobrun run  JOB_ID
    sudo -n ...                                                          stop JOB_ID
    sudo -n ...                                                          clean JOB_ID

The service (user oasalab) may run this and nothing else as root (see deploy/sudoers). So it does not
trust what it is given. The service passes only a job id. Everything else is read from root-owned files or
checked here:

  - the lab config /etc/oasa-lab/service.toml (root-owned, the service cannot edit it) says who the job
    user is, the limits, the rf setup, the device file and which wirings the owner confirmed;
  - the job folder must be a real folder owned by the caller, its launch.json is read without following
    symlinks, and every field is matched against a strict pattern;
  - the command is always `python -m oasa_lab run SUITE --seed N --out RUNS` from the job's own source
    copy, as the job user, in a transient systemd unit (its own cgroup, so stop kills every process).

The job sees its source copy and its runs folder, nothing else of the service's state.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import stat
import sys
from pathlib import Path

CONFIG = "/etc/oasa-lab/service.toml"
JOB_ID = re.compile(r"^[0-9a-f]{12}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SUITE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ABS = re.compile(r"^/[A-Za-z0-9_./+-]{0,200}$")
ENV_PATTERNS = {   # the only variables a job gets from the service, and what each may look like
    "OASA_LAB_JOB_ID": JOB_ID,
    "OASA_LAB_REQUESTED_BY": NAME,
    "OASA_LAB_PROFILE": NAME,
    "OASA_LAB_DEVICES": re.compile(r"^[A-Za-z0-9_.,-]{0,300}$"),
    "OASA_SEC_BIN_DIR": ABS,
    "OASA_RADIO_BIN_DIR": ABS,
    "LANG": re.compile(r"^[A-Za-z0-9_.@-]{1,32}$"),
    "TZ": re.compile(r"^[A-Za-z0-9_./+-]{1,64}$"),
}
DEVICE_ALLOW = ("char-usb_device rw", "char-ttyUSB rw", "char-ttyACM rw")


class Refused(Exception):
    pass


def _direct() -> bool:
    """True when run by hand or by a test, not through sudo. sudo always sets SUDO_UID (overwriting any value
    the caller made up) and resets the rest of the environment, so the service user can never be 'direct'."""
    return "SUDO_UID" not in os.environ


def _tool(name: str, env_var: str) -> str:
    """systemd-run or systemctl. Tests may point at a stand-in; through sudo the path is never taken from the environment."""
    return os.environ.get(env_var, name) if _direct() else name


def _config():
    from .config import load_config

    path = CONFIG
    if _direct() and os.environ.get("OASA_LAB_RUNNER_CONFIG"):  # tests only: through sudo this is never read
        path = os.environ["OASA_LAB_RUNNER_CONFIG"]
    return load_config(path, {})


def _real_dir(path: Path, owner: int | None) -> None:
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{path} is not a plain folder")
    if owner is not None and st.st_uid != owner:
        raise Refused(f"{path} is not owned by the caller")


def _caller() -> int | None:
    """Who asked: under sudo, the service user. None when run directly (tests, or an admin at a shell)."""
    if _direct():
        return None
    try:
        return int(os.environ["SUDO_UID"])
    except ValueError:
        raise Refused("bad SUDO_UID") from None


def _read_launch(jd: Path) -> dict:
    fd = os.open(jd / "launch.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Refused("launch.json is not a plain file")
        raw = fh.read(20_000)
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused("launch.json is not JSON") from None
    if not isinstance(data, dict):
        raise Refused("launch.json is not an object")
    return data


def validated(cfg, job_id: str, launch: dict) -> dict:
    """The launch request, checked field by field, with the owner's config as the authority."""
    suite, sha, seed = launch.get("suite"), launch.get("sha"), launch.get("seed")
    if not (isinstance(suite, str) and SUITE.match(suite)):
        raise Refused("bad suite name")
    if not (isinstance(sha, str) and SHA.match(sha)):
        raise Refused("bad commit id")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise Refused("bad seed")
    confirm = launch.get("confirm_wiring")
    simulated = cfg.inventory_backend == "simulated"
    if confirm is not None:
        # a wiring confirmation must be one the owner wrote into the root-owned config, for this suite
        if simulated or not (isinstance(confirm, str) and NAME.match(confirm)) or cfg.confirm_wiring.get(suite) != confirm:
            raise Refused("wiring confirmation does not match the lab config")
    env_in = launch.get("env", {})
    if not isinstance(env_in, dict):
        raise Refused("bad env")
    env: dict[str, str] = {}
    for k, v in env_in.items():
        pat = ENV_PATTERNS.get(k)
        if pat is None or not isinstance(v, str) or not pat.match(v) or ".." in v.split("/"):
            raise Refused(f"environment variable {k!r} is not allowed or has a bad value")
        env[k] = v
    if env.get("OASA_LAB_JOB_ID", job_id) != job_id:
        raise Refused("job id mismatch")
    env["OASA_LAB_JOB_ID"] = job_id
    # from the owner's config, never from the caller
    env["OASA_LAB_RF_SETUP"] = cfg.rf_setup
    if simulated:
        env["OASA_LAB_SIM"] = "1"
    elif cfg.devices_file:
        env["OASA_LAB_INVENTORY"] = cfg.devices_file
    if confirm:
        env["OASA_LAB_WIRING_CONFIRMED"] = confirm
    return {"suite": suite, "sha": sha, "seed": seed, "confirm": confirm, "env": env}


def unit_name(job_id: str) -> str:
    return f"oasa-job-{job_id}"


def command(cfg, job_id: str, req: dict, src: Path, runs: Path, python: str, timeout: float) -> list[str]:
    """The systemd-run command line. Pure, so a test can read it."""
    sb, state = cfg.sandbox, str(cfg.state_dir)
    bindir = str(Path(python).parent)
    env = {**req["env"], "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    props = [
        "Type=exec", "KillMode=control-group", "KillSignal=SIGTERM", "SendSIGKILL=yes",
        f"TimeoutStopSec={max(1, int(cfg.stop_grace_seconds))}",
        f"RuntimeMaxSec={int(cfg.job_timeout_seconds + cfg.stop_grace_seconds + 30)}",
        f"MemoryMax={sb.memory_max}", f"TasksMax={sb.tasks_max}", "MemorySwapMax=0",
        f"WorkingDirectory={src}",
        # the file system: read-only everywhere, the service's state hidden, only this job's two folders visible
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", f"TemporaryFileSystem={state}:ro",
        f"BindReadOnlyPaths={src}", f"BindPaths={runs}", f"ReadWritePaths={runs}",
        "InaccessiblePaths=/etc/oasa-lab",   # the job gets its device file from /etc/oasa-lab.d, never this folder
        # the process
        "NoNewPrivileges=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectProc=invisible", "ProcSubset=pid",
        "SystemCallFilter=@system-service", "SystemCallArchitectures=native", "UMask=0022",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        # devices: USB radios and serial ports, nothing else
        "DevicePolicy=closed", *[f"DeviceAllow={d}" for d in DEVICE_ALLOW],
        "SupplementaryGroups=plugdev dialout",
    ]
    argv = [_tool("systemd-run", "OASA_LAB_SYSTEMD_RUN"),
            "--quiet", "--collect", "--wait", "--pipe", f"--unit={unit_name(job_id)}",
            f"--uid={sb.user}", f"--gid={sb.user}"]
    for k in sorted(env):
        argv.append(f"--setenv={k}={env[k]}")
    for p in props:
        argv += ["-p", p]
    argv += ["--", python, "-m", "oasa_lab", "run", req["suite"], "--seed", str(req["seed"]), "--out", str(runs)]
    if req["confirm"]:
        argv += ["--confirm-wiring", req["confirm"]]
    return argv


def _job_folder(cfg, job_id: str) -> Path:
    if not JOB_ID.match(job_id):
        raise Refused("bad job id")
    jd = Path(cfg.state_dir) / "jobs" / job_id
    _real_dir(Path(cfg.state_dir) / "jobs", _caller())
    _real_dir(jd, _caller())
    return jd


def run(job_id: str) -> int:
    cfg = _config()
    if cfg.sandbox.mode != "systemd":
        raise Refused("the config does not use the systemd sandbox")
    jd = _job_folder(cfg, job_id)
    req = validated(cfg, job_id, _read_launch(jd))
    src, runs = jd / "src", jd / "runs"
    _real_dir(src, _caller())
    if runs.exists() or runs.is_symlink():
        raise Refused("runs folder already exists")
    runs.mkdir(mode=0o755)
    if os.geteuid() == 0 and not _direct():
        try:
            pw = pwd.getpwnam(cfg.sandbox.user)
        except KeyError:
            raise Refused(f"there is no user {cfg.sandbox.user!r}; run deploy/05-service.sh") from None
        if pw.pw_uid == 0:
            raise Refused("the job user must not be root")
        os.chown(runs, pw.pw_uid, pw.pw_gid)
    argv = command(cfg, job_id, req, src, runs, sys.executable, cfg.job_timeout_seconds)
    os.execvp(argv[0], argv)  # replaces this process: the service sees systemd-run's exit status
    return 0  # only reached when a test replaced execvp


def stop(job_id: str) -> int:
    cfg = _config()
    _job_folder(cfg, job_id)
    ctl = _tool("systemctl", "OASA_LAB_SYSTEMCTL")
    os.execvp(ctl, [ctl, "stop", "--quiet", f"{unit_name(job_id)}.service"])
    return 0


def clean(job_id: str) -> int:
    cfg = _config()
    jd = _job_folder(cfg, job_id)
    runs = jd / "runs"
    if runs.is_symlink() or not runs.exists():
        return 0
    shutil.rmtree(runs, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("run", "stop", "clean"):
        print("usage: jobrun run|stop|clean JOB_ID", file=sys.stderr)
        return 2
    try:
        return {"run": run, "stop": stop, "clean": clean}[argv[0]](argv[1])
    except Refused as exc:
        print(f"jobrun: refused: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"jobrun: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
