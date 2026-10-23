"""State of the run in progress, shared by the runner and the pytest plugin."""

from __future__ import annotations

import hashlib
import random
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonl import JsonlLog, now_rfc3339
from .suites import Suite


class RunContext:
    def __init__(self, suite: Suite, seed: int, run_dir: Path, run_id: str | None = None):
        self.suite = suite
        self.seed = seed
        self.run_dir = Path(run_dir)
        self.run_id = run_id or uuid.uuid4().hex
        self.start = now_rfc3339()
        self.start_dt = datetime.now(timezone.utc)
        (self.run_dir / "captures").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "logs").mkdir(exist_ok=True)
        self.log = JsonlLog(self.run_dir / "log.jsonl")
        self.markers: list[str] = []
        self.channels: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.binaries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._n = 0

    def next_index(self) -> int:
        with self._lock:
            self._n += 1
            return self._n


_current: RunContext | None = None


def set_current(ctx: RunContext | None) -> None:
    global _current
    _current = ctx


def current() -> RunContext:
    """The run in progress. Outside `oasa-lab run` (plain pytest) a throw-away run is made."""
    global _current
    if _current is None:
        tmp = Path(tempfile.mkdtemp(prefix="oasa-lab-adhoc-"))
        suite = Suite(name="adhoc", path=tmp, description="plain pytest run", captures={})
        _current = RunContext(suite, 0, tmp)
    return _current


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_seed(*parts: Any) -> int:
    """A stable 32-bit seed from the run seed and a label (test id, channel number)."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def make_rng(*parts: Any) -> random.Random:
    return random.Random(derive_seed(*parts))


def git_info(repo: Path) -> dict[str, Any]:
    import os

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              check=True, timeout=20).stdout.strip()

    try:
        sha = os.environ.get("OASA_LAB_GIT_SHA") or git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        try:
            branch = git("rev-parse", "--abbrev-ref", "HEAD")
        except Exception:
            branch = None
        return {"git_sha": sha, "git_dirty": dirty, "git_branch": branch}
    except Exception as exc:
        raise SystemExit(f"oasa-lab: cannot read the git state of {repo} ({exc}). Run from a git clone.")
