"""Backtest configuration. Costs come from ``tbot.core.config.CostConfig`` -- nothing is hard-coded here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from tbot.core.config import Config, CostConfig
from tbot.core.types import SymbolFilters, Timeframe
from tbot.execution.simulated import FeeSchedule, FlatSlippage, SlippageModel

__all__ = ["BacktestConfig"]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything a run needs besides the data and the strategy.

    Two fill models (SPEC D-051), selected by ``fill_delay`` / ``exec_timeframe``:

    * next-open (default): ``fill_delay = 0`` and no exec feed. An order created at the close of signal
      bar ``t`` fills at the open of bar ``t+1`` plus slippage.
    * manual-delay: ``fill_delay = 6h`` and ``exec_timeframe = 1h``. The order fills at the open of the
      first hourly bar that opens at or after ``close(t) + fill_delay`` plus slippage.

    ``slippage_model`` is the hook for the depth-at-size model of SPEC section 7 (calibrated in phase 3);
    until one is supplied, the flat ``costs.slippage_bps`` placeholder applies.
    """

    symbol: str
    timeframe: Timeframe
    filters: SymbolFilters
    costs: CostConfig
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    initial_cash: Decimal = Decimal(10_000)
    fill_delay: timedelta = timedelta(0)
    exec_timeframe: Timeframe | None = None
    slippage_model: SlippageModel | None = field(default=None, compare=False)
    # StubRiskManager knobs (phase 2 stand-in only; the real RiskManager arrives in phase 4).
    drift_tolerance: float = 0.02
    buy_buffer_bps: Decimal = Decimal(10)

    def __post_init__(self) -> None:
        if self.filters.symbol != self.symbol:
            raise ValueError(f"filters are for {self.filters.symbol}, config is for {self.symbol}")
        if not self.initial_cash.is_finite() or self.initial_cash <= 0:
            raise ValueError("initial_cash must be a finite Decimal > 0")
        if self.fill_delay < timedelta(0):
            raise ValueError("fill_delay must be >= 0")
        if self.fill_delay > timedelta(0) and self.exec_timeframe is None:
            raise ValueError(
                "fill_delay > 0 needs a finer exec_timeframe (and an exec feed): with signal bars only, "
                "'first bar opening after close + delay' would skip a whole signal bar"
            )
        if self.exec_timeframe is not None and self.exec_timeframe.delta >= self.timeframe.delta:
            raise ValueError("exec_timeframe must be finer than the signal timeframe")
        if not 0.0 <= self.drift_tolerance < 1.0:
            raise ValueError("drift_tolerance must be in [0, 1)")
        if self.buy_buffer_bps < 0:
            raise ValueError("buy_buffer_bps must be >= 0")

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        filters: SymbolFilters,
        timeframe: Timeframe,
        initial_cash: Decimal = Decimal(10_000),
        fill_delay: timedelta = timedelta(0),
        exec_timeframe: Timeframe | None = None,
        slippage_model: SlippageModel | None = None,
    ) -> BacktestConfig:
        """Build from the project ``Config`` (assets and costs are taken from it)."""
        return cls(
            symbol=filters.symbol,
            timeframe=timeframe,
            filters=filters,
            costs=config.costs,
            base_asset=config.runtime.base_asset,
            quote_asset=config.runtime.quote_asset,
            initial_cash=initial_cash,
            fill_delay=fill_delay,
            exec_timeframe=exec_timeframe,
            slippage_model=slippage_model,
        )

    # -- derived cost objects ----------------------------------------------------------------

    def fee_schedule(self) -> FeeSchedule:
        return FeeSchedule.from_config(self.costs)

    def slippage(self) -> SlippageModel:
        if self.slippage_model is not None:
            return self.slippage_model
        return FlatSlippage.from_config(self.costs)
