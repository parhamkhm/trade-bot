"""Robustness helpers: neighbours, index schedule, Stooq parsing, stress and reuse (SPEC 7.2 E, I)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tbot.core.types import Timeframe
from tbot.validation.grid import HOLDOUT_START
from tbot.validation.robustness import (
    index_schedule,
    neighbour_test,
    parse_stooq_csv,
    reuse_run,
    stress_run,
)
from tbot.validation.runs import NORMAL_COSTS, STRESS_COSTS, FillMode, SeriesRun
from tbot.validation.walk_forward import CandidateResult, Selection, run_candidate, run_fixed_points
from tests.validation.wf_fixtures import (
    CANDIDATE,
    FIRST_TRADABLE,
    POINTS,
    SPEC,
    factory,
    fake_run,
    make_market,
)

D1, H4 = Timeframe.D1, Timeframe.H4
T0 = datetime(2019, 1, 1, tzinfo=UTC)
CLOSES = [T0 + timedelta(days=i + 1) for i in range(11)]


def _selection(chosen: str | None) -> Selection:
    return Selection(
        train_end=SPEC.final_train_end,
        chosen=chosen,
        eligible=tuple(p.key for p in POINTS),
        common_start=None,
        n_days=100,
        sharpes={},
        tied=(chosen,) if chosen else (),
    )


def _runs(mean: dict[str, float]) -> dict[str, SeriesRun]:
    out: dict[str, SeriesRun] = {}
    for key, m in mean.items():
        rets = [m + (0.01 if i % 2 else -0.01) for i in range(10)]
        out[key] = fake_run(CLOSES, rets)
    return out


# -- neighbour test ------------------------------------------------------------------------------------


def test_neighbour_test_uses_the_plus_minus_one_step_neighbours_of_p() -> None:
    fixed = _runs({"T-sma10": 0.003, "T-sma20": 0.004, "T-sma40": 0.003})
    res = neighbour_test(CANDIDATE, _selection("T-sma20"), fixed)
    assert res.point_key == "T-sma20"
    assert set(res.neighbour_sharpes) == {"T-sma10", "T-sma40"}
    assert res.reference_sharpe == fixed["T-sma20"].sharpe
    assert res.gate.passed  # 0.003 vs 0.004: both clearly above 70 % of the reference Sharpe


def test_a_neighbour_that_collapses_fails_the_gate() -> None:
    fixed = _runs({"T-sma10": 0.003, "T-sma20": 0.004, "T-sma40": 0.0005})
    res = neighbour_test(CANDIDATE, _selection("T-sma20"), fixed)
    assert not res.gate.passed and "T-sma40" in res.gate.note


def test_edge_point_has_a_single_neighbour() -> None:
    fixed = _runs({"T-sma10": 0.004, "T-sma20": 0.004, "T-sma40": 0.0})
    res = neighbour_test(CANDIDATE, _selection("T-sma10"), fixed)
    assert set(res.neighbour_sharpes) == {"T-sma20"}


def test_flat_final_selection_fails_with_a_reason() -> None:
    res = neighbour_test(CANDIDATE, _selection(None), {})
    assert res.point_key is None and not res.gate.passed and "flat" in res.gate.note


def test_non_positive_reference_sharpe_fails() -> None:
    fixed = _runs({"T-sma10": 0.003, "T-sma20": -0.004, "T-sma40": 0.003})
    assert not neighbour_test(CANDIDATE, _selection("T-sma20"), fixed).gate.passed


# -- index schedule --------------------------------------------------------------------------------------


def test_index_schedule_flattens_non_daily_choices_only() -> None:
    t = {"a": D1, "b": H4}
    sched = [(CLOSES[0], "a"), (CLOSES[1], "b"), (CLOSES[2], None)]
    assert index_schedule(sched, t) == [(CLOSES[0], "a"), (CLOSES[1], None), (CLOSES[2], None)]


# -- Stooq parsing ---------------------------------------------------------------------------------------

CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2024-01-02,100,110,95,105,1000\n"
    "2024-01-03,105,112,101,111,\n"
    "2024-01-04,111,0,101,111,5\n"  # non-positive high: dropped
    "2024-01-05,111,100,101,105,5\n"  # high below close: dropped
    "not-a-date,1,2,1,2,3\n"  # dropped
    "2024-01-02,1,2,1,2,3\n"  # duplicate date: the first row wins
    "2025-09-30,200,210,190,205,7\n"  # closes exactly at the holdout start: dropped
    "2025-09-29,200,210,190,205,7\n"
)


def test_stooq_bar_closes_one_day_after_its_date() -> None:
    bars = parse_stooq_csv(CSV, "SPX")
    assert bars[0].ts == datetime(2024, 1, 3, tzinfo=UTC)
    assert bars[0].open == Decimal(100) and bars[0].close == Decimal(105)
    assert all(b.timeframe is D1 for b in bars)


def test_stooq_invalid_rows_and_duplicates_are_dropped_and_sorted() -> None:
    bars = parse_stooq_csv(CSV, "SPX")
    assert [b.ts.date().isoformat() for b in bars] == ["2024-01-03", "2024-01-04", "2025-09-30"]
    assert bars[1].volume == Decimal(0)  # blank volume -> 0
    assert bars[0].open == Decimal(100)  # not the duplicate's 1


def test_stooq_never_returns_a_bar_closing_at_or_after_the_holdout_start() -> None:
    bars = parse_stooq_csv(CSV, "SPX")
    assert all(b.ts < HOLDOUT_START for b in bars)
    assert bars[-1].ts == datetime(2025, 9, 30, tzinfo=UTC)  # date 2025-09-29 closes 09-30 00:00, kept
    custom = parse_stooq_csv(CSV, "SPX", before=datetime(2024, 1, 4, tzinfo=UTC))
    assert [b.ts for b in custom] == [datetime(2024, 1, 3, tzinfo=UTC)]


def test_stooq_header_case_and_whitespace_are_tolerated() -> None:
    text = " date , Open,HIGH,low ,CLOSE\n2024-01-02,1,2,1,2\n"
    assert len(parse_stooq_csv(text, "X")) == 1


# -- reuse and stress on the stand-in family --------------------------------------------------------------


@pytest.fixture(scope="module")
def btc_result() -> CandidateResult:
    market = make_market()
    fixed = run_fixed_points(market, POINTS, factory, (FillMode.MANUAL_DELAY,), NORMAL_COSTS)
    return run_candidate(
        CANDIDATE,
        market,
        SPEC,
        factory=factory,
        first_tradable=FIRST_TRADABLE,
        fixed=fixed,
        costs=NORMAL_COSTS,
    )


def test_reuse_keeps_the_btc_schedule_and_does_not_reselect(btc_result: CandidateResult) -> None:
    eth = make_market(seed=13, symbol="ETHUSDT")
    run = reuse_run(eth, btc_result, CANDIDATE, factory, SPEC, FillMode.NEXT_OPEN, NORMAL_COSTS)
    assert run.return_ts[0] >= SPEC.first_test_start
    assert run.return_ts[-1] < SPEC.last_test_end


def test_stress_run_costs_more_than_the_normal_run(btc_result: CandidateResult) -> None:
    market = make_market()
    normal = reuse_run(market, btc_result, CANDIDATE, factory, SPEC, FillMode.MANUAL_DELAY, NORMAL_COSTS)
    stressed = stress_run(market, btc_result, CANDIDATE, factory, SPEC, STRESS_COSTS)
    assert stressed.n_fills == normal.n_fills  # same schedule, same trades ...
    assert stressed.fees_paid + stressed.slippage_cost > normal.fees_paid + normal.slippage_cost  # ... dearer
    assert STRESS_COSTS.fee_bps == 2 * NORMAL_COSTS.fee_bps
    assert STRESS_COSTS.slippage_bps == 2 * NORMAL_COSTS.slippage_bps
