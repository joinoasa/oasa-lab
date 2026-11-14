"""tinySA (Ultra / Ultra+) as a level reference: a small reader over its USB serial console (CDC).

F15 measures TX power and RX scale against the tinySA to about 2 dB. It only reads: it is a spectrum analyzer
with no output used here (its signal generator mode is never touched), so it needs no TX gate. The one
risk is to the tinySA itself: keep 40 dB or more between a transmitter and its input, with its 30 dB pad
always fitted, and set `max_input_dbm` from its datasheet. A level within `margin_db` of that stops with an
error.

The console takes a command line ending in a carriage return, echoes it, prints its answer and ends with the
prompt `ch> `. The command strings below follow the tinySA USB interface notes (tinysa.org). `CONFIRMED` lists
the ones tried on a real unit (tinySA Ultra+ ZS407, firmware tinySA4_v1.4-217, 2026-11-14); `info()` records
`commands_confirmed: false` until every one is:

    version                                   firmware string                                    confirmed
    scan START STOP POINTS 3                  one line per point: "FREQ_HZ LEVEL_DBM 0.0"        confirmed
                                              (outmask 1 freq + 2 level; a third field follows, ignored)
    marker 1 peak, then marker 1              "1 INDEX FREQ_HZ LEVEL_DBM"                        NOT for use after scan

On that firmware the marker follows the sweep on the tinySA's own screen, not the last `scan`: after a scan of
914 to 916 MHz it answered with a peak at 1002.8 MHz. So `read_marker_dbm` reads the screen's sweep, whatever
that is set to; use `read_peak_dbm` (the scan's own points) for a level at a known frequency.

`scanraw` (binary samples) is not used. `input_pad_db` (the fitted pad, e.g. 30) is added to every reading in
software; leave the tinySA's own external gain setting at 0 so it is not counted twice.
"""

from __future__ import annotations

import math
import random
import re
import time
from typing import Any

from .base import Device, DeviceError
from .modem import PortLike, SerialPort

PROMPT = b"ch> "
BAUD = 115200   # CDC ignores it
COMMANDS: dict[str, str] = {        # see CONFIRMED and the notes above
    "version": "version",
    "scan": "scan {start:.0f} {stop:.0f} {points:d} 3",
    "marker_peak": "marker 1 peak",
    "marker": "marker 1",
}
CONFIRMED: tuple[str, ...] = ("version", "scan")
DEFAULT_MAX_INPUT_DBM = 10.0
DEFAULT_MARGIN_DB = 3.0
MAX_POINTS = 450


class SimTinySAPort:
    """Speaks the console: echo, answer, prompt. A noise floor plus one tone.

    sim settings: tone_hz, tone_dbm (at the tinySA's input, after the pad), floor_dbm, noise_db, seed, version."""

    def __init__(self, cfg: dict[str, Any]):
        self.tone_hz = float(cfg.get("tone_hz", 915e6))
        self.tone_dbm = float(cfg.get("tone_dbm", -40.0))
        self.floor_dbm = float(cfg.get("floor_dbm", -95.0))
        self.noise_db = float(cfg.get("noise_db", 0.1))
        self.version = str(cfg.get("version", "tinySA4_sim-0.1"))
        self.rng = random.Random(int(cfg.get("seed", 0)))
        self.written: list[str] = []
        self.out = b""
        self.last_scan: list[tuple[float, float]] = []

    def _level(self, f: float, step: float) -> float:
        tone = self.tone_dbm if abs(f - self.tone_hz) <= step / 2 else -999.0
        return max(tone, self.floor_dbm) + self.rng.gauss(0, self.noise_db)

    def write(self, data: bytes) -> None:
        line = data.decode().rstrip("\r\n")
        self.written.append(line)
        words = line.split()
        reply = ""
        if line == COMMANDS["version"]:
            reply = self.version + "\r\nHW Version:sim\r\n"
        elif words[:1] == ["scan"] and len(words) >= 4:
            start, stop, n = float(words[1]), float(words[2]), int(words[3])
            step = (stop - start) / max(n - 1, 1)
            self.last_scan = [(start + i * step, self._level(start + i * step, step)) for i in range(n)]
            reply = "".join(f"{f:.0f} {lv:.2f}\r\n" for f, lv in self.last_scan)
        elif line == COMMANDS["marker"] and self.last_scan:
            i = max(range(len(self.last_scan)), key=lambda k: self.last_scan[k][1])
            reply = f"1 {i} {self.last_scan[i][0]:.0f} {self.last_scan[i][1]:.2f}\r\n"
        self.out += line.encode() + b"\r\n" + reply.encode() + PROMPT

    def read(self, timeout: float = 0.05) -> bytes:
        out, self.out = self.out, b""
        return out

    def close(self) -> None:
        pass


