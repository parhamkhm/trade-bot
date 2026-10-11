"""Event-driven backtest loop over CLOSED bars.

Per signal bar ``t`` (in this order, always):

1. every execution bar that closes at or before ``close(t)`` is fed to the broker, so orders created at
   earlier closes fill at an OPEN that is already in the past; fills are booked into the ``Portfolio``;
2. the portfolio is marked to market at ``close(t)`` (accounting identity checked, broker and portfolio
   balances reconciled);
3. once the strategy (and regime) warm-up is over: ``Strategy.on_bar`` -> regime multiplier ->
   ``Allocator`` -> ``RiskManager`` -> ``Broker.submit``. The new order cannot fill before the next
   eligible execution bar OPEN -- the broker enforces that structurally, the engine merely never shows it a
   bar that is not yet closed.

The loop pulls bars lazily from the feed iterators, so no future bar is ever materialised. The same
``Strategy`` / ``RegimeModel`` / ``RiskManager`` / ``Portfolio`` interfaces are used as live; only the feed
and the broker are simulated. No stops are modelled here (SPEC section 7.1 stop model: phase 3/4, see report).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

from tbot.backtest.config import BacktestConfig
from tbot.backtest.result import (
    NO_ACTION,
    ORDER,
    REFUSED,
    SKIPPED_PENDING,
    SUBMIT_REJECTED,
    BacktestResult,
    DecisionRecord,
)
from tbot.backtest.risk_stub import StubRiskManager
from tbot.core.types import (
    Approved,
    Bar,
    BarWindow,
    DataFeed,
    MarketState,
    RegimeModel,
    RiskManager,
    Strategy,
    TargetIntent,
)
from tbot.data.binance_loader import (
    HOLDOUT_UNSEAL_ENV,
    HOLDOUT_UNSEAL_VALUE,
    HoldoutLockError,
    is_holdout_sealed,
    log_holdout_event,
)
from tbot.execution.simulated import (
    ExecutionReport,
    FillRecord,
    OrderRejection,
    ResizeRecord,
    SimulatedBroker,
)
from tbot.portfolio.allocator import Allocator, PassThroughAllocator
from tbot.portfolio.portfolio import EquityPoint, Portfolio
from tbot.strategies.base import validate_strategy

__all__ = ["EngineError", "effective_warmup_bars", "run_backtest"]

_ZERO = Decimal(0)


class EngineError(RuntimeError):
    """The run is inconsistent (feed mismatch, strategy stamping a wrong time, books out of sync)."""


def _fast_window(symbol: str, bars: tuple[Bar, ...], bar: Bar) -> BarWindow:
    """``BarWindow`` without the O(n) re-validation: the engine already guarantees homogeneity and
    strictly increasing timestamps bar by bar (an equivalence test pins this to ``BarWindow.of``)."""
    window = object.__new__(BarWindow)
    object.__setattr__(window, "symbol", symbol)
    object.__setattr__(window, "timeframe", bar.timeframe)
    object.__setattr__(window, "bars", bars)
    return window


def effective_warmup_bars(strategy: Strategy, regime: RegimeModel | None) -> int:
    """The bar count the engine actually waits for before the first decision: whichever of the
    strategy's and the regime's warm-up is larger. Benchmarks must align to this, not to
    ``strategy.warmup_bars`` alone, or a regime overlay with a longer warm-up shifts their start
    relative to the strategy under test and the comparison window stops being the same bars."""
    return max(strategy.warmup_bars, regime.warmup_bars if regime is not None else 1)


class _SealGuard:
    """Engine-level holdout guard, one instance per run. A refusal raises and is logged; an unsealing is
    logged ONCE per run (the first sealed bar), not once per bar, so ``research/HOLDOUT_LOG.md`` records
    one entry per unsealed run."""

    def __init__(self, *, allow_holdout: bool) -> None:
        self._allow = allow_holdout
        self._unseal_logged = False

    def check(self, bar: Bar, *, caller: str) -> None:
        if not is_holdout_sealed(bar.ts):
            return
        env_matches = os.environ.get(HOLDOUT_UNSEAL_ENV) == HOLDOUT_UNSEAL_VALUE
        unsealed = self._allow and env_matches
        if unsealed and self._unseal_logged:
            return
        outcome = "unsealed" if unsealed else "refused: locks mismatched or absent"
        # best-effort: an installed package with no repo root above it has nowhere to log
        with contextlib.suppress(RuntimeError):
            log_holdout_event(caller=caller, reason="backtest engine bar guard", outcome=outcome)
        if not unsealed:
            raise HoldoutLockError(
                f"a bar closing at {bar.ts.isoformat()} falls in the sealed holdout; this engine-level "
                "guard catches it even from a custom DataFeed that does not enforce the seal itself "
                "(SPEC section 5.2, both locks required)"
            )
        self._unseal_logged = True


class _Peekable:
    """One-bar lookahead over the *execution* feed, used only to stop at the signal close."""

    def __init__(self, it: Iterator[Bar]) -> None:
        self._it = it
        self._head: Bar | None = next(it, None)

    def pop_until(self, ts: datetime) -> Iterator[Bar]:
        while self._head is not None and self._head.ts <= ts:
            bar, self._head = self._head, next(self._it, None)
            yield bar


class _Run:
    def __init__(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        regime: RegimeModel | None,
        risk: RiskManager | None,
        allocator: Allocator | None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.regime = regime
        self.allocator: Allocator = allocator or PassThroughAllocator()
        self.risk: RiskManager = risk or StubRiskManager(
            taker_fee_bps=config.costs.taker_fee_bps,
            slippage_bps=config.costs.slippage_bps,
            buy_buffer_bps=config.buy_buffer_bps,
            drift_tolerance=config.drift_tolerance,
        )
        self.portfolio = Portfolio(
            symbol=config.symbol,
            base_asset=config.base_asset,
            quote_asset=config.quote_asset,
            initial_cash=config.initial_cash,
        )
        self.broker = SimulatedBroker(
            filters=config.filters,
            fees=config.fee_schedule(),
            slippage=config.slippage(),
            base_asset=config.base_asset,
            quote_asset=config.quote_asset,
            initial_cash=config.initial_cash,
            fill_delay=config.fill_delay,
            resize_anomaly_threshold=config.resize_anomaly_threshold,
        )
        self.warmup = effective_warmup_bars(strategy, regime)
        self.history: list[Bar] = []
        self.equity: list[EquityPoint] = []
        self.fills: list[FillRecord] = []
        self.rejections: list[OrderRejection] = []
        self.resizes: list[ResizeRecord] = []
        self.decisions: list[DecisionRecord] = []
        self.first_decision: int | None = None

    # -- step 1: execution -------------------------------------------------------------------

    def execute(self, exec_bar: Bar) -> None:
        report: ExecutionReport = self.broker.advance(exec_bar)
        for rec in report.fills:
            self.portfolio.apply_fill(rec.fill)
            self.fills.append(rec)
        self.rejections.extend(report.rejections)
        self.resizes.extend(report.resizes)

    # -- step 2: mark ------------------------------------------------------------------------

    def mark(self, bar: Bar) -> None:
        if self.broker.balances() != self.portfolio.balances():
            raise EngineError(
                f"broker {self.broker.balances()} != portfolio {self.portfolio.balances()} at {bar.ts}"
            )
        point = self.portfolio.mark(bar.ts, bar.close)
        if point.equity != point.cash + point.quantity * bar.close:  # identity, exact in Decimal
            raise EngineError(f"equity identity violated at {bar.ts}")
        self.equity.append(point)

    # -- step 3: decide ----------------------------------------------------------------------

    def decide(self, bar: Bar) -> None:
        if len(self.history) < self.warmup:
            return
        window = _fast_window(self.config.symbol, tuple(self.history), bar)
        if self.first_decision is None:
            self.first_decision = len(self.equity) - 1
        intent = self.strategy.on_bar(window)
        if intent.ts != bar.ts:
            raise EngineError(f"strategy returned an intent stamped {intent.ts}, expected bar close {bar.ts}")
        if self.regime is not None:
            state = self.regime.update(window)
            intent = intent.scaled(state.exposure)
        intent = self.allocator.allocate([intent])
        if self.broker.open_orders(self.config.symbol):
            self._log(intent, SKIPPED_PENDING, "an earlier order is still unfilled")
            return
        self._route(intent, bar)

    def _route(self, intent: TargetIntent, bar: Bar) -> None:
        market = MarketState(self.config.symbol, bar.ts, bar.close, self.config.filters)
        decision = self.risk.evaluate(intent, self.portfolio.view(bar.ts, bar.close), market)
        if not isinstance(decision, Approved):
            self._log(intent, REFUSED, f"{decision.reason.value}: {decision.detail}")
        elif decision.order is None:
            self._log(intent, NO_ACTION, decision.note)
        else:
            ack = self.broker.submit(decision.order)
            outcome = ORDER if ack.accepted else SUBMIT_REJECTED
            self._log(intent, outcome, ack.message or decision.note, decision.order.client_order_id)

    def _log(self, intent: TargetIntent, outcome: str, detail: str, order_id: str | None = None) -> None:
        self.decisions.append(DecisionRecord(intent.ts, intent.target_weight, outcome, detail, order_id))

    # -- result ------------------------------------------------------------------------------

    def result(self) -> BacktestResult:
        self.portfolio.verify()
        return BacktestResult(
            config=self.config,
            strategy_id=self.strategy.id,
            equity=tuple(self.equity),
            fills=tuple(self.fills),
            rejections=tuple(self.rejections),
            resizes=tuple(self.resizes),
            decisions=tuple(self.decisions),
            trades=self.portfolio.trades,
            first_decision_index=self.first_decision,
            unfilled_order_ids=tuple(o.client_order_id for o in self.broker.open_orders(self.config.symbol)),
            fees_paid=self.portfolio.fees_paid,
            slippage_cost=sum((f.slippage_cost for f in self.fills), _ZERO),
            traded_notional=self.portfolio.ledger.traded_notional,
        )


def run_backtest(
    feed: DataFeed,
    strategy: Strategy,
    config: BacktestConfig,
    *,
    regime: RegimeModel | None = None,
    risk: RiskManager | None = None,
    allocator: Allocator | None = None,
    exec_feed: DataFeed | None = None,
    allow_holdout: bool = False,
) -> BacktestResult:
    """Run one backtest and return positions per bar, fills, equity curve, trades and costs.

    ``feed`` supplies the signal bars (``config.symbol`` / ``config.timeframe``). ``exec_feed`` supplies finer
    bars (``config.exec_timeframe``) at whose opens orders fill; it is required exactly when
    ``config.exec_timeframe`` is set (the manual-delay model). Without it, orders fill at the next signal
    bar's open. ``risk`` defaults to the phase-2 ``StubRiskManager``.

    Every bar pulled from ``feed`` or ``exec_feed`` is checked against the sealed holdout here, in the
    engine, not only in ``BacktestFeed``: a custom ``DataFeed`` that skips that check (or a live-replay
    feed built later) cannot bypass the seal just by not calling it. ``allow_holdout`` is the same
    first lock ``BacktestFeed`` takes; the second lock is the ``TBOT_UNSEAL_HOLDOUT=G4`` env var.
    """
    validate_strategy(strategy)
    if (exec_feed is None) != (config.exec_timeframe is None):
        raise EngineError("exec_feed must be given if and only if config.exec_timeframe is set")
    run = _Run(config, strategy, regime, risk, allocator)
    cursor = (
        _Peekable(exec_feed.bars(config.symbol, config.exec_timeframe))
        if exec_feed is not None and config.exec_timeframe is not None
        else None
    )
    guard = _SealGuard(allow_holdout=allow_holdout)
    exec_timeframe = config.exec_timeframe or config.timeframe  # the bars orders actually fill on
    previous: Bar | None = None
    for bar in feed.bars(config.symbol, config.timeframe):
        if bar.symbol != config.symbol or bar.timeframe != config.timeframe:
            raise EngineError(
                f"feed yielded {bar.symbol}/{bar.timeframe}, expected {config.symbol}/{config.timeframe}"
            )
        if previous is not None and bar.ts <= previous.ts:
            raise EngineError(f"feed went backwards: {bar.ts} after {previous.ts}")
        previous = bar
        guard.check(bar, caller="run_backtest:signal_feed")
        for exec_bar in cursor.pop_until(bar.ts) if cursor is not None else (bar,):
            if exec_bar.symbol != config.symbol or exec_bar.timeframe != exec_timeframe:
                raise EngineError(
                    f"exec feed yielded {exec_bar.symbol}/{exec_bar.timeframe}, "
                    f"expected {config.symbol}/{exec_timeframe}"
                )
            guard.check(exec_bar, caller="run_backtest:exec_feed")
            run.execute(exec_bar)
        run.history.append(bar)
        run.mark(bar)
        run.decide(bar)
    if not run.equity:
        raise EngineError("the feed produced no bars")
    return run.result()
