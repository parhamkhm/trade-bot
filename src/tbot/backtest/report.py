"""Turn a ``BacktestResult`` (and optional benchmarks) into a plain dict or a markdown table.

No plotting and no I/O: ``build_report`` returns JSON-serialisable data, ``render_markdown`` returns text.
Undefined metrics are shown as ``n/a`` (never as 0), and run-health counters (rejections, resizes, gap
fills, skipped decisions, unfilled orders) are always printed so a silently degraded run cannot look clean.

Two things make a run outright suspect, not just worth a footnote, and are surfaced as
``report["health"]["issues"]`` plus a prominent block at the top of the markdown (``is_run_invalid``
summarises the same check for a caller, e.g. the CLI, that needs a single boolean):

* any fill-time rejection (``fill_rejections > 0``) -- e.g. below min_notional, or a cash cap that leaves
  nothing tradable;
* any resize ANOMALY (``resizes_anomaly > 0``): in both fill modes a BUY is capped at fill time to what cash
  affords at the real price (D-051). The cap is classified by cause: a request that was affordable at the
  sizing price (the signal-bar close) and became unaffordable because the price moved is counted under
  ``resizes_price_move`` and does not invalidate the run, however large the move; a request that was not
  affordable even at the sizing price (or carried no sizing reference) was mis-sized, is an anomaly and
  invalidates the run just like a rejection;
* approved orders that neither filled nor are still pending at the end -- more than half of them vanishing
  this way means the accounting between decisions and fills has come apart, which should never happen by
  construction and is worth a loud flag if it ever does.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from tbot.backtest.benchmarks import BenchmarkSet
from tbot.backtest.metrics import Metrics, compute_metrics
from tbot.backtest.result import ORDER, BacktestResult

__all__ = ["build_report", "is_run_invalid", "render_markdown"]

CAVEATS = (
    "Phase-2 engine: stops are not executed; the risk manager is a stub (sizing, filters, drift tolerance).",
    "Vol-matched buy-and-hold uses the strategy realised volatility on the same window (ex post).",
)

_PROVISIONAL_SLIPPAGE_CAVEAT = (
    "PROVISIONAL SLIPPAGE (D-060/D-067): the slippage figure is a placeholder, not yet measured from "
    "recorded order-book depth. No phase-3 gate may be decided on it."
)


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    return asdict(m)


def _health_issues(
    decisions: Counter[str],
    fills: int,
    rejections: int,
    unfilled: int,
    anomalies: int = 0,
) -> list[str]:
    issues = []
    if rejections > 0:
        issues.append(f"{rejections} order(s) were rejected at fill time -- the result may be cost-biased")
    if anomalies > 0:
        issues.append(
            f"{anomalies} BUY(s) were resized at fill time although the request was not affordable at the "
            "sizing price, or carried no sizing reference (resize anomaly): a sizing bug, not price movement"
        )
    orders = decisions.get(ORDER, 0)
    unresolved = orders - fills - unfilled
    if unresolved > max(1, orders // 2):
        issues.append(
            f"{orders} order(s) were approved but only {fills} filled ({unfilled} still pending at the "
            "end): decisions and fills have come apart (should not happen by construction)"
        )
    return issues


def _result_issues(result: BacktestResult) -> list[str]:
    decisions = Counter(d.outcome for d in result.decisions)
    unfilled = len(result.unfilled_order_ids)
    anomalies = sum(1 for r in result.resizes if r.kind == "anomaly")
    return _health_issues(decisions, len(result.fills), len(result.rejections), unfilled, anomalies)


def is_run_invalid(report: dict[str, Any]) -> bool:
    """True when the run has at least one issue serious enough that a report reader (or the CLI's exit
    code) should not treat the numbers as a clean result. See the module docstring."""
    return bool(report["health"]["issues"])


def build_report(result: BacktestResult, benchmarks: BenchmarkSet | None = None) -> dict[str, Any]:
    cfg = result.config
    first, last = result.equity[0].ts, result.equity[-1].ts
    costs = cfg.costs
    decisions = Counter(d.outcome for d in result.decisions)
    n_rejections = len(result.rejections)
    n_unfilled = len(result.unfilled_order_ids)
    n_anomalies = sum(1 for r in result.resizes if r.kind == "anomaly")
    issues = _result_issues(result)
    caveats = list(CAVEATS)
    if costs.slippage_provisional:
        caveats.insert(0, _PROVISIONAL_SLIPPAGE_CAVEAT)
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
            "slippage_provisional": costs.slippage_provisional,
            "fill_delay": str(cfg.fill_delay),
            "exec_timeframe": None if cfg.exec_timeframe is None else str(cfg.exec_timeframe.value),
        },
        "initial_cash": str(result.initial_cash),
        # Only meaningful when the default phase-2 StubRiskManager (engine.py) was used; a custom
        # RiskManager passed to run_backtest may ignore these config values entirely.
        "stub_risk_manager": {
            "buy_buffer_bps": str(cfg.buy_buffer_bps),
            "drift_tolerance": cfg.drift_tolerance,
        },
        "health": {
            "decisions": dict(decisions),
            "fill_rejections": n_rejections,
            "unfilled_orders_at_end": n_unfilled,
            "resizes": len(result.resizes),
            "resizes_price_move": sum(1 for r in result.resizes if r.kind == "price_move"),
            "resizes_anomaly": n_anomalies,
            "gap_fills": sum(1 for f in result.fills if f.gap_fill),
            "issues": issues,
        },
        "metrics": {"strategy": _metrics_dict(compute_metrics(result))},
        "caveats": caveats,
    }
    if benchmarks is not None:
        # a benchmark that silently failed to trade makes every comparison against it meaningless, so its
        # health issues are folded into the same list that drives the INVALID RUN block and the CLI exit code
        named = (("buy & hold", benchmarks.buy_and_hold), ("vol-matched B&H", benchmarks.vol_matched))
        for label, bench in named:
            issues.extend(f"{label}: {issue}" for issue in _result_issues(bench))
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
    stub = report["stub_risk_manager"]
    health = report["health"]
    lines = [f"# Backtest: {report['strategy']} on {report['symbol']} {report['timeframe']}", ""]
    if health["issues"]:
        lines += ["> **INVALID RUN WARNING**", *(f"> - {issue}" for issue in health["issues"]), ""]
    if costs["slippage_provisional"]:
        lines += ["> **PROVISIONAL SLIPPAGE** -- see the caveats below (D-060/D-067)", ""]
    lines += [
        f"- Window: {report['first_bar_close']} to {report['last_bar_close']} ({report['n_bars']} bars)",
        f"- Costs: taker {costs['taker_fee_bps']} bps, slippage {costs['slippage_bps']} bps per side; "
        f"fill delay {costs['fill_delay']}",
        f"- Stub risk manager: buy buffer {stub['buy_buffer_bps']} bps, drift tolerance "
        f"{stub['drift_tolerance']:g} (phase-2 stand-in -- see caveats)",
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
    lines += [
        "",
        "Run health: decisions "
        + ", ".join(f"{k}={v}" for k, v in sorted(health["decisions"].items()))
        + f"; fill rejections {health['fill_rejections']}"
        + f"; resizes {health['resizes']} (price_move {health['resizes_price_move']}, "
        + f"anomaly {health['resizes_anomaly']})"
        + f"; gap fills {health['gap_fills']}"
        + f"; unfilled at end {health['unfilled_orders_at_end']}",
        "",
        "Caveats:",
        *[f"- {c}" for c in report["caveats"]],
        "",
    ]
    return "\n".join(lines)
