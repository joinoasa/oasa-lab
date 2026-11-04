"""Measurements for the W1 suite: the oasa-radio W0 modem between two radios over cable.

Kept apart from the pytest file so the lab's own tests can run it on the simulated bench. Pure Python (no
numpy), like the rest of the lab.

How a point is measured:
1. `oasa-radio waveform iq` writes frames with known random payloads. They are joined with gaps of silence,
   shifted up by a digital IF (250 kHz) so the receiver's DC offset and LO leak fall outside the signal
   band, and scaled to a peak of 0.5 (DAC headroom). One radio sends this block on repeat.
2. The other radio records it; the recording is shifted back down and `oasa-radio waveform decode` finds
   the frames. Each found frame is compared with the payload nearest to it (random payloads differ by
   about half their bits, so this is unambiguous while a frame has well under 100 bit errors).
3. Eb/N0 is measured from the recording in the modem's own convention (oasa-radio docs/waveform-w0.md):
   Es = the energy of one symbol = signal power per sample x samples per symbol, from the power on the
   frames minus the power in the gaps; N0 = noise variance per sample, from the noise spectrum of the gaps
   inside the signal band (what the matched filter sees), away from the DC spike.

Bench findings this follows (owner's bench, 2026-11-04): at 200 kS/s nothing decoded (the E310 has no
DSP); at zero IF the BER was about 5 dB off theory; at 1 MS/s with the IF it is on theory. At RX gain 40 the
E310's noise was quantized (its raw samples took about 7 values) and BER read up to 0.65x theory at 7 dB; at 60
it is on theory. Frames are decoded with the payload length known (it is fixed here), so a header bit error does
not lose a frame: below about 4 dB most W0 headers have one.
"""

from __future__ import annotations

import cmath
import json
import math
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .devices.usrp import Usrp
from .iq import IQ, fft

SAMPLE_RATE = 1e6
SYMBOL_RATE = 125_000
SPS = 8
IF_HZ = 250_000.0
N_FRAMES = 24
PAYLOAD_LEN = 64
GAP = 400                   # silent samples after every frame
LEAD = 2000                 # silent samples at the start of the block
PEAK = 0.5                  # peak magnitude of the block sent
RX_GAIN_DB = 60.0           # at 40 the noise was about one ADC step (E310) and BER read better than theory
PERIODS = 4                 # a recording holds this many repeats of the block
CFO_SEARCH = 0.1            # cycles per symbol: +-12.5 kHz, +-13.7 ppm at 915 MHz
MAX_PROBE_PEAK = 0.3        # a recording this strong (of full scale) means the cable is wrong: stop
NOISE_FFT = 256
BITS_PER_SYMBOL = {"bpsk": 1, "qpsk": 2}


class TooStrong(Exception):
    """The receiver saw far more than a 60 dB cable should give. TX is off again when this is raised."""


def q(x: float) -> float:
    return 0.5 * math.erfc(x / math.sqrt(2))


def theory_ber(mod: str, ebn0_db: float) -> float:
    """BPSK and QPSK with Gray coding: Q(sqrt(2 Eb/N0)), computed here, not taken from the modem."""
    return q(math.sqrt(2 * 10 ** (ebn0_db / 10)))


def ebn0_for_ber(mod: str, ber: float) -> float:
    """The Eb/N0 (dB) at which theory gives this BER (bisection; theory falls with Eb/N0)."""
    lo, hi = -10.0, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if theory_ber(mod, mid) > ber:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass
class Block:
    mod: str
    samples: list[complex]
    payloads: list[bytes]
    meta: dict[str, Any]


