"""Walk-forward harness (docs/SPEC.md section 7.2 B, E): calendar, selection, pooled series, benchmark.

Two kinds of tests. Unit tests of ``select`` use hand-made return series, so the expected choice is derived by
hand. Engine tests use the stand-in family ``T`` (SMA filters, see ``wf_fixtures``) on seeded synthetic bars.

The two truncation tests are the harness' causality proof:

* ``test_choices_ignore_data_after_the_period_start``: replace every bar that closes at or after a
  period start
  with other data; the choices for that period and all earlier ones must not change (and the choice inputs for
  later periods MUST change, otherwise the test would have no power).
* ``test_pooled_series_is_a_prefix_of_the_longer_run``: the whole candidate run on data cut at the end of
  period
  2 equals the first two periods of the run on the longer data, in both fill modes.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from tbot.core.types import BarWindow, Strategy, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.validation.grid import HOLDOUT_START, SPEC_PERIODS, make_periods
from tbot.validation.runs import (
    NORMAL_COSTS,
    STRESS_COSTS,
    BlendStrategy,
    CompositeStrategy,
    FillMode,
    Market,
    SeriesRun,
    run_fixed,
)
from tbot.validation.stats import annualised_sharpe
from tbot.validation.walk_forward import (
    Candidate,
    CandidateResult,
    Selection,
    WalkForwardSpec,
    _baseline,
    _end,
    anchor_schedule,
    benchmark_runs,
    blend_run,
    pooled_run,
    run_candidate,
    run_fixed_points,
    select,
    walk_forward_selections,
)
from tests.validation.wf_fixtures import (
    BOUNDARY,
    CANDIDATE,
    FIRST_TRADABLE,
    POINTS,
    SPEC,
    StopAwareSma,
    factory,
    fake_run,
    make_market,
)

D1 = Timeframe.D1
MD, NO = FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN
T0 = datetime(2019, 1, 1, tzinfo=UTC)
Fixed = Mapping[tuple[str, FillMode], SeriesRun]


def _closes(n: int) -> list[datetime]:
    return [T0 + timedelta(days=i + 1) for i in range(n)]


def _fixed(closes: list[datetime], series: dict[str, list[float]]) -> dict[str, SeriesRun]:
    """Each series is aligned to the LAST ``len(series)`` closes (shorter = later start)."""
    out: dict[str, SeriesRun] = {}
    for key, rets in series.items():
        sub = closes[len(closes) - len(rets) - 1 :]
        out[key] = fake_run(sub, rets)
    return out


def _noise(n: int, mean: float, sd: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


# ---------------------------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------------------------


def test_spec_calendar_is_twelve_periods_ending_at_the_holdout() -> None:
    assert len(SPEC_PERIODS) == 12
    assert SPEC_PERIODS[0].start == datetime(2020, 1, 1, tzinfo=UTC)
    assert SPEC_PERIODS[-2].start == datetime(2025, 1, 1, tzinfo=UTC)
    assert SPEC_PERIODS[-1].start == datetime(2025, 7, 1, tzinfo=UTC)
    assert SPEC_PERIODS[-1].end == HOLDOUT_START == datetime(2025, 10, 1, tzinfo=UTC)
    assert all(a.end == b.start for a, b in pairwise(SPEC_PERIODS))


def test_make_periods_rejects_unsorted_starts() -> None:
    with pytest.raises(ValueError, match="strictly increase"):
        make_periods([BOUNDARY, T0], BOUNDARY + timedelta(days=30))


# ---------------------------------------------------------------------------------------------
# select (hand-made series)
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def closes() -> list[datetime]:
    return _closes(500)


def _sel(
    cand: Candidate,
    train_end: datetime,
    first: dict[str, datetime],
    closes: list[datetime],
    fixed: dict[str, SeriesRun],
    min_days: int = 60,
) -> Selection:
    return select(
        cand, train_end, first_tradable=first, daily_closes=closes, fixed=fixed, min_train_days=min_days
    )


def test_all_tied_goes_to_the_base_point(closes: list[datetime]) -> None:
    same = _noise(499, 0.001, 0.01, 1)
    fixed = _fixed(closes, {p.key: same for p in POINTS})
    first = {p.key: closes[0] for p in POINTS}
    sel = _sel(CANDIDATE, closes[400], first, closes, fixed)
    assert sel.chosen == "T-sma20"  # the base point (position 1), not position 0
    assert set(sel.tied) == {p.key for p in POINTS}


def test_tie_goes_to_lowest_position_when_the_base_is_not_eligible(closes: list[datetime]) -> None:
    same = _noise(499, 0.001, 0.01, 2)
    fixed = _fixed(closes, {p.key: same for p in POINTS})
    first = {"T-sma10": closes[0], "T-sma20": closes[470], "T-sma40": closes[0]}
    sel = _sel(CANDIDATE, closes[400], first, closes, fixed)
    assert "T-sma20" not in sel.eligible
    assert sel.chosen == "T-sma10"


def test_a_clear_winner_beats_the_base_point(closes: list[datetime]) -> None:
    fixed = _fixed(
        closes,
        {
            "T-sma10": _noise(499, 0.0, 0.01, 3),
            "T-sma20": _noise(499, 0.0, 0.01, 4),
            "T-sma40": _noise(499, 0.01, 0.01, 5),
        },
    )
    first = {p.key: closes[0] for p in POINTS}
    sel = _sel(CANDIDATE, closes[400], first, closes, fixed)
    assert sel.chosen == "T-sma40"
    assert sel.tied == ("T-sma40",)


def test_no_eligible_point_means_flat(closes: list[datetime]) -> None:
    fixed = _fixed(closes, {p.key: _noise(499, 0.001, 0.01, 6) for p in POINTS})
    first = {p.key: closes[0] for p in POINTS}
    sel = _sel(CANDIDATE, closes[30], first, closes, fixed)  # only 30 training closes < 60
    assert sel.chosen is None
    assert sel.eligible == ()


def test_selection_compares_points_on_their_common_days_only(closes: list[datetime]) -> None:
    n = 499
    early = _noise(200, 0.012, 0.01, 7)
    late_a = _noise(n - 200, -0.002, 0.01, 8)
    series_a = early + late_a
    series_b = _noise(n - 200, 0.002, 0.01, 9)
    # B only exists from close #200 on (its returns are the last n-200 of the grid).
    fixed = {
        "T-sma10": fake_run(closes[: n + 1], series_a),
        "T-sma20": fake_run(closes[200 : n + 1], series_b),
        "T-sma40": fake_run(closes[: n + 1], _noise(n, -0.01, 0.01, 10)),
    }
    first = {"T-sma10": closes[0], "T-sma20": closes[200], "T-sma40": closes[0]}
    train_end = closes[450]
    full_a = annualised_sharpe(fixed["T-sma10"].returns_between(closes[0], train_end), 365.0)
    full_b = annualised_sharpe(fixed["T-sma20"].returns_between(closes[200], train_end), 365.0)
    common_a = annualised_sharpe(fixed["T-sma10"].returns_between(closes[200], train_end), 365.0)
    assert full_a is not None and full_b is not None and common_a is not None
    assert full_a > full_b  # on its own history A looks better ...
    assert common_a < full_b  # ... but on the common days B is better
    sel = _sel(CANDIDATE, train_end, first, closes, fixed)
    assert sel.chosen == "T-sma20"
    assert sel.common_start == closes[200]


def test_select_never_looks_at_returns_on_or_after_the_training_end(closes: list[datetime]) -> None:
    base = {p.key: _noise(499, 0.001 * (i + 1), 0.01, 20 + i) for i, p in enumerate(POINTS)}
    first = {p.key: closes[0] for p in POINTS}
    train_end = closes[300]
    ref = _sel(CANDIDATE, train_end, first, closes, _fixed(closes, base))
    poisoned = {k: v[:299] + [5.0 if k == "T-sma10" else -0.9] * (len(v) - 299) for k, v in base.items()}
    got = _sel(CANDIDATE, train_end, first, closes, _fixed(closes, poisoned))
    assert got == ref  # identical, including every Sharpe it printed
    # sanity: the poison does change the selection once the window is allowed to see it
    later = _sel(CANDIDATE, closes[450], first, closes, _fixed(closes, poisoned))
    assert later.sharpes != _sel(CANDIDATE, closes[450], first, closes, _fixed(closes, base)).sharpes


def test_an_undefined_sharpe_is_scored_zero_not_dropped(closes: list[datetime]) -> None:
    fixed = _fixed(
        closes,
        {
            "T-sma10": [0.0] * 499,  # flat: Sharpe undefined
            "T-sma20": _noise(499, -0.003, 0.01, 30),  # negative
            "T-sma40": _noise(499, -0.003, 0.01, 31),
        },
    )
    first = {p.key: closes[0] for p in POINTS}
    sel = _sel(CANDIDATE, closes[400], first, closes, fixed)
    assert sel.sharpes["T-sma10"] == 0.0
    assert sel.chosen == "T-sma10"  # a flat point beats two losing ones


# ---------------------------------------------------------------------------------------------
# engine-level: composite, benchmark, blend
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def market() -> Market:
    return make_market()


@pytest.fixture(scope="module")
def fixed_runs(market: Market) -> Fixed:
    return run_fixed_points(market, POINTS, factory, (MD,), NORMAL_COSTS)


def test_composite_is_flat_before_its_first_start_and_while_the_key_is_none(market: Market) -> None:
    sched = [(SPEC.periods[0].start, None), (BOUNDARY, "T-sma10")]
    run = pooled_run(market, sched, factory, SPEC, NO, NORMAL_COSTS, strategy_id="x")
    before = run.returns_between(SPEC.periods[0].start, BOUNDARY)
    assert before and all(r == 0.0 for r in before)
    assert run.n_fills >= 1  # it did trade once the key appeared


@pytest.mark.parametrize("mode", [NO, MD])
@pytest.mark.parametrize("costs", [NORMAL_COSTS, STRESS_COSTS])
def test_pooled_series_of_one_point_is_a_slice_of_its_fixed_anchored_run(
    market: Market, mode: FillMode, costs: object
) -> None:
    """SPEC 7.1: folds never restart the strategy, they only slice the replayed series.

    A candidate that holds one point in every period must reproduce ``run_fixed`` (the same engine from the
    data start) bar for bar on the test days: same equity, same fills inside the slice.
    """
    from tbot.validation.runs import CostSpec

    assert isinstance(costs, CostSpec)
    key = "T-sma10"
    sched = [(p.start, key) for p in SPEC.periods]
    pooled = pooled_run(market, sched, factory, SPEC, mode, costs, strategy_id="x")
    baseline, end = _baseline(market, SPEC.first_test_start), _end(market, SPEC)
    fixed = run_fixed(market, factory(key), D1, mode, costs, end=end)
    keep = [i for i, t in enumerate(fixed.ts) if t >= baseline]
    assert pooled.ts == tuple(fixed.ts[i] for i in keep)
    assert pooled.equity == tuple(fixed.equity[i] for i in keep)  # exact: the very same engine arithmetic
    # the run had history before the slice (otherwise this test would not distinguish anchoring)
    assert keep[0] > 100
    assert pooled.n_fills <= fixed.n_fills
    assert pooled.fees_paid <= fixed.fees_paid + 1e-9


def test_a_position_entered_before_the_first_test_day_is_carried_into_it(market: Market) -> None:
    """A flat-start replay is 0.0 on day one if no signal formed that day; the anchored one is invested."""
    key = "T-sma10"
    anchored = pooled_run(market, [(SPEC.periods[0].start, key)], factory, SPEC, NO, NORMAL_COSTS)
    baseline = _baseline(market, SPEC.first_test_start)
    fixed = run_fixed(market, factory(key), D1, NO, NORMAL_COSTS, end=_end(market, SPEC))
    entries_before = fixed.n_fills - anchored.n_fills
    assert entries_before >= 1  # at least one trade happened before 2020-01-01 and is NOT in the slice
    assert anchored.ts[0] == baseline


def test_anchor_schedule_applies_the_first_choice_from_the_data_start(market: Market) -> None:
    sched = [(SPEC.periods[0].start, "a"), (BOUNDARY, "b")]
    anchored = anchor_schedule(market, sched)
    assert anchored[0] == (market.anchor, "a")
    assert anchored[1:] == sched[1:]
    assert market.anchor < SPEC.first_test_start
    with pytest.raises(ValueError, match="empty"):
        anchor_schedule(market, [])


def test_key_at_and_the_schedule_validation(market: Market) -> None:
    comp = CompositeStrategy(
        strategy_id="c",
        market=market,
        schedule=[(BOUNDARY, "T-sma10")],
        factory=factory,
    )
    assert comp.key_at(BOUNDARY - timedelta(seconds=1)) is None
    assert comp.key_at(BOUNDARY) == "T-sma10"
    with pytest.raises(ValueError, match="strictly increasing"):
        CompositeStrategy(
            strategy_id="c",
            market=market,
            schedule=[(BOUNDARY, "a"), (BOUNDARY, "b")],
            factory=factory,
        )


class _AwareBuyAndHold(BuyAndHold):
    """Constant-weight stand-in that is ``StopOutAware`` (it never reports a stop-out)."""

    def stopped_out_on(self, window: BarWindow) -> bool:  # noqa: ARG002
        return False


class _NoStopSma(StopAwareSma):
    """SMA filter whose protective stop sits at 1 % of the entry price, so it never triggers."""

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 100


def _no_stop_sma(key: str) -> Strategy:
    return _NoStopSma(int(key.removeprefix("T-sma")))


def _const(weights: dict[str, float]) -> Callable[[str], Strategy]:
    def make(key: str) -> Strategy:
        # stop_fraction 0.99: the stand-in's protective stop (engine-enforced since PR #26) sits at 1 % of
        # the entry price, so it can never trigger and these tests isolate the boundary trade
        return _AwareBuyAndHold(weight=weights[key], stop_fraction=0.99)

    return make


def test_a_parameter_change_at_a_boundary_is_charged_as_a_trade(market: Market) -> None:
    fac = _const({"a": 1.0, "b": 0.4})
    only_a = pooled_run(market, [(SPEC.periods[0].start, "a")], fac, SPEC, NO, NORMAL_COSTS)
    switch = [(SPEC.periods[0].start, "a"), (BOUNDARY, "b")]
    a_then_b = pooled_run(market, switch, fac, SPEC, NO, NORMAL_COSTS)
    assert only_a.n_fills == 0  # entered at the anchor, long before the slice: no trade inside it
    # the cut from 1.0 to 0.4 at the boundary is a sale inside the slice (later band rebalances may add more)
    assert a_then_b.n_fills >= 1
    assert a_then_b.fees_paid > only_a.fees_paid


def test_blend_target_is_the_equal_weight_mean(market: Market) -> None:
    fac = _const({"a": 1.0, "b": 0.0})
    members = [
        CompositeStrategy(
            strategy_id=f"m{i}",
            market=market,
            schedule=[(SPEC.periods[0].start, key)],
            factory=fac,
        )
        for i, key in enumerate(("a", "b"))
    ]
    blend = BlendStrategy("S1", members)
    window = market.window_to(D1, SPEC.periods[1].start)
    assert window is not None
    assert blend.on_bar(window).target_weight == pytest.approx(0.5)
    with pytest.raises(ValueError, match="at least one member"):
        BlendStrategy("S1", [])


def test_blend_run_matches_a_single_member_when_only_one_member_is_given(market: Market) -> None:
    sched = [(SPEC.periods[0].start, "T-sma10")]
    single = pooled_run(market, sched, _no_stop_sma, SPEC, NO, NORMAL_COSTS)
    blended = blend_run(market, [sched], _no_stop_sma, SPEC, NO, NORMAL_COSTS)
    assert blended.returns == single.returns


def test_buy_and_hold_enters_once_and_is_never_rebalanced_or_stopped(market: Market) -> None:
    runs = benchmark_runs(market, SPEC, NORMAL_COSTS, (MD, NO))
    for mode, run in runs.items():
        assert run.n_fills == 1, mode
        assert run.n_stop_events == 0, mode
        # one side of costs on a fully invested book: 10 bps fee + 5 bps slippage on ~all of the equity
        entry_cost = (run.fees_paid + run.slippage_cost) / run.equity[0]
        assert 0.0010 < entry_cost < 0.0016, (mode, entry_cost)
        assert len(run.returns) == len(
            [c for c in market.daily_closes if SPEC.first_test_start <= c < SPEC.last_test_end]
        )


def test_buy_and_hold_pays_costs_on_entry_only(market: Market) -> None:
    run = benchmark_runs(market, SPEC, NORMAL_COSTS, (NO,))[NO]
    closes = [b.close for b in market.series(D1)]
    ts = [b.ts for b in market.series(D1)]
    i0, i1 = ts.index(run.ts[0]), ts.index(run.ts[-1])
    price_ratio = float(closes[i1] / closes[i0])
    equity_ratio = run.equity[-1] / run.equity[0]
    # The strategy return differs from the price return only by the entry cost (<= 0.2 % of equity) and the
    # unfilled gap between the baseline close and the first fill; 25 % band would show up as extra fills.
    assert run.n_fills == 1
    assert equity_ratio == pytest.approx(price_ratio, rel=0.08)


# ---------------------------------------------------------------------------------------------
# run_candidate
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def result(market: Market, fixed_runs: Fixed) -> CandidateResult:
    return run_candidate(
        CANDIDATE,
        market,
        SPEC,
        factory=factory,
        first_tradable=FIRST_TRADABLE,
        fixed=fixed_runs,
        costs=NORMAL_COSTS,
    )


def test_candidate_result_shape(result: CandidateResult, market: Market) -> None:
    n_days = len([c for c in market.daily_closes if SPEC.first_test_start <= c < SPEC.last_test_end])
    assert len(result.selections) == len(SPEC.periods) == len(result.schedule)
    for mode in (MD, NO):
        run = result.pooled[mode]
        assert len(run.returns) == n_days
        assert run.return_ts[0] == SPEC.first_test_start
        assert run.returns[0] == 0.0  # structurally zero: the first decision is only taken at that close
        assert run.return_ts[-1] == SPEC.last_test_end - timedelta(days=1)
    assert all(k in {p.key for p in POINTS} for k in result.chosen_keys if k is not None)


def test_final_selection_uses_the_window_ending_at_the_holdout_start(result: CandidateResult) -> None:
    assert result.final_selection.train_end == SPEC.final_train_end


def test_the_two_fill_modes_differ(result: CandidateResult) -> None:
    assert result.pooled[MD].returns != result.pooled[NO].returns


# ---------------------------------------------------------------------------------------------
# causality of the harness
# ---------------------------------------------------------------------------------------------


def test_choices_ignore_data_after_the_period_start(fixed_runs: Fixed) -> None:
    base = make_market()
    altered = make_market(tail_seed=99, tail_from=BOUNDARY)
    fixed_alt = run_fixed_points(altered, POINTS, factory, (MD,), NORMAL_COSTS)

    def choose(mkt: Market, fixed: Fixed) -> tuple[list[Selection], Selection]:
        return walk_forward_selections(
            CANDIDATE,
            SPEC,
            first_tradable=FIRST_TRADABLE,
            daily_closes=mkt.daily_closes,
            fixed={p.key: fixed[(p.key, MD)] for p in POINTS},
        )

    sel_a, final_a = choose(base, fixed_runs)
    sel_b, final_b = choose(altered, fixed_alt)

    # (1) the engine inputs of the selection are identical for every close before the boundary
    for p in POINTS:
        lo = SPEC.train_start
        assert fixed_runs[(p.key, MD)].returns_between(lo, BOUNDARY) == fixed_alt[
            (p.key, MD)
        ].returns_between(lo, BOUNDARY), p.key
    # (2) so the choices for every period starting at or before the boundary are identical, in full
    for i, period in enumerate(SPEC.periods):
        if period.start <= BOUNDARY:
            assert sel_a[i] == sel_b[i], period.label
    # (3) power: the later windows DO see the altered data, so their inputs differ
    assert sel_a[2].sharpes != sel_b[2].sharpes
    assert final_a.sharpes != final_b.sharpes


def test_pooled_series_is_a_prefix_of_the_longer_run(
    market: Market, fixed_runs: Fixed, result: CandidateResult
) -> None:
    end = SPEC.periods[1].end  # 2020-07-01
    last_close = max(c for c in market.daily_closes if c < end)
    short_market = market.truncated(last_close)
    spec2 = WalkForwardSpec(
        periods=SPEC.periods[:2],
        train_start=SPEC.train_start,
        min_train_days=SPEC.min_train_days,
        final_train_end=end,
    )
    short_fixed = run_fixed_points(short_market, POINTS, factory, (MD,), NORMAL_COSTS)
    short = run_candidate(
        CANDIDATE,
        short_market,
        spec2,
        factory=factory,
        first_tradable=FIRST_TRADABLE,
        fixed=short_fixed,
        costs=NORMAL_COSTS,
    )
    assert short.chosen_keys == result.chosen_keys[:2]
    for mode in (MD, NO):
        full_part = result.pooled[mode].returns_between(spec2.first_test_start, end)
        assert short.pooled[mode].returns == tuple(full_part), mode
