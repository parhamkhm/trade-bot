"""CLI: run a baseline backtest on stored research bars and print a markdown report.

Loads closed bars from the local Parquet store (the loader excludes the sealed holdout and this script
never unseals it), runs the chosen strategy through the event-driven engine with the costs from
``config/<name>.yaml``, runs buy-and-hold and vol-matched buy-and-hold on the same bars and prints the
report.

Usage::

    uv run python scripts/run_backtest.py --strategy sma_filter --sma-n 200 --timeframe 1d
    uv run python scripts/run_backtest.py --strategy buy_and_hold --start 2019-01-01 --json out.json

The symbol filters here are LBank-like placeholders (tick 0.01, step 0.00001, min notional 1 USDT) until the
exchange filters are recorded; the engine takes them from ``SymbolFilters`` so nothing is exchange-specific.
Delayed fills (``--delay-hours``) need finer execution bars (1h) in the store.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.report import build_report, render_markdown
from tbot.core.config import load_config
from tbot.core.types import Strategy, SymbolFilters, Timeframe
from tbot.data.binance_loader import load_bars
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter

__all__ = ["main"]


def placeholder_filters(symbol: str) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal(1),
    )


def make_strategy(name: str, sma_n: int) -> Strategy:
    if name == "buy_and_hold":
        return BuyAndHold()
    if name == "sma_filter":
        return SmaFilter(sma_n)
    raise ValueError(f"unknown strategy {name!r}")


def _parse_date(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a baseline backtest on stored research bars.")
    p.add_argument("--strategy", choices=("buy_and_hold", "sma_filter"), default="sma_filter")
    p.add_argument("--sma-n", type=int, default=200)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", choices=[t.value for t in Timeframe], default="1d")
    p.add_argument("--config", default="default", help="config name or path (costs come from here)")
    p.add_argument("--start", type=_parse_date, default=None, help="first bar date, YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, default=None, help="end date (exclusive), YYYY-MM-DD")
    p.add_argument("--initial-cash", type=Decimal, default=Decimal(10_000))
    p.add_argument("--delay-hours", type=float, default=0.0, help="manual-fill delay (D-051); needs 1h bars")
    p.add_argument("--json", type=Path, default=None, help="also write the report dict here")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    timeframe = Timeframe(args.timeframe)
    config = load_config(args.config)
    delay = timedelta(hours=args.delay_hours)
    exec_tf = Timeframe.H1 if delay > timedelta(0) else None
    bt_config = BacktestConfig.from_config(
        config,
        filters=placeholder_filters(args.symbol),
        timeframe=timeframe,
        initial_cash=args.initial_cash,
        fill_delay=delay,
        exec_timeframe=exec_tf,
    )
    feed = BacktestFeed(load_bars(args.symbol, timeframe, start=args.start, end=args.end))
    exec_feed = (
        BacktestFeed(load_bars(args.symbol, exec_tf, start=args.start, end=args.end))
        if exec_tf is not None
        else None
    )
    strategy = make_strategy(args.strategy, args.sma_n)
    result = run_backtest(feed, strategy, bt_config, exec_feed=exec_feed)
    benchmarks = run_benchmarks(feed, strategy, bt_config, result, exec_feed=exec_feed)
    report = build_report(result, benchmarks)
    sys.stdout.write(render_markdown(report))
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