def make_block(radio: Path, mod: str, work: Path, seed: int) -> Block:
    rng = random.Random(seed)
    samples: list[complex] = [0j] * LEAD
    payloads = []
    meta: dict[str, Any] = {}
    for i in range(N_FRAMES):
        p = bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN))
        base = work / f"tx-{mod}-{i}"
        subprocess.run([str(radio), "waveform", "iq", "--mod", mod, "--out", str(base), "--payload-hex", p.hex(),
                        "--symbol-rate", str(SYMBOL_RATE), "--sps", str(SPS), "--delay", "0", "--tail", "0"],
                       check=True, capture_output=True, timeout=60)
        x = IQ((base.with_suffix(".sigmf-data")).read_bytes(), "cf32_le", SAMPLE_RATE, 0).samples()
        if not meta:
            meta = json.loads(base.with_suffix(".sigmf-meta").read_text())
        payloads.append(p)
        samples += x + [0j] * GAP
    w = 2 * math.pi * IF_HZ / SAMPLE_RATE
    samples = [v * cmath.exp(1j * w * k) for k, v in enumerate(samples)]
    scale = PEAK / max(abs(v) for v in samples)
    return Block(mod, [v * scale for v in samples], payloads, meta)


@dataclass
class Point:
    direction: str
    mod: str
    tx_gain_db: float
    frames: int = 0
    crc_ok: int = 0
    bits: int = 0
    errors: int = 0
    ebn0_db: float | None = None
    peak: float = 0.0
    frames_sent: int = 0
    rx_id: str = ""
    report: dict[str, Any] = field(default_factory=dict)
    recording: IQ | None = None
    decoded: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ber(self) -> float | None:
        return self.errors / self.bits if self.bits else None


def write_sigmf(meta: dict[str, Any], samples: list[complex], base: Path, description: str) -> None:
    from array import array
    m = json.loads(json.dumps(meta))
    m["global"].update({"oasa:simulated": False, "core:description": description})
    m["annotations"] = []
    base.with_suffix(".sigmf-data").write_bytes(array("f", (c for v in samples for c in (v.real, v.imag))).tobytes())
    base.with_suffix(".sigmf-meta").write_text(json.dumps(m))


REPORT_KEYS = ("preambles", "sync_rejected", "header_rejected", "too_short", "inside_frame")


