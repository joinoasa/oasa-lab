"""W0 points counted by position (w1test.place_frames, accumulate_slots), on synthetic recordings. No radio and no
oasa-radio needed: the decoder's output is written out as it comes on the bench.

A recording starts anywhere in the block, so the frame it cuts has its data inside the recording but not its
preamble. On the bench the decoder sometimes takes some of that data for a preamble and, with the length known,
reads 64 bytes of random data (about 235 bit errors). That frame's span covers the next real preamble, so the next
frame is not found, and "its" gap lies inside that next frame. These tests build that case, check that it moved the
old count by more than the 1 dB limit and that counting by position does not, and that position is the only test."""

from __future__ import annotations

import cmath
import math
import random

from oasa_lab.w1test import (GAP, IF_HZ, LEAD, N_FRAMES, PAYLOAD_LEN, SAMPLE_RATE, SLOT_TOL, SPS, Block, Point,
                             accumulate_point, ebn0_for_ber, finish_ebn0, measure_point, place_frames, theory_ber)

FRAME = 1600                # samples a frame (the bench's are 5105; shorter keeps the test fast)
EBN0_DB = 7.0               # the top point of a sweep, where one false frame did the most harm on the bench
BITS = 8 * PAYLOAD_LEN


def _block(seed: int = 3) -> Block:
    rng = random.Random(seed)
    payloads = [bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN)) for _ in range(N_FRAMES)]
    spans, pos = [], LEAD
    for _ in range(N_FRAMES):
        spans.append((pos, pos + FRAME))
        pos += FRAME + GAP
    return Block("bpsk", [0j] * pos, payloads, {}, "w0", "none", spans)


def _recording(block: Block, start: int, periods: int = 4, seed: int = 1) -> list[complex]:
    """`block` on repeat from `start` samples into it, `periods` repeats long, shifted back down from the IF: random
    BPSK symbols on the frames at Es/N0 = EBN0_DB, white noise of variance 1, and the receiver's DC spike at -IF."""
    rng = random.Random(seed)
    L = len(block.samples)
    amp = math.sqrt(10 ** (EBN0_DB / 10) / SPS)
    one = [0j] * L
    for a, b in block.spans:
        for k in range(a, b, SPS):
            one[k:k + SPS] = [amp * (1 if rng.random() < 0.5 else -1)] * SPS
    w = -2 * math.pi * IF_HZ / SAMPLE_RATE
    s = 1 / math.sqrt(2)
    return [one[(start + k) % L] + complex(rng.gauss(0, s), rng.gauss(0, s)) + 0.05 * cmath.exp(1j * w * k)
            for k in range(periods * L)]


def _flip(p: bytes, ber: float, rng: random.Random) -> bytes:
    return bytes(v ^ sum(1 << j for j in range(8) if rng.random() < ber) for v in p)


