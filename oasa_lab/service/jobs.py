"""The job queue. One job at a time, because the radios are shared.

A job is `oasa-lab run SUITE --seed N` for a commit on main, started by the sandbox (see sandbox.py): in the
real lab a systemd unit that runs the installed `oasa-lab job-run` as another user, which checks the request
again and applies the transmit lock itself. The service runs no code from any commit. Nothing a caller sends is
ever run: only a suite name from the commit's own list.

What a job wrote is never trusted. When it ends, the service copies the bundle out of the job's folder
(plain files only, nothing followed), runs its own installed checker on the copy, and serves only the copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..jsonl import now_rfc3339
from .audit import AuditLog
from .auth import Principal
from . import files
from ..policy import JOB_ID_RE, interlock, wiring_to_confirm
from .checkout import SUITE_RE, Checkout, CheckoutError, Checkouts, read_suites
from .config import Config
from .inventory import Inventory
from .sandbox import ENV_REQUEST, Launch, make_sandbox

MAX_SEED = 2**31 - 1
ACTIVE = ("queued", "running")
FINISHED = ("passed", "failed", "stopped", "error", "cancelled", "interrupted")
MAX_READ = 1_000_000          # the most one log read returns
MAX_STREAM_BYTES = 20_000_000  # the most one live stream sends before the client must reconnect
MAX_STREAM_SECONDS = 3600


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
    wiring_confirmed: str | None = None  # the wiring the owner's config confirmed for this run
    checker: str | None = None  # version of the checker that judged the evidence (the service's own copy)

    def to_dict(self) -> dict:
        return asdict(self)


class JobManager:
    def __init__(self, cfg: Config, checkouts: Checkouts, inventory: Inventory, audit: AuditLog):
        self.cfg, self.checkouts, self.inventory, self.audit = cfg, checkouts, inventory, audit
        self.root = cfg.state_dir / "jobs"
        self.pause_file = cfg.state_dir / "PAUSE"                # the API can set and clear this one
        self.local_pause_file = Path(cfg.local_pause_file)       # the owner's, from a shell; the API cannot clear it
        self.sandbox = make_sandbox(cfg)
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
                if job.status == "running":
                    await self.sandbox.stop(job.id, None)  # a unit may still be running; stop it
                await self.sandbox.clean(job.id, self.job_dir(job))
                for leftover in ("src", "judge"):
                    shutil.rmtree(self.job_dir(job) / leftover, ignore_errors=True)
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
    @staticmethod
    def _pause_info(path: Path, default_by: str) -> dict:
        try:
            raw = path.read_text()
        except OSError:  # a folder, a dangling link, no permission: it still counts as a pause
            return {"by": default_by, "reason": "paused by file"}
        try:
            info = json.loads(raw)
        except ValueError:
            info = None
        return info if isinstance(info, dict) else {"by": default_by, "reason": raw.strip()[:200] or "paused by file"}

    @property
    def paused(self) -> dict | None:
        """None when running normally. Otherwise who paused and why. Two files, and their mere existence
        pauses the lab (even if unreadable, empty or not a regular file): the API's `PAUSE` in the state
        folder, and the owner's local file (default `LOCAL_PAUSE`, set to a root-owned folder in the real
        config) which no API call can clear. From a shell: `touch <file>`."""
        if os.path.lexists(self.local_pause_file):
            return {**self._pause_info(self.local_pause_file, "owner (local file)"), "local": True}
        if os.path.lexists(self.pause_file):
            return self._pause_info(self.pause_file, "shell")
        return None

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
        """Clear the API's pause. A local pause stays until the owner removes the file."""
        try:
            if self.pause_file.is_dir() and not self.pause_file.is_symlink():
                self.pause_file.rmdir()
            else:
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
        try:
            return co, await read_suites(self.checkouts, co)
        except CheckoutError as exc:
            raise JobError(502, str(exc))

    @staticmethod
    def missing_requirements(requires: list[str], present: list) -> list[str]:
        """Suite `requires` use the hardware suites' form: a device kind, or `kind:N` for at least N."""
        out = []
        for req in requires:
            kind, _, n = req.partition(":")
            need = int(n) if n.isdigit() else 1
            if sum(1 for d in present if d.kind == kind) < need:
                out.append(req)
        return out

    async def _device_problem(self, suite: dict, profile: str | None) -> str | None:
        want_ids = list(self.cfg.profiles[profile].requires) if profile else []
        if not want_ids and not suite["requires"]:
            return None
        status = await self.inventory.scan()
        present = [d for d in status if d.present]
        missing = [i for i in want_ids if i not in {d.id for d in present}]
        missing += self.missing_requirements(suite["requires"], present)
        return f"missing device(s): {', '.join(sorted(set(missing)))}" if missing else None

    @property
    def simulated(self) -> bool:
        return self.cfg.inventory_backend == "simulated"

    def _interlock(self, suite: dict) -> str | None:
        """The transmit rules (oasa_lab/policy.py, the same function `job-run` applies on its own side)."""
        return interlock(suite, self.cfg.rf_setup, self.simulated, self.cfg.confirm_wiring)

    def wiring_to_confirm(self, suite: dict) -> str | None:
        return wiring_to_confirm(self.cfg.confirm_wiring, suite)

    def _disk_problem(self) -> str | None:
        free = shutil.disk_usage(self.cfg.state_dir if self.cfg.state_dir.exists() else self.cfg.state_dir.parent).free
        if free < self.cfg.min_free_mb * 1_000_000:
            return f"not enough free disk to run a job ({free // 1_000_000} MB free, {self.cfg.min_free_mb} MB needed)"
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
        try:
            suites = await read_suites(self.checkouts, co)
        except CheckoutError as exc:
            raise JobError(502, str(exc))
        if suite_name not in suites:
            raise JobError(400, f"no suite named {suite_name!r}; known: {', '.join(suites) or 'none'}")
        suite = suites[suite_name]
        if (why := self._interlock(suite)):
            raise JobError(403, why)
        if (why := await self._device_problem(suite, profile)):
            raise JobError(409, why)
        if (why := self._disk_problem()):
            raise JobError(507, why)
        job = Job(id=uuid.uuid4().hex, suite=suite_name, seed=seed, profile=profile,
                  requested_by=who.name, via=who.via, commit=co.sha, created=now_rfc3339(),
                  transmits=suite["transmits"],
                  wiring_confirmed=self.wiring_to_confirm(suite) if suite["transmits"] and not self.simulated else None)
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
        """Stop the running job: SIGTERM, a grace period, then SIGKILL (the whole cgroup in systemd mode)."""
        if self._running_id:
            await self.sandbox.stop(self._running_id, self._proc)

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
                self.retain()

    def job_dir(self, job: Job) -> Path:
        return self.root / job.id

    def _launch_env(self, job: Job) -> dict[str, str]:
        env = {"OASA_LAB_JOB_ID": job.id, "OASA_LAB_REQUESTED_BY": job.requested_by}
        env.update({k: os.environ[k] for k in ENV_REQUEST if k in os.environ})
        if job.profile:
            env["OASA_LAB_PROFILE"] = job.profile
            env["OASA_LAB_DEVICES"] = ",".join(self.cfg.profiles[job.profile].requires)
        if self.sandbox.mode == "none":  # in systemd mode `job-run` takes these from the root-owned policy file
            env["OASA_LAB_RF_SETUP"] = self.cfg.rf_setup
            if self.simulated:
                env["OASA_LAB_SIM"] = "1"  # the simulated service runs the hardware suites on the simulated bench
            elif self.cfg.devices_file:
                env["OASA_LAB_INVENTORY"] = self.cfg.devices_file
        return env

    async def _run(self, job: Job) -> None:
        if (p := self.paused) is not None:
            self._finish(job, "cancelled", f"lab paused by {p.get('by')}")
            return
        co = await self.checkouts.at(job.commit)
        suite = (await read_suites(self.checkouts, co)).get(job.suite)
        if suite is None:
            self._finish(job, "error", "suite is gone from this commit")
            return
        if (why := self._interlock(suite)) or (why := await self._device_problem(suite, job.profile)) or (why := self._disk_problem()):
            self._finish(job, "error", why)
            return
        jd = self.job_dir(job)
        jd.mkdir(parents=True, exist_ok=True)
        if self.sandbox.mode == "none":
            src = await self.checkouts.export(job.commit, jd / "src")  # this job's own copy of the code
        else:  # the job's code is cloned by `job-run`; we only need the suites folder, as data, to judge the evidence
            src = await self.checkouts.export_suites(job.commit, jd / "judge")
        launch = Launch(job.id, jd, job.suite, job.seed, job.commit, job.wiring_confirmed, self._launch_env(job))
        job.status, job.started = "running", now_rfc3339()
        self._running_id = job.id
        self._save(job)
        try:
            with open(jd / "stdout.log", "wb") as out:
                self._proc = await self.sandbox.start(launch, out)
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
            problem = await self._collect(job, jd, src)
        finally:
            shutil.rmtree(src, ignore_errors=True)
            await self.sandbox.clean(job.id, jd)
        stop = self._stop_requests.pop(job.id, None)
        if stop:
            self._finish(job, "stopped", stop)
        elif problem:
            self._finish(job, "error", problem)
        elif job.exit_code == 0 and job.check_ok:
            self._finish(job, "passed")
        elif job.exit_code in (0, 1) and job.check_ok is not None:
            self._finish(job, "failed")
        else:
            self._finish(job, "error", getattr(self._proc, "detail", None) or f"oasa-lab exited with {job.exit_code}")

    async def _collect(self, job: Job, jd: Path, src: Path) -> str | None:
        """Take the evidence out of the job's folder and judge it ourselves. Returns a problem, or None."""
        runs = self.sandbox.runs_dir(job.id, jd)
        names = files.list_dirs(runs)
        if not names:
            return None
        name = names[0]
        dest = jd / "verified" / name
        try:
            stats = await asyncio.to_thread(files.copy_tree, runs, name, dest, self.cfg.max_bundle_mb * 1_000_000)
        except (OSError, files.TooBig) as exc:
            return f"evidence rejected: {exc}"[:300]
        if stats["skipped"]:
            self.audit.write(kind="job", event="evidence_skipped", job=job.id, names=stats["skipped"][:20])
        job.bundle = name
        verdict = await self._check(dest, src / "suites")
        job.check_ok = bool(verdict["ok"]) if verdict else None
        job.checker = ((verdict or {}).get("checker") or {}).get("version")
        job.counts = self._counts(dest)
        return None

    async def _check(self, bundle: Path, suites_dir: Path) -> dict | None:
        """The independent checker, this service's own installed copy, on the copy of the evidence."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "oasa_lab.checker", str(bundle), "--suites-dir", str(suites_dir),
            cwd="/", env={}, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 600)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        return self._read_json(bundle.parent / (bundle.name + ".check.json"))

    # --- evidence ------------------------------------------------------------------------------
    def bundle_dir(self, job: Job) -> Path | None:
        """The service's own verified copy of the evidence, once the job has ended."""
        if not job.bundle:
            return None
        b = self.job_dir(job) / "verified" / job.bundle
        return b if b.is_dir() and not b.is_symlink() else None

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
            ver = (check.get("checker") or {}).get("version")
            out["checker"] = {"ok": check.get("ok"), "name": (check.get("checker") or {}).get("name"), "version": ver,
                              "checks": [{"name": c.get("name"), "status": c.get("status"), "reason": c.get("reason")}
                                         for c in check.get("checks", [])]}
            out["judged_by"] = f"oasa-lab-checker {ver}, the job service's own copy, run on the evidence after the job ended"
        else:
            out["checker"] = None
        b = self.bundle_dir(job)
        if b and (b / "summary.md").is_file():
            out["summary_md"] = (b / "summary.md").read_text(encoding="utf-8", errors="replace")[:8000]
        out["counted"] = bool(job.status == "passed" and check and check.get("ok"))
        return out

    def _log_source(self, job: Job, stream: str) -> tuple[Path, tuple[str, ...]] | None:
        jd = self.job_dir(job)
        if stream == "stdout":
            return jd, ("stdout.log",)
        if job.bundle and self.bundle_dir(job):
            return jd / "verified", (job.bundle, "log.jsonl")
        runs = self.sandbox.runs_dir(job.id, jd)  # a run in progress: the job's own folder, read without trust
        names = files.list_dirs(runs)
        return (runs, (names[0], "log.jsonl")) if names else None

    def read_log(self, job: Job, offset: int = 0, stream: str = "log", max_bytes: int = 256_000) -> tuple[list[str], int]:
        """Complete lines of the live log from a byte offset. `stream` is "log" (log.jsonl) or "stdout".
        Reads at most MAX_READ bytes."""
        src = self._log_source(job, stream)
        if src is None:
            return [], offset
        try:
            data = files.read_inside(src[0], *src[1], offset=max(0, offset), limit=max(1, min(max_bytes, MAX_READ)))
        except OSError:
            return [], offset
        end = data.rfind(b"\n") + 1
        lines = [ln[:4000] for ln in data[:end].decode("utf-8", errors="replace").splitlines()]
        return lines, offset + end

    def log_end(self, job: Job, stream: str = "log") -> int:
        """Byte offset just after the last complete line, without reading the whole log."""
        src = self._log_source(job, stream)
        if src is None:
            return 0
        try:
            size = files.size_inside(src[0], *src[1])
            tail = files.read_inside(src[0], *src[1], offset=max(0, size - 65536), limit=65536)
        except OSError:
            return 0
        i = tail.rfind(b"\n")
        return size - len(tail) + i + 1 if i >= 0 else max(0, size - len(tail))

    def tail_log(self, job: Job, n: int = 30, stream: str = "log") -> list[str]:
        src = self._log_source(job, stream)
        if src is None:
            return []
        try:
            size = files.size_inside(src[0], *src[1])
            start = max(0, size - 262_144)
            data = files.read_inside(src[0], *src[1], offset=start, limit=262_144)
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        lines = [ln[:4000] for ln in data[:end].decode("utf-8", errors="replace").splitlines()]
        if start > 0 and lines:
            lines = lines[1:]  # the first one is cut in the middle
        return lines[-max(1, min(n, 500)):]

    # --- state ---------------------------------------------------------------------------------
    def _finish(self, job: Job, status: str, reason: str | None = None) -> None:
        job.status, job.finished = status, now_rfc3339()
        if reason:
            job.stop_reason = reason
        self._save(job)
        self.audit.write(kind="job", event="finished", job=job.id, suite=job.suite, status=status,
                         reason=reason, exit_code=job.exit_code, check_ok=job.check_ok, checker=job.checker)

    def _save(self, job: Job) -> None:
        d = self.job_dir(job)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict(), indent=2))
        os.replace(tmp, d / "job.json")

    def retain(self) -> list[str]:
        """Delete the evidence of the oldest finished jobs beyond `keep_jobs`. Never a queued or running one."""
        done = sorted((j for j in self.jobs.values() if j.status in FINISHED), key=lambda j: j.created, reverse=True)
        gone = []
        for j in done[self.cfg.keep_jobs:]:
            shutil.rmtree(self.job_dir(j), ignore_errors=True)
            del self.jobs[j.id]
            gone.append(j.id)
        if gone:
            self.audit.write(kind="job", event="retention", removed=len(gone), kept=self.cfg.keep_jobs)
        return gone
