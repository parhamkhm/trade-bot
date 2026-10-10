"""Turn a ``BacktestResult`` (and optional benchmarks) into a plain dict or a markdown table.

No plotting and no I/O: ``build_report`` returns JSON-serialisable data, ``render_markdown`` returns text.
Undefined metrics are shown as ``n/a`` (never as 0), and run-health counters (rejections, skipped decisions,
unfilled orders) are always printed so a silently degraded run cannot look clean.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from tbot.backtest.benchmarks import BenchmarkSet
from tbot.backtest.metrics import Metrics, compute_metrics
from tbot.backtest.result import BacktestResult

__all__ = ["build_report", "render_markdown"]

CAVEATS = (
    "Phase-2 engine: stops are not executed; the risk manager is a stub (sizing, filters, drift tolerance).",
    "Vol-matched buy-and-hold uses the strategy realised volatility on the same window (ex post).",
)


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    return asdict(m)


def build_report(result: BacktestResult, benchmarks: BenchmarkSet | None = None) -> dict[str, Any]:
    cfg = result.config
    first, last = result.equity[0].ts, result.equity[-1].ts
    costs = cfg.costs
    report: dict[str, Any] = {
        "strategy": result.strategy_id,
        "symbol": cfg.symbol,
        "timeframe": str(cfg.timeframe.value),
        "first_bar_close": first.isoformat(),
        "last_bar_close": last.isoformat(),
        "n_bars": result.n_bars,
        "first_decision_index": result.first_decision_index,
        "costs": {
            "taker_fee_bps": str(costs.taker_fee_bps),
            "maker_fee_bps": str(costs.maker_fee_bps),
            "slippage_bps": str(costs.slippage_bps),
            "fill_delay": str(cfg.fill_delay),
            "exec_timeframe": None if cfg.exec_timeframe is None else str(cfg.exec_timeframe.value),
        },
        "initial_cash": str(result.initial_cash),
        "health": {
            "decisions": dict(Counter(d.outcome for d in result.decisions)),
            "fill_rejections": len(result.rejections),
            "unfilled_orders_at_end": len(result.unfilled_order_ids),
        },
        "metrics": {"strategy": _metrics_dict(compute_metrics(result))},
        "caveats": list(CAVEATS),
    }
    if benchmarks is not None:
        report["metrics"]["buy_and_hold"] = _metrics_dict(compute_metrics(benchmarks.buy_and_hold))
        report["metrics"]["vol_matched_buy_and_hold"] = _metrics_dict(compute_metrics(benchmarks.vol_matched))
        report["vol_match"] = {"weight": benchmarks.vol_match_weight, "capped_at_1": benchmarks.capped}
    return report


def _fmt(value: float | int | None, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.2f}%"
    if kind == "int":
        return str(int(value))
    if kind == "money":
        return f"{value:,.2f}"
    return f"{value:.3f}"


_ROWS: tuple[tuple[str, str, str], ...] = (
    ("CAGR", "cagr", "pct"),
    ("Total return", "total_return", "pct"),
    ("Volatility (ann.)", "vol", "pct"),
    ("Sharpe", "sharpe", "num"),
    ("Sortino", "sortino", "num"),
    ("Max drawdown", "max_drawdown", "pct"),
    ("Max DD duration (bars)", "max_dd_duration_bars", "int"),
    ("Calmar", "calmar", "num"),
    ("Time in market", "time_in_market", "pct"),
    ("Avg exposure", "avg_exposure", "pct"),
    ("Fills (sides)", "n_fills", "int"),
    ("Turnover (sides/yr)", "turnover_sides_per_year", "num"),
    ("Turnover (notional/equity/yr)", "turnover_notional_per_year", "num"),
    ("Fees paid", "fees_paid", "money"),
    ("Slippage cost", "slippage_cost", "money"),
    ("Cost drag (per yr)", "cost_drag_annual", "pct"),
)

_TRADE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Closed trades", "n_trades", "int"),
    ("Win rate", "win_rate", "pct"),
    ("Expectancy / trade", "expectancy", "money"),
    ("Profit factor", "profit_factor", "num"),
)


def render_markdown(report: dict[str, Any]) -> str:
    metrics: dict[str, dict[str, Any]] = report["metrics"]
    names = {
        "strategy": report["strategy"],
        "buy_and_hold": "buy & hold",
        "vol_matched_buy_and_hold": "vol-matched B&H",
    }
    cols = [k for k in names if k in metrics]
    costs = report["costs"]
    lines = [
        f"# Backtest: {report['strategy']} on {report['symbol']} {report['timeframe']}",
        "",
        f"- Window: {report['first_bar_close']} to {report['last_bar_close']} ({report['n_bars']} bars)",
        f"- Costs: taker {costs['taker_fee_bps']} bps, slippage {costs['slippage_bps']} bps per side; "
        f"fill delay {costs['fill_delay']}",
        f"- Start equity: {report['initial_cash']}",
        "",
        "| Metric | " + " | ".join(names[c] for c in cols) + " |",
        "|---|" + "---:|" * len(cols),
    ]
    for label, key, kind in _ROWS:
        lines.append(f"| {label} | " + " | ".join(_fmt(metrics[c][key], kind) for c in cols) + " |")
    for label, key, kind in _TRADE_ROWS:
        lines.append(f"| {label} | " + " | ".join(_fmt(metrics[c]["trades"][key], kind) for c in cols) + " |")
    if "vol_match" in report:
        vm = report["vol_match"]
        note = " (capped at 1: the strategy is more volatile than buy & hold)" if vm["capped_at_1"] else ""
        lines += ["", f"Vol-matched weight: {vm['weight']:.3f}{note}"]
    health = report["health"]
    lines += [
        "",
        "Run health: decisions "
        + ", ".join(f"{k}={v}" for k, v in sorted(health["decisions"].items()))
        + f"; fill rejections {health['fill_rejections']}"
        + f"; unfilled at end {health['unfilled_orders_at_end']}",
        "",
        "Caveats:",
        *[f"- {c}" for c in report["caveats"]],
        "",
    ]
    return "\n".join(lines)
