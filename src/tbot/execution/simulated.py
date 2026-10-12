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
* **In BOTH fill modes (next-open and manual delay, D-051) a BUY is sized at fill time, not rejected, when
  the price has moved against it since the risk manager sized it at ``close(t)``.** At fill time the
  quantity is capped at ``floor_to_step(cash / (fill_price * (1 + taker_fee_rate)))`` -- the most the
  current cash can still afford at the actual fill price. This mirrors live behaviour: an automated market
  buy is sized at the open from the real price and the real cash (quote-amount market buy), and a manual
  order is sized when it is placed. Every cap is reported as an explicit :class:`ResizeRecord` (requested
  vs. filled quantity, reason, kind), never silently, is counted in the report, and is queryable per order
  via :meth:`SimulatedBroker.resize_of`. A cap that leaves zero, or less than ``min_qty`` /
  ``min_notional``, is still rejected (and counted as a fill rejection, which the report's INVALID RUN rule
  flags).
* The fill price is re-derived at the FINAL capped quantity (the slippage model may be size dependent, e.g.
  the depth curve of SPEC section 7): cap, re-price, re-check affordability, repeat until the quantity is
  stable (at most ``_MAX_CAP_ITERATIONS`` rounds; the quantity only ever shrinks). The books are never
  overdrawn: if the loop bound is hit, the last candidate still fills when cash affords it at its own
  price, and is rejected with ``INSUFFICIENT_FUNDS`` only when it does not.
* Resize kinds are decided by CAUSE, not by size (Parham, 2026-10-11). The risk manager sizes a BUY from the
  signal bar's close (the SIZING PRICE) and the cash on hand when the order is submitted (the SIZING CASH).
  The backtest engine hands both to :meth:`SimulatedBroker.submit` through the keyword-only ``sizing_price``
  / ``sizing_cash`` arguments (``OrderRequest`` is frozen and has no such field; the extra optional keywords
  keep the ``Broker`` protocol signature compatible). When a cap happens at fill time the broker asks one
  question: was the REQUESTED quantity affordable at the sizing price,
  ``quantity * sizing_price * (1 + taker_fee_rate) <= sizing_cash``?
  Yes: the order was sized correctly and only the price moved afterwards -- ``"price_move"``, whatever the
  size of the move (+0.4 %, +5 %, a 6 h drift). No: the request exceeded cash even at the price it was sized
  with -- a sizing bug, ``"anomaly"``. The broker still fills an anomaly (the cap itself is correct), but the
  backtest report flags the run INVALID exactly as it does for a fill rejection.
  The affordability test does NOT depend on a cap happening or on the price direction: it runs at FILL time,
  on the reference stored at submit, for EVERY BUY that has one. An oversized request whose price then falls
  fills uncapped, and is still recorded as an ``"anomaly"`` (a :class:`ResizeRecord` with
  ``capped_quantity == requested_quantity``; :meth:`SimulatedBroker.was_resized` stays False for it).
  FAIL SAFE: a capped BUY with no sizing reference (a direct broker user that passed neither argument) is an
  ``"anomaly"`` with reason "no sizing reference", because the broker cannot prove the cap was caused by
  price movement. An uncapped BUY with no reference is not recorded: nothing is known to be wrong.
* A SELL is never capped in either mode: it is rejected whole if the position does not cover it -- a
  long-only book has no symmetric "resize down" for an exit.
* LIMIT orders are rejected: SPEC defines no limit-fill semantics yet (needs a maker/queue model).
* Protective stops (SPEC section 7.1, D-050): :meth:`SimulatedBroker.stop_out` sells the WHOLE position when
  the execution bar's low reached the stop level. The reference price is ``min(open, stop)`` -- the stop
  level when the bar traded down through it, the open when the bar gapped below it -- and the usual adverse
  slippage (floored to the tick) and taker fee are charged on top, so a stop exit is never free. The fill
  is stamped with the bar's open time (the intra-bar time of the touch is unknown), is flagged
  ``stop_fill=True`` and carries its own ``stop-NNNNNN`` order id. Which level applies, when it is armed and
  what is cancelled around it is the ENGINE's business; the broker only prices the exit.

