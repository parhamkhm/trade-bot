"""G2 independent metrics check: Sharpe, max drawdown and CAGR recomputed with separate code.

``tbot.backtest.metrics`` uses ``math.fsum`` loops. The recomputation here uses numpy / pandas vector
operations written from the definitions in the metrics module docstring (365-day annualisation, risk-free 0,
sample standard deviation, window starting at the close of the first decision bar). Agreement is required
to 1e-9 relative on real BTCUSDT 1d results, and on literal curves whose answers are worked out by hand.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
import pytest

from tbot.backtest.metrics import cagr, compute_metrics, max_drawdown, returns_from_equity, sharpe_ratio
from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars, synthetic_hourly_for_days
from tests.validation.harness import make_cfg, run

needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")
REL = 1e-9


def independent(equity: np.ndarray, periods_per_year: float) -> dict[str, float]:
    """Sharpe, maxDD, CAGR and total return from the equity values of the decision window."""
    rets = pd.Series(equity).pct_change().dropna()
    running_peak = np.maximum.accumulate(equity)
    return {
        "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(periods_per_year)),
        "max_drawdown": float(np.max(1.0 - equity / running_peak)),
        "cagr": float((equity[-1] / equity[0]) ** (periods_per_year / len(rets)) - 1.0),
        "total_return": float(equity[-1] / equity[0] - 1.0),
        "vol": float(rets.std(ddof=1) * np.sqrt(periods_per_year)),
    }


def window_equity(res: BacktestResult) -> np.ndarray:
    assert res.first_decision_index is not None
    return np.array([float(p.equity) for p in res.equity[res.first_decision_index :]])


def _daily_real() -> list[Bar]:
    return real_bars(
        "BTCUSDT", Timeframe.D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
    )


def _check(res: BacktestResult, ppy: float) -> dict[str, tuple[float, float]]:
    m = compute_metrics(res)
    mine = independent(window_equity(res), ppy)
    assert m.periods_per_year == ppy
    pairs = {
        "sharpe": (m.sharpe, mine["sharpe"]),
        "max_drawdown": (m.max_drawdown, mine["max_drawdown"]),
        "cagr": (m.cagr, mine["cagr"]),
        "total_return": (m.total_return, mine["total_return"]),
        "vol": (m.vol, mine["vol"]),
    }
    out: dict[str, tuple[float, float]] = {}
    for key, (theirs, ours) in pairs.items():
        assert theirs is not None
        assert math.isclose(theirs, ours, rel_tol=REL, abs_tol=1e-12), (
            f"{key}: module {theirs!r} vs mine {ours!r}"
        )
        out[key] = (theirs, ours)
    return out


@needs_store
@pytest.mark.parametrize("strategy_name", ["buy_and_hold", "sma100"])
def test_metrics_match_independent_recomputation_on_real_btc_daily(strategy_name: str) -> None:
    strategy = BuyAndHold() if strategy_name == "buy_and_hold" else SmaFilter(100)
    res = run(strategy, _daily_real(), make_cfg())
    _check(res, 365.0)


def test_metrics_match_independent_recomputation_on_synthetic_daily() -> None:
    res = run(SmaFilter(50), synthetic_bars(900, seed=4), make_cfg())
    _check(res, 365.0)


def test_hourly_run_is_annualised_with_8760_not_365() -> None:
    days = synthetic_bars(30, seed=8)
    hours = synthetic_hourly_for_days(days, seed=9)
    res = run(SmaFilter(48), hours, make_cfg(timeframe=Timeframe.H1))
    _check(res, 8760.0)


@needs_store
def test_annualisation_factor_is_365_for_daily_bars() -> None:
    """A guard against the classic 252 mistake: scaling by 252 instead of 365 would change Sharpe by 17 %."""
    res = run(SmaFilter(100), _daily_real(), make_cfg())
    m = compute_metrics(res)
    mine_252 = independent(window_equity(res), 252.0)["sharpe"]
    assert m.sharpe is not None
    assert not math.isclose(m.sharpe, mine_252, rel_tol=0.05)


def test_window_starts_at_the_first_decision_bar_not_at_bar_zero() -> None:
    res = run(SmaFilter(100), synthetic_bars(400, seed=2), make_cfg())
    m = compute_metrics(res)
    assert res.first_decision_index == 99
    assert m.n_periods == len(res.equity) - 99 - 1
    assert m.start_equity == float(res.equity[99].equity)


# ---------------------------------------------------------------------------------------------
# Literal curves with answers worked out by hand (no engine involved)
# ---------------------------------------------------------------------------------------------


def test_max_drawdown_by_hand() -> None:
    # peak 120, trough 90 -> 25 %; the earlier 100 -> 95 dip is only 5 %
    dd, longest = max_drawdown([100.0, 95.0, 110.0, 120.0, 90.0, 100.0, 119.0])
    assert dd == pytest.approx(0.25, abs=1e-12)
    assert longest == 3  # bars 4, 5, 6 are all below the 120 peak


def test_cagr_by_hand() -> None:
    # 366 equity points = 365 periods = exactly one year: CAGR equals the total return
    curve = [100.0] + [100.0] * 364 + [150.0]
    assert cagr(curve, 365.0) == pytest.approx(0.5, abs=1e-12)
    # 3 periods at 365/yr: (121/100)^(365/3) - 1
    assert cagr([100.0, 110.0, 110.0, 121.0], 365.0) == pytest.approx(1.21 ** (365.0 / 3.0) - 1.0, rel=1e-12)


def test_sharpe_by_hand() -> None:
    # returns +10 %, -10 %, +10 %: mean = 0.1/3, sample std = sqrt(((0.2/3)^2*2 + (0.4/3)^2)/2)
    curve = [100.0, 110.0, 99.0, 108.9]
    rets = returns_from_equity(curve)
    mean = (0.10 - 0.10 + 0.10) / 3
    std = math.sqrt(((0.10 - mean) ** 2 + (-0.10 - mean) ** 2 + (0.10 - mean) ** 2) / 2)
    assert sharpe_ratio(rets, 365.0) == pytest.approx(mean / std * math.sqrt(365.0), rel=1e-12)
    assert mean / std == pytest.approx(0.1 / 3 / 0.11547005383792516, rel=1e-9)


def test_undefined_ratios_are_none_not_zero() -> None:
    assert sharpe_ratio([0.0, 0.0, 0.0], 365.0) is None  # zero variance
    assert sharpe_ratio([0.01], 365.0) is None  # one observation
