"""Trailing rank count: how many of the last ``n`` values are at or below the latest one (SPEC 7.2 C4).

``pct_t`` of C4 is the share of ``rv_{t-364..t}`` that is ``<= rv_t``; this returns the integer COUNT (as a
float) so the caller can compare ``count / n <= p / 100`` exactly in integers (``count * 100 <= p * n``). The
latest value counts itself (``<=``), so the count is at least 1. The window is finite and ends at ``t``, so
the value is causal and start-independent; it is NaN until a full window of non-NaN values exists.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from tbot.indicators.moving_average import as_float_array

__all__ = ["trailing_count_at_or_below"]


def trailing_count_at_or_below(values: Sequence[float] | NDArray[np.float64], n: int) -> NDArray[np.float64]:
    """Number of ``values[t-n+1 .. t]`` that are ``<= values[t]``; NaN if the window is short or has a NaN."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    x = as_float_array(values).tolist()
    out = np.full(len(x), np.nan)
    for t in range(n - 1, len(x)):
        window = x[t - n + 1 : t + 1]
        if any(math.isnan(v) for v in window):
            continue
        out[t] = float(sum(1 for v in window if v <= x[t]))
    return out
