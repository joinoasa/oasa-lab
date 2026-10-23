"""The REST API. Every route except /healthz needs a known user."""

from __future__ import annotations

import asyncio
import json
import tarfile
import tempfile
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import audit
from .auth import Authenticator, Principal
from .config import Config
from .inventory import Inventory
from .jobs import FINISHED, Job, JobError, JobManager
from .views import job_view, lab_view

MAX_BODY = 4096


def build_routes(cfg: Config, jobs: JobManager, inventory: Inventory, auth: Authenticator) -> list[Route]:
    def authed(permission: str):
        def deco(fn):
            async def handler(request: Request) -> Response:
                h = request.headers
                if "authorization" not in h and "cf-access-jwt-assertion" in h:
                    who = await asyncio.to_thread(auth.authenticate, h)  # may fetch Cloudflare's keys
                else:
                    who = auth.authenticate(h)
                if who is None:
                    return JSONResponse({"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
                audit.note(actor=who.name, via=who.via, credential=who.credential)
                if not who.can(permission):
                    return JSONResponse({"error": "your role may not do this"}, 403)
                try:
                    return await fn(request, who)
                except JobError as e:
                    audit.note(refused=e.message)
                    return JSONResponse({"error": e.message}, e.status)
            return handler
        return deco

    async def json_body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise JobError(413, "request too large")
        if not raw:
            return {}
        try:
            d = json.loads(raw)
        except ValueError:
            raise JobError(400, "body must be JSON")
        if not isinstance(d, dict):
            raise JobError(400, "body must be a JSON object")
        return d

    def job_of(request: Request) -> Job:
        return jobs.get(request.path_params["job_id"])

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @authed("read")
    async def suites(request, who):
        co, found = await jobs.list_suites()
        audit.note(action="list_suites")
        return JSONResponse({"commit": co.sha, "suites": list(found.values())})

    @authed("run")
    async def submit(request, who):
        body = await json_body(request)
        unknown = set(body) - {"suite", "seed", "profile", "commit"}
        if unknown:
            raise JobError(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        audit.note(action="submit", suite=str(body.get("suite"))[:64], seed=body.get("seed", 0),
                   profile=body.get("profile"))
        job = await jobs.submit(who, body.get("suite"), body.get("seed", 0), body.get("profile"), body.get("commit"))
        audit.note(job=job.id, commit=job.commit)
        return JSONResponse(job_view(cfg, jobs, job), 202, headers={"Location": f"/api/jobs/{job.id}"})

    @authed("read")
    async def job_list(request, who):
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            raise JobError(400, "limit must be a number")
        audit.note(action="list_jobs")
        return JSONResponse({"jobs": [job_view(cfg, jobs, j) for j in jobs.list(limit)]})

    @authed("read")
    async def job_get(request, who):
        job = job_of(request)
        audit.note(action="job_status", job=job.id)
        return JSONResponse(job_view(cfg, jobs, job))

    @authed("read")
    async def job_log(request, who):
        job = job_of(request)
        stream = request.query_params.get("stream", "log")
        if stream not in ("log", "stdout"):
            raise JobError(400, "stream must be log or stdout")
        try:
            offset = max(0, int(request.query_params.get("offset", request.headers.get("last-event-id", "0"))))
        except ValueError:
            raise JobError(400, "offset must be a number")
        audit.note(action="job_log", job=job.id, follow=request.query_params.get("follow") in ("1", "true"))
        if request.query_params.get("follow") not in ("1", "true"):
            lines, off = jobs.read_log(job, offset, stream)
            return JSONResponse({"status": job.status, "lines": lines, "offset": off})

        async def events():
            off = offset
            idle = 0.0
            while True:
                lines, off = jobs.read_log(job, off, stream)
                for ln in lines:
                    yield f"data: {ln}\n\n"
                if lines:
                    yield f"id: {off}\n\n"
                    idle = 0.0
                elif job.status in FINISHED:
                    lines, off = jobs.read_log(job, off, stream)  # last look after the final status
                    if not lines:
                        yield f"event: end\ndata: {json.dumps({'status': job.status, 'offset': off})}\n\n"
                        return
                    continue
                else:
                    idle += 0.3
                    if idle >= 15:
                        yield ": keep-alive\n\n"
                        idle = 0.0
                    await asyncio.sleep(0.3)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @authed("read")
    async def job_results(request, who):
        job = job_of(request)
        audit.note(action="results", job=job.id)
        d = jobs.results(job)
        d["job"] = job_view(cfg, jobs, job)
        return JSONResponse(d)

    @authed("read")
    async def job_check(request, who):
        job = job_of(request)
        audit.note(action="download_check", job=job.id)
        check = jobs.check_result(job)
        if check is None:
            raise JobError(404, "no checker verdict for this job")
        return JSONResponse(check)

    @authed("read")
    async def job_bundle(request, who):
        job = job_of(request)
        b = jobs.bundle_dir(job)
        if b is None:
            raise JobError(404, "this job has no bundle")
        audit.note(action="download_bundle", job=job.id)
        tmp = tempfile.NamedTemporaryFile(prefix="bundle-", suffix=".tar.gz", delete=False)
        tmp.close()

        def pack() -> None:
            with tarfile.open(tmp.name, "w:gz") as tf:
                for p in sorted(b.rglob("*")):
                    if p.is_file() and not p.is_symlink():
                        tf.add(p, arcname=f"{b.name}/{p.relative_to(b).as_posix()}", recursive=False)
                chk = b.parent / (b.name + ".check.json")
                if chk.is_file():
                    tf.add(chk, arcname=f"{b.name}.check.json", recursive=False)

        await asyncio.to_thread(pack)
        return FileResponse(tmp.name, media_type="application/gzip", filename=f"{b.name}.tar.gz",
                            background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))

    @authed("read")
    async def bundles(request, who):
        audit.note(action="list_bundles")
        out = []
        for j in jobs.list(200):
            if j.bundle:
                out.append({"job": j.id, "suite": j.suite, "bundle": j.bundle, "status": j.status,
                            "check_ok": j.check_ok, "created": j.created})
        return JSONResponse({"bundles": out})

    @authed("run")
    async def job_stop(request, who):
        job = job_of(request)
        body = await json_body(request)
        audit.note(action="stop", job=job.id)
        job = await jobs.stop(who, job.id, str(body.get("reason", "")))
        return JSONResponse(job_view(cfg, jobs, job))

    @authed("read")
    async def lab(request, who):
        audit.note(action="lab_status")
        return JSONResponse(await lab_view(cfg, jobs, inventory))

    @authed("pause")
    async def pause(request, who):
        body = await json_body(request)
        audit.note(action="pause", stop_running=bool(body.get("stop_running")))
        res = await jobs.pause(who.name, str(body.get("reason", "")), bool(body.get("stop_running")))
        return JSONResponse(res)

    @authed("pause")
    async def resume(request, who):
        audit.note(action="resume")
        return JSONResponse({"resumed": jobs.resume()})

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/suites", suites, methods=["GET"]),
        Route("/api/jobs", job_list, methods=["GET"]),
        Route("/api/jobs", submit, methods=["POST"]),
        Route("/api/jobs/{job_id}", job_get, methods=["GET"]),
        Route("/api/jobs/{job_id}/log", job_log, methods=["GET"]),
        Route("/api/jobs/{job_id}/results", job_results, methods=["GET"]),
        Route("/api/jobs/{job_id}/check.json", job_check, methods=["GET"]),
        Route("/api/jobs/{job_id}/bundle.tar.gz", job_bundle, methods=["GET"]),
        Route("/api/jobs/{job_id}/stop", job_stop, methods=["POST"]),
        Route("/api/bundles", bundles, methods=["GET"]),
        Route("/api/lab", lab, methods=["GET"]),
        Route("/api/pause", pause, methods=["POST"]),
        Route("/api/resume", resume, methods=["POST"]),
    ]
