"""S0 ``sma100_1d``: the locked SMA100 trend filter with volatility-targeted size (SPEC section 7.1, D-063).

Rule source: SPEC section 7.1 (locked by Parham on 2026-10-10, D-063). The SMA trend filter is the textbook
long-only trend rule (long while ``close_t > SMA_n(close)_t``, flat at ``close_t <= SMA``); sizing is the
finite 150-return kernel of D-056; the stop is Wilder-style ``entry - 3 x ATR`` with a SIMPLE 20-day ATR.

The state machine (FLAT / LONG / STOPPED, fixed stop, fresh-entry rule, anchored replay) is the shared
``ReplayStrategy`` of ``tbot.strategies.replay``; this module only supplies the entry condition.
"""

from __future__ import annotations

import math
from datetime import datetime

from tbot.core.types import Timeframe
from tbot.indicators import sma
from tbot.strategies.replay import (
    DEFAULT_VOL_TARGET,
    HISTORY_START,
    SIGMA_WINDOW,
    ReplayStrategy,
)

__all__ = ["S0Sma100", "make"]

DEFAULT_SMA_N = 100


class S0Sma100(ReplayStrategy):
    """``close_t > SMA_n`` long filter. Defaults are the locked S0; ``sma_n`` and ``vol_target`` exist only
    for the six pre-registered robustness variants of SPEC section 7.1 (SMA 50/75/150/200, vol 20 %/40 %)."""

    def __init__(
        self,
        sma_n: int = DEFAULT_SMA_N,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        if sma_n < 2:
            raise ValueError(f"sma_n must be >= 2, got {sma_n}")
        self.sma_n = sma_n
        self.warmup_bars = max(SIGMA_WINDOW + 1, sma_n)
        suffix = "" if vol_target == DEFAULT_VOL_TARGET else f"_v{round(vol_target * 100)}"
        self.id = f"sma{sma_n}_1d{suffix}"
        super().__init__(vol_target, anchor)

    def _reset_signal(self) -> None:
        pass

    def _signal_at(self, i: int) -> bool:
        if i + 1 < self.sma_n:
            return False
        avg = float(sma(self._close[i + 1 - self.sma_n : i + 1], self.sma_n)[-1])
        return self._close[i] > avg


def make(*, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: float) -> S0Sma100:
    """Factory used by ``phase3_registry`` (S0 runs on D1 only; replay anchor, default 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"S0 runs on 1d bars only, got {timeframe}")
    sma_n = params.pop("sma_n", DEFAULT_SMA_N)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected S0 parameters: {sorted(params)}")
    if int(sma_n) != sma_n or not math.isfinite(vol_target):
        raise ValueError("sma_n must be an integer and vol_target finite")
    return S0Sma100(sma_n=int(sma_n), vol_target=float(vol_target), anchor=anchor)
