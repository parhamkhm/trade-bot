"""SMA-filter baseline: long when the last close is above its simple moving average, else flat.

Causal by construction: the average at bar ``t`` uses only the last ``n`` closes up to and including ``t``
(``window.bars[-n:]``). The result is an all-or-nothing intent (weight 1.0 or 0.0); sizing and costs are the
engine's business, so the filter itself has no parameters beyond ``n``.
"""

from __future__ import annotations

import math
from decimal import Decimal

from tbot.core.types import BarWindow, TargetIntent

__all__ = ["SmaFilter"]

_STOP_CLEARANCE = Decimal("0.0001")  # keeps the stop strictly below the entry when the SMA is above it


class SmaFilter:
    def __init__(self, n: int = 200) -> None:
        if n < 2:
            raise ValueError(f"n must be >= 2, got {n}")
        self.n = n
        self.warmup_bars = n
        self.id = f"sma_filter_{n}"

    def sma(self, window: BarWindow) -> float:
        """Simple moving average of the last ``n`` closes (needs ``len(window) >= n``)."""
        if len(window) < self.n:
            raise ValueError(f"need {self.n} bars, window has {len(window)}")
        return math.fsum(float(b.close) for b in window.bars[-self.n :]) / self.n

    def on_bar(self, window: BarWindow) -> TargetIntent:
        avg = self.sma(window)
        close = float(window.last.close)
        weight = 1.0 if close > avg else 0.0
        return TargetIntent(self.id, window.as_of, weight, f"close {close:.2f} vs SMA{self.n} {avg:.2f}")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        """The SMA itself is the exit level; clamped to stay strictly below ``entry_price``."""
        level = Decimal(repr(self.sma(window)))
        return min(level, entry_price * (1 - _STOP_CLEARANCE))
