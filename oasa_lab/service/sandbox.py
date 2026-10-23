"""Starting and stopping a job's process. Two ways, chosen in the config.

`systemd` (the real lab): the job runs as another, unprivileged user inside its own systemd unit and
cgroup, started by the small root helper (jobrun.py). It cannot read the service's secrets or state, cannot
touch other jobs, and stopping it kills every process in the cgroup, including ones that left the process group.

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

from .config import Config

# What a job inherits from the service in `none` mode. The service's own secrets are never among them.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV", "OASA_SEC_BIN_DIR", "OASA_RADIO_BIN_DIR")
# What the service asks the helper to pass in `systemd` mode (the helper checks each against a pattern).
ENV_REQUEST = ("LANG", "TZ", "OASA_SEC_BIN_DIR", "OASA_RADIO_BIN_DIR")


@dataclass
class Launch:
    job_id: str
    job_dir: Path          # state/jobs/<id>
    suite: str
    seed: int
    sha: str
    confirm_wiring: str | None
    env: dict[str, str] = field(default_factory=dict)   # OASA_LAB_* the service wants set

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

    def __init__(self, cfg: Config):
        self.cfg = cfg

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


class RemoteProc:
    """Stands in for a process: the job is running in a systemd unit and the root helper tells us how it ended."""

    pid = None

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._done = asyncio.Event()
        self.writer: asyncio.StreamWriter | None = None

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode

    def finish(self, code: int | None) -> None:
        if not self._done.is_set():
            self.returncode = code if code is not None else -1
            self._done.set()


class SystemdSandbox:
    """The job user, a transient unit, and the root helper behind a Unix socket (no sudo, no setuid)."""

    mode = "systemd"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.socket = cfg.sandbox.socket
        self._pumps: set[asyncio.Task] = set()

    async def _ask(self, verb: str, job_id: str, timeout: float = 60) -> str:
        """One short request (stop, clean). Returns the helper's one-line answer."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket), 10)
        except (OSError, asyncio.TimeoutError) as exc:
            return f"cannot reach the job helper: {exc}"
        try:
            writer.write(f"{verb} {job_id}\n".encode())
            await writer.drain()
            return (await asyncio.wait_for(reader.read(400), timeout)).decode(errors="replace").strip()
        except (OSError, asyncio.TimeoutError) as exc:
            return f"no answer from the job helper: {exc}"
        finally:
            writer.close()

    async def start(self, launch: Launch, out) -> RemoteProc:
        fd = os.open(launch.job_dir / "launch.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"suite": launch.suite, "seed": launch.seed, "sha": launch.sha,
                       "confirm_wiring": launch.confirm_wiring, "env": launch.env}, fh)
        proc = RemoteProc()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket), 10)
        except (OSError, asyncio.TimeoutError) as exc:
            out.write(f"cannot reach the job helper at {self.socket}: {exc}\n".encode())
            out.flush()
            proc.finish(None)
            return proc
        proc.writer = writer
        writer.write(f"run {launch.job_id}\n".encode())
        await writer.drain()
        task = asyncio.create_task(self._pump(reader, writer, out, launch, proc))
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)
        return proc

    @staticmethod
    async def _pump(reader, writer, out, launch: Launch, proc: RemoteProc) -> None:
        """Copy the job's output into stdout.log until the helper closes the connection, then read how it ended."""
        try:
            while chunk := await reader.read(65536):
                out.write(chunk)
                out.flush()
        except (OSError, ValueError, asyncio.CancelledError):  # ValueError: the log was closed under us after a forced stop
            pass
        finally:
            writer.close()
        code = None
        try:
            from . import files

            data = json.loads(files.read_inside(launch.job_dir, "exit.json", limit=1000))
            code = data["code"] if isinstance(data.get("code"), int) else None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        proc.finish(code)

    async def stop(self, job_id: str, proc: RemoteProc | None) -> None:
        """Stop the unit (SIGTERM, then SIGKILL to the whole cgroup after the grace period)."""
        if proc is None:
            await self._ask("stop", job_id)  # a job from before a restart: its unit may still be running
            return
        if proc._done.is_set():
            return
        await self._ask("stop", job_id)
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_grace_seconds + 10)
        except asyncio.TimeoutError:
            if proc.writer:
                proc.writer.close()  # hanging up makes the helper stop the unit again
            await asyncio.sleep(1)
            proc.finish(None)

    async def clean(self, job_id: str, job_dir: Path) -> None:
        await self._ask("clean", job_id)


def make_sandbox(cfg: Config):
    return SystemdSandbox(cfg) if cfg.sandbox.mode == "systemd" else ProcessGroupSandbox(cfg)
