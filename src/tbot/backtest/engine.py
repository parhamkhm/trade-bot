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
from tbot.execution.simulated import ExecutionReport, FillRecord, OrderRejection, SimulatedBroker
from tbot.portfolio.allocator import Allocator, PassThroughAllocator
from tbot.portfolio.portfolio import EquityPoint, Portfolio
from tbot.strategies.base import validate_strategy

__all__ = ["EngineError", "run_backtest"]

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
        )
        self.warmup = max(strategy.warmup_bars, regime.warmup_bars if regime is not None else 1)
        self.history: list[Bar] = []
        self.equity: list[EquityPoint] = []
        self.fills: list[FillRecord] = []
        self.rejections: list[OrderRejection] = []
        self.decisions: list[DecisionRecord] = []
        self.first_decision: int | None = None

    # -- step 1: execution -------------------------------------------------------------------

    def execute(self, exec_bar: Bar) -> None:
        report: ExecutionReport = self.broker.advance(exec_bar)
        for rec in report.fills:
            self.portfolio.apply_fill(rec.fill)
            self.fills.append(rec)
        self.rejections.extend(report.rejections)

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
) -> BacktestResult:
    """Run one backtest and return positions per bar, fills, equity curve, trades and costs.

    ``feed`` supplies the signal bars (``config.symbol`` / ``config.timeframe``). ``exec_feed`` supplies finer
    bars (``config.exec_timeframe``) at whose opens orders fill; it is required exactly when
    ``config.exec_timeframe`` is set (the manual-delay model). Without it, orders fill at the next signal
    bar's open. ``risk`` defaults to the phase-2 ``StubRiskManager``.
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
    previous: Bar | None = None
    for bar in feed.bars(config.symbol, config.timeframe):
        if bar.symbol != config.symbol or bar.timeframe != config.timeframe:
            raise EngineError(
                f"feed yielded {bar.symbol}/{bar.timeframe}, expected {config.symbol}/{config.timeframe}"
            )
        if previous is not None and bar.ts <= previous.ts:
            raise EngineError(f"feed went backwards: {bar.ts} after {previous.ts}")
        previous = bar
        for exec_bar in cursor.pop_until(bar.ts) if cursor is not None else (bar,):
            run.execute(exec_bar)
        run.history.append(bar)
        run.mark(bar)
        run.decide(bar)
    if not run.equity:
        raise EngineError("the feed produced no bars")
    return run.result()
