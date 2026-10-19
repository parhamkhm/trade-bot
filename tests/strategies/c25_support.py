"""Shared synthetic fixtures for the Phase 3 C2-C5 tests (no real data, no performance numbers).

Rows are ``(open, high, low, close)`` strings. The ``linear`` path has close_k = 100 + k with open = previous
close, high = close and low = open, so every true range is exactly 1 (ATR20 = 1, entry stop = open - 3) and
realised volatility falls steadily with the price level. Bars are anchored: bar 0 closes 2018-01-02 00:00 UTC.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies.replay import PositionState, ReplayStrategy
from tests.sim_helpers import make_bar

D1 = Timeframe.D1
START = datetime(2018, 1, 1, tzinfo=UTC)  # open of bar 0; its close is 2018-01-02 00:00 UTC
FLAT, LONG, STOPPED = PositionState.FLAT, PositionState.LONG, PositionState.STOPPED
Row = tuple[str, str, str, str]


def linear_rows(first: int, last: int, base: int = 100) -> list[Row]:
    """Rows for k = first..last of the linear path (close = base + k, open = previous close)."""
    return [
        (str(base + k - 1), str(base + k), str(base + k - 1), str(base + k)) for k in range(first, last + 1)
    ]


def bars_from_rows(rows: Sequence[Row]) -> list[Bar]:
    return [make_bar(i, o, c, high=h, low=lo, start=START) for i, (o, h, lo, c) in enumerate(rows)]


def linear_bars(n: int) -> list[Bar]:
    """The first ``n`` bars of the linear path; bar 0 is flat at 100."""
    return bars_from_rows([("100", "100", "100", "100"), *linear_rows(1, n - 1)])


def window(bars: Sequence[Bar], n: int | None = None) -> BarWindow:
    return BarWindow.of(bars if n is None else bars[:n])


def states_from(strategy: ReplayStrategy, bars: Sequence[Bar], first: int) -> list[PositionState]:
    """State after the close of every bar from window length ``first`` on (one growing window)."""
    return [strategy.state(window(bars, n)) for n in range(first, len(bars) + 1)]


def stop_flags_from(strategy: ReplayStrategy, bars: Sequence[Bar], first: int) -> list[bool]:
    return [strategy.stopped_out_on(window(bars, n)) for n in range(first, len(bars) + 1)]


def run_lengths(items: Sequence[PositionState]) -> list[PositionState]:
    """Consecutive duplicates collapsed: [F, F, L, L, S] -> [F, L, S]."""
    out: list[PositionState] = []
    for item in items:
        if not out or out[-1] is not item:
            out.append(item)
    return out


def cyclic_wicky_bars(
    n: int, seed: int, *, every: int = 29, cycle: int = 240, daily_vol: float = 0.025
) -> list[Bar]:
    """Anchored bull/bear cycles (so trend and mean-reversion rules all enter and exit) with periodic long
    lower wicks that trigger the ATR stop. Seeded; two-decimal prices; all bars close well before the
    holdout."""
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 10_000.0
    for i in range(n):
        drift = 0.004 * math.sin(2 * math.pi * i / cycle)
        o = price
        c = max(1.0, o * (1 + rng.gauss(drift, daily_vol)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, daily_vol / 3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, daily_vol / 3)))
        bar = make_bar(
            i, f"{o:.2f}", f"{c:.2f}", high=f"{max(hi, o, c):.2f}", low=f"{min(lo, o, c):.2f}", start=START
        )
        if i % every == every - 1:
            bar = replace(bar, low=(bar.low * Decimal("0.85")).quantize(Decimal("0.01")))
        out.append(bar)
        price = c
    return out
