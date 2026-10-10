"""Metrics against hand-computed values."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tbot.backtest.benchmarks import run_benchmarks, vol_match_weight
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.metrics import (
    annualized_vol,
    cagr,
    compute_metrics,
    max_drawdown,
    returns_from_equity,
    sharpe_ratio,
    sortino_ratio,
    trade_stats,
)
from tbot.core.types import Timeframe
from tests.backtest.helpers import Scripted, make_config
from tests.backtest.test_engine import five_bars
from tests.sim_helpers import bars_from_closes

Dec = Decimal
CURVE = [100.0, 110.0, 99.0, 99.0, 108.9]  # returns +10 %, -10 %, 0 %, +10 %
STD = 0.09574271077563382  # sqrt(0.0275 / 3): sample std of the four returns above


def test_returns() -> None:
    assert returns_from_equity(CURVE) == pytest.approx([0.1, -0.1, 0.0, 0.1])
    assert returns_from_equity([0.0, 5.0]) == [0.0]  # no division by a non-positive base
    assert returns_from_equity([5.0]) == []


def test_ratios_hand_computed_daily_365() -> None:
    r = returns_from_equity(CURVE)
    # mean 0.025; sample variance (0.075^2 + 0.125^2 + 0.025^2 + 0.075^2) / 3 = 0.0275 / 3
    assert annualized_vol(r, 365) == pytest.approx(STD * 365**0.5)
    assert sharpe_ratio(r, 365) == pytest.approx(0.025 / STD * 365**0.5)
    # downside deviation: sqrt(0.1^2 / 4) = 0.05
    assert sortino_ratio(r, 365) == pytest.approx(0.025 / 0.05 * 365**0.5)
    assert annualized_vol(r, 8760) == pytest.approx(STD * 8760**0.5)  # hourly uses 8760


def test_undefined_ratios_are_none_not_zero() -> None:
    assert sharpe_ratio([0.01, 0.01, 0.01], 365) is None  # zero variance
    assert sortino_ratio([0.01, 0.02], 365) is None  # no downside
    assert annualized_vol([0.01], 365) is None
    assert sharpe_ratio([], 365) is None and sortino_ratio([], 365) is None
    assert cagr([100.0], 365) is None


def test_cagr() -> None:
    assert cagr(CURVE, 365) == pytest.approx(1.089 ** (365 / 4) - 1)
    assert cagr([100.0] * 366, 365) == pytest.approx(0.0)
    assert cagr([100.0] + [200.0] * 365, 365) == pytest.approx(1.0)  # doubled over exactly one year
    assert cagr([100.0, 0.0], 365) == -1.0


def test_max_drawdown_depth_and_duration() -> None:
    assert max_drawdown(CURVE) == pytest.approx((0.1, 3))  # never recovers: runs to the last bar
    # touching the previous peak again ends the underwater stretch
    assert max_drawdown([100.0, 90.0, 100.0, 95.0, 120.0]) == pytest.approx((0.1, 1))
    assert max_drawdown([1.0, 2.0, 3.0]) == (0.0, 0)
    assert max_drawdown([100.0, 80.0, 70.0, 90.0, 100.0, 110.0]) == pytest.approx((0.3, 3))


def test_trade_stats() -> None:
    res = run_backtest(
        BacktestFeed(bars_from_closes([100, 100, 120, 120, 100, 100, 100])),
        Scripted([1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0]),
        make_config(fee="0", slip="0"),
    )
    stats = trade_stats(res.trades)
    # trade 1: buy 100 @ 100, sell @ 120 (opens equal the previous close): +2000; trade 2: flat
    assert stats.n_trades == 2
    assert stats.win_rate == 0.5
    assert float(res.trades[0].pnl) == pytest.approx(2000.0)
    assert stats.expectancy == pytest.approx(float(res.trades[0].pnl + res.trades[1].pnl) / 2)
    if res.trades[1].pnl < 0:
        assert stats.profit_factor == pytest.approx(2000.0 / -float(res.trades[1].pnl))
    assert stats.avg_win == pytest.approx(2000.0)
    assert trade_stats(()).n_trades == 0 and trade_stats(()).win_rate is None


def test_compute_metrics_on_the_five_bar_example() -> None:
    res = run_backtest(BacktestFeed(five_bars()), Scripted([0.5, 0.55, 0.0, 0.4, 0.4]), make_config())
    m = compute_metrics(res)
    eq = [10000.0, 10924.75, 9424.75, 9357.475, 9498.1424572725]
    assert m.n_periods == 4 and m.periods_per_year == 365
    assert m.start_equity == 10000.0 and m.end_equity == pytest.approx(eq[-1])
    assert m.total_return == pytest.approx(eq[-1] / eq[0] - 1)
    assert m.cagr == pytest.approx((eq[-1] / eq[0]) ** (365 / 4) - 1)
    assert m.max_drawdown == pytest.approx(1 - 9357.475 / 10924.75)
    assert m.max_dd_duration_bars == 3
    assert m.cagr is not None and m.calmar == pytest.approx(m.cagr / m.max_drawdown)
    assert m.n_fills == 3
    assert m.fees_paid == pytest.approx(66.4270972275)
    assert m.slippage_cost == pytest.approx(132.4298955)
    assert m.time_in_market == pytest.approx(3 / 4)  # holding at closes 1, 2 and 4; flat at close 3
    weights = [6000 / 10924.75, 4500 / 9424.75, 0.0, 39.39989 * 100 / eq[-1]]
    assert m.avg_exposure == pytest.approx(sum(weights) / 4)
    years = 4 / 365
    assert m.turnover_sides_per_year == pytest.approx(3 / years)
    notional = 5050 + 4455 + 3780.4194455
    assert m.turnover_notional_per_year == pytest.approx(notional / (sum(eq) / 5) / years)
    assert m.cost_drag_annual == pytest.approx((66.4270972275 + 132.4298955) / (sum(eq) / 5) / years)
    assert m.trades.n_trades == 1 and m.trades.win_rate == 0.0
    assert m.trades.expectancy == pytest.approx(-642.525)
    assert m.trades.avg_loss == pytest.approx(-642.525)
    assert m.trades.profit_factor == 0.0  # no winners, some loss


def test_hourly_metrics_annualise_with_8760() -> None:
    bars = bars_from_closes([100, 101, 100, 102, 101], tf=Timeframe.H1)
    cfg = make_config(tf=Timeframe.H1, fee="0", slip="0")
    m = compute_metrics(run_backtest(BacktestFeed(bars), Scripted([1.0]), cfg))
    assert m.periods_per_year == 8760


def test_metrics_start_at_the_first_decision_bar() -> None:
    bars = bars_from_closes([100, 110, 120, 130, 140, 150])
    res = run_backtest(BacktestFeed(bars), Scripted([1.0], warmup_bars=3), make_config(fee="0", slip="0"))
    assert res.first_decision_index == 2
    m = compute_metrics(res)
    assert m.n_periods == 3  # closes of bars 2..5 give three returns; the warm-up is excluded
    assert m.start_equity == 10000.0  # nothing was held before the first decision


def test_vol_match_weight() -> None:
    assert vol_match_weight(0.5, 1.0) == (0.5, False)
    assert vol_match_weight(2.0, 1.0) == (1.0, True)
    assert vol_match_weight(0.0, 1.0) == (0.0, False)
    assert vol_match_weight(None, 1.0) == (0.0, False)
    assert vol_match_weight(1.0, 0.0) == (0.0, False)


def test_benchmarks_share_bars_costs_and_start() -> None:
    closes = [100, 104, 99, 103, 97, 105, 101, 108, 100, 109, 103, 111]
    feed = BacktestFeed(bars_from_closes(closes))
    cfg = make_config(fee="35", slip="5", buffer="10")  # buffer: weight-1.0 buys must survive tick rounding
    strat = Scripted([0.3], warmup_bars=3)
    res = run_backtest(feed, strat, cfg)
    bench = run_benchmarks(feed, strat, cfg, res)
    for other in (bench.buy_and_hold, bench.vol_matched):
        assert other.first_decision_index == res.first_decision_index == 2
        assert other.n_bars == res.n_bars
    bh, vm, s = compute_metrics(bench.buy_and_hold), compute_metrics(bench.vol_matched), compute_metrics(res)
    assert bh.fees_paid > 0  # benchmarks pay the same costs
    assert s.vol is not None and bh.vol is not None and vm.vol is not None
    assert bench.vol_match_weight == pytest.approx(min(1.0, s.vol / bh.vol))
    assert 0.0 < bench.vol_match_weight < 1.0 and not bench.capped
    assert vm.vol < bh.vol
    assert vm.vol == pytest.approx(s.vol, rel=0.25)  # same volatility up to drift and rounding


def test_buy_and_hold_benchmark_matches_a_manual_calculation() -> None:
    bars = bars_from_closes([100, 110, 121, 133.1])
    cfg = make_config(fee="0", slip="0")
    strat = Scripted([0.0])
    feed = BacktestFeed(bars)
    bench = run_benchmarks(feed, strat, cfg, run_backtest(feed, strat, cfg))
    # signal at close 0 (100) -> fill at the open of bar 1; 10 000 / 100 = 100 units
    first_fill = bench.buy_and_hold.fills[0].fill
    assert first_fill.quantity == Dec("100") and first_fill.price == Dec("100")
    assert bench.buy_and_hold.equity[-1].equity == Dec("100") * Dec("133.1")
    assert bench.vol_match_weight == 0.0 and bench.vol_matched.fills == ()
