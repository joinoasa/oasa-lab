"""Small statistics helpers (standard library only)."""

from __future__ import annotations

import math


def _gammainc_upper(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:  # series for P, then 1 - P
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(10000):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - gln))
    tiny = 1e-300  # continued fraction for Q
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_sf(chi2: float, df: int) -> float:
    """P(X >= chi2) for a chi-square distribution with df degrees of freedom."""
    return _gammainc_upper(df / 2.0, chi2 / 2.0)


def chi2_bytes(data_counts: list[int]) -> tuple[float, float]:
    """Chi-square statistic and p-value for 256 byte counts against a uniform distribution."""
    n = sum(data_counts)
    expected = n / 256.0
    chi2 = sum((c - expected) ** 2 / expected for c in data_counts)
    return chi2, chi2_sf(chi2, 255)


def poisson_sf(k: int, mean: float) -> float:
    """P(X > k) for a Poisson variable."""
    if mean <= 0:
        return 0.0
    term = math.exp(-mean)
    cdf = term
    for i in range(1, k + 1):
        term *= mean / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def poisson_limit(mean: float, alpha: float = 1e-6) -> int:
    """Smallest k such that P(X > k) < alpha."""
    k = 0
    while poisson_sf(k, mean) >= alpha:
        k += 1
    return k


def binomial_bound(n: int, p: float, sigmas: float = 5.0) -> tuple[float, float]:
    """Allowed range for a count of successes in n trials with probability p."""
    mean = n * p
    tol = sigmas * math.sqrt(n * p * (1 - p)) + 0.5
    return max(0.0, mean - tol), min(float(n), mean + tol)
