"""pytest plugin: writes the live log and gives tests the `lab` fixture.

Load with `-p oasa_lab.pytest_plugin` (oasa-lab run and pyproject do this).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from . import context, sigmf
from .channel import Channel, ChannelSettings
from .context import RunContext, derive_seed, make_rng, sha256_file
from .devices import Inventory, InventoryError
from .hw import Hardware
from .pcap import PcapWriter


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
        self._air: list[tuple[Any, dict[str, Any], str, int]] = []

    def log(self, event: str, **fields: Any) -> None:
        self.ctx.log.write(event, test=self.nodeid, **fields)

    def marker(self) -> str:
        """A fresh random marker string. Not secret. The checker looks for it in captures."""
        m = "OASA-MARK-" + "".join(self.rng.choice("0123456789abcdef") for _ in range(24))
        self.markers.append(m)
        self.ctx.markers.append(m)
        return m

    def channel(self, *, a=None, b=None, seed: int | None = None, profile: str | None = None,
                **settings: Any) -> Channel:
        """A simulated channel for this test. Started and recorded; stopped when the test ends.

        Unless `seed` is given, the seed is derived from the run seed and this test's id.
        `profile` starts from a radio profile (see profiles.py); settings you pass override it.
        The manifest records the profile name and the fully resolved settings.
        """
        n = self.ctx.next_index()
        if seed is None:
            seed = derive_seed(self.ctx.seed, self.nodeid, n)
        cfg = ChannelSettings.for_profile(profile, seed=seed, **settings)
        cid = f"ch{n:02d}"
        rel = f"captures/{n:02d}-{_slug(self.nodeid.split('::')[-1])}.pcap"
        flags = self.ctx.suite.capture_flags()
        ch = Channel(cfg, a=a, b=b, pcap_path=self.ctx.run_dir / rel, name=cid,
                     on_event=lambda ev, **f: self.log(ev, **f), stats_interval=1.0)
        entry = {"id": cid, "test": self.nodeid, "profile": cfg.profile, "settings": cfg.to_dict(),
                 "capture": rel}
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

    def air_capture(self, link: Any, device_id: str, name: str | None = None) -> str:
        """Record what a modem delivers to its host (what it heard on the air) as captures/NN-air-NAME.pcap.

        `link` is a ModemLink. The file is written when the test ends. Returns its path in the bundle.
        The frames are packets of the radio, wrapped in synthetic IP and UDP headers so the checker's
        scanner and Wireshark can read them.
        """
        n = self.ctx.next_index()
        rel = f"captures/{n:02d}-air-{_slug(name or device_id)}.pcap"
        cap = {"file": rel, "kind": "air", "device": device_id, **self.ctx.suite.capture_flags(), "markers": []}
        self.ctx.captures.append(cap)
        self._air.append((link, cap, rel, n))
        return rel

    def sigmf(self, name: str, data: Any, *, datatype: str, sample_rate: float, center_hz: float, start_time: float,
              device: str, purpose: str, description: str = "", burst_check: dict[str, Any] | None = None,
              annotations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Write a radio recording as SigMF into recordings/ and list it in the manifest.

        `data` is bytes or the path of a file to move in. `burst_check` asks the checker to line the
        recording's bursts up with the airtime of the frames in the named air captures.
        """
        info = self.ctx.hardware["devices"].get(device, {})
        hw = f"{info.get('model', device)} SN {info.get('serial', 'n/a')} ({info.get('backend', '?')})"
        entry = sigmf.write_recording(
            self.ctx.run_dir, self.ctx.next_index(), name, data, datatype=datatype, sample_rate=sample_rate,
            center_hz=center_hz, start_time=start_time, device=device, hw=hw, run_id=self.ctx.run_id,
            purpose=purpose, description=description, annotations=annotations, burst_check=burst_check)
        self.ctx.recordings.append(entry)
        self.log("recording", file=entry["meta"], samples=entry["samples"], datatype=datatype,
                 sample_rate=sample_rate, center_hz=center_hz, device=device, purpose=purpose)
        return entry

    def measure(self, metric: str, name: str, value: float, unit: str = "") -> bool:
        """Log a measurement against the limits the suite declares in `[limits.METRIC]`. Returns True if inside.

        The checker re-applies the same limits from suite.toml, so a test cannot loosen them.
        """
        lim = self.ctx.suite.limits.get(metric)
        if lim is None:
            raise KeyError(f"{metric!r} has no [limits.{metric}] in suite.toml")
        lo, hi = lim.get("min"), lim.get("max")
        ok = (lo is None or value >= lo) and (hi is None or value <= hi)
        self.log("measurement", metric=metric, name=name, value=value, unit=unit, min=lo, max=hi, ok=ok)
        return ok

    def _finish(self) -> None:
        for link, cap, rel, n in self._air:
            w = PcapWriter(self.ctx.run_dir / rel)
            for fr in link.rx_packets():
                w.write(("10.99.0.254", 9), (f"10.99.0.{n % 250 + 1}", 9), fr.data, ts=fr.ts)
            w.close()
            cap["markers"] = list(self.markers)
        self._air.clear()
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


@pytest.fixture
def hw(request: pytest.FixtureRequest, lab: Lab):
    """The bench: devices from lab.toml (or the simulated bench). Skips the test if the suite's
    `requires` are not all there. Devices are closed when the test ends."""
    ctx = context.current()
    try:
        inv = Inventory.load()
    except InventoryError as exc:
        pytest.skip(f"lab.toml is not usable: {exc}")
    missing = inv.missing(ctx.suite.requires)
    if missing:
        pytest.skip(f"hardware missing from {inv.source}: {', '.join(missing)}. "
                    "Write lab.toml (see lab.example.toml) or set OASA_LAB_SIM=1 for the simulated bench")
    h = Hardware(ctx, inv, request.node.nodeid)
    try:
        yield h
    finally:
        h.close()


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