Fees and slippage come from ``FeeSchedule`` / ``SlippageModel`` objects built from configuration.
``SlippageModel`` is the hook for the depth-curve model of SPEC section 7 (G3): :class:`DepthCurveSlippage`.
The context handed to the model carries the account equity at the bar's open (cash + position), which only
the optional equity normaliser of the depth curve uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from typing import Literal, Protocol

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
    "CurvePoints",
    "DepthCurveSlippage",
    "ExecutionReport",
    "FeeSchedule",
    "FillRecord",
    "FlatSlippage",
    "OrderRejection",
    "ResizeKind",
    "ResizeRecord",
    "SimulatedBroker",
    "SlippageContext",
    "SlippageModel",
    "UnknownOrderError",
]

_BPS = Decimal(10_000)
_ZERO = Decimal(0)
_MAX_CAP_ITERATIONS = 8  # cap -> re-price -> re-check rounds; the quantity only shrinks, so this is generous

ResizeKind = Literal["price_move", "anomaly"]


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
    reference_price: Decimal  # the price the slippage is added to (the open; a stop exit: min(open, stop))
    ts: datetime  # fill time (open time of the execution bar)
    # Account equity (cash + position marked at the bar's open) just before this fill, in the quote asset.
    # ``None`` when the caller cannot supply it; only models with an equity normaliser need it.
    equity: Decimal | None = None


class SlippageModel(Protocol):
    """Adverse price impact in basis points of the reference price (always ``>= 0``).

    The default is :class:`FlatSlippage`. :class:`DepthCurveSlippage` (SPEC section 7 G3 / D-067) is the
    size-dependent implementation, evaluated at ``quantity * reference_price`` notional.
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


CurvePoints = tuple[tuple[Decimal, Decimal], ...]  # ((notional_quote, cost_bps), ...) sorted by notional


def _validated_curve(name: str, raw: object) -> CurvePoints:
    """Normalise and check one side of a depth curve (see :class:`DepthCurveSlippage`)."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"DepthCurveSlippage.{name} must be a list/tuple of (notional, cost_bps) pairs")
    points: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"DepthCurveSlippage.{name}: every point must be a (notional, cost_bps) pair")
        n, c = item
        if not (isinstance(n, Decimal) and isinstance(c, Decimal) and n.is_finite() and c.is_finite()):
            raise ValueError(f"DepthCurveSlippage.{name}: points must be finite Decimals, got {item!r}")
        points.append((n, c))
    if len(points) < 2:
        raise ValueError(f"DepthCurveSlippage.{name} needs at least 2 points (origin (0, 0) plus one more)")
    if points[0] != (_ZERO, _ZERO):
        raise ValueError(f"DepthCurveSlippage.{name} must start at (0, 0), got {points[0]!r}")
    for (n0, c0), (n1, c1) in pairwise(points):
        if n1 <= n0:
            raise ValueError(f"DepthCurveSlippage.{name}: notionals must be strictly increasing")
        if c1 < c0:
            raise ValueError(
                f"DepthCurveSlippage.{name}: cost_bps must not decrease with size (impact is adverse), "
                f"got {c0} then {c1}"
            )
    return tuple(points)


def _curve_bps(points: CurvePoints, notional: Decimal) -> Decimal:
    """Piecewise-linear cost in bps at ``notional``; beyond the last point the slope of the last two."""
    if notional <= _ZERO:
        return _ZERO
    for (n0, c0), (n1, c1) in pairwise(points):
        if notional <= n1:
            return c0 + (c1 - c0) * (notional - n0) / (n1 - n0)
    (n0, c0), (n1, c1) = points[-2], points[-1]
    return c1 + (c1 - c0) * (notional - n1) / (n1 - n0)


