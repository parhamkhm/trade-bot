"""The pre-registered Phase 3 grid and walk-forward calendar (docs/SPEC.md section 7.2 B, C and E).

Nothing here is a tunable: the 12 test periods, the training anchor, the 365-day eligibility minimum, the
base point of every family and the +-1-step neighbour map are the SPEC's numbers. The 17 grid points come from
``tbot.strategies.phase3_registry`` (the single source the strategy tests also iterate over), so a change to
the grid cannot slip past the harness.

Calendar convention (SPEC 7.2 B, fixed here because the SPEC states it for the training window only):

* a daily bar's ``ts`` is its CLOSE (00:00 UTC);
* the training window of a period is the daily closes with ``ts`` in ``[TRAIN_START, period.start)``;
* the test days of a period are the daily closes with ``ts`` in ``[period.start, period.end)``, i.e. the
  same rule one step later. The pooled out-of-sample series therefore covers the closes 2020-01-01 ..
  2025-09-30, and the decision made at the close ``period.start`` (information up to the previous day) is
  the first one taken with that period's parameters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from tbot.core.types import Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry

__all__ = [
    "BASE_KEYS",
    "FAMILIES",
    "HISTORY_START",
    "HOLDOUT_START",
    "MIN_TRAIN_DAYS",
    "OOS_START",
    "SPEC_PERIODS",
    "TRAIN_START",
    "GridPoint",
    "Period",
    "build_grid",
    "grid_points",
    "make_periods",
    "neighbours",
]

HISTORY_START = datetime(2018, 1, 1, tzinfo=UTC)  # replay anchor: the open of close #1 (SPEC 7.1)
TRAIN_START = datetime(2018, 6, 1, tzinfo=UTC)  # S0's first tradable close (SPEC 7.2 B)
OOS_START = datetime(2020, 1, 1, tzinfo=UTC)
HOLDOUT_START = datetime(2025, 10, 1, tzinfo=UTC)  # sealed; this module never loads data
MIN_TRAIN_DAYS = 365  # SPEC 7.2 B [OPERATIONAL]

FAMILIES: tuple[str, ...] = ("C1", "C2", "C3", "C4", "C5")
# The base point of each family (bold in SPEC 7.2 C). Ties in selection go to it.
BASE_KEYS: Mapping[str, str] = {
    "C1": "C1-(20,55,100)",
    "C2": "C2-(30,90,180)",
    "C3": "C3-1d-(16,32,64)",
    "C4": "C4-p20",
    "C5": "C5-x10",
}


@dataclass(frozen=True)
class Period:
    """One walk-forward test period: closes with ``start <= ts < end`` (``end`` exclusive)."""

    index: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("period bounds must be timezone-aware UTC")
        if self.end <= self.start:
            raise ValueError(f"period {self.index} is empty: {self.start} .. {self.end}")

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m}"


def make_periods(starts: Sequence[datetime], end: datetime) -> tuple[Period, ...]:
    """Consecutive periods from each of ``starts`` to the next start (the last one ends at ``end``)."""
    if not starts:
        raise ValueError("need at least one period start")
    if any(b <= a for a, b in pairwise(starts)):
        raise ValueError("period starts must strictly increase")
    bounds = [*starts, end]
    return tuple(Period(i, bounds[i], bounds[i + 1]) for i in range(len(starts)))


def _spec_starts() -> list[datetime]:
    starts = [datetime(y, m, 1, tzinfo=UTC) for y in range(2020, 2026) for m in (1, 7)]
    return starts  # 2020-01 .. 2025-07: eleven 6-month windows plus the 3-month tail


SPEC_PERIODS: tuple[Period, ...] = make_periods(_spec_starts(), HOLDOUT_START)


@dataclass(frozen=True)
class GridPoint:
    """One of the 17 pre-registered trials (SPEC 7.2 C)."""

    key: str
    family: str
    timeframe: Timeframe
    params: Mapping[str, object]
    is_base: bool
    position: int  # index inside the family, registry order

    @property
    def entry(self) -> Phase3Entry:
        for entry in PHASE3_ENTRIES:
            if entry.key == self.key:
                return entry
        raise KeyError(self.key)


def build_grid(entries: Sequence[Phase3Entry] = PHASE3_ENTRIES) -> dict[str, tuple[GridPoint, ...]]:
    """Group the registry entries (S0 excluded) into the five families, in registry order."""
    grid: dict[str, list[GridPoint]] = {family: [] for family in FAMILIES}
    for entry in entries:
        if entry.key == "S0":
            continue
        family = entry.key.split("-", 1)[0]
        if family not in grid:
            raise ValueError(f"unknown family in registry key {entry.key!r}")
        grid[family].append(
            GridPoint(
                key=entry.key,
                family=family,
                timeframe=entry.timeframe,
                params=entry.params,
                is_base=entry.key == BASE_KEYS[family],
                position=len(grid[family]),
            )
        )
    for family, points in grid.items():
        if sum(p.is_base for p in points) != 1:
            raise ValueError(f"family {family} must have exactly one base point")
    return {family: tuple(points) for family, points in grid.items()}


def grid_points() -> dict[str, GridPoint]:
    """All 17 points by key."""
    return {p.key: p for points in build_grid().values() for p in points}


def neighbours(point: GridPoint, family_points: Sequence[GridPoint]) -> tuple[GridPoint, ...]:
    """The +-1-step neighbours of ``point`` (SPEC 7.2 E).

    The step is the adjacent scale (C1, C2, C3 speed), the adjacent p (C4) or the adjacent x (C5). For C3 the
    timeframe is not a neighbour axis, so the walk stays inside the point's own timeframe: the 1d base
    (16, 32, 64) has the single neighbour (8, 16, 32) after D-087. A point at the edge of its grid has one.
    """
    line = [p for p in family_points if p.family == point.family and p.timeframe is point.timeframe]
    i = [p.key for p in line].index(point.key)
    return tuple(line[j] for j in (i - 1, i + 1) if 0 <= j < len(line))
