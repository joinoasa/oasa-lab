"""The encrypted link over the SDR bench: an RF relay that carries UDP datagrams as frame v1 over real radios.

Design: docs/sdr-link.md. The relay stands where `lab.channel(...)` stands for the simulated link. Link A sends to
`relay.addr_a`, link B to `relay.addr_b`. Datagrams are queued per direction and carried in cycles, one direction
at a time (one UHD process per radio): each queued datagram becomes one frame v1 (`oasa-radio waveform iq`,
BPSK, 125 kbaud at 1 MS/s, digital IF 250 kHz as W1), the frames are joined into a block, one radio sends the
block on repeat while the other records about two repeats, and `oasa-radio waveform decode --frame v1` finds the
frames. Every distinct payload whose CRC passed is delivered once to the far link and kept for the air capture.

The relay never looks inside a datagram. Tests can add frames of their own to a direction (`inject`), for the
replay, changed-packet and plaintext-control cases.
"""

from __future__ import annotations

import cmath
import json
import math
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .devices.usrp import Usrp
from .iq import IQ
from .w1test import (CFO_SEARCH, GAP, IF_HZ, LEAD, MAX_PROBE_PEAK, PEAK, RX_GAIN_DB, SAMPLE_RATE, SPS, SYMBOL_RATE,
                     TooStrong, baseband, write_sigmf)

MOD = "bpsk"
MAX_FRAMES = 16          # frames per cycle; more wait for the next cycle
MAX_QUEUE = 256          # per direction; datagrams past it are dropped and counted (a radio is not a buffer)
MAX_PAYLOAD = 255        # frame v1
DIRECTIONS = ("a2b", "b2a")


@dataclass
class AirFrame:
    data: bytes
    ts: float


@dataclass
class Cycle:
    direction: str
    sent: int
    found: int            # distinct payloads decoded with a good CRC
    crc_failed: int
    seconds: float
    peak: float
    injected: int = 0


class AirSide:
    """What one receiver heard: the `link` argument for `lab.air_capture` (it needs `rx_packets()`)."""

    def __init__(self) -> None:
        self.frames: list[AirFrame] = []

    def rx_packets(self) -> list[AirFrame]:
        return list(self.frames)


@dataclass
class Leg:
    tx: Usrp
    rx: Usrp
    gain_db: float
    queue: deque = field(default_factory=deque)
    inject: list[bytes] = field(default_factory=list)
    carried: list[bytes] = field(default_factory=list)   # every payload delivered, in order
    air: AirSide = field(default_factory=AirSide)
    recording: IQ | None = None
    dropped: int = 0


