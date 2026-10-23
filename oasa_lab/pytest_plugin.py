"""pytest plugin: writes the live log and gives tests the `lab` fixture.

Load with `-p oasa_lab.pytest_plugin` (oasa-lab run and pyproject do this).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from . import context
from .channel import Channel, ChannelSettings
from .context import RunContext, derive_seed, make_rng, sha256_file


def _source_git(binary: Path) -> dict:
    """If the binary was built inside a git checkout, record which commit (for the manifest)."""
    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(binary.parent), *args], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    sha = git("rev-parse", "HEAD")
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return {}
    url = git("config", "--get", "remote.origin.url") or ""
    return {"source_git_sha": sha, "source_git_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
            "source_repo": re.sub(r"https://[^@/]*@", "https://", url)[:200]}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "test"


class Lab:
    """What a test gets from the `lab` fixture."""

    def __init__(self, ctx: RunContext, nodeid: str):
        self.ctx, self.nodeid = ctx, nodeid
        self.seed = ctx.seed
        self.rng = make_rng(ctx.seed, nodeid)  # repeatable randomness for this test
        self.markers: list[str] = []
        self._channels: list[tuple[Channel, dict[str, Any]]] = []

    def log(self, event: str, **fields: Any) -> None:
        self.ctx.log.write(event, test=self.nodeid, **fields)

    def marker(self) -> str:
        """A fresh random marker string. Not secret. The checker looks for it in captures."""
        m = "OASA-MARK-" + "".join(self.rng.choice("0123456789abcdef") for _ in range(24))
        self.markers.append(m)
        self.ctx.markers.append(m)
        return m

    def channel(self, *, a=None, b=None, seed: int | None = None, **settings: Any) -> Channel:
        """A simulated channel for this test. Started and recorded; stopped when the test ends.

        Unless `seed` is given, the seed is derived from the run seed and this test's id.
        """
        n = self.ctx.next_index()
        if seed is None:
            seed = derive_seed(self.ctx.seed, self.nodeid, n)
        cfg = ChannelSettings(seed=seed, **settings)
        cid = f"ch{n:02d}"
        rel = f"captures/{n:02d}-{_slug(self.nodeid.split('::')[-1])}.pcap"
        flags = self.ctx.suite.capture_flags()
        ch = Channel(cfg, a=a, b=b, pcap_path=self.ctx.run_dir / rel, name=cid,
                     on_event=lambda ev, **f: self.log(ev, **f), stats_interval=1.0)
        entry = {"id": cid, "test": self.nodeid, "settings": cfg.to_dict(), "capture": rel}
        self.ctx.channels.append(entry)
        cap = {"file": rel, "channel": cid, **flags, "markers": []}
        self.ctx.captures.append(cap)
        self._channels.append((ch, cap))
        return ch.start()

    def file(self, name: str) -> Path:
        """A path inside the bundle (logs/NAME) for a test to write a log or artifact to."""
        if "/" in name or name.startswith("."):
            raise ValueError("plain file name only")
        return self.ctx.run_dir / "logs" / f"{_slug(self.nodeid.split('::')[-1])}-{name}"

    def binary(self, path: str | Path) -> Path:
        """Record a binary the test uses (sha256, version) in the manifest."""
        p = Path(path).resolve()
        version = None
        try:
            r = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=5)
            line = (r.stdout or r.stderr).strip().splitlines()
            version = line[0][:200] if r.returncode == 0 and line else None
        except Exception:
            pass
        self.ctx.binaries[str(p)] = {"name": p.name, "path": str(p), "sha256": sha256_file(p),
                                     "version": version, **_source_git(p)}
        return p

    def _finish(self) -> None:
        for ch, cap in self._channels:
            ch.stop()
            entry = next(c for c in self.ctx.channels if c["id"] == ch.name)
            entry["endpoints"] = ch.endpoints()
            cap["markers"] = list(self.markers)
        self._channels.clear()


@pytest.fixture
def lab(request: pytest.FixtureRequest):
    obj = Lab(context.current(), request.node.nodeid)
    try:
        yield obj
    finally:
        obj._finish()


# --- live log ------------------------------------------------------------------------------
_reports: dict[str, list[pytest.TestReport]] = {}


def pytest_runtest_logstart(nodeid: str, location) -> None:
    context.current().log.write("test_start", test=nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _reports.setdefault(report.nodeid, []).append(report)


def _outcome(reports: list[pytest.TestReport]) -> tuple[str, str | None, float]:
    """Same rule as the CTRF plugin: the first non-passed report decides."""
    reason = None
    for r in reports:
        if r.skipped:
            lr = r.longrepr
            reason = lr[2] if isinstance(lr, tuple) and len(lr) == 3 else str(lr)
            return "skipped", reason.removeprefix("Skipped: "), sum(x.duration for x in reports)
        if r.failed:
            return "failed", None, sum(x.duration for x in reports)
    return "passed", None, sum(x.duration for x in reports)


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    outcome, reason, duration = _outcome(_reports.pop(nodeid, []))
    fields: dict[str, Any] = {"test": nodeid, "outcome": outcome, "duration_ms": round(duration * 1000, 1)}
    if reason:
        fields["reason"] = reason
    context.current().log.write("test_end", **fields)
