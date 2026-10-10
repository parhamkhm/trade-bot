"""Buy-and-hold and vol-matched buy-and-hold benchmarks.

Both benchmarks run through the SAME engine, broker, fees, slippage, filters and fill model as the strategy
(no vectorised shortcut), and start deciding at the same bar (``warmup_bars`` of the strategy), so every
number in a report is measured on identical bars with identical costs.

Vol-matched buy-and-hold holds a constant weight ``w = min(1, strategy_vol / buy_and_hold_vol)``, i.e. it
scales buy-and-hold DOWN to the strategy's realised volatility (long-only, no leverage: if the strategy is
more volatile than buy-and-hold, ``w`` is capped at 1 and ``capped`` is set). The strategy volatility is
measured ex post on the same window; it is a benchmark construction, not a tradable rule.

Feeds must be re-iterable (``BacktestFeed`` is), because each benchmark replays the data.
"""

from __future__ import annotations

from dataclasses import dataclass

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.result import BacktestResult
from tbot.core.types import DataFeed, Strategy
from tbot.strategies.buy_and_hold import BuyAndHold

__all__ = ["BenchmarkSet", "run_benchmarks", "vol_match_weight"]


@dataclass(frozen=True, slots=True)
class BenchmarkSet:
    buy_and_hold: BacktestResult
    vol_matched: BacktestResult
    vol_match_weight: float
    capped: bool  # True if the strategy was more volatile than buy-and-hold (weight limited to 1)


def vol_match_weight(strategy_vol: float | None, bh_vol: float | None) -> tuple[float, bool]:
    """Weight that scales buy-and-hold to the strategy's volatility, and whether the cap of 1 applied."""
    if strategy_vol is None or bh_vol is None or bh_vol <= 0.0:
        return 0.0, False
    raw = strategy_vol / bh_vol
    return min(1.0, raw), raw > 1.0


def run_benchmarks(
    feed: DataFeed,
    strategy: Strategy,
    config: BacktestConfig,
    strategy_result: BacktestResult,
    *,
    exec_feed: DataFeed | None = None,
) -> BenchmarkSet:
    bh = run_backtest(
        feed, BuyAndHold(warmup_bars=strategy.warmup_bars), config, exec_feed=exec_feed
    )
    weight, capped = vol_match_weight(compute_metrics(strategy_result).vol, compute_metrics(bh).vol)
    vm = run_backtest(
        feed, BuyAndHold(weight=weight, warmup_bars=strategy.warmup_bars), config, exec_feed=exec_feed
    )
    return BenchmarkSet(bh, vm, weight, capped)
