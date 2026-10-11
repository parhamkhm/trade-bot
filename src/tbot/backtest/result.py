"""Result containers returned by ``run_backtest``. Plain frozen data; no logic beyond cheap accessors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tbot.backtest.config import BacktestConfig
from tbot.execution.simulated import FillRecord, OrderRejection, ResizeRecord
from tbot.portfolio.ledger import TradeRecord
from tbot.portfolio.portfolio import EquityPoint

__all__ = ["BacktestResult", "DecisionRecord"]

# DecisionRecord.outcome values
ORDER = "ORDER"  # the RiskManager approved an order and the broker accepted it
NO_ACTION = "NO_ACTION"  # approved with no order (target already met / within tolerance)
REFUSED = "REFUSED"  # the RiskManager refused (reason in detail)
SUBMIT_REJECTED = "SUBMIT_REJECTED"  # the broker refused the submission
SKIPPED_PENDING = "SKIPPED_PENDING_ORDER"  # an earlier order was still unfilled: no new decision stacked


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """What happened to the intent produced at the close of one signal bar."""

    ts: datetime
    target_weight: float  # after the regime multiplier
    outcome: str
    detail: str = ""
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything validation and reporting need: positions per bar, fills, equity, trades, costs."""

    config: BacktestConfig
    strategy_id: str
    equity: tuple[EquityPoint, ...]  # one per signal bar, marked at that bar's CLOSE
    fills: tuple[FillRecord, ...]
    rejections: tuple[OrderRejection, ...]  # orders the broker rejected at fill time
    resizes: tuple[ResizeRecord, ...]  # BUYs capped at fill time to what cash affords (D-051), by kind
    decisions: tuple[DecisionRecord, ...]
    trades: tuple[TradeRecord, ...]  # closed flat -> flat round trips
    first_decision_index: int | None  # index into ``equity`` of the first bar a strategy decision was made
    unfilled_order_ids: tuple[str, ...]  # still pending when the data ended
    fees_paid: Decimal
    slippage_cost: Decimal
    traded_notional: Decimal

    @property
    def initial_cash(self) -> Decimal:
        return self.config.initial_cash

    @property
    def n_bars(self) -> int:
        return len(self.equity)

    def positions(self) -> list[Decimal]:
        """Base quantity held at the close of each signal bar (after any fill at that bar's open)."""
        return [p.quantity for p in self.equity]

    def weights(self) -> list[float]:
        return [p.weight for p in self.equity]

    def equity_values(self, start_index: int | None = None) -> list[float]:
        """Equity as floats. With ``start_index=None``: the initial cash followed by every bar close.
        With an index ``k``: closes from bar ``k`` on (bar ``k``'s close is the baseline)."""
        if start_index is None:
            return [float(self.initial_cash)] + [float(p.equity) for p in self.equity]
        return [float(p.equity) for p in self.equity[start_index:]]
