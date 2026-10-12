"""Simple moving average: pure, causal, finite-window and start-independent.

The value at index ``t`` is the mean of ``x[t-n+1 .. t]`` and depends on nothing else, so it is identical
whatever slice of history the series starts from once ``n`` points are available. Sums use ``math.fsum``
(correctly rounded, independent of summation order), so "identical" means bit-identical, not just close.
Source of the rule: the textbook n-period simple average used by SPEC section 7.1 (SMA100).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

__all__ = ["as_float_array", "sma"]


def as_float_array(values: Sequence[float] | NDArray[np.float64]) -> NDArray[np.float64]:
    """1-D float64 copy of ``values`` (raises on any other shape)."""
    arr = np.array(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"expected a 1-D series, got shape {arr.shape}")
    return arr


def sma(values: Sequence[float] | NDArray[np.float64], n: int) -> NDArray[np.float64]:
    """Mean of the last ``n`` values up to and including each index; NaN for the first ``n - 1`` indices."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    x = as_float_array(values).tolist()
    out = np.full(len(x), np.nan)
    for t in range(n - 1, len(x)):
        out[t] = math.fsum(x[t - n + 1 : t + 1]) / n
    return out
