"""Robustness runs (docs/SPEC.md section 7.2 E, F, I): neighbours, ETH/USDT, indices, x2 cost stress.

* **Fixed-parameter pooled series** (``fixed_pooled``): one grid point held for all 12 test periods, run in
  the same engine, fill mode and costs as the walk-forward. They feed the neighbour test and are the "trial"
  series whose Sharpe variance is V[SR] in the DSR (SPEC 7.1).
* **Neighbour test** (``neighbour_test``): point P = the candidate's selection on the final training window
  (ending at ``holdout_start``, computed only from pre-holdout data); each +-1-step neighbour keeps >= 70 %
  of P's fixed-parameter pooled OOS manual-delay Sharpe (``gates.neighbour_gate``).
* **ETH/USDT and indices** (``reuse_run``): BTC's per-period choices are reused unchanged, nothing is
  re-selected. For the indices (Stooq, daily, 252-day annualisation) a period whose BTC choice is a 4h point
  is flat (``index_schedule``); they are reported for context only and never gate.
* **x2 cost stress**: the BTC schedule is kept and the pooled series is re-run with ``STRESS_COSTS``
  (selection is NOT redone under the stressed costs: stress asks whether the selected strategy survives dearer
  trading, not what a different selection would have been).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tbot.core.types import Bar, Timeframe
from tbot.validation.gates import GateResult, neighbour_gate
from tbot.validation.grid import HOLDOUT_START, GridPoint, neighbours
from tbot.validation.runs import (
    STRESS_COSTS,
    CostSpec,
    FillMode,
    Market,
    SeriesRun,
    StrategyFactory,
)
from tbot.validation.walk_forward import (
    Candidate,
    CandidateResult,
    Selection,
    WalkForwardSpec,
    pooled_run,
)

__all__ = [
    "NeighbourResult",
    "fixed_pooled",
    "fixed_pooled_all",
    "index_schedule",
    "neighbour_test",
    "parse_stooq_csv",
    "reuse_run",
    "stress_run",
]


def fixed_pooled(
    market: Market,
    point: GridPoint,
    factory: StrategyFactory,
    spec: WalkForwardSpec,
    mode: FillMode,
    costs: CostSpec,
) -> SeriesRun:
    """The point's fixed-parameter pooled OOS series: the same days, engine and costs as the walk-forward."""
    schedule: list[tuple[datetime, str | None]] = [(spec.first_test_start, point.key)]
    return pooled_run(
        market, schedule, factory, {point.key: point.timeframe}, spec, mode, costs, strategy_id=point.key
    )


def fixed_pooled_all(
    market: Market,
    points: Sequence[GridPoint],
    factory: StrategyFactory,
    spec: WalkForwardSpec,
    mode: FillMode,
    costs: CostSpec,
) -> dict[str, SeriesRun]:
    return {p.key: fixed_pooled(market, p, factory, spec, mode, costs) for p in points}


@dataclass(frozen=True)
class NeighbourResult:
    candidate: str
    point_key: str | None  # the point P chosen on the final training window (None: flat, no eligible point)
    reference_sharpe: float | None
    neighbour_sharpes: Mapping[str, float | None]
    gate: GateResult


def neighbour_test(
    candidate: Candidate,
    final_selection: Selection,
    fixed: Mapping[str, SeriesRun],
) -> NeighbourResult:
    """SPEC 7.2 E neighbour test for a per-period-selected candidate.

    ``fixed`` must hold the fixed-parameter pooled OOS manual-delay runs of the candidate's points.
    """
    key = final_selection.chosen
    if key is None:
        gate = GateResult(
            "neighbours",
            False,
            None,
            None,
            "each neighbour Sharpe >= 0.7 x Sharpe(P)",
            "no eligible point on the final training window: the candidate is flat, fail",
        )
        return NeighbourResult(candidate.name, None, None, {}, gate)
    point = candidate.point(key)
    near = neighbours(point, candidate.points)
    reference = fixed[key].sharpe
    sharpes = {n.key: fixed[n.key].sharpe for n in near}
    return NeighbourResult(candidate.name, key, reference, sharpes, neighbour_gate(reference, sharpes))


# -- reuse of BTC's choices on other markets -----------------------------------------------------------


def index_schedule(
    schedule: Sequence[tuple[datetime, str | None]], timeframes: Mapping[str, Timeframe]
) -> list[tuple[datetime, str | None]]:
    """Indices have daily bars only: a period whose BTC choice is a 4h point is flat (SPEC 7.2 I)."""
    return [(s, None if k is not None and timeframes[k] is not Timeframe.D1 else k) for s, k in schedule]


def reuse_run(
    market: Market,
    result: CandidateResult,
    candidate: Candidate,
    factory: StrategyFactory,
    spec: WalkForwardSpec,
    mode: FillMode,
    costs: CostSpec,
    *,
    daily_only: bool = False,
) -> SeriesRun:
    """The candidate's BTC schedule replayed on ``market`` unchanged (ETH: all; indices: daily only)."""
    timeframes = {p.key: p.timeframe for p in candidate.points}
    schedule = list(result.schedule)
    if daily_only:
        schedule = index_schedule(schedule, timeframes)
    return pooled_run(market, schedule, factory, timeframes, spec, mode, costs, strategy_id=candidate.name)


def stress_run(
    market: Market,
    result: CandidateResult,
    candidate: Candidate,
    factory: StrategyFactory,
    spec: WalkForwardSpec,
    mode: FillMode = FillMode.MANUAL_DELAY,
    costs: CostSpec = STRESS_COSTS,
) -> SeriesRun:
    """The BTC schedule (selected under the normal costs) re-run with the stressed costs."""
    return reuse_run(market, result, candidate, factory, spec, mode, costs)


# -- Stooq index data ------------------------------------------------------------------------------------


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        return None
    return value if value > 0 else None


def parse_stooq_csv(text: str, symbol: str, *, before: datetime = HOLDOUT_START) -> list[Bar]:
    """Parse a Stooq daily CSV (``Date,Open,High,Low,Close[,Volume]``) into D1 bars.

    The bar of calendar date D closes at ``D + 1 day 00:00 UTC`` (the Binance daily-bar convention:
    ``ts`` is the close). Rows that cannot form a valid bar are dropped and rows with ``ts >= before`` never
    returned (the sealed-holdout rule is applied to every instrument, indices included).
    """
    out: list[Bar] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        norm = {(k or "").strip().lower(): v for k, v in row.items()}
        try:
            day = datetime.strptime(norm["date"].strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except (KeyError, ValueError):
            continue
        ohlc = [_dec(norm.get(f, "")) for f in ("open", "high", "low", "close")]
        if any(v is None for v in ohlc):
            continue
        o, h, lo, c = (v for v in ohlc if v is not None)
        ts = day + timedelta(days=1)
        if ts >= before or h < max(o, c, lo) or lo > min(o, c, h):
            continue
        volume = _dec(norm.get("volume", "")) or Decimal(0)
        out.append(Bar(symbol, Timeframe.D1, ts, o, h, lo, c, volume))
    out.sort(key=lambda b: b.ts)
    unique: list[Bar] = []
    for bar in out:
        if not unique or bar.ts > unique[-1].ts:
            unique.append(bar)
    return unique
