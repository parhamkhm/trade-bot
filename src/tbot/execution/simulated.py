"""Simulated broker for backtests (simulation only: no network, no exchange SDK, no real orders).

``SimulatedBroker`` implements the ``Broker`` protocol from ``tbot.core.types``. It keeps its own
balances (the "exchange side" of the books, reconciled against the ``Portfolio`` by the engine) and
a book of pending MARKET orders. Time is driven from outside by :meth:`SimulatedBroker.advance`, which
is handed each execution bar as it becomes available:

* An order created at ``created_ts`` (the CLOSE of signal bar ``t``) is eligible only on a bar whose
  OPEN time is ``>= created_ts + fill_delay``. With ``fill_delay = 0`` that is the next bar, so the
  fill is at the OPEN of bar ``t+1``; it can never be filled at bar ``t``'s price (CLAUDE.md 3.1).
* The fill price is that bar's open plus adverse slippage from the configured ``SlippageModel`` (buys
  fill higher, sells lower), rounded adversely to the tick size. The fee is charged in the quote asset
  on the traded notional, at the taker rate for MARKET orders (SPEC D-047).
* Quantity must already be a multiple of the step size, at least ``min_qty`` and at most ``max_qty``
  (checked at submit); the fill-time notional must reach ``min_notional``. Violations are rejected,
  never silently rounded -- exactly what the live exchange will do with the same ``SymbolFilters``.
* LIMIT orders are rejected: SPEC defines no limit-fill semantics yet (needs a maker/queue model).

Fees and slippage come from ``FeeSchedule`` / ``SlippageModel`` objects built from configuration.
``SlippageModel`` is the hook where the depth-curve model of SPEC section 7 (G3) plugs in later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from tbot.core.config import CostConfig
from tbot.core.types import (
    Bar,
    Fill,
    OrderAck,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SymbolFilters,
)

__all__ = [
    "BrokerError",
    "ExecutionReport",
    "FeeSchedule",
    "FillRecord",
    "FlatSlippage",
    "OrderRejection",
    "SimulatedBroker",
    "SlippageContext",
    "SlippageModel",
    "UnknownOrderError",
]

_BPS = Decimal(10_000)
_ZERO = Decimal(0)


class BrokerError(RuntimeError):
    """Misuse of the simulated broker (non-monotonic time, cancelling a filled order, ...)."""


class UnknownOrderError(KeyError):
    """No order with that ``client_order_id`` was ever submitted."""


# ---------------------------------------------------------------------------------
# cost model (pluggable)
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-side fees in basis points of notional. Taker unless a strategy explicitly uses limits."""

    taker_bps: Decimal
    maker_bps: Decimal

    def __post_init__(self) -> None:
        for name in ("taker_bps", "maker_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"FeeSchedule.{name} must be a finite Decimal >= 0, got {value!r}")

    @classmethod
    def from_config(cls, costs: CostConfig) -> FeeSchedule:
        return cls(taker_bps=costs.taker_fee_bps, maker_bps=costs.maker_fee_bps)

    def fee(self, notional: Decimal, *, maker: bool = False) -> Decimal:
        return notional * (self.maker_bps if maker else self.taker_bps) / _BPS


@dataclass(frozen=True, slots=True)
class SlippageContext:
    """Everything a slippage model may look at. All of it is known at fill time, nothing later."""

    side: Side
    quantity: Decimal
    reference_price: Decimal  # the execution bar's open
    ts: datetime  # fill time (open time of the execution bar)


class SlippageModel(Protocol):
    """Adverse price impact in basis points of the reference price (always ``>= 0``).

    The default is :class:`FlatSlippage`. The depth-at-size curve of SPEC section 7 (G3) will be a second
    implementation of this protocol, evaluated at ``quantity * reference_price`` notional.
    """

    def slippage_bps(self, ctx: SlippageContext) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class FlatSlippage:
    """Placeholder model: the same number of bps on every fill."""

    bps: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.bps, Decimal) or not self.bps.is_finite() or self.bps < 0:
            raise ValueError(f"FlatSlippage.bps must be a finite Decimal >= 0, got {self.bps!r}")

    @classmethod
    def from_config(cls, costs: CostConfig) -> FlatSlippage:
        return cls(bps=costs.slippage_bps)

    def slippage_bps(self, ctx: SlippageContext) -> Decimal:  # noqa: ARG002 - flat by definition
        return self.bps


