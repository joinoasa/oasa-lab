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


class SystemdSandbox:
    """The job user, a transient unit, and the root helper."""

    mode = "systemd"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.runner = list(cfg.sandbox.runner) or ["sudo", "-n", sys.executable, "-I", "-m", "oasa_lab.service.jobrun"]

    async def _helper(self, verb: str, job_id: str, timeout: float = 60) -> int:
        proc = await asyncio.create_subprocess_exec(
            *self.runner, verb, job_id, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1

    async def start(self, launch: Launch, out) -> asyncio.subprocess.Process:
        fd = os.open(launch.job_dir / "launch.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"suite": launch.suite, "seed": launch.seed, "sha": launch.sha,
                       "confirm_wiring": launch.confirm_wiring, "env": launch.env}, fh)
        return await asyncio.create_subprocess_exec(
            *self.runner, "run", launch.job_id, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def stop(self, job_id: str, proc: asyncio.subprocess.Process | None) -> None:
        """Stop the unit (SIGTERM, then SIGKILL to the whole cgroup after the grace period)."""
        if proc is None or proc.returncode is not None:
            return
        await self._helper("stop", job_id)
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_grace_seconds + 5)
        except asyncio.TimeoutError:
            _signal(proc.pid, signal.SIGKILL)  # the helper process itself; the cgroup is already gone
            await proc.wait()

    async def clean(self, job_id: str, job_dir: Path) -> None:
        await self._helper("clean", job_id)


def make_sandbox(cfg: Config):
    return SystemdSandbox(cfg) if cfg.sandbox.mode == "systemd" else ProcessGroupSandbox(cfg)
