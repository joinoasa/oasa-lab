"""`oasa-lab job-run ID`: run one lab job. Installed, root-owned code, started by systemd as the job user.

systemd starts `oasa-lab-job@ID.service` (deploy/systemd/oasa-lab-job@.service) when the service asks for it
(polkit lets the service start and stop only units with that name pattern). Nothing runs as root. This program
treats the service as untrusted: the request it left in the spool folder is data to check, not an order.

  1. The id must be 32 lowercase hex characters. The request file must be a plain file (no link), small, with
     exactly the fields id, suite, seed, commit, each matching a strict pattern.
  2. If the owner's pause file exists, nothing starts. One job at a time: an exclusive lock in the results folder. A second job-run refuses (exit 75).
  3. The commit must be on the branch and not older than the floor, both checked in the root-controlled git
     mirror. It is cloned from the mirror into this job's own private temp folder, with a clean git environment.
  4. suite.toml is parsed with the installed parser. The transmit lock is applied here, from the root-owned
     policy file (rf_setup, confirm_wiring, fixed program folders, device file), before any suite code runs.
     `--confirm-wiring` is passed only when the policy allows it.
  5. `python -m oasa_lab run SUITE` runs from the checkout with a minimal environment. Its output and the
     evidence bundle go to results/ID/ (owned by the job user, readable by the service, never writable by it).
  6. results/ID/exit.json says how it ended. The service judges the bundle with its own installed checker.

The code of a commit on `main` does run here, as the job user, with radio access. That is why `main` is
protected. The service does not run it, and cannot choose the user, the folders, the programs or the wiring.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .gitsafe import GIT_SAFE, git_env
from .policy import (DEFAULT_POLICY, JOB_ID_RE, MAX_SEED, SHA_RE, SUITE_RE, Policy, PolicyError, confirm_for_run,
                     interlock, load_policy)
from .service import files
from .suites import parse_suite, suite_facts

EX_USAGE, EX_REFUSED, EX_BUSY = 2, 3, 75
REQUEST_FIELDS = {"id", "suite", "seed", "commit"}
MAX_REQUEST = 4096
MAX_SUITE_TOML = 65536
STDOUT_CAP = 50_000_000
BACKSTOP = 30.0   # job-run cuts a job this long after the service's own time limit
GIT_TIMEOUT = 120


class Refused(Exception):
    pass


class Stopped(BaseException):
    """systemd asked us to stop (SIGTERM) while no suite process was running."""


def read_request(spool_dir: str, job_id: str) -> dict:
    """The request the service left for this job, checked field by field."""
    try:
        fd = files.open_inside(spool_dir, f"{job_id}.json")
    except OSError as exc:
        raise Refused(f"no usable request for this job: {exc}") from None
    try:
        raw = os.read(fd, MAX_REQUEST + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_REQUEST:
        raise Refused("the request is too big")
    return validate_request(raw, job_id)


def validate_request(raw: bytes, job_id: str) -> dict:
    try:
        req = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refused("the request is not JSON") from None
    if not isinstance(req, dict):
        raise Refused("the request is not an object")
    if set(req) != REQUEST_FIELDS:
        raise Refused("the request must have exactly: " + ", ".join(sorted(REQUEST_FIELDS)))
    if req["id"] != job_id:
        raise Refused("the request is for another job")
    if not (isinstance(req["suite"], str) and SUITE_RE.match(req["suite"])):
        raise Refused("bad suite name")
    seed = req["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise Refused("bad seed")
    if not (isinstance(req["commit"], str) and SHA_RE.match(req["commit"])):
        raise Refused("bad commit id")
    return req


def _real_dir(path: str, owner: int | None = None, what: str = "folder") -> None:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise Refused(f"cannot use the {what} {path}: {exc.strerror}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"the {what} {path} is not a plain folder")
    if owner is not None and st.st_uid != owner:
        raise Refused(f"the {what} {path} is not owned by this user")


def _git(policy: Policy, *args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", *GIT_SAFE, *args], cwd=cwd, env=git_env(policy.git_config), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused(f"git {args[0]} timed out") from None
    if check and r.returncode != 0:
        raise Refused(f"git {args[0]} failed: {r.stderr.strip()[:200]}")
    return r


def clone_commit(policy: Policy, commit: str, dest: str) -> None:
    """The commit must be on the branch and at or after the floor (both checked in the mirror), then it is
    cloned from the mirror into `dest`, which this job alone can see."""
    mirror, ref = policy.mirror, f"refs/heads/{policy.branch}"
    _real_dir(mirror, what="git mirror")
    if os.lstat(mirror).st_mode & 0o022:
        raise Refused("the git mirror is writable by group or others")
    if _git(policy, f"--git-dir={mirror}", "rev-parse", "--verify", "-q", ref, check=False).returncode != 0:
        raise Refused(f"the mirror has no {policy.branch} branch")
    if _git(policy, f"--git-dir={mirror}", "merge-base", "--is-ancestor", commit, ref, check=False).returncode != 0:
        raise Refused(f"commit {commit[:12]} is not on {policy.branch}")
    if policy.min_commit and _git(policy, f"--git-dir={mirror}", "merge-base", "--is-ancestor", policy.min_commit,
                                  commit, check=False).returncode != 0:
        raise Refused(f"commit {commit[:12]} is older than the lowest commit this lab runs ({policy.min_commit[:12]})")
    _git(policy, "clone", "-q", "--no-checkout", "--no-hardlinks", "--template=", mirror, dest)
    _git(policy, "checkout", "-q", "--detach", commit, cwd=dest)
    if _git(policy, "rev-parse", "HEAD", cwd=dest).stdout.strip() != commit:
        raise Refused("the checkout is not the requested commit")


def load_suite_facts(src: str, suite: str) -> dict:
    """suite.toml of the checkout, read without following links and parsed by the installed parser."""
    try:
        raw = files.read_inside(os.path.join(src, "suites"), suite, "suite.toml", limit=MAX_SUITE_TOML + 1)
    except OSError:
        raise Refused(f"no suite named {suite!r} at this commit") from None
    if len(raw) > MAX_SUITE_TOML:
        raise Refused("suite.toml is too big")
    try:
        return suite_facts(parse_suite(suite, raw.decode("utf-8")))
    except (ValueError, TypeError, AttributeError):
        raise Refused(f"suite.toml of {suite!r} cannot be read") from None


def decide(policy: Policy, facts: dict) -> str | None:
    """The transmit lock, applied from the root-owned policy. Returns the wiring to confirm (or None). Raises
    Refused if the suite may not run now."""
    why = interlock(facts, policy.rf_setup, policy.simulated, policy.confirm_wiring)
    if why:
        raise Refused(why)
    return confirm_for_run(facts, policy.simulated, policy.confirm_wiring)


def run_env(policy: Policy, job_id: str, commit: str, confirm: str | None, home: str, python: str) -> dict[str, str]:
    """The suite's whole environment. Nothing is inherited; every value is the policy's or ours."""
    env = {"PATH": f"{Path(python).parent}:/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "OASA_LAB_JOB_ID": job_id,
           "OASA_LAB_RF_SETUP": policy.rf_setup, "OASA_LAB_GIT_SHA": commit}
    if policy.simulated:
        env["OASA_LAB_SIM"] = "1"
    elif policy.devices_file:
        env["OASA_LAB_INVENTORY"] = policy.devices_file
    if policy.sec_bin_dir:
        env["OASA_SEC_BIN_DIR"] = policy.sec_bin_dir
    if policy.radio_bin_dir:
        env["OASA_RADIO_BIN_DIR"] = policy.radio_bin_dir
    if confirm:
        env["OASA_LAB_WIRING_CONFIRMED"] = confirm
    return env


def run_command(python: str, suite: str, seed: int, out: str, confirm: str | None) -> list[str]:
    cmd = [python, "-m", "oasa_lab", "run", suite, "--seed", str(seed), "--out", out]
    if confirm:
        cmd += ["--confirm-wiring", confirm]
    return cmd


def prune_results(results: str, keep: str) -> None:
    """Delete what earlier jobs left in the results folder (the service copied it out long ago: jobs run one
    at a time). Only this user can write here, and nothing is followed."""
    with os.scandir(results) as it:
        entries = list(it)
    for e in entries:
        if e.name in (keep, ".lock"):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path)
            else:
                os.unlink(e.path)
        except OSError:
            pass


def make_group_readable(top: str) -> None:
    """Let the service read the evidence: group read on everything, group execute on folders. Links are skipped."""
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        for name in [*dirnames, *filenames]:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    continue
                os.chmod(p, st.st_mode | 0o040 | (0o010 if stat.S_ISDIR(st.st_mode) else 0))
            except OSError:
                pass
    try:
        os.chmod(top, os.lstat(top).st_mode | 0o050)
    except OSError:
        pass


def write_exit(resdir: str, code: int | None, error: str | None = None, reason: str | None = None) -> None:
    """exit.json, written last and atomically, so the service never reads half of it."""
    tmp, final = os.path.join(resdir, ".exit.json.tmp"), os.path.join(resdir, "exit.json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w") as fh:
        json.dump({"code": code, "error": (error or "")[:300] or None, "reason": reason}, fh)
    os.rename(tmp, final)


class _Stop:
    """SIGTERM state: ask the suite's process group to stop, or abort the setup steps."""
    requested = False
    child: subprocess.Popen | None = None

    @classmethod
    def handler(cls, signum, frame) -> None:
        cls.requested = True
        if cls.child is not None:
            _signal_group(cls.child, signal.SIGTERM)
        else:
            raise Stopped()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def execute(cmd: list[str], env: dict[str, str], cwd: str, log_path: str, timeout: float, grace: float) -> tuple[int, str | None]:
    """Run the suite in its own process group, copy its output into the log (capped), stop it on SIGTERM or
    timeout. Returns (exit code, reason or None). A negative code is a signal."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    reason = None
    with os.fdopen(fd, "wb") as log:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        _Stop.child = proc
        try:
            if _Stop.requested:
                _signal_group(proc, signal.SIGTERM)
            start, written, killed_at, termed = time.monotonic(), 0, None, False
            open_pipe = True
            while open_pipe or proc.poll() is None:
                now = time.monotonic()
                if (_Stop.requested or now - start > timeout) and not termed:
                    termed = True
                    reason = "stopped" if _Stop.requested else f"timed out after {timeout:g} s"
                    _signal_group(proc, signal.SIGTERM)
                    killed_at = now + grace
                if killed_at is not None and now > killed_at:
                    _signal_group(proc, signal.SIGKILL)
                    killed_at = now + 3600
                if open_pipe:
                    r, _, _ = select.select([proc.stdout], [], [], 0.5)
                    if r:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            open_pipe = False
                        elif written < STDOUT_CAP:
                            part = chunk[: STDOUT_CAP - written]
                            log.write(part)
                            log.flush()
                            written += len(part)
                            if written >= STDOUT_CAP:
                                log.write(b"\n[output cut: limit reached]\n")
                                log.flush()
                else:
                    time.sleep(0.1)
            code = proc.wait()
            if _Stop.requested and reason is None:
                reason = "stopped"   # it ended on our SIGTERM before the loop looked again
        finally:
            _signal_group(proc, signal.SIGKILL)  # anything the suite left behind
            _Stop.child = None
            if proc.stdout:
                proc.stdout.close()
    return code, reason


def run_job(job_id: str, policy_path: str = DEFAULT_POLICY, *, policy_uid: int = 0, python: str | None = None) -> int:
    """Run one job. Returns the process exit status. `policy_uid` and `python` are for tests."""
    python = python or sys.executable
    os.umask(0o027)
    _Stop.requested = False
    if not (isinstance(job_id, str) and JOB_ID_RE.match(job_id)):
        print("job-run: the job id must be 32 lowercase hex characters", file=sys.stderr)
        return EX_USAGE
    try:
        policy = load_policy(policy_path, policy_uid)
        results = policy.results_dir
        _real_dir(results, os.geteuid(), "results folder")
    except (PolicyError, Refused) as exc:
        print(f"job-run: refused: {exc}", file=sys.stderr)
        return EX_REFUSED
    lock = os.open(os.path.join(results, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("job-run: refused: another job is running", file=sys.stderr)
        os.close(lock)
        return EX_BUSY
    tmp = None
    resdir = os.path.join(results, job_id)
    old = {s: signal.signal(s, _Stop.handler) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        prune_results(results, job_id)
        try:
            os.mkdir(resdir, 0o750)   # must not exist: an id runs once
        except OSError as exc:
            print(f"job-run: refused: cannot make {resdir}: {exc.strerror}", file=sys.stderr)
            return EX_REFUSED
        try:
            if os.path.lexists(policy.pause_file):   # the owner's switch, honored here too: a service cannot override it
                raise Refused("the lab is paused (the owner's pause file exists)")
            tmp = tempfile.mkdtemp(prefix="job-")
            req = read_request(policy.spool_dir, job_id)
            src = os.path.join(tmp, "src")
            clone_commit(policy, req["commit"], src)
            facts = load_suite_facts(src, req["suite"])
            confirm = decide(policy, facts)
            home = os.path.join(tmp, "home")
            os.mkdir(home, 0o700)
            env = run_env(policy, job_id, req["commit"], confirm, home, python)
            cmd = run_command(python, req["suite"], req["seed"], resdir, confirm)
            code, reason = execute(cmd, env, src, os.path.join(resdir, "stdout.log"),
                                 policy.job_timeout_seconds + BACKSTOP, policy.stop_grace_seconds)
            error = None
        except Refused as exc:
            code, reason, error = None, "refused", str(exc)
            _note(resdir, f"job-run: refused: {exc}\n")
        except Stopped:
            code, reason, error = None, "stopped", "stopped before the suite started"
        finally:
            make_group_readable(resdir)
        write_exit(resdir, code, error, reason)
        return 0 if code in (0, 1) else (EX_REFUSED if code is None else 1)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        os.close(lock)


def _note(resdir: str, text: str) -> None:
    """A refusal goes into the job's log too, so whoever reads the log sees why."""
    try:
        fd = os.open(os.path.join(resdir, "stdout.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    """`oasa-lab job-run ID [--policy FILE]`. Only the unit file (root's) picks --policy; the service cannot."""
    args = list(argv)
    policy = DEFAULT_POLICY
    if len(args) == 3 and args[1] == "--policy":
        policy = args[2]
        args = args[:1]
    if len(args) != 1:
        print("usage: oasa-lab job-run JOB_ID   (started by oasa-lab-job@.service; not for people)", file=sys.stderr)
        return EX_USAGE
    return run_job(args[0], policy)
