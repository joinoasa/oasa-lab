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

Light error correction (oasa-radio docs/fec-light.md, "Bench check plan"): frame v1 with `--code light`, decoded
without the length as a link does. Eb counts information bits (Es / (bits per symbol x code rate)), and a point
records several captures (`run_light_point`) so that packet loss of 1 to 2 % can be resolved. Frames are counted
per slot: the block's layout and the frames found give where each sent frame sits in a recording, so frames cut
by the recording's edges are not counted as lost.
"""

from __future__ import annotations

import cmath
import json
import math
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor
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
CODE_RATE = {"none": 1.0, "light": 0.5}   # information bits per coded bit (oasa-radio docs/fec-light.md)
LIGHT_CAPTURES = 4          # a light point records this many captures of PERIODS repeats (16 repeats, >= 384 frames)
SLOT_TOL = 64               # samples: a delivered frame belongs to the slot whose start is this close


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
    frame: str = "w0"
    code: str = "none"
    spans: list[tuple[int, int]] = field(default_factory=list)   # (start, end) of each frame in `samples`


def make_block(radio: Path, mod: str, work: Path, seed: int, frame: str = "w0", code: str = "none") -> Block:
    """24 frames with random payloads from `seed`. `frame` is the oasa-radio frame format: "w0" (the
    original, unprotected length byte) or "v1" (header with a code and a CRC-8, oasa-radio docs/frame-v1.md).
    `code` "light" codes the v1 body (`--frame v1 --code light`, oasa-radio docs/fec-light.md)."""
    if code not in CODE_RATE:
        raise ValueError(f"unknown code {code!r}")
    if code != "none" and frame != "v1":
        raise ValueError("a payload code needs frame v1")
    rng = random.Random(seed)
    samples: list[complex] = [0j] * LEAD
    payloads = []
    meta: dict[str, Any] = {}
    spans: list[tuple[int, int]] = []
    for i in range(N_FRAMES):
        p = bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN))
        base = work / (f"tx-{frame}-{mod}-{i}" if code == "none" else f"tx-{frame}-{code}-{mod}-{i}")
        subprocess.run([str(radio), "waveform", "iq", "--mod", mod, "--out", str(base), "--payload-hex", p.hex(),
                        "--symbol-rate", str(SYMBOL_RATE), "--sps", str(SPS), "--delay", "0", "--tail", "0",
                        *(["--frame", frame] if frame != "w0" else []),
                        *(["--code", code] if code != "none" else [])],
                       check=True, capture_output=True, timeout=60)
        x = IQ((base.with_suffix(".sigmf-data")).read_bytes(), "cf32_le", SAMPLE_RATE, 0).samples()
        if not meta:
            meta = json.loads(base.with_suffix(".sigmf-meta").read_text())
        payloads.append(p)
        spans.append((len(samples), len(samples) + len(x)))
        samples += x + [0j] * GAP
    w = 2 * math.pi * IF_HZ / SAMPLE_RATE
    samples = [v * cmath.exp(1j * w * k) for k, v in enumerate(samples)]
    scale = PEAK / max(abs(v) for v in samples)
    return Block(mod, [v * scale for v in samples], payloads, meta, frame, code, spans)


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
    # light code (run_light_point): frames with a good CRC-16 and a payload that was sent, in the counted slots
    delivered: int = 0
    delivered_sync5: int = 0                       # the same recordings decoded with --sync-max-errors 5
    report_sync5: dict[str, Any] = field(default_factory=dict)
    captures: int = 0
    # Eb/N0 accumulators (accumulate_point / finish_ebn0)
    sig_p: list[float] = field(default_factory=list, repr=False)
    gap_p: list[float] = field(default_factory=list, repr=False)
    noise_acc: list[float] = field(default_factory=list, repr=False)
    noise_n: int = 0

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


def decode_report(radio: Path, base: Path, mod: str, frame: str = "w0", known_len: bool = True,
                  code: str | None = None, sync_max_errors: int | None = None
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Frames found, and the decoder's count of what happened to each candidate (decode_done; empty if the
    binary does not report it). With `known_len` the payload length is given (--payload-len) and the header
    is not needed; without it the receiver must read the length from the header, as a real link does.
    `code` (--code) is passed only with the length: a v1 receiver otherwise reads the code from each header.
    `sync_max_errors` sets --sync-max-errors (the decoder's default is 3 wrong bits of 16)."""
    extra = (["--payload-len", str(PAYLOAD_LEN)] if known_len else []) + (["--frame", frame] if frame != "w0" else [])
    if known_len and code is not None:
        extra += ["--code", code]
    if sync_max_errors is not None:
        extra += ["--sync-max-errors", str(sync_max_errors)]
    r = subprocess.run([str(radio), "waveform", "decode", "--in", str(base), "--mod", mod, "--sps", str(SPS),
                        "--cfo-search", str(CFO_SEARCH), *extra, "--hex"],
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
    accumulate_point(pt, block, x, frames)
    finish_ebn0(pt, block)


def accumulate_point(pt: Point, block: Block, x: list[complex], frames: list[dict[str, Any]]) -> None:
    """Bit errors, and the power and noise spectrum around each found frame, from one recording. Call once
    per recording of a point, then finish_ebn0."""
    win = [0.5 - 0.5 * math.cos(2 * math.pi * k / NOISE_FFT) for k in range(NOISE_FFT)]
    if not pt.noise_acc:
        pt.noise_acc = [0.0] * NOISE_FFT
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
            pt.sig_p.append(sum(abs(v) ** 2 for v in seg) / len(seg))
            pt.gap_p.append(sum(abs(v) ** 2 for v in gap) / len(gap))
            for k, c in enumerate(fft([a * b for a, b in zip(gap[:NOISE_FFT], win)])):
                pt.noise_acc[k] += abs(c) ** 2
            pt.noise_n += 1


def finish_ebn0(pt: Point, block: Block) -> None:
    """Eb/N0 per information bit: Es/N0 / (bits per symbol x code rate). The code rate is 1 uncoded, so W0 and
    uncoded v1 read as before; the light code's rate 1/2 puts Eb 3 dB above Es per coded BPSK symbol."""
    if not pt.noise_n:
        return
    # N0 per sample: the gaps' averaged noise spectrum inside the signal band, away from the DC spike (which
    # sits at -IF after the shift back). For white noise each Hann-windowed bin averages to the per-sample
    # variance. The spectrum is already averaged, so its mean is used (no median correction).
    win = [0.5 - 0.5 * math.cos(2 * math.pi * k / NOISE_FFT) for k in range(NOISE_FFT)]
    sw2 = sum(v * v for v in win)
    freqs = [(k if k < NOISE_FFT / 2 else k - NOISE_FFT) * SAMPLE_RATE / NOISE_FFT for k in range(NOISE_FFT)]
    clean = [pt.noise_acc[k] / pt.noise_n / sw2 for k, f in enumerate(freqs)
             if abs(f) < 0.5 * SYMBOL_RATE and abs(f + IF_HZ) > 20e3]
    n0 = sum(clean) / len(clean)
    sig_p, gap_p = pt.sig_p, pt.gap_p
    ps = sorted(sig_p)[len(sig_p) // 2] - sorted(gap_p)[len(gap_p) // 2]   # spurs are in both and cancel
    if n0 > 0 and ps > 0:
        pt.ebn0_db = 10 * math.log10(ps * SPS / n0 / (BITS_PER_SYMBOL[block.mod] * CODE_RATE[block.code]))


def run_point(radio: Path, tx: Usrp, rx: Usrp, block: Block, rf_hz: float, gain_db: float, work: Path,
              direction: str, known_len: bool = True) -> Point:
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
    base = work / f"rx-{block.frame}-{direction}-{block.mod}-{gain_db:g}".replace(".", "p")   # no dot: with_suffix would eat it
    write_sigmf(block.meta, x, base, f"W1 {direction} {block.mod} TX gain {gain_db:g} dB, shifted back from IF")
    frames, pt.report = decode_report(radio, base, block.mod, block.frame, known_len)
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


# --- light error correction (oasa-radio docs/fec-light.md) ------------------------------------------------

def supports_code(radio: Path) -> bool:
    """True if this oasa-radio has the light code (`--code` on waveform iq, decode and per)."""
    try:
        r = subprocess.run([str(radio), "waveform", "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--code" in (r.stdout + r.stderr)


def count_slots(block: Block, frames: list[dict[str, Any]], n: int) -> tuple[int, int]:
    """(slots, delivered) in one recording of `n` samples of `block` on repeat. A slot is a sent frame that
    lies wholly inside the recording; delivered = slots with a frame that has a good CRC-16 and that slot's
    payload. Where the block starts in the recording comes from the delivered frames themselves (the median of
    their offsets from the block layout, so one stray frame cannot move it). With none delivered the phase is
    unknown and the slots are the complete periods' frames."""
    L = len(block.samples)
    index = {p: i for i, p in enumerate(block.payloads)}
    good: list[tuple[int, int]] = []
    for f in frames:
        hx = f.get("payload_hex")
        if f.get("crc_ok") and hx:
            i = index.get(bytes.fromhex(hx))
            if i is not None:
                good.append((int(f["start"]), i))
    if not good or len(block.spans) != len(block.payloads):
        return N_FRAMES * (n // L), 0
    offs = [(s - block.spans[i][0]) % L for s, i in good]
    ref = offs[0]
    rel = sorted(((o - ref + L // 2) % L) - L // 2 for o in offs)   # wrapped around the first: no seam
    phase = (ref + rel[len(rel) // 2]) % L
    starts: dict[int, list[int]] = {}
    for s, i in good:
        starts.setdefault(i, []).append(s)
    slots = delivered = 0
    for r in range(-1, n // L + 2):
        for i, (a, b) in enumerate(block.spans):
            start, end = phase + r * L + a, phase + r * L + b
            if start < 0 or end > n:
                continue
            slots += 1
            delivered += any(abs(s - start) <= SLOT_TOL for s in starts.get(i, ()))
    return slots, delivered


def run_light_point(radio: Path, tx: Usrp, rx: Usrp, block: Block, rf_hz: float, gain_db: float, work: Path,
                    direction: str, captures: int = LIGHT_CAPTURES) -> Point:
    """One point of light-coded frames: `captures` recordings of PERIODS repeats plus an eighth (so each holds at
    least 96 whole frames), all taken while the block is sent, then decoded without the length (default sync
    check, and again with --sync-max-errors 5). Eb/N0 counts information bits."""
    pt = Point(direction, block.mod, gain_db, rx_id=rx.id)
    L = len(block.samples)
    n = PERIODS * L + L // 8
    recs: list[IQ] = []
    try:
        tx.tx_file(block.samples, rf_hz, gain_db, SAMPLE_RATE)
        for _ in range(captures):
            iq = rx.capture(rf_hz, SAMPLE_RATE, n, gain_db=RX_GAIN_DB)
            peak = max(abs(v) for v in iq.samples())
            pt.peak = max(pt.peak, peak)
            if peak > MAX_PROBE_PEAK:
                raise TooStrong(f"{direction} at TX gain {gain_db:g} dB peaks at {peak:.2f} of full scale; "
                                f"check the cable and attenuators from {tx.tx_port} to {rx.rx_port}")
            recs.append(iq)
    finally:
        tx.tx_off()
    pt.recording = recs[0]
    for c, iq in enumerate(recs):
        x = baseband(iq)
        base = work / f"rx-light-{direction}-{block.mod}-{gain_db:g}-{c}".replace(".", "p")
        write_sigmf(block.meta, x, base, f"W1 light {direction} {block.mod} TX gain {gain_db:g} dB, capture {c}")
        with ThreadPoolExecutor(2) as pool:      # two processes: the two decodes run side by side
            plain = pool.submit(decode_report, radio, base, block.mod, "v1", known_len=False)
            loose = pool.submit(decode_report, radio, base, block.mod, "v1", known_len=False, sync_max_errors=5)
            (frames, report), (frames5, report5) = plain.result(), loose.result()
        accumulate_point(pt, block, x, frames)
        pt.frames += len(frames)
        pt.decoded += frames
        slots, got = count_slots(block, frames, len(x))
        pt.frames_sent += slots
        pt.delivered += got
        pt.delivered_sync5 += count_slots(block, frames5, len(x))[1]
        for k, v in report.items():
            pt.report[k] = pt.report.get(k, 0) + v
        for k, v in report5.items():
            pt.report_sync5[k] = pt.report_sync5.get(k, 0) + v
        pt.captures += 1
    finish_ebn0(pt, block)
    return pt


def start_per_reference(radio: Path, mod: str, ebn0s: list[float], frames: int = 400,
                        cfo_random: float = 0.018) -> subprocess.Popen:
    """Start the simulated packet loss of light-coded v1 frames: `oasa-radio waveform per`, the receiver as on the
    air (no known length), with the bench's search, a random offset per frame and a random sub-sample delay. It
    takes minutes and one CPU, so it runs while the bench measures; read it with per_reference_result."""
    return subprocess.Popen([str(radio), "waveform", "per", "--mod", mod, "--frame", "v1", "--code", "light",
                             "--ebn0-db", ",".join(f"{e:g}" for e in ebn0s), "--frames", str(frames),
                             "--sps", str(SPS), "--payload-len", str(PAYLOAD_LEN), "--cfo-search", str(CFO_SEARCH),
                             "--cfo-random", str(cfo_random), "--random-frac-delay"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def per_reference_result(proc: subprocess.Popen, timeout: float = 7200) -> list[tuple[float, float]]:
    """(Eb/N0 dB, loss) per point of a started reference."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"oasa-radio per failed: {(err or out)[-300:]}")
    lines = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    return [(float(d["ebn0_db"]), float(d["packet_loss"])) for d in lines if d.get("event") == "per"]


def per_reference(radio: Path, mod: str, ebn0s: list[float], frames: int = 400,
                  cfo_random: float = 0.018) -> list[tuple[float, float]]:
    return per_reference_result(start_per_reference(radio, mod, ebn0s, frames, cfo_random))


def ebn0_for_loss(curve: list[tuple[float, float]], loss: float) -> float | None:
    """The Eb/N0 (dB) at which the simulated curve gives this loss: the first crossing from the low end,
    interpolated linearly in log10(loss) (a zero point counts as 1e-3). None outside the curve."""
    pts = sorted(curve)
    floor = 1e-3
    for (e1, l1), (e2, l2) in zip(pts, pts[1:]):
        if l1 >= loss >= l2 and l1 > l2:
            a, b, t = math.log10(max(l1, floor)), math.log10(max(l2, floor)), math.log10(max(loss, floor))
            return e1 + (e2 - e1) * (a - t) / (a - b)
    return None
