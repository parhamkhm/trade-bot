"""Finite exponentially weighted moving average of a price level (SPEC section 7.2 C3).

``E_k[t] = sum_{j<K} (1-a)^j x[t-j] / sum_{j<K} (1-a)^j`` with ``a = 2/(k+1)`` (span ``k``): the EWMA
truncated
at ``K`` bars and renormalised so the ``K`` weights sum to 1 (the same convention as the D-056 kernel of
``sigma_hat_kernel``: lags ``0 .. K-1``). The value at ``t`` uses only ``x[t-K+1 .. t]``, so it is causal and
start-independent once ``K`` points exist (NaN before that). ``fsum`` makes the result independent of
summation order. The rule is Carver's EWMAC trend filter (CLAUDE.md section 11: pysystemtrade ``ewmac.py``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray

from tbot.indicators.moving_average import as_float_array

__all__ = ["ewma_finite"]


@lru_cache(maxsize=64)
def _weights(span: int, k: int) -> tuple[tuple[float, ...], float]:
    decay = 1.0 - 2.0 / (span + 1)
    weights = tuple(decay**j for j in range(k))
    return weights, math.fsum(weights)


def ewma_finite(values: Sequence[float] | NDArray[np.float64], span: int, k: int) -> NDArray[np.float64]:
    """Finite renormalised EWMA (``alpha = 2/(span+1)``) over the last ``k`` points; NaN for ``t < k - 1``."""
    if span < 1 or k < 1:
        raise ValueError(f"need span >= 1 and k >= 1, got span={span}, k={k}")
    x = as_float_array(values).tolist()
    weights, total = _weights(span, k)
    out = np.full(len(x), np.nan)
    for t in range(k - 1, len(x)):
        recent_first = x[t - k + 1 : t + 1][::-1]  # x[t], x[t-1], ..., x[t-k+1]
        out[t] = math.fsum(w * v for w, v in zip(weights, recent_first, strict=True)) / total
    return out
