"""What the REST API and the connector both say about jobs and the lab. One place, so they agree."""

from __future__ import annotations

from typing import Any

from .. import __version__
from .config import RF_SAFE, Config
from .inventory import Inventory
from .jobs import Job, JobManager


def links(cfg: Config, job: Job) -> dict[str, str]:
    base = cfg.api_public_url  # empty means "relative to the API host"
    out = {"status": f"{base}/api/jobs/{job.id}", "log": f"{base}/api/jobs/{job.id}/log"}
    if job.bundle:
        out["bundle"] = f"{base}/api/jobs/{job.id}/bundle.tar.gz"
        out["check"] = f"{base}/api/jobs/{job.id}/check.json"
    return out


def job_view(cfg: Config, jobs: JobManager, job: Job) -> dict[str, Any]:
    d = job.to_dict()
    d["queue_position"] = jobs.queue_position(job)
    d["links"] = links(cfg, job)
    return d


async def lab_view(cfg: Config, jobs: JobManager, inventory: Inventory) -> dict[str, Any]:
    devices = [d.to_dict() for d in await inventory.scan()]
    run = jobs.running
    return {
        "version": __version__,
        "paused": jobs.paused,
        "rf_setup": cfg.rf_setup,
        "transmit_allowed": cfg.rf_setup in RF_SAFE,
        "running": run.id if run else None,
        "queued": [j.id for j in sorted((j for j in jobs.jobs.values() if j.status == "queued"), key=lambda j: j.created)],
        "devices": devices,
        "profiles": {n: {"requires": list(p.requires), "description": p.description,
                         "ready": all(any(d["id"] == r and d["present"] for d in devices) for r in p.requires)}
                     for n, p in cfg.profiles.items()},
        "inventory_backend": cfg.inventory_backend,
    }
