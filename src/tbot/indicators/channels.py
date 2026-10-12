"""Donchian channels over the PRIOR ``n`` bars (SPEC section 7.2 C1).

``U_n[t] = max(high[t-n .. t-1])``, ``L_n[t] = min(low[t-n .. t-1])``,
``M_n[t] = (U_n[t] + L_n[t]) / 2``.
The current bar ``t`` is excluded, so ``close_t > U_n[t]`` is a genuine breakout of the previous ``n`` bars
and nothing at ``t`` leaks into its own threshold. Each value uses a finite window, so it is start-independent
once ``n`` prior bars exist (NaN before that). Rule source: Donchian's n-period channel breakout, as
pre-registered in SPEC 7.2 C1 (evidence cited in CLAUDE.md section 11: Zarattini-Pagani-Barbon 2025).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray

from tbot.indicators.moving_average import as_float_array

__all__ = ["donchian_lower", "donchian_mid", "donchian_upper"]

Series = Sequence[float] | NDArray[np.float64]


def _prior_extreme(values: Series, n: int, reduce: Callable[..., NDArray[np.float64]]) -> NDArray[np.float64]:
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    x = as_float_array(values)
    out = np.full(len(x), np.nan)
    if len(x) > n:
        out[n:] = reduce(sliding_window_view(x[:-1], n), axis=1)  # row j covers x[j .. j+n-1] -> index j+n
    return out


def donchian_upper(high: Series, n: int) -> NDArray[np.float64]:
    """Highest high of the previous ``n`` bars."""
    return _prior_extreme(high, n, np.max)


def donchian_lower(low: Series, n: int) -> NDArray[np.float64]:
    """Lowest low of the previous ``n`` bars."""
    return _prior_extreme(low, n, np.min)


def donchian_mid(high: Series, low: Series, n: int) -> NDArray[np.float64]:
    """Middle of the previous-``n``-bar channel."""
    h, lo = as_float_array(high), as_float_array(low)
    if len(h) != len(lo):
        raise ValueError("high and low must have the same length")
    return (donchian_upper(h, n) + donchian_lower(lo, n)) / 2.0
