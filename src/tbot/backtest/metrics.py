"""Performance metrics. Pure functions on equity curves, plus ``compute_metrics`` for a ``BacktestResult``.

Conventions (fixed here so reports are comparable):

* Annualisation uses ``Timeframe.periods_per_year`` (365 for 1d, 8760 for 1h, never 252). Risk-free rate 0.
* Periodic returns are simple returns between consecutive bar closes. ``vol = std(ddof=1) * sqrt(ppy)``,
  ``sharpe = mean / std * sqrt(ppy)``, ``sortino = mean / downside_dev * sqrt(ppy)`` with
  ``downside_dev = sqrt(mean(min(r, 0)^2))`` over ALL periods (target 0).
* ``cagr = (E_end / E_start) ** (ppy / n_periods) - 1``.
* ``max_drawdown`` is a positive fraction of the running peak; its duration is the longest stretch, in bars,
  between a peak and the first bar at or above it again (an unrecovered drawdown runs to the last bar).
* ``calmar = cagr / max_drawdown`` (``None`` if there was no drawdown).
* Undefined ratios (zero variance, too few periods) are ``None``, never 0 or inf, so reports cannot hide them.
* Exposure: ``time_in_market`` = share of periods that ended holding a position; ``avg_exposure`` = mean
  position weight at the period closes.
* Turnover: ``turnover_sides_per_year`` counts executed fills per year; ``turnover_notional_per_year`` is
  traded notional divided by mean equity, per year. ``cost_drag_annual`` = (fees + slippage) / mean equity
  / years.
* The comparison window starts at the close of the first decision bar (``first_decision_index``), so the
  strategy and its benchmarks are measured over the same bars (the warm-up is excluded).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from tbot.backtest.result import BacktestResult
from tbot.portfolio.ledger import TradeRecord

__all__ = [
    "Metrics",
    "TradeStats",
    "annualized_vol",
    "cagr",
    "compute_metrics",
    "downside_deviation",
    "max_drawdown",
    "returns_from_equity",
    "sharpe_ratio",
    "sortino_ratio",
    "trade_stats",
]


def returns_from_equity(equity: Sequence[float]) -> list[float]:
    """Simple returns between consecutive equity values. A non-positive previous value gives 0.0."""
    return [(b / a - 1.0) if a > 0 else 0.0 for a, b in pairwise(equity)]


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _std(xs: Sequence[float]) -> float | None:
    """Sample standard deviation (ddof=1); ``None`` below two observations."""
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def annualized_vol(returns: Sequence[float], periods_per_year: float) -> float | None:
    s = _std(returns)
    return None if s is None else s * math.sqrt(periods_per_year)


def sharpe_ratio(returns: Sequence[float], periods_per_year: float) -> float | None:
    s = _std(returns)
    if s is None or s == 0.0:
        return None
    return _mean(returns) / s * math.sqrt(periods_per_year)


def downside_deviation(returns: Sequence[float]) -> float | None:
    """Root mean square of the negative returns (target 0), over all periods."""
    if not returns:
        return None
    return math.sqrt(math.fsum(min(r, 0.0) ** 2 for r in returns) / len(returns))


def sortino_ratio(returns: Sequence[float], periods_per_year: float) -> float | None:
    dd = downside_deviation(returns)
    if dd is None or dd == 0.0:
        return None
    return _mean(returns) / dd * math.sqrt(periods_per_year)


def cagr(equity: Sequence[float], periods_per_year: float) -> float | None:
    n = len(equity) - 1
    if n < 1 or equity[0] <= 0:
        return None
    if equity[-1] <= 0:
        return -1.0
    return float((equity[-1] / equity[0]) ** (periods_per_year / n) - 1.0)


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    """(deepest drawdown as a positive fraction, longest underwater stretch in bars)."""
    peak = -math.inf
    worst = 0.0
    since_peak = 0
    longest = 0
    for value in equity:
        if value >= peak:
            peak = value
            since_peak = 0
        else:
            since_peak += 1
            longest = max(longest, since_peak)
            if peak > 0:
                worst = max(worst, 1.0 - value / peak)
    return worst, longest


@dataclass(frozen=True, slots=True)
class TradeStats:
    n_trades: int  # closed flat -> flat round trips
    win_rate: float | None
    expectancy: float | None  # mean net P&L per trade, quote asset
    profit_factor: float | None  # gross profit / gross loss; None without losers
    avg_win: float | None
    avg_loss: float | None


def trade_stats(trades: Sequence[TradeRecord]) -> TradeStats:
    if not trades:
        return TradeStats(0, None, None, None, None, None)
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -math.fsum(losses)
    return TradeStats(
        n_trades=len(trades),
        win_rate=len(wins) / len(pnls),
        expectancy=math.fsum(pnls) / len(pnls),
        profit_factor=(math.fsum(wins) / gross_loss) if gross_loss > 0 else None,
        avg_win=(math.fsum(wins) / len(wins)) if wins else None,
        avg_loss=(math.fsum(losses) / len(losses)) if losses else None,
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    n_periods: int
    periods_per_year: float
    start_equity: float
    end_equity: float
    total_return: float
    cagr: float | None
    vol: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    max_dd_duration_bars: int
    calmar: float | None
    time_in_market: float
    avg_exposure: float
    n_fills: int
    turnover_sides_per_year: float | None
    turnover_notional_per_year: float | None
    fees_paid: float
    slippage_cost: float
    cost_drag_annual: float | None
    trades: TradeStats


def _safe_div(a: float, b: float) -> float | None:
    return None if b == 0.0 else a / b


def compute_metrics(result: BacktestResult) -> Metrics:
    """Metrics for ``result`` over the window that starts at the close of the first decision bar."""
    start = result.first_decision_index or 0
    window = result.equity[start:]
    equity = [float(p.equity) for p in window]
    ppy = result.config.timeframe.periods_per_year
    rets = returns_from_equity(equity)
    n = len(rets)
    years = n / ppy if n else 0.0
    dd, dd_len = max_drawdown(equity)
    growth = cagr(equity, ppy)
    held = window[1:]
    start_ts = window[0].ts if window else None
    fills = [f for f in result.fills if start_ts is not None and f.fill.ts >= start_ts]
    fees = float(sum((f.fill.fee for f in fills), Decimal(0)))
    slip = float(sum((f.slippage_cost for f in fills), Decimal(0)))
    notional = float(sum((f.fill.price * f.fill.quantity for f in fills), Decimal(0)))
    mean_equity = _mean(equity) if equity else 0.0
    trades = [t for t in result.trades if start_ts is not None and t.entry_ts >= start_ts]
    return Metrics(
        n_periods=n,
        periods_per_year=ppy,
        start_equity=equity[0] if equity else 0.0,
        end_equity=equity[-1] if equity else 0.0,
        total_return=(equity[-1] / equity[0] - 1.0) if equity and equity[0] > 0 else 0.0,
        cagr=growth,
        vol=annualized_vol(rets, ppy),
        sharpe=sharpe_ratio(rets, ppy),
        sortino=sortino_ratio(rets, ppy),
        max_drawdown=dd,
        max_dd_duration_bars=dd_len,
        calmar=None if growth is None else _safe_div(growth, dd),
        time_in_market=(sum(1 for p in held if p.quantity > 0) / len(held)) if held else 0.0,
        avg_exposure=_mean([p.weight for p in held]) if held else 0.0,
        n_fills=len(fills),
        turnover_sides_per_year=_safe_div(len(fills), years),
        turnover_notional_per_year=_safe_div(notional / mean_equity, years) if mean_equity else None,
        fees_paid=fees,
        slippage_cost=slip,
        cost_drag_annual=_safe_div((fees + slip) / mean_equity, years) if mean_equity else None,
        trades=trade_stats(trades),
    )
