"""Shared position machine of the Phase-3 long/flat strategies (SPEC sections 7.1 and 7.2 C, D-057, D-063).

Rule source: the state machine, the ATR20 stop and the sizing are copied from SPEC 7.1 (locked S0) and are the
"common rules" of SPEC 7.2 C; the entry condition is supplied by the subclass (S0: SMA trend filter; C1:
Donchian vote). Per Binance 1d bar ``u``, in this order:

1. LONG and an entry is pending (the signal was on bar ``u - 1``): the stop is fixed now as
   ``max(open_u - 3 x ATR20_{u-1}, 0.01)``, then LONG -> STOPPED if ``low_u <= stop`` (this includes the entry
   bar ``t + 1``).
2. The entry condition is evaluated on the close: LONG or STOPPED with the condition false -> FLAT (so a
   stop-out bar's own close can release STOPPED); FLAT with the condition true on a tradable close
   (``index + 1 >= warmup_bars``) -> LONG, entry pending.

Re-entry after a stop-out therefore needs the condition to go false and then true again. Sizing is
``w = min(1, vol_target / sigma_hat)`` while LONG, else 0. Executing the stop (``min(open, stop)`` fills)
and the 25 % rebalance band belong to the engine; this class only emits the target weight and the fixed
stop level.

Anchored replay (D-057). The state has memory, so it is DEFINED by replay from the window's first bar, which
must be the anchor (``history_start``, 2018-01-01 00:00 UTC, the open of the first daily bar). When
``anchor`` is set, a window whose first bar does not close one bar length after the anchor is refused with
``ValueError``: a late start would silently give a different state. ``make()`` of every strategy sets the
anchor; direct construction in unit tests may pass ``None``. An incremental cache is verified against the
window on every call (``window.bars[:k] == cached bars``) and falls back to a full replay on any mismatch,
so the result is always identical to a pure replay.

``trace(window)`` is the look-ahead test hook: ONE full replay that records (state, weight, stop) after every
bar. For a causal strategy, ``trace(full)[m]`` equals what ``state`` / ``on_bar`` / ``active_stop`` return for
the prefix ``window[:m + 1]`` (see ``tests/strategies/replay_checks.py``).

Initialisation is lazy: ``__init__`` only stores the common parameters and ``_reset`` (which calls the
subclass hook ``_reset_signal``) runs at the first use. A subclass may therefore set its own attributes before
or after ``super().__init__()``; they must only be in place before the first ``state`` / ``on_bar`` call.

Daily bars only. Phase 3 has no intraday-signal strategy (SPEC D-090 removed the C3 4h points), so this
class sizes and stops on the window's own daily bars.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import NamedTuple

from tbot.core.types import Bar, BarWindow, TargetIntent, Timeframe
from tbot.indicators import atr_simple, sigma_hat_kernel

__all__ = [
    "ATR_PERIOD",
    "DEFAULT_VOL_TARGET",
    "HISTORY_START",
    "SIGMA_WINDOW",
    "STOP_ATR_MULTIPLE",
    "STOP_FLOOR",
    "PositionState",
    "ReplayStrategy",
    "TracePoint",
    "stop_level",
]

ATR_PERIOD = 20
STOP_ATR_MULTIPLE = Decimal(3)
STOP_FLOOR = Decimal("0.01")
SIGMA_WINDOW = 150  # returns in the kernel -> 151 closes
DEFAULT_VOL_TARGET = 0.30
HISTORY_START = datetime(2018, 1, 1, tzinfo=UTC)  # equals DataConfig.history_start (checked in the tests)


class PositionState(Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    STOPPED = "STOPPED"


class TracePoint(NamedTuple):
    """What the strategy decides after the close of one bar."""

    state: PositionState
    weight: float
    stop: Decimal | None


def stop_level(entry_price: Decimal, atr: float) -> Decimal:
    """``max(entry - 3 x ATR20, 0.01)``; the single formula shared by the replay and ``stop_price``."""
    return max(entry_price - STOP_ATR_MULTIPLE * Decimal(repr(atr)), STOP_FLOOR)


class ReplayStrategy(ABC):
    """Long/flat strategy with the common SPEC 7.1 / 7.2 C position rules, replayed from the first bar.

    Subclasses provide the entry condition: ``_reset_signal`` clears their indicator state and
    ``_signal_at(i)`` is called once per bar, in order, for every bar of the replay (so hysteresis states
    advance from the anchor) and returns True while the strategy wants to be long. ``_signal_at(i)`` may read
    ``self._high / _low / _close`` at indices ``<= i`` only.
    """

    id: str
    warmup_bars: int

    def __init__(self, vol_target: float, anchor: datetime | None = None) -> None:
        if not 0.0 < vol_target <= 1.0:
            raise ValueError(f"vol_target must be in (0, 1], got {vol_target}")
        if anchor is not None and (anchor.tzinfo is None or anchor.utcoffset() is None):
            raise ValueError("anchor must be timezone-aware")
        self.vol_target = vol_target
        self.anchor = anchor
        self._ready = False  # the first _sync runs _reset (lazy, see the module docstring)
        self._bars: tuple[Bar, ...] = ()

    # ---- subclass hooks --------------------------------------------------------------------------
    @abstractmethod
    def _reset_signal(self) -> None: ...

    @abstractmethod
    def _signal_at(self, i: int) -> bool: ...

    # ---- replay ----------------------------------------------------------------------------------
    def _reset(self) -> None:
        self._ready = True
        self._bars = ()
        self._high: list[float] = []
        self._low: list[float] = []
        self._close: list[float] = []
        self._state = PositionState.FLAT
        self._stop: Decimal | None = None
        self._pending_atr: float | None = None
        self._reset_signal()

    def _check_anchor(self, window: BarWindow) -> None:
        if window.timeframe is not Timeframe.D1:
            # sigma_hat (sqrt(365)) and ATR20 are DAILY rules (SPEC 7.1, 7.2 C): wrong on any other bars
            raise ValueError(f"{self.id}: daily bars only, got a {window.timeframe.value} window")
        if self.anchor is None:
            return
        expected = self.anchor + window.timeframe.delta
        if window.bars[0].ts != expected:
            raise ValueError(
                f"{self.id}: the window must start at the anchor {self.anchor.isoformat()} "
                f"(first bar closing {expected.isoformat()}), got a first bar closing "
                f"{window.bars[0].ts.isoformat()}"
            )

    def _replay(self, bars: tuple[Bar, ...], start: int, record: list[TracePoint] | None) -> None:
        """Feed ``bars[start:]`` one at a time (the only place where bars enter the cache)."""
        for i in range(start, len(bars)):
            bar = bars[i]
            self._high.append(float(bar.high))
            self._low.append(float(bar.low))
            self._close.append(float(bar.close))
            self._step(i, bar)
            if record is not None:
                record.append(self._point(i))

    def _sync(self, window: BarWindow) -> None:
        """Bring the cache to ``window`` (incrementally when it extends the cached bars, else by replay)."""
        self._check_anchor(window)
        if not self._ready:
            self._reset()
        k = len(self._bars)
        if k > len(window.bars) or window.bars[:k] != self._bars:
            self._reset()
            k = 0
        self._replay(window.bars, k, None)
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

    def _sigma_hat(self, i: int) -> float:
        return float(sigma_hat_kernel(self._close[i - SIGMA_WINDOW : i + 1], k=SIGMA_WINDOW)[-1])

    def _weight(self, i: int) -> tuple[float, float]:
        """(target weight, sigma_hat) after bar ``i``; sigma_hat is NaN when the strategy is not long."""
        if self._state is not PositionState.LONG:
            return 0.0, float("nan")
        sigma = self._sigma_hat(i)
        return (1.0 if sigma <= 0.0 else min(1.0, self.vol_target / sigma)), sigma

    def _point(self, i: int) -> TracePoint:
        stop = self._stop if self._state is PositionState.LONG else None
        return TracePoint(self._state, self._weight(i)[0], stop)

    # ---- Strategy protocol ------------------------------------------------------------------------
    def state(self, window: BarWindow) -> PositionState:
        """Position state after the close of the last bar of ``window`` (anchored replay)."""
        self._sync(window)
        return self._state

    def active_stop(self, window: BarWindow) -> Decimal | None:
        """The fixed stop of the open position (None when flat or before the entry bar has been seen)."""
        self._sync(window)
        return self._stop if self._state is PositionState.LONG else None

    def trace(self, window: BarWindow) -> list[TracePoint]:
        """One full replay of ``window`` from its first bar, recording the decision after every bar.

        Entry ``m`` is the decision after the close of ``window.bars[m]``, made with bars ``0..m`` only if the
        strategy is causal. Bars before ``warmup_bars`` are recorded too (the machine runs but cannot trade).
        """
        self._check_anchor(window)
        self._reset()
        points: list[TracePoint] = []
        self._replay(window.bars, 0, points)
        self._bars = window.bars
        return points

    def on_bar(self, window: BarWindow) -> TargetIntent:
        if len(window) < self.warmup_bars:
            raise ValueError(f"{self.id} needs {self.warmup_bars} bars, window has {len(window)}")
        self._sync(window)
        weight, sigma = self._weight(len(window) - 1)
        if self._state is not PositionState.LONG:
            return TargetIntent(self.id, window.as_of, weight, f"{self._state.value}")
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
