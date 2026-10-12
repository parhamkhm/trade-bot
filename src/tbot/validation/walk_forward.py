"""Walk-forward selection and pooled out-of-sample series (docs/SPEC.md section 7.2 B, E, G).

The procedure, per candidate (a family of grid points):

1. **Fixed-parameter runs.** Each grid point is run once, from the anchor, over the full history in the manual
   delay mode, with the pre-registered costs. A run at close ``t`` depends only on data up to ``t``
   (engine causality; fills happen after the signal close), so the daily return of a point at a close before a
   period start is the return that point would have had if the data had ended at that close.
2. **Selection** for a period starting at ``S``: among the points that have at least 365 tradable closes in
   ``[first_tradable, S)`` (*eligible*), take the highest annualised Sharpe (365) over the eligible points'
   **common** days ``[max first_tradable, S)``; ties (``|diff| <= 1e-12``) go to the base point, or if the
   base point is not eligible to the lowest grid position. No eligible point means flat.
3. **Pooled out-of-sample series.** A composite strategy follows the schedule of choices through the engine,
   flat before the first period, replaying each child from the anchor; a parameter change at a boundary is
   traded by the engine's rebalance rule.

``select`` never sees a return at or after the period start. ``tests/validation/test_walk_forward.py`` proves
the property end to end: replacing every bar after a period start with different data leaves that period's
and all earlier choices unchanged.

Return convention. The return stamped with close ``c`` is ``equity(c) / equity(c - 1 day) - 1``. A close
belongs to the training window of a period iff ``c < period.start`` and to the test window iff
``period.start <= c < period.end``; so the decision taken at the close ``period.start`` is the first with the
period's parameters and the position it creates earns from the following day.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tbot.core.types import Timeframe
from tbot.validation.eligibility import is_eligible, tradable_days
from tbot.validation.grid import (
    HOLDOUT_START,
    MIN_TRAIN_DAYS,
    SPEC_PERIODS,
    TRAIN_START,
    GridPoint,
    Period,
    build_grid,
)
from tbot.validation.runs import (
    BlendStrategy,
    CompositeStrategy,
    CostSpec,
    FillMode,
    Market,
    SeriesRun,
    StrategyFactory,
    run_buy_and_hold,
    run_composite,
    run_fixed,
)
from tbot.validation.stats import annualised_sharpe

__all__ = [
    "TIE_TOLERANCE",
    "Candidate",
    "CandidateResult",
    "Selection",
    "WalkForwardSpec",
    "benchmark_runs",
    "blend_run",
    "build_schedule",
    "pooled_run",
    "registry_candidates",
    "run_candidate",
    "run_fixed_points",
    "select",
    "walk_forward_selections",
]

TIE_TOLERANCE = 1e-12


@dataclass(frozen=True)
class WalkForwardSpec:
    """The calendar. The defaults ARE the SPEC; tests pass shorter ones, nothing else may."""

    periods: tuple[Period, ...] = SPEC_PERIODS
    train_start: datetime = TRAIN_START
    min_train_days: int = MIN_TRAIN_DAYS
    final_train_end: datetime = HOLDOUT_START  # the "final training window ending at holdout_start"

    @property
    def first_test_start(self) -> datetime:
        return self.periods[0].start

    @property
    def last_test_end(self) -> datetime:
        return self.periods[-1].end


@dataclass(frozen=True)
class Candidate:
    """A family of grid points that compete in the per-period selection."""

    name: str
    points: tuple[GridPoint, ...]

    @property
    def base(self) -> GridPoint:
        return next(p for p in self.points if p.is_base)

    def point(self, key: str) -> GridPoint:
        return next(p for p in self.points if p.key == key)


def registry_candidates() -> list[Candidate]:
    """C1..C5 from the Phase 3 registry (17 points)."""
    return [Candidate(name, points) for name, points in build_grid().items()]


@dataclass(frozen=True)
class Selection:
    """The outcome of the selection for one training window."""

    train_end: datetime  # exclusive: the period start
    chosen: str | None
    eligible: tuple[str, ...]
    common_start: datetime | None
    n_days: int
    sharpes: Mapping[str, float]  # training Sharpe (365) of each eligible point over the common days
    tied: tuple[str, ...]  # the points within the tie tolerance of the best (len > 1 means a tie was broken)


def _sharpe_or_zero(returns: Sequence[float], ppy: float) -> float:
    """A flat series has no defined Sharpe; it is scored 0.0 (it earned nothing)."""
    value = annualised_sharpe(returns, ppy)
    return 0.0 if value is None else value


def select(
    candidate: Candidate,
    train_end: datetime,
    *,
    first_tradable: Mapping[str, datetime],
    daily_closes: Sequence[datetime],
    fixed: Mapping[str, SeriesRun],
    min_train_days: int = MIN_TRAIN_DAYS,
    periods_per_year: float = 365.0,
) -> Selection:
    """Per-period selection for the training window ending (exclusive) at ``train_end``."""
    eligible = [
        p
        for p in candidate.points
        if is_eligible(first_tradable[p.key], daily_closes, train_end, min_days=min_train_days)
    ]
    if not eligible:
        return Selection(train_end, None, (), None, 0, {}, ())
    common_start = max(first_tradable[p.key] for p in eligible)
    sharpes = {
        p.key: _sharpe_or_zero(fixed[p.key].returns_between(common_start, train_end), periods_per_year)
        for p in eligible
    }
    best = max(sharpes.values())
    tied = [p for p in eligible if best - sharpes[p.key] <= TIE_TOLERANCE]
    base = candidate.base
    chosen = base if base in tied else min(tied, key=lambda p: p.position)
    return Selection(
        train_end=train_end,
        chosen=chosen.key,
        eligible=tuple(p.key for p in eligible),
        common_start=common_start,
        n_days=tradable_days(common_start, daily_closes, train_end),
        sharpes=sharpes,
        tied=tuple(p.key for p in tied),
    )


def walk_forward_selections(
    candidate: Candidate,
    spec: WalkForwardSpec,
    *,
    first_tradable: Mapping[str, datetime],
    daily_closes: Sequence[datetime],
    fixed: Mapping[str, SeriesRun],
    periods_per_year: float = 365.0,
) -> tuple[list[Selection], Selection]:
    """The choice for each test period and the final one (training window ending at ``final_train_end``)."""

    def one(end: datetime) -> Selection:
        return select(
            candidate,
            end,
            first_tradable=first_tradable,
            daily_closes=daily_closes,
            fixed=fixed,
            min_train_days=spec.min_train_days,
            periods_per_year=periods_per_year,
        )

    return [one(p.start) for p in spec.periods], one(spec.final_train_end)


def build_schedule(
    spec: WalkForwardSpec, selections: Sequence[Selection]
) -> list[tuple[datetime, str | None]]:
    if len(selections) != len(spec.periods):
        raise ValueError("one selection per period is required")
    return [(p.start, s.chosen) for p, s in zip(spec.periods, selections, strict=True)]


# -- runs ----------------------------------------------------------------------------------------


def run_fixed_points(
    market: Market,
    points: Sequence[GridPoint],
    factory: StrategyFactory,
    modes: Sequence[FillMode],
    costs: CostSpec,
) -> dict[tuple[str, FillMode], SeriesRun]:
    """Full-history fixed-parameter runs of every point in every requested fill mode (fresh strategy each)."""
    out: dict[tuple[str, FillMode], SeriesRun] = {}
    for point in points:
        for mode in modes:
            out[(point.key, mode)] = run_fixed(market, factory(point.key), point.timeframe, mode, costs)
    return out


def _feed_timeframe(keys: Sequence[str | None], timeframes: Mapping[str, Timeframe]) -> Timeframe:
    chosen = {timeframes[k] for k in keys if k is not None}
    return Timeframe.H4 if Timeframe.H4 in chosen else Timeframe.D1


def _baseline(market: Market, first_test_start: datetime) -> datetime:
    closes = market.daily_closes
    idx = bisect_left(closes, first_test_start)
    if idx == 0:
        raise ValueError("need a daily close before the first test close as the return baseline")
    return closes[idx - 1]


def _end(market: Market, spec: WalkForwardSpec) -> datetime:
    """The last daily close inside the test calendar (``< last_test_end``)."""
    closes = market.daily_closes
    return closes[bisect_left(closes, spec.last_test_end) - 1]


def pooled_run(
    market: Market,
    schedule: Sequence[tuple[datetime, str | None]],
    factory: StrategyFactory,
    timeframes: Mapping[str, Timeframe],
    spec: WalkForwardSpec,
    mode: FillMode,
    costs: CostSpec,
    *,
    strategy_id: str = "walk_forward",
) -> SeriesRun:
    """The pooled out-of-sample series of a schedule over ``[first test close, last test close]``."""
    feed_tf = _feed_timeframe([k for _, k in schedule], timeframes)
    composite = CompositeStrategy(
        strategy_id=strategy_id,
        market=market,
        schedule=schedule,
        factory=factory,
        timeframes=timeframes,
        feed_timeframe=feed_tf,
    )
    return run_composite(
        market,
        composite,
        feed_tf,
        mode,
        costs,
        baseline=_baseline(market, spec.first_test_start),
        end=_end(market, spec),
    )


def blend_run(
    market: Market,
    member_schedules: Sequence[Sequence[tuple[datetime, str | None]]],
    factory: StrategyFactory,
    timeframes: Mapping[str, Timeframe],
    spec: WalkForwardSpec,
    mode: FillMode,
    costs: CostSpec,
    *,
    strategy_id: str = "S1",
) -> SeriesRun:
    """Equal-weight blend of several members' schedules (S1), pooled over the test days."""
    keys = [k for sched in member_schedules for _, k in sched]
    feed_tf = _feed_timeframe(keys, timeframes)
    members = [
        CompositeStrategy(
            strategy_id=f"{strategy_id}/{i}",
            market=market,
            schedule=sched,
            factory=factory,
            timeframes=timeframes,
            feed_timeframe=feed_tf,
        )
        for i, sched in enumerate(member_schedules)
    ]
    return run_composite(
        market,
        BlendStrategy(strategy_id, members),
        feed_tf,
        mode,
        costs,
        baseline=_baseline(market, spec.first_test_start),
        end=_end(market, spec),
    )


