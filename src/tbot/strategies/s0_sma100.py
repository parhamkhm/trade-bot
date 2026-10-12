"""S0 ``sma100_1d``: the locked SMA100 trend filter with volatility-targeted size (SPEC section 7.1, D-063).

Rule source: SPEC section 7.1 (locked by Parham on 2026-10-10, D-063). The SMA trend filter is the textbook
long-only trend rule; sizing is the finite 150-return kernel of D-056; the stop is Wilder-style
``entry - 3 x ATR`` with a SIMPLE 20-day ATR.

State machine (per Binance 1d bar ``u``, in this order):

1. LONG and an entry is pending (the signal was on bar ``u - 1``): the stop is fixed now as
   ``max(open_u - 3 x ATR20_{u-1}, 0.01)``, then LONG -> STOPPED if ``low_u <= stop`` (this includes the entry
   bar ``t + 1``).
2. The close is evaluated: LONG or STOPPED with ``close <= SMA100`` -> FLAT (so a stop-out bar's own close can
   release the STOPPED state); FLAT with ``close > SMA100`` on a tradable close -> LONG, entry pending.

Re-entry after a stop-out therefore needs a close ``<= SMA100`` and then a fresh close ``> SMA100``.

The state has memory, so it is defined by replay from the first bar of the window (the anchor, 2018-01-01 in
Phase 3; SPEC section 7.1, D-057). ``ReplayStrategy`` keeps an incremental cache that is verified against the
window on every call and falls back to a full replay on any mismatch, so the result is always identical to a
pure replay from the window's first bar. Executing the stop (``min(open, stop)`` fills) and the 25 % rebalance
band belong to the engine; this class only emits the target weight and the fixed stop level.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from decimal import Decimal
from enum import Enum

from tbot.core.types import Bar, BarWindow, TargetIntent, Timeframe
from tbot.indicators import atr_simple, sigma_hat_kernel, sma

__all__ = ["PositionState", "ReplayStrategy", "S0Sma100", "make", "stop_level"]

ATR_PERIOD = 20
STOP_ATR_MULTIPLE = Decimal(3)
STOP_FLOOR = Decimal("0.01")
SIGMA_WINDOW = 150  # returns in the kernel -> 151 closes
DEFAULT_SMA_N = 100
DEFAULT_VOL_TARGET = 0.30


class PositionState(Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    STOPPED = "STOPPED"


def stop_level(entry_price: Decimal, atr: float) -> Decimal:
    """``max(entry - 3 x ATR20, 0.01)``; the single formula shared by the replay and ``stop_price``."""
    return max(entry_price - STOP_ATR_MULTIPLE * Decimal(repr(atr)), STOP_FLOOR)


class ReplayStrategy(ABC):
    """Long/flat strategy with the common SPEC 7.1 / 7.2 C position rules, replayed from the first bar.

    Subclasses provide the entry condition: ``_reset_signal`` clears their indicator state and
    ``_signal_at(i)`` is called once per bar, in order, for every bar of the replay (so hysteresis states
    advance from the anchor) and returns True while the strategy wants to be long.
    """

    id: str
    warmup_bars: int

    def __init__(self, vol_target: float) -> None:
        if not 0.0 < vol_target <= 1.0:
            raise ValueError(f"vol_target must be in (0, 1], got {vol_target}")
        self.vol_target = vol_target
        self._reset()

    # ---- subclass hooks --------------------------------------------------------------------------
    @abstractmethod
    def _reset_signal(self) -> None: ...

    @abstractmethod
    def _signal_at(self, i: int) -> bool: ...

    # ---- cache -----------------------------------------------------------------------------------
    def _reset(self) -> None:
        self._bars: tuple[Bar, ...] = ()
        self._high: list[float] = []
        self._low: list[float] = []
        self._close: list[float] = []
        self._state = PositionState.FLAT
        self._stop: Decimal | None = None
        self._pending_atr: float | None = None
        self._reset_signal()

    def _sync(self, window: BarWindow) -> None:
        """Bring the cache to ``window`` (incrementally when it extends the cached bars, else by replay)."""
        k = len(self._bars)
        if k > len(window.bars) or window.bars[:k] != self._bars:
            self._reset()
            k = 0
        for i in range(k, len(window.bars)):
            bar = window.bars[i]
            self._high.append(float(bar.high))
            self._low.append(float(bar.low))
            self._close.append(float(bar.close))
            self._step(i, bar)
        self._bars = window.bars

    def _atr_at(self, i: int) -> float:
        lo = i - ATR_PERIOD
        return float(
            atr_simple(self._high[lo : i + 1], self._low[lo : i + 1], self._close[lo : i + 1], ATR_PERIOD)[-1]
        )

    def _step(self, i: int, bar: Bar) -> None:
        if self._state is PositionState.LONG:
            if self._pending_atr is not None:
                self._stop = stop_level(bar.open, self._pending_atr)
                self._pending_atr = None
            if self._stop is not None and bar.low <= self._stop:
                self._state = PositionState.STOPPED
        signal = self._signal_at(i)
        if self._state is not PositionState.FLAT:
            if not signal:
                self._state = PositionState.FLAT
                self._stop = None
        elif signal and i + 1 >= self.warmup_bars:
            self._state = PositionState.LONG
            self._stop = None
            self._pending_atr = self._atr_at(i)

    # ---- Strategy protocol ------------------------------------------------------------------------
    def state(self, window: BarWindow) -> PositionState:
        """Position state after the close of the last bar of ``window`` (anchored replay)."""
        self._sync(window)
        return self._state

    def active_stop(self, window: BarWindow) -> Decimal | None:
        """The fixed stop of the open position (None when flat or before the entry bar has been seen)."""
        self._sync(window)
        return self._stop if self._state is PositionState.LONG else None

    def _sigma_hat(self) -> float:
        i = len(self._close) - 1
        return float(sigma_hat_kernel(self._close[i - SIGMA_WINDOW : i + 1], k=SIGMA_WINDOW)[-1])

    def on_bar(self, window: BarWindow) -> TargetIntent:
        if len(window) < self.warmup_bars:
            raise ValueError(f"{self.id} needs {self.warmup_bars} bars, window has {len(window)}")
        self._sync(window)
        if self._state is not PositionState.LONG:
            return TargetIntent(self.id, window.as_of, 0.0, f"{self._state.value}")
        sigma = self._sigma_hat()
        weight = 1.0 if sigma <= 0.0 else min(1.0, self.vol_target / sigma)
        return TargetIntent(self.id, window.as_of, weight, f"LONG sigma_hat {sigma:.4f}")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        """``max(entry_price - 3 x ATR20_t, 0.01)`` with ATR20 at the last bar ``t`` of ``window``.

        ``entry_price`` is ``P_ref``, the open of bar ``t + 1`` before slippage (SPEC 7.1).
        """
        need = ATR_PERIOD + 1
        if len(window) < need:
            raise ValueError(f"stop_price needs {need} bars for ATR{ATR_PERIOD}, window has {len(window)}")
        tail = window.bars[-need:]
        atr = float(
            atr_simple(
                [float(b.high) for b in tail],
                [float(b.low) for b in tail],
                [float(b.close) for b in tail],
                ATR_PERIOD,
            )[-1]
        )
        return stop_level(entry_price, atr)


class S0Sma100(ReplayStrategy):
    """``close_t > SMA_n`` long filter. Defaults are the locked S0; ``sma_n`` and ``vol_target`` exist only
    for the six pre-registered robustness variants of SPEC section 7.1 (SMA 50/75/150/200, vol 20 %/40 %)."""

    def __init__(self, sma_n: int = DEFAULT_SMA_N, vol_target: float = DEFAULT_VOL_TARGET) -> None:
        if sma_n < 2:
            raise ValueError(f"sma_n must be >= 2, got {sma_n}")
        self.sma_n = sma_n
        self.warmup_bars = max(SIGMA_WINDOW + 1, sma_n)
        suffix = "" if vol_target == DEFAULT_VOL_TARGET else f"_v{round(vol_target * 100)}"
        self.id = f"sma{sma_n}_1d{suffix}"
        super().__init__(vol_target)

    def _reset_signal(self) -> None:
        pass

    def _signal_at(self, i: int) -> bool:
        if i + 1 < self.sma_n:
            return False
        avg = float(sma(self._close[i + 1 - self.sma_n : i + 1], self.sma_n)[-1])
        return self._close[i] > avg


def make(*, timeframe: Timeframe, **params: float) -> S0Sma100:
    """Factory used by ``phase3_registry`` (S0 runs on D1 only)."""
    if timeframe is not Timeframe.D1:
        raise ValueError(f"S0 runs on 1d bars only, got {timeframe}")
    sma_n = params.pop("sma_n", DEFAULT_SMA_N)
    vol_target = params.pop("vol_target", DEFAULT_VOL_TARGET)
    if params:
        raise TypeError(f"unexpected S0 parameters: {sorted(params)}")
    if int(sma_n) != sma_n or not math.isfinite(vol_target):
        raise ValueError("sma_n must be an integer and vol_target finite")
    return S0Sma100(sma_n=int(sma_n), vol_target=float(vol_target))
