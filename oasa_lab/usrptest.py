"""Measurements for the USRP loopback suite, kept apart from the pytest file so the lab's own tests
can run them on the simulated bench.

One radio sends a tone, the other receives it over cable and fixed attenuators. The two radios have
their own crystals, so the tone lands off its nominal place by their difference; the same difference,
mirrored, shows in the other direction.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from typing import Any

from .devices.usrp import Usrp
from .iq import DATATYPES, IQ, tone_report

OFFSET_HZ = 100_000.0       # the tone sits this far from the centre, clear of the DC spike
SAMPLE_RATE = 1e6
TX_SCALE = 0.3              # digital amplitude of the tone (about -10.5 dBFS)
RX_GAIN_DB = 40.0
RAMP_DB = (20.0, 30.0)      # TX gain steps before the final one
FINAL_TX_GAIN_DB = 40.0     # within the drivers' default max_tx_gain
CAPTURE_SAMPLES = 100_000
SETTLE_SAMPLES = 30_000     # dropped from the start of every capture
ANALYSIS_POINTS = 1 << 16
MAX_PROBE_DBFS = -20.0      # a probe tone this strong means the cable is wrong: stop before raising power


class TooStrong(Exception):
    """The receiver saw far more than a 60 dB cable should give. TX is off again when this is raised."""


def settled(iq: IQ) -> IQ:
    cut = SETTLE_SAMPLES * DATATYPES[iq.datatype]
    out = IQ(iq.data[cut:], iq.datatype, iq.sample_rate, iq.center_hz)
    out.start_time = getattr(iq, "start_time", 0.0) + SETTLE_SAMPLES / iq.sample_rate  # type: ignore[attr-defined]
    return out


def report(iq: IQ) -> dict[str, Any]:
    return tone_report(settled(iq), max_points=ANALYSIS_POINTS)


@dataclass
class Loopback:
    rf_hz: float
    final_gain_db: float
    on: IQ
    off: IQ
    tone: dict[str, Any]                     # tone_report at the final gain
    spur: dict[str, Any]                     # tone_report with the TX off (the strongest line left)
    levels: dict[float, float] = field(default_factory=dict)   # TX gain -> tone dBFS, every step
    offset_ppm: float = 0.0                  # where the tone landed against where it was sent
    slope_db_per_db: float = 0.0             # level change per dB of TX gain over the last step
    underflows: int = 0


def loopback(tx: Usrp, rx: Usrp, rf_hz: float, final_gain_db: float = FINAL_TX_GAIN_DB) -> Loopback:
    """Ramp the tone up while watching the receiver, then measure it on and off."""
    final = min(final_gain_db, tx.max_tx_gain)
    steps = [g for g in RAMP_DB if g < final] + [final]
    levels: dict[float, float] = {}
    try:
        for g in steps:
            tx.tx_tone(rf_hz, OFFSET_HZ, g, TX_SCALE, SAMPLE_RATE)
            on = rx.capture(rf_hz, SAMPLE_RATE, CAPTURE_SAMPLES, gain_db=RX_GAIN_DB)
            rep = report(on)
            levels[g] = rep["tone_dbfs"]
            if rep["tone_dbfs"] > MAX_PROBE_DBFS:
                tx.tx_off()
                raise TooStrong(f"at TX gain {g:g} dB {tx.id} to {rx.id} reads {rep['tone_dbfs']:.1f} dBFS; "
                                f"check the cable and attenuators from {tx.tx_port} to {rx.rx_port}")
    finally:
        underflows = tx.tx_off()["underflows"]
    off = rx.capture(rf_hz, SAMPLE_RATE, CAPTURE_SAMPLES, gain_db=RX_GAIN_DB)
    lb = Loopback(rf_hz, final, on, off, rep, report(off), levels, underflows=underflows)
    lb.offset_ppm = (rep["freq_hz"] - OFFSET_HZ) / rf_hz * 1e6
    if len(steps) >= 2:
        a, b = steps[-2], steps[-1]
        lb.slope_db_per_db = (levels[b] - levels[a]) / (b - a)
    return lb


# --- Two receive channels (E310 RX2-A and RX2-B in one stream) ------------------------------------------------
@dataclass
class PairCapture:
    rf_hz: float
    final_gain_db: float
    on: tuple[IQ, IQ]            # (RX2-A, RX2-B), tone on, settled
    off: tuple[IQ, IQ]           # the same with the transmitter off
    probe_levels: dict[float, float] = field(default_factory=dict)   # TX gain -> tone dBFS on the wired channel
    underflows: int = 0


def tone_pair(tx: Usrp, rx: Any, rf_hz: float, final_gain_db: float = FINAL_TX_GAIN_DB) -> PairCapture:
    """Ramp a tone up on the single-channel receiver (as `loopback` does), then capture both E310 channels
    together with it on and off. `rx` is an E310 (it needs `capture_pair`)."""
    final = min(final_gain_db, tx.max_tx_gain)
    steps = [g for g in RAMP_DB if g < final]
    levels: dict[float, float] = {}
    try:
        for g in steps:
            tx.tx_tone(rf_hz, OFFSET_HZ, g, TX_SCALE, SAMPLE_RATE)
            levels[g] = report(rx.capture(rf_hz, SAMPLE_RATE, CAPTURE_SAMPLES, gain_db=RX_GAIN_DB))["tone_dbfs"]
            if levels[g] > MAX_PROBE_DBFS:
                tx.tx_off()
                raise TooStrong(f"at TX gain {g:g} dB {tx.id} to {rx.id} reads {levels[g]:.1f} dBFS; "
                                f"check the cable and attenuators from {tx.tx_port} to {rx.rx_port}")
        tx.tx_tone(rf_hz, OFFSET_HZ, final, TX_SCALE, SAMPLE_RATE)
        a, b = rx.capture_pair(rf_hz, SAMPLE_RATE, CAPTURE_SAMPLES, gain_db=RX_GAIN_DB)
    finally:
        underflows = tx.tx_off()["underflows"]
    off = rx.capture_pair(rf_hz, SAMPLE_RATE, CAPTURE_SAMPLES, gain_db=RX_GAIN_DB)
    return PairCapture(rf_hz, final, (settled(a), settled(b)), (settled(off[0]), settled(off[1])), levels,
                       underflows=underflows)


def level_at_dbfs(iq: IQ, freq_hz: float, max_points: int = ANALYSIS_POINTS) -> float:
    """Power of whatever sits at baseband `freq_hz` (a Hann-windowed single-frequency DFT), in dBFS. For a pure
    tone at that frequency it equals `tone_report`'s tone_dbfs; elsewhere it reads the noise in one bin."""
    x = iq.samples(min(iq.n, max_points))
    n = len(x)
    w = [0.5 - 0.5 * math.cos(2 * math.pi * k / n) for k in range(n)]
    step = cmath.exp(-2j * math.pi * freq_hz / iq.sample_rate)
    acc, rot = 0j, 1 + 0j
    for s, v in zip(x, w):
        acc += s * v * rot
        rot *= step
    p = abs(acc) ** 2 / sum(w) ** 2
    return 10 * math.log10(max(p, 1e-30))


def cross(a: IQ, b: IQ) -> dict[str, float]:
    """`r = mean(b conj(a))` over both whole recordings: the phase of channel 2 (b) relative to channel 1 (a),
    in degrees, the coherence |r| / sqrt(pa pb), and each channel's mean power in dBFS. The same sum as
    oasa-radio `waveform df` (which the suite runs too, to check one against the other)."""
    xa, xb = a.samples(), b.samples()
    n = min(len(xa), len(xb))
    r = sum(xb[k] * xa[k].conjugate() for k in range(n))
    pa = sum(abs(v) ** 2 for v in xa[:n])
    pb = sum(abs(v) ** 2 for v in xb[:n])
    return {"phase_deg": math.degrees(math.atan2(r.imag, r.real)),
            "coherence": abs(r) / math.sqrt(pa * pb) if pa > 0 and pb > 0 else 0.0,
            "power_a_dbfs": 10 * math.log10(max(pa / n, 1e-30)), "power_b_dbfs": 10 * math.log10(max(pb / n, 1e-30)),
            "samples": n}