# ---------------------------------------------------------------------------------
# records returned to the engine
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillRecord:
    """A ``Fill`` plus the facts needed for cost attribution."""

    fill: Fill
    reference_price: Decimal  # the open the fill was priced from, before slippage
    slippage_cost: Decimal  # quote asset, >= 0: |fill price - reference| * quantity (incl. tick rounding)


@dataclass(frozen=True, slots=True)
class OrderRejection:
    client_order_id: str
    ts: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What happened to pending orders when one execution bar was processed."""

    fills: tuple[FillRecord, ...] = ()
    rejections: tuple[OrderRejection, ...] = ()


# ---------------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Order:
    req: OrderRequest
    exchange_order_id: str
    state: OrderState
    filled_qty: Decimal = _ZERO
    avg_price: Decimal | None = None
    last_ts: datetime | None = None


class SimulatedBroker:
    """Deterministic in-memory broker. See the module docstring for the fill semantics."""

    def __init__(
        self,
        *,
        filters: SymbolFilters,
        fees: FeeSchedule,
        slippage: SlippageModel,
        base_asset: str,
        quote_asset: str,
        initial_cash: Decimal,
        fill_delay: timedelta = timedelta(0),
    ) -> None:
        if fill_delay < timedelta(0):
            raise ValueError("fill_delay must be >= 0")
        if base_asset == quote_asset:
            raise ValueError("base_asset and quote_asset must differ")
        self._filters = filters
        self._fees = fees
        self._slippage = slippage
        self._base = base_asset
        self._quote = quote_asset
        self._delay = fill_delay
        self._cash = initial_cash
        self._qty = _ZERO
        self._orders: dict[str, _Order] = {}
        self._pending: list[str] = []  # client_order_ids in submission order
        self._last_bar_ts: datetime | None = None
        self._seq = 0

    # -- Broker protocol -------------------------------------------------------------------

    def submit(self, req: OrderRequest) -> OrderAck:
        """Accept (or reject) an order. Never fills it: fills happen in :meth:`advance`."""
        if req.client_order_id in self._orders:  # idempotent: the second submit creates nothing
            existing = self._orders[req.client_order_id]
            return OrderAck(
                client_order_id=req.client_order_id,
                exchange_order_id=existing.exchange_order_id,
                ts=req.created_ts,
                accepted=False,
                message="DUPLICATE_CLIENT_ORDER_ID: order already known, nothing was created",
            )
        self._seq += 1
        order = _Order(req=req, exchange_order_id=f"sim-{self._seq}", state=OrderState.NEW)
        self._orders[req.client_order_id] = order
        problem = self._validate(req)
        if problem is not None:
            order.state = OrderState.REJECTED
            order.last_ts = req.created_ts
            return OrderAck(req.client_order_id, order.exchange_order_id, req.created_ts, False, problem)
        self._pending.append(req.client_order_id)
        return OrderAck(req.client_order_id, order.exchange_order_id, req.created_ts, True, "")

    def cancel(self, client_order_id: str) -> None:
        order = self._get(client_order_id)
        if not order.state.is_open:
            raise BrokerError(f"cannot cancel {client_order_id}: order is {order.state.value}")
        order.state = OrderState.CANCELED
        self._pending.remove(client_order_id)

    def get_order(self, client_order_id: str) -> OrderStatus:
        order = self._get(client_order_id)
        req = order.req
        return OrderStatus(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            state=order.state,
            quantity=req.quantity,
            filled_quantity=order.filled_qty,
            ts=order.last_ts or req.created_ts,
            exchange_order_id=order.exchange_order_id,
            avg_fill_price=order.avg_price,
        )

    def open_orders(self, symbol: str) -> list[OrderStatus]:
        return [self.get_order(cid) for cid in self._pending if self._orders[cid].req.symbol == symbol]

    def balances(self) -> dict[str, Decimal]:
        return {self._quote: self._cash, self._base: self._qty}

    # -- simulation clock ------------------------------------------------------------------

    def advance(self, bar: Bar) -> ExecutionReport:
        """Process one execution bar: fill every eligible pending MARKET order at this bar's open.

        Eligibility is ``bar.open_ts >= created_ts + fill_delay``. Bars must arrive in strictly
        increasing order. Only ``bar.open`` is used for pricing; high/low/close are never looked at.
        """
        if bar.symbol != self._filters.symbol:
            raise BrokerError(f"bar for {bar.symbol} fed to a {self._filters.symbol} broker")
        if self._last_bar_ts is not None and bar.ts <= self._last_bar_ts:
            raise BrokerError(f"bars must be strictly increasing ({bar.ts} after {self._last_bar_ts})")
        self._last_bar_ts = bar.ts
        fills: list[FillRecord] = []
        rejections: list[OrderRejection] = []
        still_pending: list[str] = []
        for cid in self._pending:
            order = self._orders[cid]
            if bar.open_ts < order.req.created_ts + self._delay:
                still_pending.append(cid)
                continue
            outcome = self._fill_at_open(order, bar)
            if isinstance(outcome, FillRecord):
                fills.append(outcome)
            else:
                rejections.append(outcome)
        self._pending = still_pending
        return ExecutionReport(tuple(fills), tuple(rejections))

    # -- internals -------------------------------------------------------------------------

    def _get(self, client_order_id: str) -> _Order:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise UnknownOrderError(client_order_id) from None

    def _validate(self, req: OrderRequest) -> str | None:
        flt = self._filters
        if req.symbol != flt.symbol:
            return f"UNKNOWN_FILTERS: no filters for {req.symbol}"
        if req.type is not OrderType.MARKET:
            return (
                f"UNSUPPORTED_ORDER_TYPE: {req.type.value} orders are not simulated "
                "(SPEC defines no limit-fill semantics)"
            )
        if flt.floor_qty(req.quantity) != req.quantity:
            return f"INVALID_QUANTITY: {req.quantity} is not a multiple of step size {flt.step_size}"
        if req.quantity < flt.min_qty:
            return f"MIN_QUANTITY: {req.quantity} < {flt.min_qty}"
        if flt.max_qty is not None and req.quantity > flt.max_qty:
            return f"MAX_QUANTITY: {req.quantity} > {flt.max_qty}"
        return None

    def _fill_price(self, side: Side, quantity: Decimal, bar: Bar) -> Decimal:
        ctx = SlippageContext(side=side, quantity=quantity, reference_price=bar.open, ts=bar.open_ts)
        bps = self._slippage.slippage_bps(ctx)
        if bps < 0:
            raise BrokerError(f"slippage model returned negative bps ({bps}); slippage is always adverse")
        tick = self._filters.tick_size
        if side is Side.BUY:
            raw = bar.open * (1 + bps / _BPS)
            return (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        raw = bar.open * (1 - bps / _BPS)
        return (raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    def _fill_at_open(self, order: _Order, bar: Bar) -> FillRecord | OrderRejection:
        req = order.req
        price = self._fill_price(req.side, req.quantity, bar)
        notional = price * req.quantity
        fee = self._fees.fee(notional)
        reason = self._fill_problem(req, price, notional, fee)
        if reason is not None:
            order.state, order.last_ts = OrderState.REJECTED, bar.open_ts
            return OrderRejection(req.client_order_id, bar.open_ts, reason)
        if req.side is Side.BUY:
            self._cash -= notional + fee
            self._qty += req.quantity
        else:
            self._cash += notional - fee
            self._qty -= req.quantity
        order.state, order.filled_qty = OrderState.FILLED, req.quantity
        order.avg_price, order.last_ts = price, bar.open_ts
        fill = Fill(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            ts=bar.open_ts,
            price=price,
            quantity=req.quantity,
            fee=fee,
            fee_asset=self._quote,
            exchange_order_id=order.exchange_order_id,
        )
        return FillRecord(fill, bar.open, abs(price - bar.open) * req.quantity)

    def _fill_problem(self, req: OrderRequest, price: Decimal, notional: Decimal, fee: Decimal) -> str | None:
        if not self._filters.passes_notional(price, req.quantity):
            return f"MIN_NOTIONAL: {notional} < {self._filters.min_notional}"
        if req.side is Side.BUY and notional + fee > self._cash:
            return f"INSUFFICIENT_FUNDS: need {notional + fee}, have {self._cash} {self._quote}"
        if req.side is Side.SELL and req.quantity > self._qty:
            return f"INSUFFICIENT_FUNDS: need {req.quantity}, have {self._qty} {self._base}"
        return None
