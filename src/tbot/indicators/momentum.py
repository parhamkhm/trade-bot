"""Momentum indicators on closes: log-return sign (C2) and Cutler's RSI (C5), SPEC section 7.2 C.

Both are pure, causal and finite-window: the value at ``t`` uses only the closes named below, so it is the
same whatever slice of history the series starts from once the window is full.

* ``log_momentum_sign``: ``sign(ln(close_t / close_{t-h}))`` -- Moskowitz-Ooi-Pedersen time-series momentum;
  exactly 0 when the two closes are equal.
* ``rsi_cutler``: Cutler's RSI. ``U = max(change, 0)`` and ``D = max(-change, 0)`` are averaged with a SIMPLE
  mean over the last ``n`` changes (denominator ``n`` whatever the signs); ``RSI = 100 - 100 / (1 + U/D)``,
  and 100 if the mean ``D`` is 0 (no down move). Source: Connors' RSI(2) pullback rule uses this form.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from tbot.indicators.moving_average import as_float_array

__all__ = ["log_momentum_sign", "rsi_cutler"]


def log_momentum_sign(close: Sequence[float] | NDArray[np.float64], h: int) -> NDArray[np.float64]:
    """``sign(ln(close[t] / close[t-h]))`` in {-1, 0, +1}; NaN for the first ``h`` indices."""
    if h < 1:
        raise ValueError(f"h must be >= 1, got {h}")
    c = as_float_array(close).tolist()
    out = np.full(len(c), np.nan)
    for t in range(h, len(c)):
        move = math.log(c[t] / c[t - h])
        out[t] = float((move > 0.0) - (move < 0.0))
    return out


def rsi_cutler(close: Sequence[float] | NDArray[np.float64], n: int = 2) -> NDArray[np.float64]:
    """Cutler RSI over the last ``n`` changes; 100 if no down move; NaN for the first ``n`` indices."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    c = as_float_array(close).tolist()
    out = np.full(len(c), np.nan)
    for t in range(n, len(c)):
        changes = [c[j] - c[j - 1] for j in range(t - n + 1, t + 1)]
        mean_up = math.fsum(max(d, 0.0) for d in changes) / n
        mean_down = math.fsum(max(-d, 0.0) for d in changes) / n
        out[t] = 100.0 if mean_down == 0.0 else 100.0 - 100.0 / (1.0 + mean_up / mean_down)
    return out