@dataclass(frozen=True, slots=True)
class DepthCurveSlippage:
    """Depth-at-size slippage (SPEC section 7 G3, D-067): cost grows with the order's own notional.

    ``buy_points`` / ``sell_points`` are the curve of each side as ``(notional_quote, cost_bps)`` pairs,
    sorted by strictly increasing notional, starting at ``(0, 0)``, cost non-decreasing. The points are INPUT:
    nothing about how they are measured is hard-coded here (the provisional construction lives in
    :func:`tbot.backtest.slippage.curve_from_g0_summary`).

    The cost of an order of notional ``n`` on its side is the piecewise-linear interpolation of the curve at
    ``n``; beyond the last point it is extrapolated linearly at the slope of the last two points. Then
    ``half_spread_bps`` is added exactly once. The result is adverse by construction (buys fill higher, sells
    lower: the broker applies the sign), and the broker prices at ``quantity * reference_price`` of the FINAL
    quantity (D-081 re-pricing).

    ``equity_normaliser`` (the SPEC G3 rule "evaluated at the trade's own notional with the backtest equity
    fixed at 10,000 USDT") is optional: when set to ``E``, the lookup notional is
    ``order_notional * E / current_equity``, i.e. the order is looked up as if the account were ``E`` big
    (the same fraction of equity costs the same bps at any account size). ``None`` looks up the raw notional.
    """

    buy_points: CurvePoints
    sell_points: CurvePoints
    half_spread_bps: Decimal
    equity_normaliser: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "buy_points", _validated_curve("buy_points", self.buy_points))
        object.__setattr__(self, "sell_points", _validated_curve("sell_points", self.sell_points))
        hs = self.half_spread_bps
        if not isinstance(hs, Decimal) or not hs.is_finite() or hs < 0:
            raise ValueError(f"DepthCurveSlippage.half_spread_bps must be a finite Decimal >= 0, got {hs!r}")
        eq = self.equity_normaliser
        if eq is not None and (not isinstance(eq, Decimal) or not eq.is_finite() or eq <= 0):
            raise ValueError(f"DepthCurveSlippage.equity_normaliser must be a finite Decimal > 0, got {eq!r}")

    def scaled(self, factor: Decimal) -> DepthCurveSlippage:
        """Copy with every ``cost_bps`` AND the half spread multiplied by ``factor`` (the x2 stress)."""
        if not isinstance(factor, Decimal) or not factor.is_finite() or factor < 0:
            raise ValueError(f"scale factor must be a finite Decimal >= 0, got {factor!r}")

        def scale(points: CurvePoints) -> CurvePoints:
            return tuple((n, c * factor) for n, c in points)

        return DepthCurveSlippage(
            buy_points=scale(self.buy_points),
            sell_points=scale(self.sell_points),
            half_spread_bps=self.half_spread_bps * factor,
            equity_normaliser=self.equity_normaliser,
        )

    def lookup_notional(self, ctx: SlippageContext) -> Decimal:
        """The notional the curve is read at: the order's own, rescaled when an equity normaliser is set."""
        notional = ctx.quantity * ctx.reference_price
        if self.equity_normaliser is None:
            return notional
        if ctx.equity is None or ctx.equity <= _ZERO:
            raise ValueError(
                f"equity_normaliser needs a positive equity in the SlippageContext, got {ctx.equity!r}"
            )
        return notional * self.equity_normaliser / ctx.equity

    def slippage_bps(self, ctx: SlippageContext) -> Decimal:
        points = self.buy_points if ctx.side is Side.BUY else self.sell_points
        return _curve_bps(points, self.lookup_notional(ctx)) + self.half_spread_bps


