"""Test doubles and config builders shared by the backtest tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from decimal import Decimal

from tbot.backtest.config import BacktestConfig
from tbot.backtest.risk_stub import StubRiskManager
from tbot.core.config import CostConfig
from tbot.core.types import (
    Approved,
    Bar,
    BarWindow,
    MarketState,
    PortfolioView,
    RiskDecision,
    Side,
    TargetIntent,
    Timeframe,
)
from tests.sim_helpers import SYMBOL, lbank_like_filters

Dec = Decimal


def make_config(
    *,
    fee: str = "50",
    slip: str = "100",
    buffer: str = "0",
    tf: Timeframe = Timeframe.D1,
    **kwargs: object,
) -> BacktestConfig:
    """Round-number costs by default (0.5 % fee, 1 % slippage) so results can be hand-computed."""
    return BacktestConfig(
        symbol=SYMBOL,
        timeframe=tf,
        filters=lbank_like_filters(),
        costs=CostConfig(taker_fee_bps=Dec(fee), maker_fee_bps=Dec("40"), slippage_bps=Dec(slip)),
        buy_buffer_bps=Dec(buffer),
        **kwargs,  # type: ignore[arg-type]
    )


class Scripted:
    """Emits ``weights[t]`` at bar ``t`` (the last weight repeats afterwards)."""

    def __init__(
        self, weights: Sequence[float], *, warmup_bars: int = 1, strategy_id: str = "scripted"
    ) -> None:
        self.id = strategy_id
        self.warmup_bars = warmup_bars
        self._weights = list(weights)

    def on_bar(self, window: BarWindow) -> TargetIntent:
        t = len(window) - 1
        return TargetIntent(self.id, window.as_of, self._weights[min(t, len(self._weights) - 1)], "script")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


class OversizedRisk:
    """A BUGGY risk manager: the stub's BUY quantity multiplied by ``factor`` (default 2x), i.e. orders
    that cash could not cover even at the price they were sized at. Used to provoke the sizing-bug anomaly."""

    def __init__(self, config: BacktestConfig, factor: Decimal = Decimal(2)) -> None:
        self._stub = StubRiskManager(
            taker_fee_bps=config.costs.taker_fee_bps,
            slippage_bps=config.costs.slippage_bps,
            buy_buffer_bps=config.buy_buffer_bps,
            rebalance_band=config.rebalance_band,
        )
        self._factor = factor
        self._filters = config.filters

    def evaluate(self, intent: TargetIntent, portfolio: PortfolioView, market: MarketState) -> RiskDecision:
        decision = self._stub.evaluate(intent, portfolio, market)
        if isinstance(decision, Approved) and decision.order is not None and decision.order.side is Side.BUY:
            quantity = self._filters.floor_qty(decision.order.quantity * self._factor)
            big = replace(decision.order, quantity=quantity)
            return Approved(decision.intent, big, decision.note)
        return decision


class Recorder:
    """Wraps a weight function and records what the engine showed it."""

    def __init__(
        self, fn: Callable[[BarWindow], float], on_call: Callable[[BarWindow], None] | None = None
    ) -> None:
        self.id = "recorder"
        self.warmup_bars = 1
        self.windows: list[BarWindow] = []
        self._fn = fn
        self._on_call = on_call

    def on_bar(self, window: BarWindow) -> TargetIntent:
        self.windows.append(window)
        if self._on_call is not None:
            self._on_call(window)
        return TargetIntent(self.id, window.as_of, self._fn(window), "")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


class CountingFeed:
    """A one-shot generator feed that counts how many bars the engine has pulled so far."""

    def __init__(self, bars: Sequence[Bar]) -> None:
        self._bars = list(bars)
        self.consumed = 0

    def bars(self, symbol: str, timeframe: Timeframe) -> Iterator[Bar]:  # noqa: ARG002
        for bar in self._bars:
            self.consumed += 1
            yield bar
