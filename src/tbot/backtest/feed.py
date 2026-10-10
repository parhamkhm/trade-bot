"""BacktestFeed: replays a fixed list of CLOSED bars, lazily, one at a time.

Implements the ``DataFeed`` protocol. The engine pulls bars from the iterator one by one, so a strategy
can never see a bar the feed has not yet yielded (tested with a counting generator). The feed is also the
last line of defence for the sealed holdout (CLAUDE.md 3.5): bars opening at or after
``CANONICAL_HOLDOUT_START`` are refused unless BOTH locks of the data layer are engaged.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence

from tbot.core.types import Bar, Timeframe
from tbot.data.binance_loader import (
    CANONICAL_HOLDOUT_START,
    HOLDOUT_UNSEAL_ENV,
    HOLDOUT_UNSEAL_VALUE,
    HoldoutLockError,
)

__all__ = ["BacktestFeed", "FeedError"]


class FeedError(ValueError):
    """The bars handed to a feed are not a clean, ordered, single-series history."""


class BacktestFeed:
    """Re-iterable replay of one (symbol, timeframe) series."""

    def __init__(self, bars: Sequence[Bar], *, allow_holdout: bool = False) -> None:
        if not bars:
            raise FeedError("BacktestFeed needs at least one bar")
        first = bars[0]
        previous = None
        for bar in bars:
            if bar.symbol != first.symbol or bar.timeframe != first.timeframe:
                raise FeedError(
                    f"mixed series: {bar.symbol}/{bar.timeframe} among {first.symbol}/{first.timeframe}"
                )
            if previous is not None and bar.ts <= previous:
                raise FeedError(f"bars must strictly increase in time ({bar.ts} after {previous})")
            previous = bar.ts
        if bars[-1].open_ts >= CANONICAL_HOLDOUT_START:
            unsealed = allow_holdout and os.environ.get(HOLDOUT_UNSEAL_ENV) == HOLDOUT_UNSEAL_VALUE
            if not unsealed:
                raise HoldoutLockError(
                    f"feed contains bars from the sealed holdout (>= {CANONICAL_HOLDOUT_START.isoformat()}); "
                    "this needs gate G4 and both locks (SPEC section 5.2)"
                )
        self._bars: tuple[Bar, ...] = tuple(bars)
        self.symbol = first.symbol
        self.timeframe: Timeframe = first.timeframe

    def __len__(self) -> int:
        return len(self._bars)

    def bars(self, symbol: str, timeframe: Timeframe) -> Iterator[Bar]:
        if symbol != self.symbol or timeframe != self.timeframe:
            raise FeedError(f"feed holds {self.symbol}/{self.timeframe}, asked for {symbol}/{timeframe}")
        return iter(self._bars)
