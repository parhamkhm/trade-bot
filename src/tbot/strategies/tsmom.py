"""C2: time-series momentum on 1d bars (SPEC section 7.2 C2).

Rule source: time-series momentum of Moskowitz, Ooi and Pedersen (2012), applied to the sign of the past
return over three horizons; crypto evidence cited in CLAUDE.md section 11 (Le and Ruthbah, Monash). The
three-horizon vote is the pre-registered form of SPEC 7.2 C2.

``s_h = sign(ln(close_t / close_{t-h}))`` (sign 0 = 0) for each of the three horizons ``h`` in days; the
strategy is long iff ``mean(s_h1, s_h2, s_h3) > 0``. A mean of exactly 0 is flat. The signal is a pure
function of the last ``max(h) + 1`` closes. Sizing, the ATR20 stop, the fresh-entry rule after a stop-out and
the anchored replay are the common rules of SPEC 7.2 C, shared with S0 through ``ReplayStrategy``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from tbot.core.types import BarWindow, Timeframe
from tbot.indicators import log_momentum_sign
from tbot.strategies.replay import DEFAULT_VOL_TARGET, HISTORY_START, SIGMA_WINDOW, ReplayStrategy

__all__ = ["Tsmom", "make"]

DEFAULT_HORIZONS = (30, 90, 180)


class Tsmom(ReplayStrategy):
    def __init__(
        self,
        horizons: Sequence[int] = DEFAULT_HORIZONS,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        hs = tuple(int(h) for h in horizons)
        if len(hs) != 3 or any(h < 1 for h in hs):
            raise ValueError(f"need three horizons >= 1, got {tuple(horizons)}")
        self.horizons = hs
        # sigma_hat needs 151 closes; close_{t-h} needs h + 1 closes
        self.warmup_bars = max(SIGMA_WINDOW + 1, max(hs) + 1)
        self.id = "tsmom_" + "_".join(str(h) for h in hs) + "_1d"
        super().__init__(vol_target, anchor)

    def _reset_signal(self) -> None:
        self._votes: tuple[int, ...] = (0, 0, 0)
        self._last_signal = False

    def horizon_signs(self, window: BarWindow) -> tuple[int, ...]:
        """``(s_h1, s_h2, s_h3)`` after the close of the last bar of ``window`` (0 where undefined)."""
        self._sync(window)
        return self._votes

    def signal_long(self, window: BarWindow) -> bool:
        """True iff ``mean(s_h) > 0`` after the close of the last bar of ``window``."""
        self._sync(window)
        return self._last_signal

    def _signal_at(self, i: int) -> bool:
        signs: list[int] = []
        for h in self.horizons:
            if i < h:  # close_{t-h} does not exist yet: undefined, counts as 0
                signs.append(0)
                continue
            signs.append(int(log_momentum_sign(self._close[i - h : i + 1], h)[-1]))
        self._votes = tuple(signs)
        self._last_signal = math.fsum(signs) / len(signs) > 0.0
        return self._last_signal


def make(*, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: object) -> Tsmom:
    """Factory used by ``phase3_registry`` (C2 runs on D1 only; replay anchor, default 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"C2 runs on 1d bars only, got {timeframe}")
    horizons = params.pop("horizons", DEFAULT_HORIZONS)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected C2 parameters: {sorted(params)}")
    if not isinstance(horizons, Sequence) or not isinstance(vol_target, float | int):
        raise TypeError("horizons must be a sequence and vol_target a number")
    return Tsmom(horizons=horizons, vol_target=float(vol_target), anchor=anchor)
