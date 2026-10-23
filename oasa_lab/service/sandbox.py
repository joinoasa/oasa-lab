"""Starting and stopping a job. Two ways, chosen in the config.

`systemd` (the real lab): the service writes a small request into the spool folder and asks systemd to start
`oasa-lab-job@ID.service`. That unit is root's: it runs the installed `oasa-lab job-run ID` as the job user,
fully hardened, in its own cgroup. The service may only start and stop units with that name (a polkit rule,
deploy/polkit/50-oasa-lab.rules). It cannot pick the user, the program, the folders or the wiring. It learns how
the job ended from `results/ID/exit.json` and copies the evidence out without following links.

`none` (tests and development): the job runs as the service's own user in a new process group. Not a
sandbox; the config must say so on purpose.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..policy import JOB_ID_RE, JOB_UNIT, MIRROR_UNIT
from . import files
from .config import Config

# What a job inherits from the service in `none` mode. The service's own secrets are never among them.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV", "OASA_SEC_BIN_DIR", "OASA_RADIO_BIN_DIR")
# What `none` mode adds from the service's own environment (systemd mode takes all of this from the policy file).
ENV_REQUEST = ("LANG", "TZ", "OASA_SEC_BIN_DIR", "OASA_RADIO_BIN_DIR")
POLL = 0.25            # seconds between looks at a running unit
START_GRACE = 15.0     # how long a unit may stay "inactive" after the start call before we call it gone
EXIT_GRACE = 20.0      # how long after exit.json the unit may take to finish before we stop it
CTL_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


@dataclass
class Launch:
    job_id: str
    job_dir: Path          # state/jobs/<id>
    suite: str
    seed: int
    sha: str
    confirm_wiring: str | None
    env: dict[str, str] = field(default_factory=dict)   # OASA_LAB_* for `none` mode; systemd mode ignores it

    @property
    def src(self) -> Path:
        return self.job_dir / "src"

    @property
    def runs(self) -> Path:
        return self.job_dir / "runs"


def _signal(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class ProcessGroupSandbox:
    """No isolation: same user, own process group."""

    mode = "none"
    refresh_mirror = None

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def runs_dir(self, job_id: str, job_dir: Path) -> Path:
        return job_dir / "runs"

    async def start(self, launch: Launch, out) -> asyncio.subprocess.Process:
        launch.runs.mkdir(parents=True, exist_ok=True)
        env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
        env.update(launch.env)
        env.update({"HOME": str(launch.job_dir), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        cmd = [sys.executable, "-m", "oasa_lab", "run", launch.suite, "--seed", str(launch.seed), "--out", str(launch.runs)]
        if launch.confirm_wiring:
            cmd += ["--confirm-wiring", launch.confirm_wiring]
            env["OASA_LAB_WIRING_CONFIRMED"] = launch.confirm_wiring
        return await asyncio.create_subprocess_exec(*cmd, cwd=launch.src, env=env, stdout=out,
                                                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def stop(self, launch_id: str, proc: asyncio.subprocess.Process | None) -> None:
        """SIGTERM the whole process group, wait out the grace period, then SIGKILL."""
        if proc is None or proc.returncode is not None:
            return
        _signal(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_grace_seconds)
        except asyncio.TimeoutError:
            _signal(proc.pid, signal.SIGKILL)
            await proc.wait()
        _signal(proc.pid, signal.SIGKILL)  # anything the run left behind

    async def clean(self, job_id: str, job_dir: Path) -> None:
        shutil.rmtree(job_dir / "runs", ignore_errors=True)


class UnitProc:
    """Stands in for a process: the job is a systemd unit, and `results/ID/exit.json` says how it ended."""

    pid = None

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.detail: str | None = None      # why job-run refused or what went wrong, for the job's stop reason
        self._done = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode

    def finish(self, code: int | None, detail: str | None = None) -> None:
        if not self._done.is_set():
            self.returncode = code if code is not None else -1
            self.detail = detail
            self._done.set()


class SystemdSandbox:
    """A request in the spool folder, then `systemctl start oasa-lab-job@ID.service`. No sudo, no helper, no root code."""

    mode = "systemd"

    def __init__(self, cfg: Config):
        if cfg.policy is None:
            raise ValueError("systemd mode needs the lab policy")
        self.cfg = cfg
        self.spool, self.results = Path(cfg.policy.spool_dir), Path(cfg.policy.results_dir)
        self.ctl = cfg.sandbox.systemctl
        self._tasks: set[asyncio.Task] = set()

    def runs_dir(self, job_id: str, job_dir: Path) -> Path:
        return self.results / job_id

    # --- systemctl ---------------------------------------------------------------------------------
    async def _ctl(self, *args: str, timeout: float = 60) -> tuple[int, str]:
        """Run systemctl. Never raises: (exit status, its output) or (-1, why not)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ctl, "--no-ask-password", *args, env=CTL_ENV, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            return -1, f"cannot run {self.ctl}: {exc.strerror}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "systemctl timed out"
        return proc.returncode, out.decode(errors="replace").strip()[:400]

    async def refresh_mirror(self) -> None:
        """Ask for an update of the root-controlled git mirror (waits for it). A failure leaves the old mirror."""
        await self._ctl("start", MIRROR_UNIT, timeout=120)

    async def _state(self, job_id: str) -> str:
        rc, out = await self._ctl("is-active", JOB_UNIT.format(job_id), timeout=15)
        word = out.split()[0] if out.split() else ""
        return word if word in ("active", "activating", "deactivating", "reloading", "inactive", "failed", "maintenance") else "unknown"

    # --- the request -------------------------------------------------------------------------------
    def _write_request(self, launch: Launch) -> None:
        if not JOB_ID_RE.match(launch.job_id):
            raise ValueError("bad job id")
        body = json.dumps({"id": launch.job_id, "suite": launch.suite, "seed": launch.seed, "commit": launch.sha}).encode()
        # O_EXCL: never overwrite, never follow. The folder is setgid, so the job user's group can read the file.
        fd = os.open(self.spool / f"{launch.job_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
        try:
            os.write(fd, body)
            os.fchmod(fd, 0o640)
        finally:
            os.close(fd)

    def _drop_request(self, job_id: str) -> None:
        if not JOB_ID_RE.match(job_id):
            return
        try:
            dfd = os.open(self.spool, files.DIR_FLAGS)
        except OSError:
            return
        try:
            os.unlink(f"{job_id}.json", dir_fd=dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    async def start(self, launch: Launch, out) -> UnitProc:
        proc = UnitProc()
        try:
            self._write_request(launch)
        except (OSError, ValueError) as exc:
            out.write(f"cannot write the job request: {exc}\n".encode())
            out.flush()
            proc.finish(None, f"cannot write the job request: {exc}")
            return proc
        rc, text = await self._ctl("start", "--no-block", JOB_UNIT.format(launch.job_id))
        if rc != 0:
            msg = f"cannot start the job unit: {text}"
            out.write((msg + "\n").encode())
            out.flush()
            self._drop_request(launch.job_id)
            proc.finish(None, msg)
            return proc
        proc.task = asyncio.create_task(self._watch(launch, out, proc))
        self._tasks.add(proc.task)
        proc.task.add_done_callback(self._tasks.discard)
        return proc

    # --- watching ----------------------------------------------------------------------------------
    def _read_exit(self, job_id: str) -> dict | None:
        try:
            data = json.loads(files.read_inside(self.results / job_id, "exit.json", limit=2000))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _copy_log(src_dir: Path, offset: int, out) -> int:
        """Copy new bytes of the job's stdout.log into ours. Reads without following links."""
        try:
            chunk = files.read_inside(src_dir, "stdout.log", offset=offset, limit=1_000_000)
        except OSError:
            return offset
        if chunk:
            try:
                out.write(chunk)
                out.flush()
            except ValueError:  # our log was closed under us after a forced stop
                return offset
        return offset + len(chunk)

    async def _watch(self, launch: Launch, out, proc: UnitProc) -> None:
        job_id, rdir = launch.job_id, self.results / launch.job_id
        loop = asyncio.get_running_loop()
        t0, offset, seen_active, exit_seen_at = loop.time(), 0, False, None
        try:
            while True:
                offset = self._copy_log(rdir, offset, out)
                state = await self._state(job_id)
                now = loop.time()
                seen_active = seen_active or state in ("active", "activating", "deactivating", "reloading")
                exit_info = self._read_exit(job_id)
                # "inactive" right after the start call can mean "not started yet"; a result file or a seen
                # start (or enough time) says it is over
                gone = state in ("inactive", "failed") and (seen_active or exit_info is not None or now - t0 > START_GRACE)
                if exit_info is not None and exit_seen_at is None:
                    exit_seen_at = now
                if gone or (exit_info is not None and now - exit_seen_at > EXIT_GRACE):
                    if exit_info is not None and not gone:
                        await self._ctl("stop", JOB_UNIT.format(job_id))   # wrote its result, but will not exit
                    break
                await asyncio.sleep(POLL)
            for _ in range(3):  # the last bytes of the log, and the exit file if it landed just now
                offset = self._copy_log(rdir, offset, out)
                exit_info = self._read_exit(job_id) or exit_info
                if exit_info is not None:
                    break
                await asyncio.sleep(POLL)
        except asyncio.CancelledError:
            proc.finish(None, "service shutting down")
            raise
        code = exit_info.get("code") if exit_info else None
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        detail = None
        if exit_info is None:
            detail = "the job unit ended without a result"
        elif exit_info.get("error") and isinstance(exit_info["error"], str):
            detail = exit_info["error"][:300]
        proc.finish(code, detail)

    # --- stop / clean ------------------------------------------------------------------------------
    async def stop(self, job_id: str, proc: UnitProc | None) -> None:
        """Stop the unit (job-run passes SIGTERM on, then SIGKILL after the grace period; systemd kills the cgroup)."""
        if proc is not None and proc._done.is_set():
            return
        if not JOB_ID_RE.match(job_id):
            return
        await self._ctl("stop", JOB_UNIT.format(job_id))
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_grace_seconds + 20)
        except asyncio.TimeoutError:
            if proc.task:
                proc.task.cancel()
            proc.finish(None, "the job unit did not report an end")

    async def clean(self, job_id: str, job_dir: Path) -> None:
        """Remove the request. The service cannot delete results/ID (the job user owns it); job-run clears old
        results at the start of the next job."""
        self._drop_request(job_id)


def make_sandbox(cfg: Config):
    return SystemdSandbox(cfg) if cfg.sandbox.mode == "systemd" else ProcessGroupSandbox(cfg)
