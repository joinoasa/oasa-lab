"""The one small program that runs as root: it starts a job as the unprivileged job user.

systemd runs one copy per connection to a Unix socket that only the service user can open
(deploy/systemd/oasa-lab-jobrun.socket). There is no sudo and no setuid file, so the service keeps its full
sandbox (no new privileges, no capabilities). The connection carries one line:

    run JOB_ID     start the job; its output streams back; when it ends the exit status is written to
                   jobs/JOB_ID/exit.json and the connection closes. Closing the connection early stops the job.
    stop JOB_ID    stop the job's systemd unit (SIGTERM, then SIGKILL to its whole cgroup)
    clean JOB_ID   delete the job's runs folder (it belongs to the job user, so the service cannot)

This program does not trust what it is given. The caller is whoever systemd says is on the socket (SO_PEERCRED).
The service passes only a job id. Everything else is read from root-owned files or checked here:

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
import select
import shutil
import socket
import stat
import struct
import subprocess
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


def _test_mode() -> bool:
    """Only a test sets this. In production the helper's environment is systemd's, which has no such variable,
    and the service (a client of the socket) cannot add to it."""
    return os.environ.get("OASA_LAB_JOBRUN_TEST") == "1"


def _tool(name: str, env_var: str) -> str:
    """systemd-run or systemctl; tests may point at a stand-in."""
    return os.environ.get(env_var, name) if _test_mode() else name


def _config():
    from .config import load_config

    path = CONFIG
    if _test_mode() and os.environ.get("OASA_LAB_RUNNER_CONFIG"):
        path = os.environ["OASA_LAB_RUNNER_CONFIG"]
    return load_config(path, {})


def peer_uid(sock: socket.socket) -> int:
    """The user id on the other end of the socket, as the kernel reports it."""
    pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


def _real_dir(path: Path, owner: int) -> None:
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{path} is not a plain folder")
    if st.st_uid != owner:
        raise Refused(f"{path} is not owned by the caller")


def _read_launch(jd: Path) -> dict:
    try:
        fd = os.open(jd / "launch.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise Refused(f"cannot read launch.json: {exc.strerror}") from None
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


def command(cfg, job_id: str, req: dict, src: Path, runs: Path, python: str, timeout: float, tail: list[str] | None = None) -> list[str]:
    """The systemd-run command line. Pure, so a test can read it. `tail` replaces the run command (the self-test)."""
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
    if tail is not None:
        return argv + ["--", python, *tail]
    argv += ["--", python, "-m", "oasa_lab", "run", req["suite"], "--seed", str(req["seed"]), "--out", str(runs)]
    if req["confirm"]:
        argv += ["--confirm-wiring", req["confirm"]]
    return argv


def _job_folder(cfg, job_id: str, caller: int) -> Path:
    if not JOB_ID.match(job_id):
        raise Refused("bad job id")
    jd = Path(cfg.state_dir) / "jobs" / job_id
    _real_dir(Path(cfg.state_dir) / "jobs", caller)
    _real_dir(jd, caller)
    return jd


def prepare(cfg, caller: int, job_id: str) -> tuple[list[str], Path]:
    """Check a run request and make the job's runs folder. Returns the systemd-run command and the job folder."""
    if cfg.sandbox.mode != "systemd":
        raise Refused("the config does not use the systemd sandbox")
    jd = _job_folder(cfg, job_id, caller)
    req = validated(cfg, job_id, _read_launch(jd))
    src, runs = jd / "src", jd / "runs"
    _real_dir(src, caller)
    if runs.exists() or runs.is_symlink():
        raise Refused("runs folder already exists")
    runs.mkdir(mode=0o755)
    if os.geteuid() == 0 and not _test_mode():
        try:
            pw = pwd.getpwnam(cfg.sandbox.user)
        except KeyError:
            raise Refused(f"there is no user {cfg.sandbox.user!r}; run deploy/05-service.sh") from None
        if pw.pw_uid == 0:
            raise Refused("the job user must not be root")
        os.chown(runs, pw.pw_uid, pw.pw_gid)
    return command(cfg, job_id, req, src, runs, sys.executable, cfg.job_timeout_seconds), jd