class RfRelay:
    """Carries UDP datagrams between two local endpoints over the radios.

    `legs` maps "a2b" and "b2a" to (tx radio, rx radio, TX gain). `a_dest` and `b_dest` are where link A and
    link B listen (their --bind). Call `start()`, then `run(until)` from the test thread.
    """

    def __init__(self, radio: Path, work: Path, rf_hz: float, legs: dict[str, tuple[Usrp, Usrp, float]],
                 a_dest: tuple[str, int], b_dest: tuple[str, int], host: str = "127.0.0.1",
                 log: Callable[..., Any] | None = None):
        self.radio, self.work, self.rf_hz = radio, work, rf_hz
        self.legs = {d: Leg(*legs[d]) for d in DIRECTIONS}
        self.dest = {"a2b": b_dest, "b2a": a_dest}
        self.sock = {"a": socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
                     "b": socket.socket(socket.AF_INET, socket.SOCK_DGRAM)}
        for s in self.sock.values():
            s.bind((host, 0))
            s.settimeout(0.2)
        self.addr_a, self.addr_b = self.sock["a"].getsockname(), self.sock["b"].getsockname()
        self.cycles: list[Cycle] = []
        self._log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._listen, args=(side, d), daemon=True)
                         for side, d in (("a", "a2b"), ("b", "b2a"))]
        self._n = 0

    # --- UDP side --------------------------------------------------------------------------------------
    def start(self) -> "RfRelay":
        for t in self._threads:
            t.start()
        return self

    def _listen(self, side: str, direction: str) -> None:
        s, leg = self.sock[side], self.legs[direction]
        while not self._stop.is_set():
            try:
                data, _ = s.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            with self._lock:
                if len(data) > MAX_PAYLOAD or len(leg.queue) >= MAX_QUEUE:
                    leg.dropped += 1
                else:
                    leg.queue.append(data)

    def pending(self, direction: str) -> int:
        with self._lock:
            leg = self.legs[direction]
            return len(leg.queue) + len(leg.inject)

    def inject(self, direction: str, payload: bytes) -> None:
        """Send `payload` as a frame in the next cycle of `direction`, as if a link had sent it."""
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("a frame v1 carries at most 255 bytes")
        with self._lock:
            self.legs[direction].inject.append(payload)

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(1)
        for s in self.sock.values():
            s.close()

    # --- RF side ---------------------------------------------------------------------------------------
    def _frame(self, payload: bytes) -> list[complex]:
        self._n += 1
        base = self.work / f"relay-tx-{self._n}"
        subprocess.run([str(self.radio), "waveform", "iq", "--mod", MOD, "--frame", "v1", "--out", str(base),
                        "--payload-hex", payload.hex(), "--symbol-rate", str(SYMBOL_RATE), "--sps", str(SPS),
                        "--delay", "0", "--tail", "0"], check=True, capture_output=True, timeout=60)
        x = IQ(base.with_suffix(".sigmf-data").read_bytes(), "cf32_le", SAMPLE_RATE, 0).samples()
        if not hasattr(self, "_meta"):
            self._meta = json.loads(base.with_suffix(".sigmf-meta").read_text())
        for ext in (".sigmf-data", ".sigmf-meta"):
            base.with_suffix(ext).unlink(missing_ok=True)
        return x

    def _block(self, payloads: list[bytes]) -> list[complex]:
        samples: list[complex] = [0j] * LEAD
        for p in payloads:
            samples += self._frame(p) + [0j] * GAP
        w = 2 * math.pi * IF_HZ / SAMPLE_RATE
        samples = [v * cmath.exp(1j * w * k) for k, v in enumerate(samples)]
        scale = PEAK / max(abs(v) for v in samples)
        return [v * scale for v in samples]

    def _decode(self, base: Path) -> list[dict[str, Any]]:
        r = subprocess.run([str(self.radio), "waveform", "decode", "--in", str(base), "--mod", MOD, "--sps", str(SPS),
                            "--cfo-search", str(CFO_SEARCH), "--frame", "v1", "--hex"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"oasa-radio decode failed: {(r.stderr or r.stdout)[-300:]}")
        return [d for d in (json.loads(l) for l in r.stdout.splitlines() if l.startswith("{"))
                if d.get("event") == "frame"]

    def cycle(self, direction: str) -> Cycle | None:
        """Carry what is queued for one direction. None if nothing was queued."""
        leg = self.legs[direction]
        with self._lock:
            payloads = list(leg.inject)
            leg.inject.clear()
            n_inj = len(payloads)
            while leg.queue and len(payloads) < MAX_FRAMES:
                payloads.append(leg.queue.popleft())
        if not payloads:
            return None
        t0 = time.monotonic()
        block = self._block(payloads)
        n = 2 * len(block) + LEAD          # two repeats, whatever the phase the recording starts at
        try:
            leg.tx.tx_file(block, self.rf_hz, leg.gain_db, SAMPLE_RATE)
            iq = leg.rx.capture(self.rf_hz, SAMPLE_RATE, n, gain_db=RX_GAIN_DB)
        finally:
            leg.tx.tx_off()
        raw = iq.samples()
        peak = max(abs(v) for v in raw)
        if peak > MAX_PROBE_PEAK:
            raise TooStrong(f"{direction} at TX gain {leg.gain_db:g} dB peaks at {peak:.2f} of full scale; "
                            f"check the cable and attenuators from {leg.tx.tx_port} to {leg.rx.rx_port}")
        leg.recording = iq
        base = self.work / f"relay-rx-{direction}-{len(self.cycles)}"
        write_sigmf(self._meta, baseband(iq), base, f"relay {direction}")
        frames = self._decode(base)
        for ext in (".sigmf-data", ".sigmf-meta"):
            base.with_suffix(ext).unlink(missing_ok=True)
        seen: set[bytes] = set()
        bad = 0
        side = "b" if direction == "a2b" else "a"
        for f in frames:
            if not f.get("crc_ok"):
                bad += 1
                continue
            p = bytes.fromhex(f.get("payload_hex") or "")
            if p in seen:
                continue                    # the block repeats: one delivery per frame sent
            seen.add(p)
            self.sock[side].sendto(p, self.dest[direction])
            leg.carried.append(p)
            leg.air.frames.append(AirFrame(p, time.time()))
        c = Cycle(direction, len(payloads), len(seen), bad, round(time.monotonic() - t0, 2), round(peak, 4), n_inj)
        self.cycles.append(c)
        self._log("relay_cycle", direction=direction, sent=c.sent, found=c.found, crc_failed=c.crc_failed,
                  injected=c.injected, seconds=c.seconds, peak=c.peak, tx_gain_db=leg.gain_db)
        return c

    def run(self, until: Callable[[], bool], timeout: float, idle: float = 0.3) -> bool:
        """Alternate directions until `until()` is true or `timeout` seconds pass. True if `until()` held."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if until():
                return True
            did = [self.cycle(d) for d in DIRECTIONS]
            if not any(did):
                time.sleep(idle)
        return until()

    def totals(self, direction: str) -> dict[str, int]:
        cs = [c for c in self.cycles if c.direction == direction]
        return {"cycles": len(cs), "sent": sum(c.sent for c in cs), "found": sum(c.found for c in cs),
                "crc_failed": sum(c.crc_failed for c in cs), "dropped": self.legs[direction].dropped}


def probe_gains(radio: Path, b200: Usrp, e310: Usrp, rf_hz: float, work: Path, want_db: float = 10.0,
                margin_db: float = 3.0) -> dict[str, tuple[float, float | None]]:
    """The TX gain for each direction: W1's probe (24 v1 frames, raised from a low start until Eb/N0 reaches
    `want_db`), plus a margin, capped at the radio's limit. Returns {direction: (gain, probe Eb/N0)}."""
    from .w1test import make_block, probe_gain
    out: dict[str, tuple[float, float | None]] = {}
    for direction, tx, rx in (("a2b", b200, e310), ("b2a", e310, b200)):
        block = make_block(radio, MOD, work, seed=21 if direction == "a2b" else 22, frame="v1")
        g, pt = probe_gain(radio, tx, rx, block, rf_hz, work, direction, want_db=want_db)
        out[direction] = (min(g + margin_db, tx.max_tx_gain), pt.ebn0_db)
    return out


def radio_bin() -> Path | None:
    """oasa-radio from OASA_RADIO_BIN_DIR, or None."""
    import os
    d = os.environ.get("OASA_RADIO_BIN_DIR")
    p = Path(d) / "oasa-radio" if d else None
    return p if p is not None and p.is_file() and os.access(p, os.X_OK) else None