# ---------------------------------------------------------------------------------
# records returned to the engine
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillRecord:
    """A ``Fill`` plus the facts needed for cost attribution."""

    fill: Fill
    reference_price: Decimal  # the open the fill was priced from, before slippage
    slippage_cost: Decimal  # quote asset, >= 0: |fill price - reference| * quantity (incl. tick rounding)
    gap_fill: bool = False  # bar.open_ts != created_ts + fill_delay: the feed had a gap at the deadline
    stop_fill: bool = False  # a protective-stop exit (SimulatedBroker.stop_out), not a strategy order


@dataclass(frozen=True, slots=True)
class OrderRejection:
    client_order_id: str
    ts: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ResizeRecord:
    """A fill-time sizing event for a BUY (see the module docstring): either the quantity was capped down
    because cash no longer covered the amount it was sized at, or the request was not affordable at the
    sizing price (an ``"anomaly"``, which is recorded even when no cap followed, so
    ``capped_quantity == requested_quantity``). Logged whether the order then filled or was still rejected
    (e.g. the capped quantity fell below ``min_qty``/``min_notional``): ``filled`` says which.

    ``capped_quantity`` is the quantity the cap left, NOT necessarily what was filled: when ``filled`` is
    False nothing was filled and the order is also in the report's rejections.

    ``kind`` is decided by cause. ``"price_move"``: the requested quantity WAS affordable at the sizing price
    (``requested_quantity * sizing_price * (1 + taker fee rate) <= sizing_cash``), so only the price moved
    between sizing and fill. ``"anomaly"``: it was NOT affordable even at the sizing price (a sizing bug), or
    no sizing reference was supplied (fail safe; ``sizing_price`` / ``sizing_cash`` are then ``None``).
    ``reason`` states the cause with the numbers."""

    client_order_id: str
    ts: datetime
    requested_quantity: Decimal
    capped_quantity: Decimal
    filled: bool
    reason: str
    kind: ResizeKind
    sizing_price: Decimal | None = None  # close of the signal bar the order was sized at
    sizing_cash: Decimal | None = None  # cash on hand when the order was submitted

    @property
    def capped_fraction(self) -> Decimal:
        """Share of the requested quantity that was capped away, in ``[0, 1]``."""
        return (self.requested_quantity - self.capped_quantity) / self.requested_quantity


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What happened to pending orders when one execution bar was processed."""

    fills: tuple[FillRecord, ...] = ()
    rejections: tuple[OrderRejection, ...] = ()
    resizes: tuple[ResizeRecord, ...] = ()


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
    resize: ResizeRecord | None = None
    sizing_price: Decimal | None = None
    sizing_cash: Decimal | None = None


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
        self._stop_seq = 0

    # -- Broker protocol -------------------------------------------------------------------

    def submit(
        self,
        req: OrderRequest,
        *,
        sizing_price: Decimal | None = None,
        sizing_cash: Decimal | None = None,
    ) -> OrderAck:
        """Accept (or reject) an order. Never fills it: fills happen in :meth:`advance`.

        ``sizing_price`` / ``sizing_cash`` are the backtest-only sizing reference: the signal-bar close the
        risk manager sized the order at and the cash on hand at that moment. They are stored with the order
        and used only to classify a fill-time BUY cap by cause (module docstring). They are optional so the
        method still satisfies the ``Broker`` protocol; give both or neither. A capped BUY submitted without
        them is classified ``"anomaly"`` (fail safe). The reference of a duplicate submit is ignored.
        """
        if (sizing_price is None) != (sizing_cash is None):
            raise ValueError("sizing_price and sizing_cash must be given together or not at all")
        if sizing_price is not None and (not sizing_price.is_finite() or sizing_price <= _ZERO):
            raise ValueError(f"sizing_price must be a finite Decimal > 0, got {sizing_price!r}")
        if sizing_cash is not None and (not sizing_cash.is_finite() or sizing_cash < _ZERO):
            raise ValueError(f"sizing_cash must be a finite Decimal >= 0, got {sizing_cash!r}")
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
        order = _Order(
            req=req,
            exchange_order_id=f"sim-{self._seq}",
            state=OrderState.NEW,
            sizing_price=sizing_price,
            sizing_cash=sizing_cash,
        )
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
        """Current status of an order.

        ``quantity`` is always the REQUESTED quantity. A BUY that was capped at fill time (both fill modes,
        see the module docstring) is reported as ``FILLED`` with ``filled_quantity < quantity``
        (``remaining`` is then the resized-away amount, NOT working on the book; nothing is left pending).
        ``OrderStatus`` has no resized flag (``core/types.py`` is frozen for this agent), so use
        :meth:`resize_of` / :meth:`was_resized` to tell a resized fill from any other state.
        """
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

    def resize_of(self, client_order_id: str) -> ResizeRecord | None:
        """The fill-time sizing record of this order (cap and/or anomaly), if any."""
        return self._get(client_order_id).resize

    def was_resized(self, client_order_id: str) -> bool:
        """True when the order's quantity was actually capped (an uncapped anomaly record is not a resize)."""
        rec = self.resize_of(client_order_id)
        return rec is not None and rec.capped_quantity != rec.requested_quantity

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
        resizes: list[ResizeRecord] = []
        still_pending: list[str] = []
        for cid in self._pending:
            order = self._orders[cid]
            if bar.open_ts < order.req.created_ts + self._delay:
                still_pending.append(cid)
                continue
            outcome, resize = self._fill_at_open(order, bar)
            if resize is not None:
                resizes.append(resize)
            if isinstance(outcome, FillRecord):
                fills.append(outcome)
            else:
                rejections.append(outcome)
        self._pending = still_pending
        return ExecutionReport(tuple(fills), tuple(rejections), tuple(resizes))

    def stop_out(self, bar: Bar, stop: Decimal) -> FillRecord | OrderRejection | None:
        """Sell the whole position if ``bar`` traded down to ``stop`` (module docstring, protective stops).

        Must be called after :meth:`advance` of the SAME bar, so a strategy order that fills at this bar's
        open is booked before the stop is tested (an entry can be stopped on its own bar; an exit order at
        the open leaves nothing to stop). Returns ``None`` when there is no position or ``bar.low > stop``.
        Otherwise the position is sold at ``min(bar.open, stop)`` less adverse slippage (floored to the tick),
        with the taker fee; a stop exit that would fall below ``min_notional`` is returned as an
        :class:`OrderRejection` and the position is left untouched (the run is then flagged INVALID).
        """
        if bar.symbol != self._filters.symbol:
            raise BrokerError(f"bar for {bar.symbol} fed to a {self._filters.symbol} broker")
        if self._last_bar_ts != bar.ts:
            raise BrokerError("stop_out must be called after advance() of the same bar")
        if not stop.is_finite() or stop <= _ZERO:
            raise ValueError(f"stop must be a finite Decimal > 0, got {stop!r}")
        if self._qty <= _ZERO or bar.low > stop:
            return None
        quantity = self._qty
        reference = min(bar.open, stop)
        price = self._fill_price(Side.SELL, quantity, bar, reference)
        notional = price * quantity
        fee = self._fees.fee(notional)
        self._seq += 1
        self._stop_seq += 1
        cid = f"stop-{self._stop_seq:06d}"
        req = OrderRequest(
            client_order_id=cid,
            symbol=self._filters.symbol,
            side=Side.SELL,
            type=OrderType.MARKET,
            quantity=quantity,
            created_ts=bar.open_ts,
        )
        order = _Order(req=req, exchange_order_id=f"sim-{self._seq}", state=OrderState.NEW)
        self._orders[cid] = order
        order.last_ts = bar.open_ts
        if not self._filters.passes_notional(price, quantity):
            order.state = OrderState.REJECTED
            reason = f"MIN_NOTIONAL: stop exit {notional} < {self._filters.min_notional}"
            return OrderRejection(cid, bar.open_ts, reason)
        self._cash += notional - fee
        self._qty -= quantity
        order.state, order.filled_qty, order.avg_price = OrderState.FILLED, quantity, price
        fill = Fill(
            client_order_id=cid,
            symbol=req.symbol,
            side=Side.SELL,
            ts=bar.open_ts,
            price=price,
            quantity=quantity,
            fee=fee,
            fee_asset=self._quote,
            exchange_order_id=order.exchange_order_id,
        )
        return FillRecord(fill, reference, abs(price - reference) * quantity, stop_fill=True)

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

    def _fill_price(
        self, side: Side, quantity: Decimal, bar: Bar, reference: Decimal | None = None
    ) -> Decimal:
        """Adverse-slipped, tick-rounded price; ``reference`` defaults to the bar's open (a stop exit
        passes ``min(open, stop)``)."""
        ref = bar.open if reference is None else reference
        equity = self._cash + self._qty * bar.open  # marked at the bar's open, before this fill
        ctx = SlippageContext(
            side=side, quantity=quantity, reference_price=ref, ts=bar.open_ts, equity=equity
        )
        bps = self._slippage.slippage_bps(ctx)
        if bps < 0:
            raise BrokerError(f"slippage model returned negative bps ({bps}); slippage is always adverse")
        tick = self._filters.tick_size
        if side is Side.BUY:
            raw = ref * (1 + bps / _BPS)
            return (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        raw = ref * (1 - bps / _BPS)
        return (raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    def _affordable_quantity(self, price: Decimal) -> Decimal:
        """Most the current cash buys at ``price`` including the taker fee, floored to the step size."""
        unit_cost = price * (1 + self._fees.taker_bps / _BPS)
        return self._filters.floor_qty(self._cash / unit_cost)

    def _size_at_fill(self, req: OrderRequest, bar: Bar) -> tuple[Decimal, Decimal]:
        """Final ``(quantity, fill_price)`` for an order at this bar's open (module docstring).

        SELLs are never capped. A BUY is capped to what cash affords at the fill price, and the price is
        then re-derived at the capped quantity (a size-dependent slippage model prices a smaller order
        better); this repeats until the quantity is stable. The quantity only ever shrinks, so the loop
        ends; if the bound is hit anyway the last candidate is returned unconfirmed: it still fills when cash
        affords it at its own price, and ``_fill_problem`` rejects it otherwise, so the books never go
        negative."""
        quantity = req.quantity
        price = self._fill_price(req.side, quantity, bar)
        if req.side is not Side.BUY:
            return quantity, price
        for _ in range(_MAX_CAP_ITERATIONS):
            affordable = self._affordable_quantity(price)
            if affordable >= quantity:
                break
            quantity = affordable
            if quantity <= _ZERO:
                break
            price = self._fill_price(req.side, quantity, bar)
        return quantity, price

    def _sizing_record(
        self, order: _Order, bar: Bar, quantity: Decimal, *, filled: bool
    ) -> ResizeRecord | None:
        """Fill-time sizing record of a BUY, or ``None`` when there is nothing to report.

        The cause test (module docstring) runs for every BUY with a sizing reference, capped or not:
        ``requested * sizing_price * (1 + taker fee) > sizing_cash`` is an anomaly whatever the fill price
        did. A capped BUY that was affordable at the sizing price is a ``price_move``; a capped BUY with no
        reference is an anomaly (fail safe); an uncapped BUY with no reference gets no record."""
        req = order.req
        price, cash = order.sizing_price, order.sizing_cash
        capped = quantity != req.quantity
        fraction = (req.quantity - quantity) / req.quantity
        kind: ResizeKind
        if price is None or cash is None:
            if not capped:
                return None
            reason = (
                "anomaly: no sizing reference (sizing_price / sizing_cash) was supplied with the order, so "
                f"the broker cannot show that the {fraction:.3%} cap was caused by price movement (fail safe)"
            )
            return ResizeRecord(
                req.client_order_id, bar.open_ts, req.quantity, quantity, filled, reason, "anomaly"
            )
        fee_rate = self._fees.taker_bps / _BPS
        cost = req.quantity * price * (1 + fee_rate)
        numbers = (
            f"requested {req.quantity} x sizing price {price} x (1 + taker fee {fee_rate:.4%}) = "
            f"{cost} {self._quote} vs cash at sizing {cash} {self._quote}"
        )
        if cost > cash:
            kind = "anomaly"
            outcome = (
                f"the fill then capped {fraction:.3%} of the quantity"
                if capped
                else f"the fill price (open {bar.open}) needed no cap, so it filled in full"
            )
            reason = (
                f"anomaly: the request was not affordable even at the sizing price ({numbers}); "
                f"sizing bug; {outcome}"
            )
        elif capped:
            kind = "price_move"
            cause = (
                f"the price moved up before the fill (open {bar.open} > sizing price {price})"
                if bar.open > price
                else f"fill-price slippage/fees only (open {bar.open} <= sizing price {price})"
            )
            reason = (
                f"price_move: the request was affordable at the sizing price ({numbers}); {cause}; "
                f"{fraction:.3%} of the quantity was capped to what cash affords"
            )
        else:
            return None
        return ResizeRecord(
            req.client_order_id, bar.open_ts, req.quantity, quantity, filled, reason, kind, price, cash
        )

    def _fill_at_open(
        self, order: _Order, bar: Bar
    ) -> tuple[FillRecord | OrderRejection, ResizeRecord | None]:
        req = order.req
        quantity, price = self._size_at_fill(req, bar)
        notional = price * quantity
        fee = self._fees.fee(notional)
        reason = self._fill_problem(req, quantity, price, notional, fee)
        resize = (
            self._sizing_record(order, bar, quantity, filled=reason is None)
            if req.side is Side.BUY
            else None
        )
        order.resize = resize
        if reason is not None:
            order.state, order.last_ts = OrderState.REJECTED, bar.open_ts
            return OrderRejection(req.client_order_id, bar.open_ts, reason), resize
        if req.side is Side.BUY:
            self._cash -= notional + fee
            self._qty += quantity
        else:
            self._cash += notional - fee
            self._qty -= quantity
        order.state, order.filled_qty = OrderState.FILLED, quantity
        order.avg_price, order.last_ts = price, bar.open_ts
        fill = Fill(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            ts=bar.open_ts,
            price=price,
            quantity=quantity,
            fee=fee,
            fee_asset=self._quote,
            exchange_order_id=order.exchange_order_id,
        )
        expected_ts = req.created_ts + self._delay
        gap = bar.open_ts != expected_ts
        record = FillRecord(fill, bar.open, abs(price - bar.open) * quantity, gap_fill=gap)
        return record, resize

    def _fill_problem(
        self, req: OrderRequest, quantity: Decimal, price: Decimal, notional: Decimal, fee: Decimal
    ) -> str | None:
        if req.side is Side.BUY and quantity <= 0:
            return "ZERO_QUANTITY: cash no longer covers even one step at the fill price"
        if req.side is Side.BUY and quantity < self._filters.min_qty:
            return f"MIN_QUANTITY: {quantity} < {self._filters.min_qty} after resizing to available cash"
        if not self._filters.passes_notional(price, quantity):
            return f"MIN_NOTIONAL: {notional} < {self._filters.min_notional}"
        if req.side is Side.BUY and notional + fee > self._cash:
            return f"INSUFFICIENT_FUNDS: need {notional + fee}, have {self._cash} {self._quote}"
        if req.side is Side.SELL and quantity > self._qty:
            return f"INSUFFICIENT_FUNDS: need {quantity}, have {self._qty} {self._base}"
        return None
