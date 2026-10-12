"""First tradable close and period eligibility of a grid point (docs/SPEC.md section 7.2 B).

* A point's **first tradable close** is the close at which every indicator it uses has its full window, but
  never before S0's first tradable close (2018-06-01), because every Phase 3 strategy sizes with S0's
  151-close sigma-hat and uses S0's ATR20 for the stop.
* A point is **eligible** for a period iff it has at least 365 tradable daily closes inside the period's
  training window, i.e. at least 365 daily closes with ``first_tradable <= ts < period.start``.
* Phase 3 is daily only (D-090): every window is counted in daily closes actually present in the data.

The window sizes below are the SPEC's text, written once:

====  =====================================================================================
C1    ``n_max + 1`` closes (channel highs/lows over ``t-n..t-1`` plus the current close)
C2    ``h_max + 1`` closes (the log return over ``h`` days needs ``close_{t-h}``)
C3    ``K = 3 * 4 * f_max`` daily closes (finite EWMA of the slow span 4f truncated at 3k bars)
C4    385 closes (20 returns for ``rv_t`` plus 364 earlier ``rv`` values for the percentile)
C5    200 closes (SMA200)
====  =====================================================================================

The ``test_eligibility_vs_spec`` test recomputes the SPEC table from the real local data and compares every
cell, so a wrong rule here cannot go unnoticed.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from tbot.core.types import Bar, Timeframe
from tbot.validation.grid import MIN_TRAIN_DAYS, TRAIN_START, GridPoint, Period

__all__ = [
    "eligibility_table",
    "first_tradable_close",
    "is_eligible",
    "required_closes",
    "tradable_days",
]

_C4_CLOSES = 385
_C5_CLOSES = 200


def required_closes(family: str, params: Mapping[str, object]) -> int:
    """Number of daily closes a point needs before it can trade (SPEC 7.2 B/C)."""
    if family == "C1":
        return max(_ints(params["lookbacks"])) + 1
    if family == "C2":
        return max(_ints(params["horizons"])) + 1
    if family == "C3":
        return 3 * 4 * max(_ints(params["fast_spans"]))
    if family == "C4":
        return _C4_CLOSES
    if family == "C5":
        return _C5_CLOSES
    raise ValueError(f"unknown family {family!r}")


def _ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, tuple | list) or not value:
        raise ValueError(f"expected a non-empty tuple of ints, got {value!r}")
    return tuple(int(v) for v in value)


def first_tradable_close(
    point: GridPoint, bars_by_timeframe: Mapping[Timeframe, Sequence[Bar]], *, floor: datetime = TRAIN_START
) -> datetime:
    """The first tradable close of ``point`` computed from the bars actually present.

    ``bars_by_timeframe`` must hold the point's own (daily) timeframe, starting at the data start (the first
    close is close number 1). Raises if the series is too short for the window.
    """
    bars = bars_by_timeframe[point.timeframe]
    need = required_closes(point.family, point.params)
    if len(bars) < need:
        raise ValueError(f"{point.key}: {len(bars)} bars, needs {need}")
    return max(bars[need - 1].ts, floor)


def tradable_days(first_tradable: datetime, daily_closes: Sequence[datetime], period_start: datetime) -> int:
    """Daily closes inside the training window with ``first_tradable <= ts < period_start``."""
    lo = bisect_left(daily_closes, first_tradable)
    hi = bisect_left(daily_closes, period_start)
    return max(0, hi - lo)


def is_eligible(
    first_tradable: datetime,
    daily_closes: Sequence[datetime],
    period_start: datetime,
    *,
    min_days: int = MIN_TRAIN_DAYS,
) -> bool:
    return tradable_days(first_tradable, daily_closes, period_start) >= min_days


@dataclass(frozen=True)
class EligibilityRow:
    key: str
    first_tradable: datetime
    eligible: tuple[bool, ...]  # one flag per period, in period order


def eligibility_table(
    points: Sequence[GridPoint],
    bars_by_timeframe: Mapping[Timeframe, Sequence[Bar]],
    periods: Sequence[Period],
    *,
    min_days: int = MIN_TRAIN_DAYS,
) -> list[EligibilityRow]:
    """The SPEC 7.2 B table, recomputed from data."""
    daily = [b.ts for b in bars_by_timeframe[Timeframe.D1]]
    rows = []
    for point in points:
        first = first_tradable_close(point, bars_by_timeframe)
        flags = tuple(is_eligible(first, daily, p.start, min_days=min_days) for p in periods)
        rows.append(EligibilityRow(point.key, first, flags))
    return rows