class TinySA(Device):
    kind = "tinysa"

    def _open(self) -> None:
        self.timeout_s = float(self.cfg.get("timeout_s", 10.0))
        self.input_pad_db = float(self.cfg.get("input_pad_db", 30.0))
        self.max_input_dbm = float(self.cfg.get("max_input_dbm", DEFAULT_MAX_INPUT_DBM))
        self.margin_db = float(self.cfg.get("margin_db", DEFAULT_MARGIN_DB))
        self.port: PortLike = self._make_port()
        try:
            lines = self.command(COMMANDS["version"])
            if not lines or not re.match(r"tinySA", lines[0], re.I):
                raise DeviceError(f"{self.id}: this is not a tinySA (version said {lines!r})")
            self.firmware = lines[0].strip()
        except BaseException:
            self.port.close()
            raise

    def _make_port(self) -> PortLike:
        if self.backend == "sim":
            return SimTinySAPort(dict(self.cfg.get("sim", {})))
        return SerialPort(str(self.cfg["port"]), BAUD)

    def _close(self) -> None:
        self.port.close()

    def _info(self) -> dict[str, Any]:
        return {"model": str(self.cfg.get("model", "tinySA Ultra+")), "serial": str(self.cfg.get("serial", "")),
                "firmware": self.firmware, "input_pad_db": self.input_pad_db, "max_input_dbm": self.max_input_dbm,
                "commands_confirmed": all(k in CONFIRMED for k in COMMANDS)}

    # --- the console ------------------------------------------------------------------------------
    def command(self, line: str, timeout_s: float | None = None) -> list[str]:
        """Send one command; return the answer's lines (echo and prompt removed)."""
        self.port.read()
        self.port.write(line.encode() + b"\r")
        end = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        got = b""
        while not got.endswith(PROMPT):
            if time.monotonic() > end:
                raise DeviceError(f"{self.id}: no prompt after {line!r}")
            data = self.port.read()
            if data:
                got += data
            elif self.backend != "sim":
                time.sleep(0.01)
        text = got[: -len(PROMPT)].decode(errors="replace").replace("\r", "")
        lines = [x for x in text.split("\n") if x.strip()]
        if lines and lines[0].strip() == line:
            lines = lines[1:]
        return lines

    def _checked(self, raw_dbm: float) -> float:
        if not math.isfinite(raw_dbm):
            raise DeviceError(f"{self.id}: no valid level")
        if raw_dbm >= self.max_input_dbm - self.margin_db:
            raise DeviceError(f"{self.id}: {raw_dbm:.1f} dBm at the input is within {self.margin_db:g} dB of its maximum "
                              f"({self.max_input_dbm:g} dBm). Remove the signal and add attenuation")
        return raw_dbm + self.input_pad_db

    # --- what suites call ------------------------------------------------------------------------
    def identify(self) -> dict[str, str]:
        return {"model": str(self.cfg.get("model", "tinySA Ultra+")), "firmware": self.firmware}

    def scan(self, start_hz: float, stop_hz: float, points: int = 101) -> list[tuple[float, float]]:
        """(frequency Hz, level dBm before the pad correction) per point."""
        if not (0 < start_hz < stop_hz and 2 <= points <= MAX_POINTS):
            raise DeviceError(f"{self.id}: scan {start_hz:g} to {stop_hz:g} Hz, {points} points is not sane")
        out = []
        for ln in self.command(COMMANDS["scan"].format(start=start_hz, stop=stop_hz, points=int(points)),
                               timeout_s=self.timeout_s + points * 0.05):
            parts = ln.split()
            try:
                out.append((float(parts[0]), float(parts[1])))
            except (IndexError, ValueError):
                raise DeviceError(f"{self.id}: scan gave {ln!r}") from None
        if len(out) != points:
            raise DeviceError(f"{self.id}: scan gave {len(out)} points, asked for {points}")
        return out

    def read_peak_dbm(self, center_hz: float, span_hz: float = 1e6, points: int = 101) -> tuple[float, float]:
        """The strongest point around center_hz: (frequency Hz, level dBm with the pad added)."""
        pts = self.scan(center_hz - span_hz / 2, center_hz + span_hz / 2, points)
        f, lv = max(pts, key=lambda p: p[1])
        return f, self._checked(lv)

    def read_marker_dbm(self) -> tuple[float, float]:
        """Marker 1 moved to the peak of the sweep on the tinySA's own screen: (frequency Hz, level dBm with the pad
        added). Not the last `scan` (see the module notes); for a level at a known frequency use `read_peak_dbm`."""
        self.command(COMMANDS["marker_peak"])
        lines = self.command(COMMANDS["marker"])
        mm = re.match(r"\s*1\s+\d+\s+(\S+)\s+(\S+)", lines[0]) if lines else None
        if not mm:
            raise DeviceError(f"{self.id}: marker gave {lines!r}")
        return float(mm.group(1)), self._checked(float(mm.group(2)))