def decode_report(radio: Path, base: Path, mod: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Frames found, and the decoder's count of what happened to each candidate (decode_done; empty if the
    binary does not report it). The payload length is given (--payload-len), not read from the header."""
    r = subprocess.run([str(radio), "waveform", "decode", "--in", str(base), "--mod", mod, "--sps", str(SPS),
                        "--cfo-search", str(CFO_SEARCH), "--payload-len", str(PAYLOAD_LEN), "--hex"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"oasa-radio decode failed: {(r.stderr or r.stdout)[-300:]}")
    lines = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    done = next((d for d in lines if d.get("event") == "decode_done"), {})
    return [d for d in lines if d.get("event") == "frame"], {k: done[k] for k in REPORT_KEYS if k in done}


def decode(radio: Path, base: Path, mod: str) -> list[dict[str, Any]]:
    return decode_report(radio, base, mod)[0]


def baseband(iq: IQ) -> list[complex]:
    w = -2 * math.pi * IF_HZ / iq.sample_rate
    return [v * cmath.exp(1j * w * k) for k, v in enumerate(iq.samples())]


def measure_point(pt: Point, block: Block, x: list[complex], frames: list[dict[str, Any]]) -> None:
    pt.decoded, pt.frames = frames, len(frames)
    sig_p, gap_p, gaps = [], [], []
    for f in frames:
        got = bytes.fromhex(f["payload_hex"]) if f.get("payload_hex") else b""
        if len(got) != PAYLOAD_LEN:
            continue
        pt.errors += min(sum(bin(a ^ b).count("1") for a, b in zip(got, w)) for w in block.payloads)
        pt.bits += 8 * PAYLOAD_LEN
        pt.crc_ok += bool(f.get("crc_ok"))
        seg = x[f["start"]:f["end"]]
        gap = x[f["end"] + 50:f["end"] + GAP - 50]
        if len(seg) > 100 and len(gap) >= NOISE_FFT:
            sig_p.append(sum(abs(v) ** 2 for v in seg) / len(seg))
            gap_p.append(sum(abs(v) ** 2 for v in gap) / len(gap))
            gaps.append(gap[:NOISE_FFT])
    if not gaps:
        return
    # N0 per sample: the gaps' averaged noise spectrum inside the signal band, away from the DC spike (which
    # sits at -IF after the shift back). For white noise each Hann-windowed bin averages to the per-sample
    # variance. The spectrum is already averaged, so its mean is used (no median correction).
    win = [0.5 - 0.5 * math.cos(2 * math.pi * k / NOISE_FFT) for k in range(NOISE_FFT)]
    sw2 = sum(v * v for v in win)
    acc = [0.0] * NOISE_FFT
    for g in gaps:
        for k, c in enumerate(fft([a * b for a, b in zip(g, win)])):
            acc[k] += abs(c) ** 2
    freqs = [(k if k < NOISE_FFT / 2 else k - NOISE_FFT) * SAMPLE_RATE / NOISE_FFT for k in range(NOISE_FFT)]
    clean = [acc[k] / len(gaps) / sw2 for k, f in enumerate(freqs)
             if abs(f) < 0.5 * SYMBOL_RATE and abs(f + IF_HZ) > 20e3]
    n0 = sum(clean) / len(clean)
    ps = sorted(sig_p)[len(sig_p) // 2] - sorted(gap_p)[len(gap_p) // 2]   # spurs are in both and cancel
    if n0 > 0 and ps > 0:
        pt.ebn0_db = 10 * math.log10(ps * SPS / n0 / BITS_PER_SYMBOL[block.mod])


def run_point(radio: Path, tx: Usrp, rx: Usrp, block: Block, rf_hz: float, gain_db: float, work: Path,
              direction: str) -> Point:
    pt = Point(direction, block.mod, gain_db, rx_id=rx.id)
    n = PERIODS * len(block.samples)
    try:
        tx.tx_file(block.samples, rf_hz, gain_db, SAMPLE_RATE)
        iq = rx.capture(rf_hz, SAMPLE_RATE, n, gain_db=RX_GAIN_DB)
    finally:
        tx.tx_off()
    raw = iq.samples()
    pt.peak = max(abs(v) for v in raw)
    if pt.peak > MAX_PROBE_PEAK:
        raise TooStrong(f"{direction} at TX gain {gain_db:g} dB peaks at {pt.peak:.2f} of full scale; "
                        f"check the cable and attenuators from {tx.tx_port} to {rx.rx_port}")
    pt.recording = iq
    pt.frames_sent = round(n / len(block.samples) * N_FRAMES)
    x = baseband(iq)
    base = work / f"rx-{direction}-{block.mod}-{gain_db:g}".replace(".", "p")   # no dot: with_suffix would eat it
    write_sigmf(block.meta, x, base, f"W1 {direction} {block.mod} TX gain {gain_db:g} dB, shifted back from IF")
    frames, pt.report = decode_report(radio, base, block.mod)
    measure_point(pt, block, x, frames)
    return pt


def probe_gain(radio: Path, tx: Usrp, rx: Usrp, block: Block, rf_hz: float, work: Path, direction: str,
               start_db: float = 20.0, step_db: float = 6.0, want_db: float = 6.0) -> tuple[float, Point]:
    """Raise the TX gain from a low start until the frames are clearly there; return (gain, point).
    The level follows the gain 1 dB per dB (l2-usrp-loopback), so other points are set from this one."""
    g = min(start_db, tx.max_tx_gain)
    while True:
        pt = run_point(radio, tx, rx, block, rf_hz, g, work, direction)
        if (pt.ebn0_db is not None and pt.ebn0_db >= want_db) or g >= tx.max_tx_gain:
            return g, pt
        g = min(g + step_db, tx.max_tx_gain)
