"""C1: Donchian channel-breakout ensemble on 1d bars (SPEC section 7.2 C1).

Rule source: Donchian's n-period channel breakout with a mid-channel exit, as pre-registered in SPEC 7.2 C1
(trend-following evidence cited in CLAUDE.md section 11: Zarattini-Pagani-Barbon 2025).

For each lookback ``n``: ``U_n = max(high_{t-n..t-1})``, ``L_n = min(low_{t-n..t-1})``,
``M_n = (U_n + L_n) / 2``.
A channel turns long when ``close_t > U_n``, turns flat when ``close_t < M_n`` and otherwise keeps its state
(hysteresis, flat at the anchor; it can only move once ``n`` prior bars exist). The strategy is long iff at
least 2 of the 3 channels are long. Sizing, the ATR20 stop, the fresh-entry rule after a stop-out and the
anchored replay are the common rules of SPEC 7.2 C, shared with S0 through ``ReplayStrategy``.

The channel states are replayed from the window's first bar (the anchor), even before the strategy's first
tradable close, so the state at close ``t`` is the same for every backtest, fold and live start that replays
the same bars (D-057).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tbot.core.types import BarWindow, Timeframe
from tbot.indicators import donchian_mid, donchian_upper
from tbot.strategies.replay import DEFAULT_VOL_TARGET, HISTORY_START, SIGMA_WINDOW, ReplayStrategy

__all__ = ["DonchianEnsemble", "make"]

DEFAULT_LOOKBACKS = (20, 55, 100)
CHANNELS_REQUIRED = 2


class DonchianEnsemble(ReplayStrategy):
    def __init__(
        self,
        lookbacks: Sequence[int] = DEFAULT_LOOKBACKS,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        ns = tuple(int(n) for n in lookbacks)
        if len(ns) != 3 or any(n < 1 for n in ns):
            raise ValueError(f"need three lookbacks >= 1, got {tuple(lookbacks)}")
        self.lookbacks = ns
        # sigma_hat needs 151 closes; a channel needs n prior bars plus the current one -> n + 1 closes
        self.warmup_bars = max(SIGMA_WINDOW + 1, max(ns) + 1)
        self.id = "donchian_" + "_".join(str(n) for n in ns) + "_1d"
        super().__init__(vol_target, anchor)

    def _reset_signal(self) -> None:
        self._long = [False] * len(self.lookbacks)

    def channel_states(self, window: BarWindow) -> tuple[bool, ...]:
        """Per-channel long flags after the close of the last bar of ``window`` (anchored replay)."""
        self._sync(window)
        return tuple(self._long)

    def _signal_at(self, i: int) -> bool:
        for k, n in enumerate(self.lookbacks):
            if i < n:  # channel undefined: it keeps its (flat) state
                continue
            lo = i - n
            upper = float(donchian_upper(self._high[lo : i + 1], n)[-1])
            mid = float(donchian_mid(self._high[lo : i + 1], self._low[lo : i + 1], n)[-1])
            close = self._close[i]
            if close > upper:
                self._long[k] = True
            elif close < mid:
                self._long[k] = False
        return sum(self._long) >= CHANNELS_REQUIRED


def make(
    *, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: object
) -> DonchianEnsemble:
    """Factory used by ``phase3_registry`` (C1 runs on D1 only; replay anchor, default 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"C1 runs on 1d bars only, got {timeframe}")
    lookbacks = params.pop("lookbacks", DEFAULT_LOOKBACKS)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected C1 parameters: {sorted(params)}")
    if not isinstance(lookbacks, Sequence) or not isinstance(vol_target, float | int):
        raise TypeError("lookbacks must be a sequence and vol_target a number")
    return DonchianEnsemble(lookbacks=lookbacks, vol_target=float(vol_target), anchor=anchor)
