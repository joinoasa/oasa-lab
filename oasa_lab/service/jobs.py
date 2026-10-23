"""The job queue. One job at a time, because the radios are shared.

A job is `oasa-lab run SUITE --seed N` in a subprocess, from a clean checkout of the lab repo at a
commit on main. Nothing a caller sends is ever run: only a suite name from the checkout's own list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..jsonl import now_rfc3339
from .audit import AuditLog
from .auth import Principal
from .checkout import SUITE_RE, Checkout, CheckoutError, Checkouts, read_suites
from .config import RF_SAFE, Config
from .inventory import Inventory

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_SEED = 2**31 - 1
ACTIVE = ("queued", "running")
FINISHED = ("passed", "failed", "stopped", "error", "cancelled", "interrupted")
# The only things a job's environment inherits. The service's own secrets are never among them.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV", "OASA_SEC_BIN_DIR")


class JobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Job:
    id: str
    suite: str
    seed: int
    profile: str | None
    requested_by: str
    via: str
    commit: str
    created: str
    status: str = "queued"
    started: str | None = None
    finished: str | None = None
    exit_code: int | None = None
    stop_reason: str | None = None
    bundle: str | None = None
    check_ok: bool | None = None
    counts: dict = field(default_factory=dict)
    transmits: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class JobManager:
    def __init__(self, cfg: Config, checkouts: Checkouts, inventory: Inventory, audit: AuditLog):
        self.cfg, self.checkouts, self.inventory, self.audit = cfg, checkouts, inventory, audit
        self.root = cfg.state_dir / "jobs"
        self.pause_file = cfg.state_dir / "PAUSE"
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._running_id: str | None = None
        self._stop_requests: dict[str, str] = {}
        self._closing = False

    # --- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for p in sorted(self.root.glob("*/job.json")):
            try:
                job = Job(**json.loads(p.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if job.status in ACTIVE:  # the service was restarted under it
                job.status = "interrupted" if job.status == "running" else "cancelled"
                job.stop_reason = "service restarted"
                job.finished = now_rfc3339()
                self._save(job)
            self.jobs[job.id] = job
        self._worker = asyncio.create_task(self._work(), name="oasa-lab-worker")

    async def shutdown(self) -> None:
        self._closing = True  # the worker takes no new job from here
        for job in list(self.jobs.values()):
            if job.status == "queued":
                self._finish(job, "cancelled", "service shutting down")
        if self._running_id:
            self._stop_requests[self._running_id] = "service shutting down"
            await self._kill()
            for _ in range(100):  # let the worker write the job's end before we cancel it
                if self._running_id is None:
                    break
                await asyncio.sleep(0.05)
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass

    # --- pause (the global kill switch) ---------------------------------------------------------
    @property
    def paused(self) -> dict | None:
        """None when running normally. Otherwise who paused and why. A file, so it survives restarts
        and the owner can also pause from a shell: `touch <state_dir>/PAUSE`."""
        try:
            raw = self.pause_file.read_text()
        except OSError:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return {"by": "shell", "reason": raw.strip()[:200] or "paused by file"}

    async def pause(self, who: str, reason: str = "", stop_running: bool = False) -> dict:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        info = {"by": who, "reason": reason[:200], "since": now_rfc3339()}
        self.pause_file.write_text(json.dumps(info))
        cancelled = []
        for job in self.jobs.values():
            if job.status == "queued":
                self._finish(job, "cancelled", "lab paused")
                cancelled.append(job.id)
        stopped = None
        if stop_running and self._running_id:
            stopped = self._running_id
            self._stop_requests[stopped] = f"lab paused by {who}"
            await self._kill()
        return {**info, "cancelled": cancelled, "stopped": stopped}

    def resume(self) -> bool:
        try:
            self.pause_file.unlink()
            return True
        except FileNotFoundError:
            return False

    # --- submit / read -------------------------------------------------------------------------
    async def list_suites(self) -> tuple[Checkout, dict[str, dict]]:
        try:
            co = await self.checkouts.latest()
        except CheckoutError as exc:
            raise JobError(502, f"cannot get the lab code: {exc}")
        return co, read_suites(co)

    async def _device_problem(self, suite: dict, profile: str | None) -> str | None:
        want_ids = list(self.cfg.profiles[profile].requires) if profile else []
        if not want_ids and not suite["requires"]:
            return None
        status = await self.inventory.scan()
        present_ids = {d.id for d in status if d.present}
        present_kinds = {d.kind for d in status if d.present}
        missing = [i for i in want_ids if i not in present_ids]
        missing += [k for k in suite["requires"] if k not in present_kinds]
        return f"missing device(s): {', '.join(sorted(set(missing)))}" if missing else None

    def _interlock(self, suite: dict) -> str | None:
        if suite["transmits"] and self.cfg.rf_setup not in RF_SAFE:
            return (f"refused: suite {suite['name']!r} transmits, and this lab's rf_setup is "
                    f"{self.cfg.rf_setup!r} (must be one of: {', '.join(RF_SAFE)})")
        return None

    async def submit(self, who: Principal, suite_name: Any, seed: Any = 0, profile: Any = None, commit: Any = None) -> Job:
        if not who.can("run"):
            raise JobError(403, "your role may not run jobs")
        if (p := self.paused) is not None:
            raise JobError(423, f"the lab is paused by {p.get('by')}: {p.get('reason') or 'no reason given'}")
        if not isinstance(suite_name, str) or not SUITE_RE.match(suite_name):
            raise JobError(400, "suite must be a suite name (lowercase letters, digits, dashes)")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
            raise JobError(400, f"seed must be a whole number from 0 to {MAX_SEED}")
        if profile is not None and (not isinstance(profile, str) or profile not in self.cfg.profiles):
            raise JobError(400, f"unknown hardware profile; known: {', '.join(self.cfg.profiles) or 'none'}")
        if commit is not None and not isinstance(commit, str):
            raise JobError(400, "commit must be a full commit id")
        if sum(1 for j in self.jobs.values() if j.status in ACTIVE) >= self.cfg.max_queued:
            raise JobError(429, "the queue is full")
        try:
            co = await self.checkouts.at(commit)
        except CheckoutError as exc:
            raise JobError(400 if commit else 502, str(exc))
        suites = read_suites(co)
        if suite_name not in suites:
            raise JobError(400, f"no suite named {suite_name!r}; known: {', '.join(suites) or 'none'}")
        suite = suites[suite_name]
        if (why := self._interlock(suite)):
            raise JobError(403, why)
        if (why := await self._device_problem(suite, profile)):
            raise JobError(409, why)
        job = Job(id=uuid.uuid4().hex[:12], suite=suite_name, seed=seed, profile=profile,
                  requested_by=who.name, via=who.via, commit=co.sha, created=now_rfc3339(),
                  transmits=suite["transmits"])
        self.jobs[job.id] = job
        self._save(job)
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: Any) -> Job:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id) or job_id not in self.jobs:
            raise JobError(404, "no such job")
        return self.jobs[job_id]

    def list(self, limit: int = 20) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)[: max(1, min(limit, 200))]

    def queue_position(self, job: Job) -> int | None:
        if job.status != "queued":
            return None
        queued = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.created)
        return queued.index(job) + 1

    @property
    def running(self) -> Job | None:
        return self.jobs.get(self._running_id) if self._running_id else None

    # --- stop ----------------------------------------------------------------------------------
    async def stop(self, who: Principal, job_id: Any, reason: str = "") -> Job:
        job = self.get(job_id)
        if not (who.can("stop_any") or (who.can("stop_own") and job.requested_by == who.name)):
            raise JobError(403, "your role may not stop this job")
        if job.status == "queued":
            self._finish(job, "cancelled", f"stopped by {who.name}")
        elif job.status == "running":
            self._stop_requests[job.id] = f"stopped by {who.name}" + (f": {reason[:100]}" if reason else "")
            await self._kill()
            for _ in range(100):  # let the worker record the end
                if job.status == "running":
                    await asyncio.sleep(0.05)
        else:
            raise JobError(409, f"job is already {job.status}")
        return job

    async def _kill(self) -> None:
        """SIGTERM the whole process group, wait for the grace period, then SIGKILL."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        pgid = proc.pid  # started with start_new_session, so the group id is the pid
        self._signal(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_grace_seconds)
        except asyncio.TimeoutError:
            self._signal(pgid, signal.SIGKILL)
            await proc.wait()
        self._signal(pgid, signal.SIGKILL)  # anything the run left behind (radio tools, proxies)

    @staticmethod
    def _signal(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    # --- the worker ---------------------------------------------------------------------------
    async def _work(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued" or self._closing:
                continue
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bug must not stop the queue
                self._finish(job, "error", f"{type(exc).__name__}: {exc}"[:300])
            finally:
                self._proc = None
                self._running_id = None
                self.checkouts.prune({j.commit for j in self.jobs.values() if j.status in ACTIVE})

    def job_dir(self, job: Job) -> Path:
        return self.root / job.id

    async def _run(self, job: Job) -> None:
        if (p := self.paused) is not None:
            self._finish(job, "cancelled", f"lab paused by {p.get('by')}")
            return
        co = await self.checkouts.at(job.commit)
        suite = read_suites(co).get(job.suite)
        if suite is None:
            self._finish(job, "error", "suite is gone from this commit")
            return
        if (why := self._interlock(suite)) or (why := await self._device_problem(suite, job.profile)):
            self._finish(job, "error", why)
            return
        jd = self.job_dir(job)
        (jd / "runs").mkdir(parents=True, exist_ok=True)
        env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
        env.update({
            "HOME": str(jd), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "OASA_LAB_JOB_ID": job.id, "OASA_LAB_REQUESTED_BY": job.requested_by,
            "OASA_LAB_RF_SETUP": self.cfg.rf_setup,
        })
        if job.profile:
            env["OASA_LAB_PROFILE"] = job.profile
            env["OASA_LAB_DEVICES"] = ",".join(self.cfg.profiles[job.profile].requires)
        job.status, job.started = "running", now_rfc3339()
        self._running_id = job.id
        self._save(job)
        with open(jd / "stdout.log", "wb") as out:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "oasa_lab", "run", job.suite, "--seed", str(job.seed), "--out", str(jd / "runs"),
                cwd=co.path, env=env, stdout=out, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                if job.id in self._stop_requests:  # stop arrived before the process existed
                    await self._kill()
                await asyncio.wait_for(asyncio.shield(self._proc.wait()), self.cfg.job_timeout_seconds)
            except asyncio.TimeoutError:
                self._stop_requests.setdefault(job.id, f"timed out after {self.cfg.job_timeout_seconds:g} s")
                await self._kill()
            except asyncio.CancelledError:
                self._stop_requests.setdefault(job.id, "service shutting down")
                await self._kill()
                raise
        job.exit_code = self._proc.returncode
        bundle = self.bundle_dir(job)
        if bundle:
            job.bundle = bundle.name
            check = self._read_json(bundle.parent / (bundle.name + ".check.json"))
            job.check_ok = bool(check["ok"]) if check else None
            job.counts = self._counts(bundle)
        stop = self._stop_requests.pop(job.id, None)
        if stop:
            self._finish(job, "stopped", stop)
        elif job.exit_code == 0 and job.check_ok:
            self._finish(job, "passed")
        elif job.exit_code == 1 and job.check_ok is not None:
            self._finish(job, "failed")
        else:
            self._finish(job, "error", f"oasa-lab exited with {job.exit_code}")

    # --- evidence ------------------------------------------------------------------------------
    def bundle_dir(self, job: Job) -> Path | None:
        runs = self.job_dir(job) / "runs"
        if not runs.is_dir():
            return None
        found = sorted(p for p in runs.iterdir() if p.is_dir() and not p.is_symlink())
        return found[0] if found else None

    @staticmethod
    def _read_json(p: Path) -> Any:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _counts(self, bundle: Path) -> dict:
        d = self._read_json(bundle / "results.ctrf.json")
        try:
            s = d["results"]["summary"]
            return {"total": s["tests"], "passed": s["passed"], "failed": s["failed"], "skipped": s["skipped"]}
        except (TypeError, KeyError):
            return {}

    def check_result(self, job: Job) -> dict | None:
        b = self.bundle_dir(job)
        return self._read_json(b.parent / (b.name + ".check.json")) if b else None

    def results(self, job: Job) -> dict:
        check = self.check_result(job)
        out: dict[str, Any] = {"job": job.to_dict(), "counts": job.counts}
        if check:
            out["checker"] = {"ok": check.get("ok"), "version": (check.get("checker") or {}).get("version"),
                              "checks": [{"name": c.get("name"), "status": c.get("status"), "reason": c.get("reason")}
                                         for c in check.get("checks", [])]}
        else:
            out["checker"] = None
        b = self.bundle_dir(job)
        if b and (b / "summary.md").is_file():
            out["summary_md"] = (b / "summary.md").read_text(encoding="utf-8", errors="replace")[:8000]
        out["counted"] = bool(job.status == "passed" and check and check.get("ok"))
        return out

    def read_log(self, job: Job, offset: int = 0, stream: str = "log", max_bytes: int = 256_000) -> tuple[list[str], int]:
        """Complete lines of the live log from a byte offset. `stream` is "log" (log.jsonl) or "stdout"."""
        if stream == "stdout":
            p = self.job_dir(job) / "stdout.log"
        else:
            b = self.bundle_dir(job)
            p = (b / "log.jsonl") if b else None
        if p is None or not p.is_file():
            return [], offset
        with open(p, "rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
        end = data.rfind(b"\n") + 1
        lines = [ln[:4000] for ln in data[:end].decode("utf-8", errors="replace").splitlines()]
        return lines, offset + end

    def tail_log(self, job: Job, n: int = 30, stream: str = "log") -> list[str]:
        lines, _ = self.read_log(job, 0, stream, max_bytes=2_000_000)
        return lines[-max(1, min(n, 500)):]

    # --- state ---------------------------------------------------------------------------------
    def _finish(self, job: Job, status: str, reason: str | None = None) -> None:
        job.status, job.finished = status, now_rfc3339()
        if reason:
            job.stop_reason = reason
        self._save(job)
        self.audit.write(kind="job", event="finished", job=job.id, suite=job.suite, status=status,
                         reason=reason, exit_code=job.exit_code, check_ok=job.check_ok)

    def _save(self, job: Job) -> None:
        d = self.job_dir(job)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict(), indent=2))
        os.replace(tmp, d / "job.json")
