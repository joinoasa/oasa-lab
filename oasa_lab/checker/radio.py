"""Find bursts of energy in an IQ recording and line them up with packets (standard library only).

Timing and energy only; nothing is demodulated. The level of each short window is the mean of
|I| and |Q| (cheap, and monotonic in power). The noise level is the 25th percentile of windows,
which holds as long as the channel is busy less than three quarters of the time.
"""

from __future__ import annotations

import math
import sys
from array import array
from bisect import bisect_left
from pathlib import Path
from typing import Any

from .sigmf import sample_bytes

WINDOW_S = 0.0005            # 0.5 ms: well under a LoRa symbol at SF7 (1.02 ms)
DETECT_RATIO = 3.0           # a window is "on" at 3x the noise level (about 9.5 dB)
MERGE_GAP_WINDOWS = 1        # a dip of one window (0.5 ms) inside a burst is bridged; a real gap between packets is longer
MIN_BURST_WINDOWS = 4        # shorter than 2 ms is a glitch
MIN_SNR_DB = 12.0            # matched bursts must be this far above the noise
DURATION_TOL_ABS_S = 0.002   # burst length vs airtime: 2 ms (4 windows of edge error) ...
DURATION_TOL_REL = 0.03      # ... plus 3 %
MATCH_TOL_S = 0.050          # a frame reaches the host within 50 ms of its burst ending (USB, firmware)
MAX_OFFSET_S = 1.5           # recording clock vs host clock, after fitting
MIN_MATCH_FRACTION = 0.95
MAX_FILE = 256 * 1024 * 1024

_ABS_U8 = bytes(abs(2 * b - 255) for b in range(256))              # cu8: |I| or |Q| around 127.5, doubled
_ABS_I8 = bytes(min(abs(b - 256 if b > 127 else b) * 2, 255) for b in range(256))


class RadioError(ValueError):
    pass


def window_levels(path: Path, datatype: str, sample_rate: float, window_s: float = WINDOW_S) -> tuple[list[float], float]:
    """Mean level per window, in units of full scale (1.0 = full scale), and the window length in seconds."""
    size = sample_bytes(datatype)
    if size is None:
        raise RadioError(f"unsupported datatype {datatype!r}")
    if path.stat().st_size > MAX_FILE:
        raise RadioError("recording is too large to analyze")
    raw = path.read_bytes()
    per = max(1, round(sample_rate * window_s))          # samples per window
    nwin = len(raw) // size // per
    if nwin < 8:
        raise RadioError("recording is too short to analyze")
    out: list[float] = []
    if datatype in ("cu8", "ci8"):
        data = raw.translate(_ABS_U8 if datatype == "cu8" else _ABS_I8)
        step = per * 2
        for k in range(nwin):
            out.append(sum(data[k * step:(k + 1) * step]) / (step * 255.0))
        return out, per / sample_rate
    code = {"ci16_le": "h", "cf32_le": "f"}.get(datatype)
    if code is None:
        raise RadioError(f"burst analysis does not support {datatype} (use cu8, ci8, ci16_le or cf32_le)")
    arr = array(code)
    arr.frombytes(raw[:nwin * per * size])
    if sys.byteorder == "big":
        arr.byteswap()
    full = 32768.0 if code == "h" else 1.0
    stride = max(1, per * 2 // 64)                    # look at about 64 values per window
    step = per * 2
    for k in range(nwin):
        seg = arr[k * step:(k + 1) * step:stride]
        out.append(sum(abs(v) for v in seg) / len(seg) / full)
    return out, per / sample_rate


def detect_bursts(levels: list[float], win_s: float) -> tuple[list[dict[str, float]], float]:
    """Bursts as dicts with start and end (seconds from the first sample) and snr_db. Also the noise level."""
    srt = sorted(levels)
    noise = max(srt[len(srt) // 4], 1e-9)
    thr = noise * DETECT_RATIO
    on = [lv > thr for lv in levels]
    runs: list[list[int]] = []
    for i, flag in enumerate(on):
        if not flag:
            continue
        if runs and i - runs[-1][1] - 1 <= MERGE_GAP_WINDOWS:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    bursts = []
    for a, b in runs:
        if b - a + 1 < MIN_BURST_WINDOWS:
            continue
        seg = levels[a:b + 1]
        mean = sum(seg) / len(seg)
        bursts.append({"start": a * win_s, "end": (b + 1) * win_s, "snr_db": 20 * math.log10(mean / noise)})
    return bursts, noise


def align(frames: list[tuple[float, float]], bursts: list[dict[str, float]], t0: float) -> dict[str, Any]:
    """Match each frame (arrival time, expected airtime) to a burst (the one that ended just before it).

    The recording's clock and the host's differ by a fixed amount (start-up, USB), so first fit one
    offset d, trying the first few frame/burst pairs, and take the one that matches most frames.
    Then every frame must find its own burst within MATCH_TOL_S whose length matches the airtime.
    """
    ends = [t0 + b["end"] for b in bursts]

    def run(d: float) -> tuple[int, int, list[dict[str, float]], set[int]]:
        used: set[int] = set()
        matched = dur_bad = 0
        rows: list[dict[str, float]] = []
        for ts, air in frames:
            want = ts - d
            lo = bisect_left(ends, want - MATCH_TOL_S)
            best = None
            for j in range(lo, len(ends)):
                if ends[j] > want + MATCH_TOL_S:
                    break
                if j in used:
                    continue
                if best is None or abs(ends[j] - want) < abs(ends[best] - want):
                    best = j
            if best is None:
                continue
            length = bursts[best]["end"] - bursts[best]["start"]
            if abs(length - air) <= DURATION_TOL_ABS_S + DURATION_TOL_REL * air:
                used.add(best)
                matched += 1
                rows.append({"airtime_ms": air * 1000, "burst_ms": length * 1000, "snr_db": bursts[best]["snr_db"],
                             "end_error_ms": (ends[best] - want) * 1000})
            else:
                dur_bad += 1
        return matched, dur_bad, rows, used

    cands = {0.0}
    for ts, _ in frames[:3]:
        for e in ends[:8]:
            cands.add(ts - e)
    best_d, best = 0.0, None
    for d in sorted(cands, key=abs):
        if abs(d) > MAX_OFFSET_S:
            continue
        r = run(d)
        if best is None or r[0] > best[0]:
            best_d, best = d, r
    if best is None:
        best_d, best = 0.0, run(0.0)
    matched, dur_bad, rows, used = best
    snrs = [r["snr_db"] for r in rows]
    return {"frames": len(frames), "bursts": len(bursts), "matched": matched, "duration_mismatch": dur_bad,
            "unmatched_bursts": len(bursts) - len(used), "offset_s": round(best_d, 4),
            "mean_snr_db": round(sum(snrs) / len(snrs), 1) if snrs else None,
            "worst_duration_error_ms": round(max((abs(r["burst_ms"] - r["airtime_ms"]) for r in rows), default=0.0), 3)}
