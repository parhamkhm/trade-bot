"""Completed UTC days aggregated from intraday bars (for the daily sigma_hat / ATR20 of C3-4h, SPEC 7.2 C3).

A bar's ``ts`` is its CLOSE time, so a bar with close ``T`` covers ``[T - length, T]`` and belongs to the
UTC day that contains its open time ``T - length``. A day is COMPLETE only when it holds exactly
``86400 / length`` bars with open times ``00:00, length, 2 x length, ...`` (no gap, no duplicate); an
incomplete day (a partial first day, a day with a missing bar, the still-running current day) is excluded.
A day is therefore returned only once its last bar has closed (at 00:00 UTC of the next day), which keeps
the aggregate causal: aggregating a prefix of the bars returns a prefix of the days aggregated from the
whole series.

Per day: open of the first bar, highest high, lowest low, close of the last bar. The daily bar's close time is
the next 00:00 UTC, the same ``ts`` convention as Binance 1d bars.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

__all__ = ["DailyBars", "complete_utc_days"]

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class DailyBars:
    """Completed UTC days; ``close_ts[k]`` is the 00:00 UTC that ends day ``k``."""

    close_ts: tuple[datetime, ...]
    open: NDArray[np.float64]
    high: NDArray[np.float64]
    low: NDArray[np.float64]
    close: NDArray[np.float64]

    def __len__(self) -> int:
        return len(self.close_ts)


def complete_utc_days(
    close_ts: Sequence[datetime],
    open_: Sequence[float],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    bar_seconds: int,
) -> DailyBars:
    """Aggregate intraday bars (``close_ts`` = close times, increasing, UTC) into complete UTC days."""
    if bar_seconds <= 0 or SECONDS_PER_DAY % bar_seconds != 0:
        raise ValueError(f"bar_seconds must divide a day (86400), got {bar_seconds}")
    n = len(close_ts)
    if not n == len(open_) == len(high) == len(low) == len(close):
        raise ValueError("close_ts, open, high, low and close must have the same length")
    length = timedelta(seconds=bar_seconds)
    per_day = SECONDS_PER_DAY // bar_seconds
    opens = []
    for ts in close_ts:
        if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
            raise ValueError("bar timestamps must be timezone-aware UTC")
        opens.append(ts.astimezone(UTC) - length)
    if any(b <= a for a, b in itertools.pairwise(opens)):
        raise ValueError("bar timestamps must strictly increase")

    days: list[tuple[datetime, float, float, float, float]] = []
    i = 0
    while i < n:
        day_start = opens[i].replace(hour=0, minute=0, second=0, microsecond=0)
        j = i
        while j < n and opens[j] < day_start + timedelta(days=1):
            j += 1
        complete = (
            j - i == per_day
            and opens[i] == day_start
            and all(opens[i + k] == day_start + k * length for k in range(per_day))
        )
        if complete:
            days.append(
                (
                    day_start + timedelta(days=1),
                    float(open_[i]),
                    float(max(high[i:j])),
                    float(min(low[i:j])),
                    float(close[j - 1]),
                )
            )
        i = j
    return DailyBars(
        close_ts=tuple(d[0] for d in days),
        open=np.array([d[1] for d in days], dtype=np.float64),
        high=np.array([d[2] for d in days], dtype=np.float64),
        low=np.array([d[3] for d in days], dtype=np.float64),
        close=np.array([d[4] for d in days], dtype=np.float64),
    )
