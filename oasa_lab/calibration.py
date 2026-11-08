"""The bench calibration file (`calibration.toml`, version 1): measured path losses, TX power and RX scale.

lab.toml points at it (optional):

    [calibration]
    path = "calibration.toml"     # relative to lab.toml
    recabled = "2026-11-25"       # optional: the last re-cabling; an older calibration is refused

Suites read it through the `lab` fixture (`lab.calibration`, `lab.require_calibration()`). With none
configured, nothing changes. Each run's manifest records the file's sha256 and date (or null). Format and
rules: docs/calibration.md.

Values between measured points are interpolated linearly (in dB against MHz and against gain dB). Nothing
is extrapolated: asking outside the measured range is an error, not a guess.
"""

from __future__ import annotations

import hashlib
import math
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

VERSION = 1
MIN_PATH_DB = 60.0          # between transmitters (AGENTS.md)
MIN_RX_ONLY_DB = 40.0       # toward a receive-only device
TX_SPREAD_MAX = 0.2         # dB, spread of repeated TX power readings
CERT_MAX_DAYS = 365         # power sensor calibration certificate age on the measurement date
BUNDLE_NAME = "calibration.toml"   # where a run that used the file keeps a copy


class CalibrationError(Exception):
    """The calibration file is missing, unreadable, invalid, stale, or asked for a value it does not hold."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _numlist(v: Any) -> bool:
    return isinstance(v, list) and all(_num(x) for x in v)


def _rising(v: list) -> bool:
    return all(b > a for a, b in zip(v, v[1:]))


def _day(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def validate(d: Any) -> list[str]:
    """The problems with a parsed calibration file; empty means valid.

    The same rules as the bench's standalone validator, plus what interpolation needs (numbers, rising axes)."""
    if not isinstance(d, dict):
        return ["the file is not a TOML table"]
    bad: list[str] = []
    if d.get("version") != VERSION:
        bad.append("version must be 1")
    day = _day(d.get("date"))
    if day is None:
        bad.append("date must be YYYY-MM-DD")
    ps = d.get("power_sensor", {})
    if not isinstance(ps, dict):
        bad.append("power_sensor must be a table")
        ps = {}
    cert = _day(ps.get("cal_cert_date"))
    if cert is None:
        bad.append("power_sensor.cal_cert_date must be YYYY-MM-DD")
    elif day and (day - cert).days > CERT_MAX_DAYS:
        bad.append(f"power sensor cert {cert} is older than 12 months on {day}")
    for tname in ("power_sensor", "vna"):
        t = d.get(tname, {})
        if not isinstance(t, dict):
            continue
        if "serial" in t:
            bad.append(f"{tname}.serial in clear: use serial_hash (a sha256 prefix)")
        looks = [k for k, v in t.items() if isinstance(v, str) and re.fullmatch(r"[0-9A-F]{6,}", v) and k != "serial_hash"]
        if looks:
            bad.append(f"{tname} fields look like serials: {looks}")
    paths = d.get("paths", {})
    if not isinstance(paths, dict) or not paths:
        bad.append("no [paths.*] tables")
        paths = {}
    for name, p in paths.items():
        if not isinstance(p, dict):
            bad.append(f"paths.{name} must be a table")
            continue
        f = p.get("freq_mhz", [])
        if not (_numlist(f) and f and _rising(f)):
            bad.append(f"paths.{name}.freq_mhz must be a list of rising numbers")
            f = f if isinstance(f, list) else []
        for key in ("loss_db", "phase_deg"):
            v = p.get(key, [])
            if not isinstance(v, list) or len(v) != len(f):
                bad.append(f"paths.{name}.{key} has {len(v) if isinstance(v, list) else 0} values for {len(f)} frequencies")
            elif not _numlist(v):
                bad.append(f"paths.{name}.{key} must be numbers")
        rx_only = p.get("receive_only", False) is True
        losses = p.get("loss_db", [])
        for fm, loss in zip(f, losses if isinstance(losses, list) else []):
            if not _num(loss):
                continue
            if loss <= 0:
                bad.append(f"paths.{name}: loss {loss} dB at {fm} MHz is not positive")
            elif loss < MIN_PATH_DB and not rx_only:
                bad.append(f"paths.{name}: {loss} dB at {fm} MHz is under the {MIN_PATH_DB:.0f} dB minimum between transmitters")
            elif rx_only and loss < MIN_RX_ONLY_DB:
                bad.append(f"paths.{name}: {loss} dB at {fm} MHz is under 40 dB toward a receive-only device")
        if not p.get("route"):
            bad.append(f"paths.{name}.route is empty")
    radios = d.get("radios", {})
    if not isinstance(radios, dict):
        bad.append("radios must be a table")
        radios = {}
    for rname, r in radios.items():
        if not isinstance(r, dict):
            bad.append(f"radios.{rname} must be a table")
            continue
        for side in ("tx", "rx"):
            t = r.get(side)
            if not t:
                continue
            if not isinstance(t, dict):
                bad.append(f"radios.{rname}.{side} must be a table")
                continue
            fr, gn = t.get("freq_mhz", []), t.get("gain_db", [])
            for axis, v in (("freq_mhz", fr), ("gain_db", gn)):
                if not (_numlist(v) and v and _rising(v)):
                    bad.append(f"radios.{rname}.{side}.{axis} must be a list of rising numbers")
            nf = len(fr) if isinstance(fr, list) else 0
            ng = len(gn) if isinstance(gn, list) else 0
            keys = ("dbm_out",) if side == "tx" else ("dbfs_to_dbm", "floor_dbfs")
            for key in keys:
                rows = t.get(key, [])
                if (not isinstance(rows, list) or len(rows) != nf
                        or any(not isinstance(row, list) or len(row) != ng for row in rows)):
                    bad.append(f"radios.{rname}.{side}.{key} must be {nf} rows x {ng} gains")
                elif not all(_numlist(row) for row in rows):
                    bad.append(f"radios.{rname}.{side}.{key} must be numbers")
            if side == "tx":
                spread = t.get("spread_db_max", 99)
                if not _num(spread) or spread > TX_SPREAD_MAX:
                    bad.append(f"radios.{rname}.tx spread {spread} dB > {TX_SPREAD_MAX} dB: repeat the measurement")
                for row in t.get("dbm_out", []) if isinstance(t.get("dbm_out"), list) else []:
                    if _numlist(row) and any(b < a - 0.5 for a, b in zip(row, row[1:])):
                        bad.append(f"radios.{rname}.tx dbm_out falls as gain rises: check the table")
                        break
    return bad


