"""Units of oasa_lab/w1test.py that need no radio and no oasa-radio: the Eb/N0 measurement and its code rate,
slot counting for packet loss, and reading the simulated loss curve."""

from __future__ import annotations

import cmath
import math
import random

from oasa_lab.w1test import (GAP, IF_HZ, PAYLOAD_LEN, SAMPLE_RATE, SPS, Block, Point, count_slots, ebn0_for_loss,
                             measure_point)

FRAME = 1600


def _recording(code: str, es_n0_db: float, seed: int = 5) -> tuple[Block, list[complex], list[dict]]:
    """Frames of random unit-power symbols (held SPS samples) at Es/N0 per symbol, gaps of GAP, white noise of
    variance 1 per sample everywhere, plus a DC spike at -IF (where the receiver's LO leak lands after the shift)."""
    rng = random.Random(seed)
    amp = math.sqrt(10 ** (es_n0_db / 10) / SPS)        # Es = power x SPS, N0 = 1
    x: list[complex] = [0j] * 1000
    frames, payloads = [], []
    for i in range(40):
        p = bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN))
        payloads.append(p)
        start = len(x)
        for _ in range(FRAME // SPS):
            x += [amp * cmath.exp(1j * rng.uniform(0, 2 * math.pi))] * SPS
        frames.append({"start": start, "end": len(x), "payload_hex": p.hex(), "crc_ok": True})
        x += [0j] * GAP
    w = -2 * math.pi * IF_HZ / SAMPLE_RATE
    s = 1 / math.sqrt(2)
    x = [v + complex(rng.gauss(0, s), rng.gauss(0, s)) + 0.3 * cmath.exp(1j * w * k) for k, v in enumerate(x)]
    # one repeat of a block: these frames are its layout (Block.spans), and the recording is that repeat
    return Block("bpsk", [0j] * len(x), payloads, {}, "v1", code, [(f["start"], f["end"]) for f in frames]), x, frames


def test_ebn0_counts_information_bits():
    """Uncoded, Eb/N0 = Es/N0 (BPSK); light (rate 1/2), 3 dB more from the same recording."""
    es = 4.0
    block, x, frames = _recording("none", es)
    plain = Point("t", "bpsk", 0.0)
    measure_point(plain, block, x, frames)
    block.code = "light"
    coded = Point("t", "bpsk", 0.0)
    measure_point(coded, block, x, frames)
    assert plain.ebn0_db is not None and coded.ebn0_db is not None
    assert abs(plain.ebn0_db - es) < 0.6, plain.ebn0_db      # 40 short frames: about +-0.4 dB spread over seeds
    assert abs(coded.ebn0_db - plain.ebn0_db - 10 * math.log10(2)) < 1e-9
    assert plain.bits == 40 * 8 * PAYLOAD_LEN and plain.errors == 0


def _block(n_frames: int = 24, frame_len: int = 5000) -> Block:
    rng = random.Random(9)
    payloads = [bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN)) for _ in range(n_frames)]
    spans, pos = [], 2000
    for _ in range(n_frames):
        spans.append((pos, pos + frame_len))
        pos += frame_len + GAP
    return Block("bpsk", [0j] * pos, payloads, {}, "v1", "light", spans)


def _frames_at(block: Block, phase: int, n: int, skip: set[int] = frozenset(), jitter: int = 3) -> list[dict]:
    """Every frame wholly inside a recording of n samples that starts `phase` samples into the block."""
    L, out = len(block.samples), []
    for r in range(-1, n // L + 2):
        for i, (a, b) in enumerate(block.spans):
            s = a - phase + r * L
            if s >= 0 and s + (b - a) <= n and (r, i) not in skip:
                out.append({"start": s + (jitter if i % 2 else -jitter), "end": s + b - a,
                            "payload_hex": block.payloads[i].hex(), "crc_ok": True})
    return out


def test_count_slots_counts_whole_frames_only():
    block = _block()
    L = len(block.samples)
    n = 4 * L + L // 8
    for phase in (0, 1234, L // 2, L - 3000):
        frames = _frames_at(block, phase, n)
        slots, got = count_slots(block, frames, n)
        assert slots == len(frames) and got == slots, (phase, slots, got, len(frames))
        assert slots >= 96


def test_count_slots_counts_losses_and_ignores_strays():
    block = _block()
    L = len(block.samples)
    n = 4 * L + L // 8
    frames = _frames_at(block, 777, n)
    whole = len(frames)
    lost = frames[5:8]
    rest = frames[:5] + frames[8:]
    rest[0] = dict(rest[0], crc_ok=False)                                         # bad CRC: lost
    rest[1] = dict(rest[1], payload_hex=bytes(PAYLOAD_LEN).hex())                 # not a payload sent: lost
    rest.append(dict(rest[2], start=rest[2]["start"] + 3 * len(block.samples) // 7))   # a stray: no slot
    rest.append(dict(rest[3]))                                                    # the same frame twice: once
    slots, got = count_slots(block, rest, n)
    assert slots == whole
    assert got == whole - len(lost) - 2


def test_count_slots_without_frames():
    block = _block()
    L = len(block.samples)
    assert count_slots(block, [], 4 * L + L // 8) == (96, 0)


def test_ebn0_for_loss_interpolates_in_log_loss():
    curve = [(2.0, 0.4), (3.0, 0.04), (4.0, 0.0), (1.0, 0.9)]
    assert abs(ebn0_for_loss(curve, 0.4) - 2.0) < 1e-9
    assert abs(ebn0_for_loss(curve, math.sqrt(0.4 * 0.04)) - 2.5) < 1e-9
    assert 3.0 < ebn0_for_loss(curve, 0.02) < 4.0
    assert ebn0_for_loss(curve, 0.95) is None
    # noisy tail: the first crossing from the low end counts
    assert ebn0_for_loss([(3.0, 0.05), (3.5, 0.02), (4.0, 0.025), (4.5, 0.0)], 0.02) == 3.5
