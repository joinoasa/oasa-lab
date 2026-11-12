"""oasa_lab/w1test.py: the check for an outside signal in a W1 recording (find_interference) and the bounded
retake (capture_checked, run_point), on synthetic recordings. No radio and no oasa-radio needed.

The recordings imitate the suite's: frames of random symbols with gaps of silence and a lead-in, on the IF
(+250 kHz) at 1 MS/s with a small frequency offset, in white noise. The thresholds were tuned on recordings made
from real oasa-radio blocks (docstring of find_interference); these tests pin the behaviour."""

from __future__ import annotations

import cmath
import math
import random

from oasa_lab import w1test
from oasa_lab.iq import IQ, cf32_bytes
from oasa_lab.w1test import (GAP, IF_HZ, LEAD, MAX_RECAPTURES, SAMPLE_RATE, SPS, Block, capture_checked,
                             find_interference, run_point)

N = 200_000             # 0.2 s, about 97 blocks of 2 ms
FRAME = 5000            # samples, about a W0 BPSK frame
SIGMA = 0.001           # noise: per-sample standard deviation (complex)


def recording(seed: int = 1, es_n0_db: float = 3.0, cfo_hz: float = 2300.0, n: int = N,
              gain_step: tuple[float, float] | None = None, noise_step: tuple[float, float, float] | None = None,
              ramp_ms: float = 0.0, kick_hz: float = 0.0, burst: dict | None = None) -> list[complex]:
    """A synthetic W1 recording. `gain_step` (from fraction, dB): our signal steps up or down. `noise_step`
    (from, to fraction, dB): the noise floor across the whole band rises. `ramp_ms`/`kick_hz`: the transmitter
    ramps up from silence at the start, with a frequency error that dies away. `burst`: an outside signal, a few
    tones spread over `width_hz` around `f_hz`, `rise_db` above the noise floor in its 50 kHz band, from
    fraction `at` for `ms` milliseconds."""
    rng = random.Random(seed)
    amp = math.sqrt(10 ** (es_n0_db / 10) * SIGMA ** 2 / SPS)       # Es = power x SPS, N0 = SIGMA^2
    base: list[complex] = [0j] * LEAD
    while len(base) < n + 10_000:
        for _ in range(FRAME // SPS):
            base += [amp * (1 if rng.random() < 0.5 else -1)] * SPS
        base += [0j] * GAP
    start = rng.randrange(10_000)
    w = 2 * math.pi * (IF_HZ + cfo_hz) / SAMPLE_RATE
    x = []
    phase = 0.0
    for k in range(n):
        g = 1.0
        if gain_step is not None and k >= gain_step[0] * n:
            g = 10 ** (gain_step[1] / 20)
        if ramp_ms and k < ramp_ms * 1e3:
            g *= k / (ramp_ms * 1e3)
        phase += w + 2 * math.pi * kick_hz * math.exp(-k / 5000) / SAMPLE_RATE
        s = SIGMA / math.sqrt(2)
        if noise_step is not None and noise_step[0] * n <= k < noise_step[1] * n:
            s *= 10 ** (noise_step[2] / 20)
        x.append(g * base[start + k] * cmath.exp(1j * phase) + complex(rng.gauss(0, s), rng.gauss(0, s)))
    if burst is not None:
        tones = 5
        p = 10 ** (burst["rise_db"] / 10) * SIGMA ** 2 * 50e3 / SAMPLE_RATE      # total, inside its 50 kHz band
        a = math.sqrt(p / tones)
        fs = [burst["f_hz"] + burst.get("width_hz", 40e3) * (i / (tones - 1) - 0.5) for i in range(tones)]
        ph = [rng.uniform(0, 2 * math.pi) for _ in fs]
        k0 = int(burst["at"] * n)
        for k in range(k0, min(n, k0 + int(burst["ms"] * 1e3))):
            x[k] += a * sum(cmath.exp(1j * (2 * math.pi * f * k / SAMPLE_RATE + q)) for f, q in zip(fs, ph))
    return x


BENCH_LIKE = {"f_hz": 175e3, "width_hz": 40e3, "rise_db": 13.0}   # 2026-11-11: +13 dB in 150 to 200 kHz


def test_clean_recordings_are_not_flagged():
    for seed, es in ((1, -1.0), (2, 3.0), (3, 7.0), (4, 15.0)):
        r = find_interference(recording(seed, es))
        assert not r["flagged"], (es, r)
        assert r["score_db"] < 3.0, (es, r)


def test_noise_only_is_not_flagged():
    r = find_interference(recording(5, -60.0))
    assert not r["flagged"] and r["score_db"] < 3.0, r


def test_our_own_level_changes_are_not_flagged():
    """A TX gain step either way, the transmitter ramping up with a frequency kick, or starting part way in,
    and a frequency offset at the edge of the decoder's search: all symmetric about the IF."""
    cases = [dict(gain_step=(0.5, 8.0)), dict(gain_step=(0.3, -8.0)), dict(ramp_ms=30.0, kick_hz=8000.0),
             dict(es_n0_db=15.0 - 60.0, gain_step=(0.5, 60.0)), dict(es_n0_db=12.0, cfo_hz=12_000.0)]
    for i, kw in enumerate(cases):
        r = find_interference(recording(10 + i, **kw))
        assert not r["flagged"], (kw, r)


def test_a_wideband_level_change_is_not_flagged():
    """The noise floor rising across the whole band raises every sub-band and its mirror alike: not a
    narrowband outside signal (by design; it is judged on its merits)."""
    for seed, db in ((20, 6.0), (21, 10.0)):
        r = find_interference(recording(seed, noise_step=(0.4, 0.9, db)))
        assert not r["flagged"], (db, r)


def test_a_narrowband_burst_like_the_bench_one_is_flagged():
    """+13 dB in 150 to 200 kHz for the last 60 ms: flagged, with where and how much."""
    r = find_interference(recording(30, burst={**BENCH_LIKE, "at": 0.7, "ms": 60}))
    assert r["flagged"], r
    assert r["band_hz"][0] <= 175e3 <= r["band_hz"][1], r
    assert r["band_rise_db"] > 10 and r["mirror_rise_db"] < 2, r
    assert 135 <= r["start_ms"] <= 146 and r["end_ms"] >= 190, r


def test_bursts_by_level_and_length():
    """On either side of the IF; a strong one from 10 ms, a 6 dB one when it lasts, one on for 70 % of the recording.
    Let pass: 3 dB, 1.5 ms (it touches 2 blocks at most), and one on for 90 % of the recording (it is the recording's normal level then)."""
    flagged = [({"f_hz": 175e3, "rise_db": 10.0, "ms": 10}, True),
               ({"f_hz": 330e3, "rise_db": 10.0, "ms": 10}, True),
               ({"f_hz": 175e3, "rise_db": 13.0, "ms": 140}, True),
               ({"f_hz": 175e3, "rise_db": 13.0, "ms": 180}, False),
               ({"f_hz": 175e3, "rise_db": 6.0, "ms": 100}, True),
               ({"f_hz": 175e3, "rise_db": 3.0, "ms": 100}, False),
               ({"f_hz": 175e3, "rise_db": 13.0, "ms": 1.5}, False)]
    for i, (b, want) in enumerate(flagged):
        r = find_interference(recording(40 + i, burst={"width_hz": 20e3, "at": 0.1, **b}))
        assert r["flagged"] is want, (b, r)


# --- the retake --------------------------------------------------------------------------------------------

def _iq(x: list[complex]) -> IQ:
    return IQ(cf32_bytes(x), "cf32_le", SAMPLE_RATE, 915e6)


CLEAN = _iq(recording(50))
DIRTY = _iq(recording(51, burst={**BENCH_LIKE, "at": 0.5, "ms": 60}))


def _feed(*iqs: IQ):
    calls: list[IQ] = []
    queue = list(iqs)

    def capture() -> IQ:
        calls.append(queue.pop(0))
        return calls[-1]
    return capture, calls


def test_a_flagged_capture_is_taken_again_and_logged():
    capture, calls = _feed(DIRTY, CLEAN)
    seen = []
    iq, raw, flags = capture_checked(capture, on_flag=lambda ev, rec: seen.append((ev, rec)))
    assert iq is CLEAN and len(calls) == 2 and len(raw) == CLEAN.n
    assert len(flags) == 1 and flags[0]["attempt"] == 0 and flags[0]["retaken"] is True
    assert seen == [(flags[0], DIRTY)] and flags[0]["band_rise_db"] > 10


def test_retakes_are_bounded_and_the_last_capture_is_used():
    capture, calls = _feed(*[DIRTY] * (MAX_RECAPTURES + 3))
    iq, _, flags = capture_checked(capture)
    assert len(calls) == 1 + MAX_RECAPTURES and iq is calls[-1]
    assert [f["retaken"] for f in flags] == [True] * MAX_RECAPTURES + [False]


def test_a_clean_capture_is_kept_whatever_it_measures(monkeypatch, tmp_path):
    """run_point decides on a retake before decoding. A clean recording that decodes to nothing (the worst
    result) is kept: one capture, no flag. A flagged one is replaced by the next clean one and reported."""
    monkeypatch.setattr(w1test, "decode_report", lambda *a, **k: ([], {}))

    class Tx:
        tx_port = "TX"

        def tx_file(self, *a, **k):
            pass

        def tx_off(self):
            return {}

    class Rx:
        id, rx_port = "rx0", "RX"

        def __init__(self, *iqs):
            self.capture_fn, self.calls = _feed(*iqs)

        def capture(self, *a, **k):
            return self.capture_fn()

    block = Block("bpsk", [0j] * (N // w1test.PERIODS), [], {"global": {}}, "w0", "none", [])
    events = []
    rx = Rx(CLEAN, DIRTY)
    pt = run_point(tmp_path, Tx(), rx, block, 915e6, 10.0, tmp_path, "a-b", on_flag=lambda ev, iq: events.append(ev))
    assert len(rx.calls) == 1 and pt.flagged == 0 and pt.frames == 0 and events == []
    rx = Rx(DIRTY, CLEAN)
    pt = run_point(tmp_path, Tx(), rx, block, 915e6, 10.0, tmp_path, "a-b", on_flag=lambda ev, iq: events.append(ev))
    assert len(rx.calls) == 2 and pt.recording is CLEAN and pt.flagged == 1
    assert len(events) == 1 and events[0]["direction"] == "a-b" and events[0]["tx_gain_db"] == 10.0
    assert events[0]["rx_id"] == "rx0" and events[0]["retaken"] is True
    rx = Rx(DIRTY, CLEAN)       # without on_flag (other suites calling run_point): one capture, as before
    pt = run_point(tmp_path, Tx(), rx, block, 915e6, 10.0, tmp_path, "a-b")
    assert len(rx.calls) == 1 and pt.recording is DIRTY and pt.flagged == 0
