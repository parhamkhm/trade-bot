"""Volatility indicators: true range, simple ATR and the finite exponential return kernel (SPEC section 7.1).

All three are pure and causal: the value at index ``t`` uses only data up to and including ``t``, through a
FINITE window, so it is the same whatever slice of history the series starts from once the window is full
(``fsum`` makes that bit-exact). Wilder's ATR is deliberately not used: its memory is infinite.

* ``true_range``: Wilder (1978),
  ``TR_k = max(high_k - low_k, |high_k - close_{k-1}|, |low_k - close_{k-1}|)``.
* ``atr_simple``: the SIMPLE mean of the last ``n`` true ranges (SPEC 7.1: ATR20).
* ``sigma_hat_kernel``: SPEC 7.1 / D-056, an annualised volatility from the 150-return exponential kernel with
  ``alpha = 2/31`` (span 30), applied to squared log returns without mean removal.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from tbot.indicators.moving_average import as_float_array

__all__ = ["atr_simple", "sigma_hat_kernel", "true_range"]

Series = Sequence[float] | NDArray[np.float64]


def true_range(high: Series, low: Series, close: Series) -> NDArray[np.float64]:
    """True range per bar; NaN at index 0 because it needs the previous close."""
    h, lo, c = as_float_array(high), as_float_array(low), as_float_array(close)
    if not len(h) == len(lo) == len(c):
        raise ValueError("high, low and close must have the same length")
    out = np.full(len(h), np.nan)
    for k in range(1, len(h)):
        out[k] = max(h[k] - lo[k], abs(h[k] - c[k - 1]), abs(lo[k] - c[k - 1]))
    return out


def atr_simple(high: Series, low: Series, close: Series, n: int = 20) -> NDArray[np.float64]:
    """Simple mean of the last ``n`` true ranges; first valid index is ``n`` (needs ``n + 1`` bars)."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    tr = true_range(high, low, close).tolist()
    out = np.full(len(tr), np.nan)
    for t in range(n, len(tr)):
        out[t] = math.fsum(tr[t - n + 1 : t + 1]) / n
    return out


def sigma_hat_kernel(
    close: Series,
    *,
    alpha: float = 2 / 31,
    k: int = 150,
    periods_per_year: float = 365.0,
) -> NDArray[np.float64]:
    """Annualised ``sqrt(sum_{j<k} (1-a)^j r_{t-j}^2 / sum_{j<k} (1-a)^j) * sqrt(periods)``.

    ``r_t = ln(close_t / close_{t-1})``. The first valid index is ``k`` (needs ``k`` returns, i.e. ``k + 1``
    closes), which is why S0's warm-up is 151 closes.
    """
    if k < 1 or not 0.0 < alpha < 1.0:
        raise ValueError(f"need k >= 1 and 0 < alpha < 1, got k={k}, alpha={alpha}")
    c = as_float_array(close).tolist()
    sq = [math.log(c[i] / c[i - 1]) ** 2 for i in range(1, len(c))]  # sq[j] belongs to index j + 1
    weights = [(1.0 - alpha) ** j for j in range(k)]
    denominator = math.fsum(weights)
    scale = math.sqrt(periods_per_year)
    out = np.full(len(c), np.nan)
    for t in range(k, len(c)):
        recent_first = sq[t - k : t][::-1]  # r_t, r_{t-1}, ..., r_{t-k+1}
        numerator = math.fsum(w * s for w, s in zip(weights, recent_first, strict=True))
        out[t] = math.sqrt(numerator / denominator) * scale
    return out
