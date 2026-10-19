"""C5: RSI(2) pullback mean-reversion in an up-trend, on 1d bars (SPEC section 7.2 C5, D-097).

Rule source: Connors' short-term RSI(2) pullback inside a long-term trend filter (close above the 200-day
average), here with Cutler's simple-mean RSI as pre-registered in SPEC 7.2 C5. Evidence is weak (SPEC 7.2 A).

``RSI2_t`` = Cutler RSI over 2 bars (simple means of up and down changes over the last 2 changes; 100 if no
down move; ``tbot.indicators.rsi_cutler``); ``SMA200_t`` = mean of the last 200 closes. Enter when flat if
``close_t > SMA200_t`` and ``RSI2_t < x``; exit when ``RSI2_t > 70``. There is no other exit (in particular a
close below the SMA200 does not exit). The long/flat signal state is the one of ``HysteresisStrategy``
(D-097): it starts flat at the first tradable close. Sizing, the ATR20 stop, the fresh-entry rule after a
stop-out and the anchored replay are the common rules of SPEC 7.2 C, shared with S0 through
``ReplayStrategy``.
"""

from __future__ import annotations

from datetime import datetime

from tbot.core.types import Timeframe
from tbot.indicators import rsi_cutler, sma
from tbot.strategies.hysteresis import HysteresisStrategy
from tbot.strategies.replay import DEFAULT_VOL_TARGET, HISTORY_START, SIGMA_WINDOW

__all__ = ["PullbackMr", "make"]

DEFAULT_RSI_ENTRY = 10
RSI_PERIOD = 2
RSI_EXIT = 70.0
SMA_N = 200


class PullbackMr(HysteresisStrategy):
    def __init__(
        self,
        rsi_entry: float = DEFAULT_RSI_ENTRY,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        if not 0.0 < rsi_entry <= 100.0:
            raise ValueError(f"rsi_entry must be in (0, 100], got {rsi_entry}")
        self.rsi_entry = rsi_entry
        # sigma_hat needs 151 closes; SMA200 needs 200; RSI2 needs 3
        self.warmup_bars = max(SIGMA_WINDOW + 1, SMA_N, RSI_PERIOD + 1)
        self.id = f"pullback_rsi2_x{rsi_entry:g}_1d"
        super().__init__(vol_target, anchor)

    def _rsi2(self, i: int) -> float:
        return float(rsi_cutler(self._close[i - RSI_PERIOD : i + 1], RSI_PERIOD)[-1])

    def _entry_at(self, i: int) -> bool:
        if i + 1 < SMA_N:
            return False
        average = float(sma(self._close[i + 1 - SMA_N : i + 1], SMA_N)[-1])
        return self._close[i] > average and self._rsi2(i) < self.rsi_entry

    def _exit_at(self, i: int) -> bool:
        return self._rsi2(i) > RSI_EXIT


def make(*, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: object) -> PullbackMr:
    """Factory used by ``phase3_registry`` (C5 runs on D1 only; replay anchor, default 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"C5 runs on 1d bars only, got {timeframe}")
    rsi_entry = params.pop("rsi_entry", DEFAULT_RSI_ENTRY)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected C5 parameters: {sorted(params)}")
    if isinstance(rsi_entry, bool) or not isinstance(rsi_entry, int | float):
        raise TypeError("rsi_entry must be a number")
    if not isinstance(vol_target, float | int):
        raise TypeError("vol_target must be a number")
    return PullbackMr(rsi_entry=float(rsi_entry), vol_target=float(vol_target), anchor=anchor)
