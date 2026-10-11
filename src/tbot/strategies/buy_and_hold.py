"""Buy-and-hold baseline (and its constant-weight, vol-matched variant).

Emits the same target weight on every bar once warmed up. ``warmup_bars`` is configurable so a benchmark
can be aligned to start at exactly the bar where the strategy under test starts deciding.

``stop_price`` exists only to satisfy the mandatory ``Strategy`` contract (SPEC D-050): the baseline has
no risk exit. It returns a deliberately distant level (``stop_fraction`` below entry) and the backtest
engine does not execute stops, so it never influences a benchmark result.
"""

from __future__ import annotations

from decimal import Decimal

from tbot.core.types import BarWindow, TargetIntent

__all__ = ["BuyAndHold"]


class BuyAndHold:
    def __init__(self, *, weight: float = 1.0, warmup_bars: int = 1, stop_fraction: float = 0.5) -> None:
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"weight must be in [0, 1], got {weight}")
        if warmup_bars < 1:
            raise ValueError("warmup_bars must be >= 1")
        if not 0.0 < stop_fraction < 1.0:
            raise ValueError("stop_fraction must be in (0, 1)")
        self.weight = weight
        self.warmup_bars = warmup_bars
        self._stop_fraction = Decimal(str(stop_fraction))
        self.id = "buy_and_hold" if weight == 1.0 else f"buy_and_hold_w{weight:g}"

    def on_bar(self, window: BarWindow) -> TargetIntent:
        return TargetIntent(self.id, window.as_of, self.weight, f"constant weight {self.weight:g}")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price * (1 - self._stop_fraction)
