"""C3: EWMAC trend filter on 1d bars (SPEC section 7.2 C3, D-087, D-090, D-097).

Rule source: Carver's EWMAC forecast (``pysystemtrade`` ``systems/provided/rules/ewmac.py``; CLAUDE.md
section 11): the difference of a fast and a slow EWMA, divided by a price-unit volatility and scaled by a
published forecast scalar. Reimplemented here from SPEC 7.2 C3 (the source is GPL; no code is copied).

For each pair ``(f, 4f)``: ``E_k`` is the finite EWMA of the close level with ``alpha = 2/(k+1)``, cut at
``K = 3k`` bars and renormalised (``tbot.indicators.ewma_finite``); ``raw = E_f - E_4f``;
``sigma_p = close_t x stdev(ln returns of the last 36 bars)`` (ddof 1, daily, not annualised);
``F = clip(raw / sigma_p x c_f, -20, 20)`` with Carver's scalars ``c_f`` (8: 5.3, 16: 3.75, 32: 2.65,
64: 1.87). If ``sigma_p = 0`` the pair's ``F`` is 0 (SPEC 7.2 C, D-097). The strategy is long iff the mean of
the three ``F`` is ``> 0`` (a mean of exactly 0 is flat). Sizing, the ATR20 stop, the fresh-entry rule after
a stop-out and the anchored replay are the common rules of SPEC 7.2 C, shared with S0 through
``ReplayStrategy``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from tbot.core.types import BarWindow, Timeframe
from tbot.indicators import ewma_finite, log_return_stdev
from tbot.strategies.replay import DEFAULT_VOL_TARGET, HISTORY_START, SIGMA_WINDOW, ReplayStrategy

__all__ = ["FORECAST_SCALARS", "Ewmac", "forecast", "make"]

DEFAULT_FAST_SPANS = (16, 32, 64)
SLOW_MULTIPLE = 4
TRUNCATION_MULTIPLE = 3  # K = 3k
SIGMA_P_RETURNS = 36
FORECAST_CAP = 20.0
FORECAST_SCALARS: Mapping[int, float] = MappingProxyType({8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87})


def forecast(raw: float, sigma_p: float, scalar: float) -> float:
    """``clip(raw / sigma_p x scalar, -20, 20)``; 0 when ``sigma_p = 0`` (D-097)."""
    if sigma_p == 0.0:
        return 0.0
    return max(-FORECAST_CAP, min(FORECAST_CAP, raw / sigma_p * scalar))


class Ewmac(ReplayStrategy):
    def __init__(
        self,
        fast_spans: Sequence[int] = DEFAULT_FAST_SPANS,
        vol_target: float = DEFAULT_VOL_TARGET,
        anchor: datetime | None = None,
    ) -> None:
        fs = tuple(int(f) for f in fast_spans)
        if len(fs) != 3 or any(f not in FORECAST_SCALARS for f in fs):
            allowed = sorted(FORECAST_SCALARS)
            raise ValueError(f"need three fast spans from {allowed}, got {tuple(fast_spans)}")
        self.fast_spans = fs
        self._slow_window = TRUNCATION_MULTIPLE * SLOW_MULTIPLE * max(fs)  # the longest EWMA window
        # sigma_hat needs 151 closes; the slowest E_4f needs K = 3 x 4f closes; sigma_p needs 36 returns
        self.warmup_bars = max(SIGMA_WINDOW + 1, self._slow_window, SIGMA_P_RETURNS + 1)
        self.id = "ewmac_" + "_".join(str(f) for f in fs) + "_1d"
        super().__init__(vol_target, anchor)

    def _reset_signal(self) -> None:
        self._last_signal = False

    def _forecasts_at(self, i: int) -> tuple[float, ...] | None:
        """``(F_1, F_2, F_3)`` at bar ``i``, or None while any window is not yet full."""
        if i + 1 < max(self._slow_window, SIGMA_P_RETURNS + 1):
            return None
        returns_window = self._close[i - SIGMA_P_RETURNS : i + 1]
        sigma_p = self._close[i] * float(log_return_stdev(returns_window, SIGMA_P_RETURNS)[-1])
        out: list[float] = []
        for f in self.fast_spans:
            ema: list[float] = []
            for span in (f, SLOW_MULTIPLE * f):
                k = TRUNCATION_MULTIPLE * span
                ema.append(float(ewma_finite(self._close[i - k + 1 : i + 1], span, k)[-1]))
            out.append(forecast(ema[0] - ema[1], sigma_p, FORECAST_SCALARS[f]))
        return tuple(out)

    def forecasts(self, window: BarWindow) -> tuple[float, ...] | None:
        """The three capped forecasts after the close of the last bar of ``window`` (None while undefined)."""
        self._sync(window)
        return self._forecasts_at(len(window) - 1)

    def signal_long(self, window: BarWindow) -> bool:
        """True iff ``mean(F) > 0`` after the close of the last bar of ``window``."""
        self._sync(window)
        return self._last_signal

    def _signal_at(self, i: int) -> bool:
        values = self._forecasts_at(i)
        self._last_signal = values is not None and math.fsum(values) / len(values) > 0.0
        return self._last_signal


def make(*, timeframe: Timeframe, anchor: datetime | None = HISTORY_START, **params: object) -> Ewmac:
    """Factory used by ``phase3_registry`` (Phase 3 runs C3 on D1 only, D-090; replay anchor 2018-01-01)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"C3 runs on 1d bars only (the 4h points were dropped, D-090), got {timeframe}")
    fast_spans = params.pop("fast_spans", DEFAULT_FAST_SPANS)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected C3 parameters: {sorted(params)}")
    if not isinstance(fast_spans, Sequence) or not isinstance(vol_target, float | int):
        raise TypeError("fast_spans must be a sequence and vol_target a number")
    return Ewmac(fast_spans=fast_spans, vol_target=float(vol_target), anchor=anchor)
