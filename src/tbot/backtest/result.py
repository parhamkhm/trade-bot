"""Result containers returned by ``run_backtest``. Plain frozen data; no logic beyond cheap accessors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tbot.backtest.config import BacktestConfig
from tbot.execution.simulated import FillRecord, OrderRejection, ResizeRecord
from tbot.portfolio.ledger import TradeRecord
from tbot.portfolio.portfolio import EquityPoint

__all__ = ["BacktestResult", "DecisionRecord", "StopEvent"]

# DecisionRecord.outcome values
ORDER = "ORDER"  # the RiskManager approved an order and the broker accepted it
NO_ACTION = "NO_ACTION"  # approved with no order (target already met / within the rebalance band)
REFUSED = "REFUSED"  # the RiskManager refused (reason in detail)
SUBMIT_REJECTED = "SUBMIT_REJECTED"  # the broker refused the submission
SKIPPED_PENDING = "SKIPPED_PENDING_ORDER"  # an earlier order was still unfilled: no new decision stacked
AWAITING_REARM = "AWAITING_REARM"  # stopped out, strategy target still > 0: no order until it passes 0

# StopEvent.kind values
STOP_ARMED = "STOP_ARMED"  # the stop level of a new position was computed (at P_ref) and accepted
STOP_FILL = "STOP_FILL"  # the engine sold the position at the stop
STOP_REJECTED = "STOP_REJECTED"  # a stop exit was rejected by the broker (below min_notional)
ENTRY_REFUSED = "ENTRY_REFUSED_INVALID_STOP"  # D-050: no valid stop at P_ref, pending entry cancelled
CANCELLED_BY_STOP = "ORDER_CANCELLED_BY_STOP"  # a pending order cancelled when the stop sold the position


@dataclass(frozen=True, slots=True)
class StopEvent:
    """One protective-stop event. Stop bookkeeping lives here, NOT in ``DecisionRecord``: a decision is
    written once at the signal close, while stop events happen on later execution bars (and must never
    mutate an earlier record, or a longer run would disagree with a truncated one about the past)."""

    ts: datetime  # open time of the execution bar the event happened on
    kind: str
    client_order_id: str | None = None  # the entry order (ARMED / REFUSED) or the stop exit order id
    level: Decimal | None = None  # the stop level
    detail: str = ""


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
    resizes: tuple[ResizeRecord, ...]  # BUYs capped at fill time (D-051), by cause
    decisions: tuple[DecisionRecord, ...]
    trades: tuple[TradeRecord, ...]  # closed flat -> flat round trips
    first_decision_index: int | None  # index into ``equity`` of the first bar a strategy decision was made
    unfilled_order_ids: tuple[str, ...]  # still pending when the data ended
    fees_paid: Decimal
    slippage_cost: Decimal
    traded_notional: Decimal
    stop_events: tuple[StopEvent, ...] = ()

    @property
    def stop_fills(self) -> tuple[FillRecord, ...]:
        """Fills that were protective-stop exits (also present in ``fills``)."""
        return tuple(f for f in self.fills if f.stop_fill)

    @property
    def entries_refused_invalid_stop(self) -> int:
        """Entries cancelled because the strategy gave no valid stop at P_ref (D-050)."""
        return sum(1 for e in self.stop_events if e.kind == ENTRY_REFUSED)

    @property
    def orders_cancelled_by_engine(self) -> int:
        """Approved orders the engine cancelled before they could fill (refused entry, or pending when a
        stop sold the position). They are neither fills nor ``unfilled_order_ids``."""
        return sum(1 for e in self.stop_events if e.kind in (ENTRY_REFUSED, CANCELLED_BY_STOP))

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