def _found(block: Block, start: int, n: int, seed: int = 2, jitter: int = 3) -> list[dict]:
    """What the decoder gives for every frame wholly inside the recording: its start (a few samples off, as
    the bench's frame starts are), its end and its payload with bit errors at theory's rate for EBN0_DB."""
    rng = random.Random(seed)
    L, out = len(block.samples), []
    for r in range(-1, n // L + 2):
        for i, (a, b) in enumerate(block.spans):
            s = a - start + r * L
            if s >= 0 and s + FRAME <= n:
                d = rng.randint(-jitter, jitter)
                got = _flip(block.payloads[i], theory_ber("bpsk", EBN0_DB), rng)
                out.append({"start": s + d, "end": s + d + FRAME, "payload_hex": got.hex(), "crc_ok": got == block.payloads[i]})
    return sorted(out, key=lambda f: f["start"])


def _old(block: Block, x: list[complex], frames: list[dict]) -> Point:
    """The count before: every frame found, against the nearest payload, noise after each frame's own end."""
    pt = Point("t", "bpsk", 0.0)
    pt.frames = len(frames)
    accumulate_point(pt, block, x, frames)
    finish_ebn0(pt, block)
    return pt


def _new(block: Block, x: list[complex], frames: list[dict]) -> Point:
    pt = Point("t", "bpsk", 0.0)
    measure_point(pt, block, x, frames)
    return pt


def _loss(pt: Point) -> float:
    assert pt.ebn0_db is not None and pt.errors >= 20, (pt.ebn0_db, pt.errors)
    return pt.ebn0_db - ebn0_for_ber("bpsk", pt.ber)


def _cut(block: Block, into: int = 500):
    """A recording that starts `into` samples into frame 3's data (its preamble is before the recording), the
    frames the decoder finds, and a false frame: random data read from frame 3's data, whose span covers frame 4's
    start, so frame 4 is not found."""
    a3 = block.spans[3][0]
    start = a3 + into
    x = _recording(block, start)
    frames = _found(block, start, len(x))
    nxt = block.spans[4][0] - start                    # frame 4's start in the recording
    assert frames[0]["start"] in range(nxt - SLOT_TOL, nxt + SLOT_TOL)
    false_start = nxt - 600                            # inside frame 3's data, as the bench's were
    junk = bytes(random.Random(7).randrange(256) for _ in range(PAYLOAD_LEN))
    false = {"start": false_start, "end": false_start + FRAME, "payload_hex": junk.hex(), "crc_ok": False}
    return x, frames, [false] + frames[1:], false


def test_a_false_frame_moved_the_old_count_past_the_limit_and_not_the_new():
    block = _block()
    x, clean, cut, false = _cut(block)
    errs = min(sum(bin(a ^ b).count("1") for a, b in zip(bytes.fromhex(false["payload_hex"]), p)) for p in block.payloads)
    assert 200 < errs < 260                            # random data: about half its bits off the nearest payload

    ref = _old(block, x, clean)                        # the same recording without the false frame, counted as before
    old = _old(block, x, cut)
    new = _new(block, x, cut)
    assert abs(_loss(ref)) < 0.5, _loss(ref)
    # before: the false frame's bits counted, and its "gap" inside frame 4 lowered Eb/N0
    assert old.errors >= ref.errors + 200
    assert old.ebn0_db < ref.ebn0_db - 0.1, (old.ebn0_db, ref.ebn0_db)
    assert _loss(old) > 1.0, _loss(old)
    # by position: the false frame is off the layout, frame 4 is missing, and the point is where it should be
    assert new.off_layout == 1
    assert new.bits == ref.bits - BITS and new.errors <= ref.errors
    assert abs(_loss(new)) < 0.5, _loss(new)
    assert abs(new.ebn0_db - ref.ebn0_db) < 0.05, (new.ebn0_db, ref.ebn0_db)


def test_position_is_the_only_test():
    """A frame at its place counts however many errors it has, against that place's own payload; a frame off the
    layout does not count however clean it is. Changing the errors never changes which frames count."""
    block = _block(4)
    L = len(block.samples)
    start = 777
    x = _recording(block, start, seed=5)
    frames = _found(block, start, len(x), seed=6)
    base = _new(block, x, frames)
    assert base.off_layout == 0 and base.bits == BITS * len(frames)

    def at(f: dict) -> int:
        return (f["start"] + start) % L

    slot = {a: i for i, (a, _) in enumerate(block.spans)}
    idx = {k: min(slot, key=lambda a: abs(a - at(f))) for k, f in enumerate(frames)}

    # a real frame at its place, 240 bits wrong (as bad as random data): counted, all 240 errors with it
    bad = list(frames)
    k = 10
    i = slot[idx[k]]
    mask = bytearray(PAYLOAD_LEN)
    for j in random.Random(8).sample(range(BITS), 240):
        mask[j // 8] |= 1 << (j % 8)
    bad[k] = dict(frames[k], payload_hex=bytes(p ^ m for p, m in zip(block.payloads[i], mask)).hex(), crc_ok=False)
    pt = _new(block, x, bad)
    own = sum(bin(a ^ b).count("1") for a, b in zip(bytes.fromhex(frames[k]["payload_hex"]), block.payloads[i]))
    assert pt.off_layout == 0 and pt.bits == base.bits
    assert pt.errors == base.errors - own + 240

    # a frame at its place carrying another frame's payload, intact: compared with its own place's payload
    swapped = list(frames)
    j = slot[idx[k + 1]]
    swapped[k] = dict(frames[k], payload_hex=block.payloads[j].hex(), crc_ok=True)
    pt = _new(block, x, swapped)
    cross = sum(bin(a ^ b).count("1") for a, b in zip(block.payloads[j], block.payloads[i]))
    assert pt.errors == base.errors - own + cross and cross > 200

    # an intact copy of a sent frame, found where no frame was sent (inside frame k's data, and in a gap): not counted
    for where in (frames[k]["start"] + 700, frames[k]["end"] + GAP // 2):
        extra = sorted(frames + [dict(frames[k], start=where, end=where + FRAME)], key=lambda f: f["start"])
        pt = _new(block, x, extra)
        assert pt.off_layout == 1 and pt.bits == base.bits and pt.errors == base.errors

    # the tolerance: a start SLOT_TOL samples from its place counts, one more does not
    for d, off in ((SLOT_TOL, 0), (-SLOT_TOL, 0), (SLOT_TOL + 1, 1), (-SLOT_TOL - 1, 1)):
        moved = list(frames)
        a = frames[k]["start"] - (at(frames[k]) - idx[k])                    # frame k's place in the recording
        moved[k] = dict(frames[k], start=a + d, end=a + d + FRAME)
        assert place_frames(block, moved, len(x))[1] == ([moved[k]] if off else []), d

    # errors do not move the count: the same frames with every payload replaced by noise except a few to place
    # the block, and the same frames intact, count the same frames
    rng = random.Random(9)
    noisy = [f if n % 10 == 0 else dict(f, payload_hex=bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN)).hex())
             for n, f in enumerate(frames)]
    starts = lambda fr: [f["start"] for f, _ in place_frames(block, fr, len(x))[0]]          # noqa: E731
    assert starts(noisy) == starts(frames) and len(starts(frames)) == len(frames)


def test_nothing_counts_when_the_block_cannot_be_placed():
    """No frame carries a sent payload (the recording is not this block): nothing counts, every frame is off."""
    block = _block()
    x = _recording(block, 100, periods=2)
    rng = random.Random(10)
    frames = [{"start": s, "end": s + FRAME, "payload_hex": bytes(rng.randrange(256) for _ in range(PAYLOAD_LEN)).hex()}
              for s in (5000, 20_000, 40_000)]
    pt = _new(block, x, frames)
    assert pt.bits == 0 and pt.off_layout == 3 and pt.ebn0_db is None


def test_noise_from_the_layout_gaps_matches_the_old_estimate_on_a_clean_recording():
    """Every frame found, none false: the layout's gaps are the gaps after the frames found, so N0 and Eb/N0 are
    the old ones; exactly when the frames start on the layout, within 0.05 dB when they are a few samples off (the
    bench's are within 4: the E310 adds about 2 samples at each repeat of the block)."""
    block = _block(5)
    start = 31_000                                    # in a gap: no frame is cut
    x = _recording(block, start, seed=11)
    for jitter, tol in ((0, 1e-9), (4, 0.05)):
        frames = _found(block, start, len(x), seed=12, jitter=jitter)
        old, new = _old(block, x, frames), _new(block, x, frames)
        assert (new.errors, new.bits, new.off_layout) == (old.errors, old.bits, 0)
        assert new.noise_n == old.noise_n
        assert abs(new.ebn0_db - old.ebn0_db) <= tol, (jitter, new.ebn0_db, old.ebn0_db)
        assert abs(new.ebn0_db - EBN0_DB) < 0.5
