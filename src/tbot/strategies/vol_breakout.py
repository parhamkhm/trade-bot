"""C4: volatility-compression breakout on 1d bars (SPEC section 7.2 C4, D-097).

Rule source: breakout from a low-volatility regime -- Donchian's channel breakout (CLAUDE.md section 11:
Zarattini-Pagani-Barbon 2025) gated by a trailing percentile of realised volatility, as pre-registered in
SPEC 7.2 C4.

``rv_t`` = standard deviation (ddof 1) of the last 20 daily ln returns; ``pct_t`` = the share of
``rv_{t-364..t}`` that is ``<= rv_t`` (the latest value counts itself), defined once all 365 values exist.
Enter when flat if ``pct_t <= p/100`` and ``close_t > max(high_{t-20..t-1})``; exit when
``close_t < (max(high_{t-20..t-1}) + min(low_{t-20..t-1})) / 2``. The comparison ``pct_t <= p/100`` is done in
integers (``count x 100 <= p x 365``). The long/flat signal state is the one of ``HysteresisStrategy``
(D-097): it starts flat at the first tradable close. Sizing, the ATR20 stop, the fresh-entry rule after a
stop-out and the anchored replay are the common rules of SPEC 7.2 C, shared with S0 through
``ReplayStrategy``.
"""

from __future__ import annotations

from datetime import datetime

from tbot.core.types import Timeframe
from tbot.indicators import donchian_mid, donchian_upper, log_return_stdev, trailing_count_at_or_below
from tbot.strategies.hysteresis import HysteresisStrategy
from tbot.strategies.replay import DEFAULT_VOL_TARGET, HISTORY_START, SIGMA_WINDOW

__all__ = ["VolBreakout", "make"]

DEFAULT_PCT = 20
RV_RETURNS = 20  # rv_t = stdev of the last 20 daily ln returns
PCT_WINDOW = 365  # rv_{t-364..t}
CHANNEL = 20  # max(high_{t-20..t-1}) and min(low_{t-20..t-1})


class VolBreakout(HysteresisStrategy):
    def __init__(
        self,
        pct: int = DEFAULT_PCT,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        if not 0 < pct <= 100:
            raise ValueError(f"pct must be in (0, 100], got {pct}")
        self.pct = pct
        # sigma_hat needs 151 closes; rv_{t-364} needs 21 closes ending at t-364 -> 365 + 20 closes in all
        self.warmup_bars = max(SIGMA_WINDOW + 1, PCT_WINDOW + RV_RETURNS, CHANNEL + 1)
        self.id = f"volbreakout_p{pct}_1d"
        super().__init__(vol_target, anchor)

    def _reset_indicators(self) -> None:
        self._rv: list[float] = []

    def _observe(self, i: int) -> None:
        if i < RV_RETURNS:
            self._rv.append(float("nan"))
        else:
            window = self._close[i - RV_RETURNS : i + 1]
            self._rv.append(float(log_return_stdev(window, RV_RETURNS)[-1]))

    def percentile_count(self, i: int) -> int | None:
        """How many of ``rv_{i-364..i}`` are ``<= rv_i`` (None until all 365 values exist)."""
        if i + 1 < PCT_WINDOW + RV_RETURNS:
            return None
        return int(trailing_count_at_or_below(self._rv[i - PCT_WINDOW + 1 : i + 1], PCT_WINDOW)[-1])

    def _entry_at(self, i: int) -> bool:
        count = self.percentile_count(i)
        if count is None or count * 100 > self.pct * PCT_WINDOW:
            return False
        if i < CHANNEL:
            return False
        upper = float(donchian_upper(self._high[i - CHANNEL : i + 1], CHANNEL)[-1])
        return self._close[i] > upper

    def _exit_at(self, i: int) -> bool:
        if i < CHANNEL:
            return False
        mid = float(
            donchian_mid(self._high[i - CHANNEL : i + 1], self._low[i - CHANNEL : i + 1], CHANNEL)[-1]
        )
        return self._close[i] < mid


def make(*, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: object) -> VolBreakout:
    """Factory used by ``phase3_registry`` (C4 runs on D1 only; replay anchor, default 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"C4 runs on 1d bars only, got {timeframe}")
    pct = params.pop("pct", DEFAULT_PCT)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected C4 parameters: {sorted(params)}")
    if isinstance(pct, bool) or not isinstance(pct, int) or not isinstance(vol_target, float | int):
        raise TypeError("pct must be an integer and vol_target a number")
    return VolBreakout(pct=pct, vol_target=float(vol_target), anchor=anchor)