# --- interpolation ---------------------------------------------------------------------------
def interp(xs: list[float], ys: list[float], x: float, what: str = "value") -> float:
    """Linear interpolation on a rising axis. Outside the measured range is an error (no extrapolation)."""
    x = float(x)
    if not xs:
        raise CalibrationError(f"{what}: no measured points")
    if not xs[0] <= x <= xs[-1]:
        raise CalibrationError(f"{what}: {x:g} is outside the measured range {xs[0]:g} to {xs[-1]:g}")
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        if x0 <= x <= x1:
            return float(ys[i] + (ys[i + 1] - ys[i]) * (x - x0) / (x1 - x0))
    return float(ys[-1])  # a single measured point, asked exactly


def interp2(fs: list[float], gs: list[float], table: list[list[float]], f: float, g: float, what: str) -> float:
    """Bilinear: one row per frequency, one value per gain."""
    per_f = [interp(gs, row, g, f"{what} gain") for row in table]
    return interp(fs, per_f, f, f"{what} frequency (MHz)")


# --- the loaded file ---------------------------------------------------------------------------
@dataclass
class Calibration:
    data: dict[str, Any]
    raw: bytes
    source: str = ""
    used: bool = field(default=False, compare=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def date(self) -> str:
        return str(_day(self.data["date"]))

    @property
    def paths(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("paths", {}))

    @property
    def radios(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("radios", {}))

    def _path(self, name: str) -> dict[str, Any]:
        p = self.data.get("paths", {}).get(name)
        if p is None:
            raise CalibrationError(f"calibration has no [paths.{name}] (it has: {', '.join(self.paths) or 'none'})")
        return p

    def _side(self, radio: str, side: str) -> dict[str, Any]:
        t = self.data.get("radios", {}).get(radio, {}).get(side)
        if not t:
            raise CalibrationError(f"calibration has no [radios.{radio}.{side}]")
        return t

    def path_loss_db(self, path: str, freq_mhz: float) -> float:
        """Measured loss of a route (dB, positive) at a frequency."""
        p = self._path(path)
        return interp(p["freq_mhz"], p["loss_db"], freq_mhz, f"paths.{path} frequency (MHz)")

    def tx_dbm(self, radio: str, gain_db: float, freq_mhz: float) -> float:
        """Power out of the radio's TX port (dBm) at a TX gain and frequency (digital amplitude 0.5 full scale)."""
        t = self._side(radio, "tx")
        return interp2(t["freq_mhz"], t["gain_db"], t["dbm_out"], freq_mhz, gain_db, f"radios.{radio}.tx")

    def rx_offset_db(self, radio: str, gain_db: float, freq_mhz: float) -> float:
        """dBm_in = dBFS + this, at an RX gain and frequency."""
        t = self._side(radio, "rx")
        return interp2(t["freq_mhz"], t["gain_db"], t["dbfs_to_dbm"], freq_mhz, gain_db, f"radios.{radio}.rx")

    def rx_dbm(self, radio: str, dbfs: float, gain_db: float, freq_mhz: float) -> float:
        """A tone's level at the RX port (dBm) from its measured dBFS."""
        return float(dbfs) + self.rx_offset_db(radio, gain_db, freq_mhz)

    def rx_floor_dbfs(self, radio: str, gain_db: float, freq_mhz: float) -> float:
        """The terminated noise floor (dBFS) measured at an RX gain and frequency."""
        t = self._side(radio, "rx")
        return interp2(t["freq_mhz"], t["gain_db"], t["floor_dbfs"], freq_mhz, gain_db, f"radios.{radio}.rx floor")

    def manifest_entry(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "date": self.date, "version": VERSION, "valid": True, "used": self.used}


def parse(raw: bytes, source: str = "calibration.toml", recabled: str | None = None) -> Calibration:
    """Parse and validate. Raises CalibrationError with every problem listed."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CalibrationError(f"{source}: cannot read: {exc}") from None
    bad = validate(data)
    if recabled is not None and not bad:
        rc = _day(recabled)
        if rc is None:
            bad.append(f"[calibration] recabled = {recabled!r} in lab.toml must be YYYY-MM-DD")
        elif _day(data["date"]) < rc:  # type: ignore[operator]
            bad.append(f"calibration date {_day(data['date'])} is older than the last re-cabling ({rc}): measure again")
    if bad:
        raise CalibrationError(f"{source}: invalid calibration ({len(bad)} problems): " + "; ".join(bad), bad)
    return Calibration(data, raw, source)


def load(path: str | Path, recabled: str | None = None) -> Calibration:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise CalibrationError(f"{p}: cannot read: {exc}") from None
    return parse(raw, str(p), recabled)


def configured(inv: Any) -> tuple[Path, str | None] | None:
    """The calibration path (and re-cabling date) lab.toml configures, or None."""
    cfg = getattr(inv, "calibration", None) or {}
    if not cfg.get("path"):
        return None
    return Path(cfg["path"]), (str(cfg["recabled"]) if cfg.get("recabled") is not None else None)


def load_configured(inv: Any) -> Calibration | None:
    """None if lab.toml configures no calibration; raises CalibrationError if it does and the file is bad."""
    c = configured(inv)
    if c is None:
        return None
    return load(*c)


def manifest_entry_for(inv: Any) -> dict[str, Any] | None:
    """What the manifest records when the suite did not read the file: hash and date of what is configured,
    and whether it is valid. None if nothing is configured or the file is not there."""
    c = configured(inv)
    if c is None:
        return None
    try:
        raw = c[0].read_bytes()
    except OSError:
        return None
    entry: dict[str, Any] = {"sha256": hashlib.sha256(raw).hexdigest(), "date": None, "version": None,
                             "valid": False, "used": False}
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        d = _day(data.get("date"))
        entry["date"] = str(d) if d else None
        entry["version"] = data.get("version") if isinstance(data.get("version"), int) else None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return entry
    try:
        parse(raw, str(c[0]), c[1])
        entry["valid"] = True
    except CalibrationError:
        pass
    return entry
