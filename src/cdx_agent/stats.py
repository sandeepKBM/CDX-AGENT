"""Small stdlib-only statistics helpers."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable


def quantiles(xs: Iterable[float]) -> dict[str, float]:
    """Return p50, p95, max and n using linear interpolation between order statistics."""
    s = sorted(xs)
    if not s:
        raise ValueError("quantiles() requires at least one value")

    def q(p: float) -> float:
        pos = (len(s) - 1) * p
        lo = math.floor(pos)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)

    return {"p50": q(0.5), "p95": q(0.95), "max": s[-1], "n": len(s)}


def _log_pmf(i: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if i == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if i == n else -math.inf
    log_comb = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
    return log_comb + i * math.log(p) + (n - i) * math.log1p(-p)


def _sum_pmf(lo: int, hi: int, n: int, p: float) -> float:
    """P(lo <= X <= hi) for X ~ Binomial(n, p), computed in log space."""
    if lo > hi:
        return 0.0
    logs = [_log_pmf(i, n, p) for i in range(lo, hi + 1)]
    m = max(logs)
    if m == -math.inf:
        return 0.0
    return min(1.0, math.exp(m) * sum(math.exp(v - m) for v in logs))


def _bisect(f: Callable[[float], float], lo: float, hi: float) -> float:
    """Root of monotone f on [lo, hi] (sign change assumed)."""
    flo = f(lo)
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
        if hi - lo < 1e-14:
            break
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact two-sided Clopper-Pearson interval for k successes in n trials."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError("require n > 0 and 0 <= k <= n")
    alpha = 1.0 - conf
    if k == 0:
        lo = 0.0
    else:
        # lower p solves P(X >= k | p) = alpha/2; increasing in p
        lo = _bisect(lambda p: _sum_pmf(k, n, n, p) - alpha / 2, 0.0, 1.0)
    if k == n:
        hi = 1.0
    else:
        # upper p solves P(X <= k | p) = alpha/2; decreasing in p
        hi = _bisect(lambda p: _sum_pmf(0, k, n, p) - alpha / 2, 0.0, 1.0)
    return lo, hi
