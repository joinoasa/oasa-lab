"""IQ samples and a small tone measurement, in pure Python (no numpy, so CI needs nothing extra).

`IQ.data` is raw bytes in one of two SigMF datatypes: `cf32_le` (float32 pairs, what the Pluto gives)
or `cu8` (unsigned 8-bit pairs, what rtl_sdr writes). Full scale is magnitude 1.0, so a tone of
amplitude 1 reads 0 dBFS.
"""

from __future__ import annotations

import cmath
import math
import sys
from array import array
from dataclasses import dataclass

DATATYPES = {"cf32_le": 8, "cu8": 2}  # bytes per complex sample


@dataclass
class IQ:
    data: bytes
    datatype: str
    sample_rate: float
    center_hz: float

    @property
    def n(self) -> int:
        return len(self.data) // DATATYPES[self.datatype]

    def samples(self, count: int | None = None, start: int = 0) -> list[complex]:
        count = self.n - start if count is None else min(count, self.n - start)
        size = DATATYPES[self.datatype]
        raw = self.data[start * size:(start + count) * size]
        if self.datatype == "cu8":
            return [complex((raw[i] - 127.5) / 127.5, (raw[i + 1] - 127.5) / 127.5) for i in range(0, len(raw), 2)]
        a = array("f")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        return [complex(a[i], a[i + 1]) for i in range(0, len(a), 2)]


def cf32_bytes(samples: list[complex]) -> bytes:
    a = array("f")
    for s in samples:
        a.append(s.real)
        a.append(s.imag)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def fft(x: list[complex]) -> list[complex]:
    """Radix-2 FFT. len(x) must be a power of 2."""
    n = len(x)
    if n < 2 or n & (n - 1):
        raise ValueError("FFT length must be a power of 2")
    a = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        half = length // 2
        tw = [cmath.exp(-2j * math.pi * k / length) for k in range(half)]
        for i in range(0, n, length):
            for k in range(half):
                u, v = a[i + k], a[i + k + half] * tw[k]
                a[i + k], a[i + k + half] = u + v, u - v
        length <<= 1
    return a


def _pow2_floor(n: int) -> int:
    return 1 << (n.bit_length() - 1)


def db(x: float) -> float:
    return 10.0 * math.log10(x) if x > 0 else -200.0


def tone_report(iq: IQ, max_points: int = 1 << 16, dc_guard_hz: float = 5000.0) -> dict:
    """Find the strongest tone (outside a small guard around DC) and measure it.

    Returns freq_hz (baseband, interpolated), tone_dbfs, noise_dbfs (total noise power in the whole
    band), snr_db. Frequency accuracy is a small fraction of a bin: bin = sample_rate / N.
    """
    n = _pow2_floor(min(iq.n, max_points))
    if n < 256:
        raise ValueError("need at least 256 samples")
    x = iq.samples(n)
    w = [0.5 - 0.5 * math.cos(2 * math.pi * k / n) for k in range(n)]
    sw2 = sum(v * v for v in w)
    spec = fft([s * v for s, v in zip(x, w)])
    p = [abs(c) ** 2 for c in spec]
    guard = max(4, int(dc_guard_hz / iq.sample_rate * n))
    cand = [k for k in range(n) if min(k, n - k) > guard]
    kp = max(cand, key=lambda k: p[k])
    la, lb, lc = (math.log(max(p[(kp + d) % n], 1e-30)) for d in (-1, 0, 1))
    denom = la - 2 * lb + lc
    delta = 0.5 * (la - lc) / denom if denom != 0 else 0.0
    kk = kp + max(-0.5, min(0.5, delta))
    signed = kk if kk < n / 2 else kk - n
    tone_p = sum(p[(kp + d) % n] for d in range(-3, 4)) / (n * sw2)
    near_peak = {(kp + d) % n for d in range(-8, 9)}
    rest = sorted(p[k] for k in cand if k not in near_peak)
    noise_bin = (rest[len(rest) // 2] / math.log(2)) if rest else 0.0  # median to mean of an exponential
    noise_p = noise_bin / sw2
    return {"freq_hz": signed * iq.sample_rate / n, "tone_dbfs": db(tone_p), "noise_dbfs": db(noise_p),
            "snr_db": db(tone_p) - db(noise_p), "points": n, "bin_hz": iq.sample_rate / n}


def total_power_dbfs(iq: IQ, max_points: int = 1 << 16) -> float:
    x = iq.samples(min(iq.n, max_points))
    return db(sum(abs(s) ** 2 for s in x) / len(x))