def _write_exit(jd: Path, code: int | None) -> None:
    """Record how the job ended, in the job folder, without following anything the service could have planted."""
    dfd = os.open(jd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            os.unlink("exit.json", dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open("exit.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        with os.fdopen(fd, "w") as fh:
            json.dump({"code": code}, fh)
    finally:
        os.close(dfd)


def _stop_unit(job_id: str) -> None:
    ctl = _tool("systemctl", "OASA_LAB_SYSTEMCTL")
    subprocess.run([ctl, "stop", "--quiet", f"{unit_name(job_id)}.service"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)


def do_run(sock: socket.socket, cfg, caller: int, job_id: str) -> None:
    """Start the job and relay its output until it ends. If the service hangs up, stop the job."""
    argv, jd = prepare(cfg, caller, job_id)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
    stopped = False
    try:
        while True:
            r, _, _ = select.select([proc.stdout, sock], [], [], 1.0)
            if sock in r:
                try:
                    gone = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
                except BlockingIOError:
                    gone = False
                except OSError:
                    gone = True
                if gone and not stopped:
                    stopped = True
                    _stop_unit(job_id)
            if proc.stdout in r:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                if not stopped:
                    try:
                        sock.sendall(chunk)
                    except OSError:
                        stopped = True
                        _stop_unit(job_id)
            elif proc.poll() is not None and not r:
                break
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    _write_exit(jd, code)


def do_stop(cfg, caller: int, job_id: str) -> None:
    _job_folder(cfg, job_id, caller)
    _stop_unit(job_id)


def do_clean(cfg, caller: int, job_id: str) -> None:
    jd = _job_folder(cfg, job_id, caller)
    runs = jd / "runs"
    if not runs.is_symlink() and runs.exists():
        shutil.rmtree(runs, ignore_errors=True)


def serve(sock: socket.socket, caller: int | None = None) -> int:
    """Handle one connection. `caller` is read from the socket unless a test says otherwise."""
    try:
        line = b""
        while not line.endswith(b"\n") and len(line) < 64:
            ch = sock.recv(1)
            if not ch:
                break
            line += ch
        parts = line.decode("ascii", errors="replace").split()
        if len(parts) != 2 or parts[0] not in ("run", "stop", "clean"):
            raise Refused("usage: run|stop|clean JOB_ID")
        uid = peer_uid(sock) if caller is None else caller
        cfg = _config()
        verb, job_id = parts
        if verb == "run":
            do_run(sock, cfg, uid, job_id)
        elif verb == "stop":
            do_stop(cfg, uid, job_id)
            sock.sendall(b"ok\n")
        else:
            do_clean(cfg, uid, job_id)
            sock.sendall(b"ok\n")
        return 0
    except Refused as exc:
        _say(sock, f"jobrun: refused: {exc}\n")
        return 3
    except OSError as exc:
        _say(sock, f"jobrun: {exc}\n")
        return 4
    except Exception as exc:  # a bad config must not look like a success
        _say(sock, f"jobrun: {type(exc).__name__}: {exc}\n")
        return 4


def _say(sock: socket.socket, text: str) -> None:
    try:
        sock.sendall(text.encode())
    except OSError:
        pass


SELFTEST = r"""
import os, sys, pwd
bad = []
me = pwd.getpwuid(os.getuid()).pw_name
if os.getuid() == 0 or me != sys.argv[1]:
    bad.append(f"running as {me}, expected {sys.argv[1]}")
for secret in ("/etc/oasa-lab/service.toml", "/etc/oasa-lab/env", sys.argv[2] + "/oauth.json", sys.argv[2] + "/audit.jsonl"):
    if os.path.exists(secret) and os.access(secret, os.R_OK):
        bad.append("can read " + secret)
try:
    seen = sorted(os.listdir(sys.argv[2]))
    if seen != ["jobs"] and seen != []:
        bad.append("sees the service state: " + ",".join(seen))
    others = [d for d in os.listdir(sys.argv[2] + "/jobs") if d != sys.argv[3]]
    if others:
        bad.append("sees other jobs: " + ",".join(others))
except OSError:
    pass
for path, want_write in ((sys.argv[4], False), (sys.argv[5], True)):
    try:
        open(os.path.join(path, ".probe"), "w").close(); os.unlink(os.path.join(path, ".probe")); wrote = True
    except OSError:
        wrote = False
    if wrote != want_write:
        bad.append(f"{path}: writable={wrote}, expected {want_write}")
try:
    open("/etc/oasa-lab.d/lab.toml").close()
except OSError as exc:
    bad.append("cannot read the device file: " + str(exc))
import oasa_lab
print("sandbox-bad: " + "; ".join(bad) if bad else "sandbox-ok")
sys.exit(1 if bad else 0)
"""


def selftest() -> int:
    """Run as root by hand (05-service.sh does): start a harmless command the way a job starts, and check
    from the inside that the sandbox is what it should be. Uses the real systemd."""
    cfg = _config()
    state = Path(cfg.state_dir)
    owner = os.stat(state).st_uid
    jobs = state / "jobs"
    jobs.mkdir(exist_ok=True)
    if not _test_mode():
        os.chown(jobs, owner, os.stat(state).st_gid)
    job_id = "5e1f7e57e57e"
    jd = jobs / job_id
    shutil.rmtree(jd, ignore_errors=True)
    for d in (jd, jd / "src"):
        d.mkdir(mode=0o755)
        if not _test_mode():
            os.chown(d, owner, os.stat(state).st_gid)
    req = validated(cfg, job_id, {"suite": "selftest", "sha": "0" * 40, "seed": 0, "env": {}})
    runs = jd / "runs"
    runs.mkdir(mode=0o755)
    if not _test_mode():
        try:
            pw = pwd.getpwnam(cfg.sandbox.user)
        except KeyError:
            print(f"there is no user {cfg.sandbox.user!r}; run deploy/05-service.sh", file=sys.stderr)
            return 1
        os.chown(runs, pw.pw_uid, pw.pw_gid)
    argv = command(cfg, job_id, req, jd / "src", runs, sys.executable, 60,
                   tail=["-I", "-c", SELFTEST, cfg.sandbox.user, str(state), job_id, str(jd / "src"), str(runs)])
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    print((r.stdout + r.stderr).strip())
    shutil.rmtree(jd, ignore_errors=True)
    return 0 if r.returncode == 0 and "sandbox-ok" in r.stdout else 1


def main(argv: list[str] | None = None) -> int:
    """`python -I -m oasa_lab.service.jobrun serve`: the socket is standard input (systemd, Accept=yes)."""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["selftest"]:
        return selftest()
    if argv != ["serve"]:
        print("usage: jobrun serve   (started by oasa-lab-jobrun@.service; the connection is stdin)\n"
              "       jobrun selftest   (run as root: checks the sandbox on this machine)", file=sys.stderr)
        return 2
    with socket.socket(fileno=os.dup(0)) as sock:
        return serve(sock)


if __name__ == "__main__":
    sys.exit(main())