@dataclass(frozen=True)
class CandidateResult:
    """Everything the walk-forward produces for one candidate (all numbers PROVISIONAL)."""

    name: str
    selections: tuple[Selection, ...]
    final_selection: Selection
    schedule: tuple[tuple[datetime, str | None], ...]
    pooled: Mapping[FillMode, SeriesRun]
    first_tradable: Mapping[str, datetime] = field(default_factory=dict)

    @property
    def chosen_keys(self) -> tuple[str | None, ...]:
        return tuple(s.chosen for s in self.selections)


def run_candidate(
    candidate: Candidate,
    market: Market,
    spec: WalkForwardSpec,
    *,
    factory: StrategyFactory,
    first_tradable: Mapping[str, datetime],
    fixed: Mapping[tuple[str, FillMode], SeriesRun],
    costs: CostSpec,
    modes: Sequence[FillMode] = (FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN),
) -> CandidateResult:
    """Selection on the manual-delay fixed runs, then the pooled series in each requested fill mode."""
    delay_runs = {p.key: fixed[(p.key, FillMode.MANUAL_DELAY)] for p in candidate.points}
    selections, final = walk_forward_selections(
        candidate,
        spec,
        first_tradable=first_tradable,
        daily_closes=market.daily_closes,
        fixed=delay_runs,
        periods_per_year=market.periods_per_year,
    )
    schedule = build_schedule(spec, selections)
    timeframes = {p.key: p.timeframe for p in candidate.points}
    pooled = {
        mode: pooled_run(market, schedule, factory, timeframes, spec, mode, costs, strategy_id=candidate.name)
        for mode in modes
    }
    return CandidateResult(
        name=candidate.name,
        selections=tuple(selections),
        final_selection=final,
        schedule=tuple(schedule),
        pooled=pooled,
        first_tradable=dict(first_tradable),
    )


def benchmark_runs(
    market: Market, spec: WalkForwardSpec, costs: CostSpec, modes: Sequence[FillMode]
) -> dict[FillMode, SeriesRun]:
    """Buy-and-hold (weight 1, entry charged once) over the pooled test days, per fill mode."""
    end = _end(market, spec)
    return {
        mode: run_buy_and_hold(market, mode, costs, first_test_close=spec.first_test_start, end=end)
        for mode in modes
    }
