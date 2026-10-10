"""Core contracts shared by backtest, paper and live code paths.

These types are owned by the orchestrator. Sub-agents must not modify this module; they
propose changes in their task report instead (CLAUDE.md section 10, rule 2).

Conventions enforced here:

* Every timestamp is timezone-aware UTC. A bar's ``ts`` is its CLOSE time.
* Money and quantities are ``Decimal`` everywhere in the execution/portfolio path.
  ``float`` is allowed only for research/indicator math (weights, exposures, metrics).
* Objects are immutable (frozen dataclasses). Causality is a property of how a
  ``BarWindow`` is built: it never contains a bar that closes after ``as_of``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from math import isfinite
from typing import Protocol, runtime_checkable

__all__ = [
    "Approved",
    "Bar",
    "BarWindow",
    "Broker",
    "Clock",
    "DataFeed",
    "Fill",
    "MarketState",
    "OrderAck",
    "OrderRequest",
    "OrderState",
    "OrderStatus",
    "OrderType",
    "PortfolioView",
    "RefusalReason",
    "Refused",
    "RegimeModel",
    "RegimeState",
    "RiskDecision",
    "RiskManager",
    "Side",
    "Strategy",
    "SymbolFilters",
    "TargetIntent",
    "TimeInForce",
    "Timeframe",
    "TradingState",
    "ensure_utc",
]


# ---------------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------------


def ensure_utc(ts: datetime, field: str = "ts") -> datetime:
    """Return ``ts`` normalised to UTC, rejecting naive or non-UTC-offset datetimes."""
    if ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware UTC, got naive {ts!r}")
    offset = ts.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValueError(f"{field} must be UTC (offset 0), got {ts!r}")
    return ts.astimezone(UTC)


def _dec(value: Decimal, field: str, *, allow_zero: bool = False) -> Decimal:
    """Validate that ``value`` is a finite, positive (or non-negative) ``Decimal``."""
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite, got {value}")
    if value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{field} must be {bound}, got {value}")
    return value


def _unit_interval(value: float, field: str) -> float:
    """Validate that ``value`` is a finite float in the closed interval [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be a float, got {type(value).__name__}")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} must be finite, got {number}")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be within [0, 1], got {number}")
    return number


# ---------------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------------


class Timeframe(StrEnum):
    """Supported bar sizes. Signals run on 4h and 1d; 1h is the storage/recorder base."""

    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def delta(self) -> timedelta:
        """Length of one bar."""
        return _TIMEFRAME_DELTA[self]

    @property
    def periods_per_year(self) -> float:
        """Annualisation factor on a 365-day year (CLAUDE.md section 3.4)."""
        return _TIMEFRAME_PERIODS_PER_YEAR[self]


_TIMEFRAME_DELTA: dict[Timeframe, timedelta] = {
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}

_TIMEFRAME_PERIODS_PER_YEAR: dict[Timeframe, float] = {
    Timeframe.H1: 8760.0,
    Timeframe.H4: 2190.0,
    Timeframe.D1: 365.0,
}


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderState(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_open(self) -> bool:
        return self in (OrderState.NEW, OrderState.PARTIALLY_FILLED)

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )


class TradingState(StrEnum):
    """Kill-switch states. REDUCING allows position-reducing orders only."""

    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    HALTED = "HALTED"


class RefusalReason(StrEnum):
    """Machine-readable refusal codes. Every refusal is logged with one of these."""

    HALTED = "HALTED"
    REDUCING_ONLY = "REDUCING_ONLY"
    STALE_DATA = "STALE_DATA"
    NO_MARKET_DATA = "NO_MARKET_DATA"
    UNKNOWN_FILTERS = "UNKNOWN_FILTERS"
    INVALID_INTENT = "INVALID_INTENT"
    MIN_NOTIONAL = "MIN_NOTIONAL"
    MIN_QUANTITY = "MIN_QUANTITY"
    ZERO_QUANTITY = "ZERO_QUANTITY"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    MAX_POSITION = "MAX_POSITION"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    ORDER_RATE_LIMIT = "ORDER_RATE_LIMIT"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    DUPLICATE_CLIENT_ORDER_ID = "DUPLICATE_CLIENT_ORDER_ID"
    LIVE_TRADING_DISABLED = "LIVE_TRADING_DISABLED"


# ---------------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """A CLOSED OHLCV bar. ``ts`` is the bar's close time (UTC)."""

    symbol: str
    timeframe: Timeframe
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "ts", ensure_utc(self.ts, "Bar.ts"))
        for field in ("open", "high", "low", "close"):
            _dec(getattr(self, field), f"Bar.{field}")
        _dec(self.volume, "Bar.volume", allow_zero=True)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"Bar.high {self.high} is below open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Bar.low {self.low} is above open/close/high")

    @property
    def open_ts(self) -> datetime:
        """Open time of the bar (close time minus one timeframe)."""
        return self.ts - self.timeframe.delta


@dataclass(frozen=True, slots=True)
class BarWindow:
    """Immutable, ordered view of CLOSED bars up to and including ``as_of``.

    A strategy may use every bar in the window without introducing look-ahead, because
    a feed only appends a bar once that bar has closed.
    """

    symbol: str
    timeframe: Timeframe
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("BarWindow must contain at least one bar")
        previous: datetime | None = None
        for bar in self.bars:
            if bar.symbol != self.symbol or bar.timeframe != self.timeframe:
                raise ValueError(f"BarWindow holds a foreign bar: {bar.symbol}/{bar.timeframe}")
            if previous is not None and bar.ts <= previous:
                raise ValueError(f"BarWindow ts must strictly increase ({bar.ts} after {previous})")
            previous = bar.ts

    @classmethod
    def of(cls, bars: Sequence[Bar]) -> BarWindow:
        """Build a window from a non-empty sequence of homogeneous bars."""
        if not bars:
            raise ValueError("cannot build a BarWindow from an empty sequence")
        return cls(symbol=bars[0].symbol, timeframe=bars[0].timeframe, bars=tuple(bars))

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self.bars)

    def __getitem__(self, index: int) -> Bar:
        return self.bars[index]

    @property
    def last(self) -> Bar:
        """Most recent closed bar (bar ``t``)."""
        return self.bars[-1]

    @property
    def as_of(self) -> datetime:
        """Close time of the most recent bar: no later data is visible."""
        return self.bars[-1].ts

    def tail(self, n: int) -> BarWindow:
        """Last ``n`` bars as a new window (causal: drops only the oldest bars)."""
        if n <= 0:
            raise ValueError(f"tail(n) requires n > 0, got {n}")
        return BarWindow(self.symbol, self.timeframe, self.bars[-n:])

    def opens(self) -> tuple[float, ...]:
        return tuple(float(bar.open) for bar in self.bars)

    def highs(self) -> tuple[float, ...]:
        return tuple(float(bar.high) for bar in self.bars)

    def lows(self) -> tuple[float, ...]:
        return tuple(float(bar.low) for bar in self.bars)

    def closes(self) -> tuple[float, ...]:
        return tuple(float(bar.close) for bar in self.bars)


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """Exchange trading rules for one symbol (from ``exchangeInfo``)."""

    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None

    def __post_init__(self) -> None:
        for field in ("tick_size", "step_size", "min_qty", "min_notional"):
            _dec(getattr(self, field), f"SymbolFilters.{field}")
        if self.max_qty is not None:
            _dec(self.max_qty, "SymbolFilters.max_qty")

    def round_price(self, price: Decimal) -> Decimal:
        """Round a price DOWN to the tick size."""
        _dec(price, "price")
        return (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def floor_qty(self, quantity: Decimal) -> Decimal:
        """Round a quantity DOWN to the step size; never rounds up into unavailable funds."""
        _dec(quantity, "quantity", allow_zero=True)
        return (quantity / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def passes_notional(self, price: Decimal, quantity: Decimal) -> bool:
        """True when ``price * quantity`` reaches the exchange minimum notional."""
        return price * quantity >= self.min_notional


@dataclass(frozen=True, slots=True)
class MarketState:
    """Latest tradable market snapshot used by the RiskManager and the OMS."""

    symbol: str
    ts: datetime
    last_price: Decimal
    filters: SymbolFilters
    bid: Decimal | None = None
    ask: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "MarketState.ts"))
        _dec(self.last_price, "MarketState.last_price")
        if self.bid is not None:
            _dec(self.bid, "MarketState.bid")
        if self.ask is not None:
            _dec(self.ask, "MarketState.ask")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError(f"crossed book: ask {self.ask} < bid {self.bid}")

    @property
    def spread_bps(self) -> float | None:
        """Bid/ask spread in basis points of the mid, or ``None`` if the book is unknown."""
        if self.bid is None or self.ask is None:
            return None
        mid = (self.bid + self.ask) / 2
        return float((self.ask - self.bid) / mid) * 10_000

    def age_seconds(self, now: datetime) -> float:
        """Seconds between this snapshot and ``now`` (used by the stale-data guard)."""
        return (ensure_utc(now, "now") - self.ts).total_seconds()


# ---------------------------------------------------------------------------------
# strategy / regime
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetIntent:
    """What a strategy wants: a target weight of equity in the base asset. Never an order."""

    strategy_id: str
    ts: datetime
    target_weight: float
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("strategy_id must be non-empty")
        object.__setattr__(self, "ts", ensure_utc(self.ts, "TargetIntent.ts"))
        object.__setattr__(self, "target_weight", _unit_interval(self.target_weight, "target_weight"))

    def scaled(self, multiplier: float) -> TargetIntent:
        """Apply a regime exposure multiplier, keeping the result inside [0, 1]."""
        _unit_interval(multiplier, "multiplier")
        suffix = f"regime x{multiplier:.4f}"
        return TargetIntent(
            strategy_id=self.strategy_id,
            ts=self.ts,
            target_weight=self.target_weight * multiplier,
            reason=f"{self.reason}|{suffix}" if self.reason else suffix,
        )


@dataclass(frozen=True, slots=True)
class RegimeState:
    """Output of the regime layer: an exposure multiplier in [0, 1], never a strategy switch."""

    ts: datetime
    exposure: float
    label: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "RegimeState.ts"))
        object.__setattr__(self, "exposure", _unit_interval(self.exposure, "exposure"))


# ---------------------------------------------------------------------------------
# portfolio / orders
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Read-only snapshot of the portfolio handed to the RiskManager."""

    ts: datetime
    cash_quote: Decimal
    base_qty: Decimal
    equity_quote: Decimal
    avg_entry_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "PortfolioView.ts"))
        _dec(self.cash_quote, "PortfolioView.cash_quote", allow_zero=True)
        _dec(self.base_qty, "PortfolioView.base_qty", allow_zero=True)
        _dec(self.equity_quote, "PortfolioView.equity_quote", allow_zero=True)
        if self.avg_entry_price is not None:
            _dec(self.avg_entry_price, "PortfolioView.avg_entry_price")

    def weight(self, price: Decimal) -> float:
        """Current fraction of equity held in the base asset at ``price``."""
        _dec(price, "price")
        if self.equity_quote == 0:
            return 0.0
        return float(self.base_qty * price / self.equity_quote)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An order the RiskManager approved. Only the OMS creates these from ``Approved``."""

    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    created_ts: datetime
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("client_order_id must be non-empty")
        object.__setattr__(self, "created_ts", ensure_utc(self.created_ts, "OrderRequest.created_ts"))
        _dec(self.quantity, "OrderRequest.quantity")
        if self.type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("LIMIT order requires limit_price")
            _dec(self.limit_price, "OrderRequest.limit_price")
        elif self.limit_price is not None:
            raise ValueError("MARKET order must not carry a limit_price")


@dataclass(frozen=True, slots=True)
class OrderAck:
    """Broker acknowledgement of a submission (not a fill)."""

    client_order_id: str
    exchange_order_id: str | None
    ts: datetime
    accepted: bool
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "OrderAck.ts"))


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution against an order, with the fee actually charged."""

    client_order_id: str
    symbol: str
    side: Side
    ts: datetime
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    exchange_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "Fill.ts"))
        _dec(self.price, "Fill.price")
        _dec(self.quantity, "Fill.quantity")
        _dec(self.fee, "Fill.fee", allow_zero=True)

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class OrderStatus:
    """Current known state of an order, as reported by the broker."""

    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    state: OrderState
    quantity: Decimal
    filled_quantity: Decimal
    ts: datetime
    exchange_order_id: str | None = None
    avg_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "OrderStatus.ts"))
        _dec(self.quantity, "OrderStatus.quantity")
        _dec(self.filled_quantity, "OrderStatus.filled_quantity", allow_zero=True)
        if self.filled_quantity > self.quantity:
            raise ValueError(f"filled_quantity {self.filled_quantity} exceeds quantity {self.quantity}")
        if self.avg_fill_price is not None:
            _dec(self.avg_fill_price, "OrderStatus.avg_fill_price")

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_quantity


# ---------------------------------------------------------------------------------
# risk decisions
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approved:
    """Risk approval. ``order is None`` means: target already met, do nothing."""

    intent: TargetIntent
    order: OrderRequest | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Refused:
    """Risk refusal with a machine-readable reason; always logged."""

    intent: TargetIntent
    reason: RefusalReason
    detail: str = ""


RiskDecision = Approved | Refused


# ---------------------------------------------------------------------------------
# protocols (the seams where backtest and live differ)
# ---------------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Time source. Nothing in core/risk/execution may call ``datetime.now()`` directly."""

    def now(self) -> datetime: ...


@runtime_checkable
class Strategy(Protocol):
    """Pure mapping from a window of closed bars to a target weight, plus its protective stop.

    ``stop_price`` is mandatory (SPEC D-050): for an open long entered at ``entry_price``, it returns the
    price at which the position must be closed, computed only from ``window`` (closed bars up to and
    including ``t``). It must be positive and below the current price; the RiskManager refuses a long
    whose stop is missing or invalid, and ``tbot.strategies.base.StrategyRegistry`` refuses a strategy
    that does not implement it. The stop rule is part of the strategy: its parameters are fixed in
    advance and every variant counts as a trial.
    """

    id: str
    warmup_bars: int

    def on_bar(self, window: BarWindow) -> TargetIntent: ...

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal: ...


@runtime_checkable
class RegimeModel(Protocol):
    """Causal exposure overlay; scales intents, never switches strategies."""

    warmup_bars: int

    def update(self, window: BarWindow) -> RegimeState: ...


@runtime_checkable
class RiskManager(Protocol):
    """Sole authority that turns an intent into an order (or refuses it)."""

    def evaluate(
        self, intent: TargetIntent, portfolio: PortfolioView, market: MarketState
    ) -> RiskDecision: ...


@runtime_checkable
class Broker(Protocol):
    """Order gateway. SimulatedBroker (backtest) and TabdealBroker (live) implement this."""

    def submit(self, req: OrderRequest) -> OrderAck: ...

    def cancel(self, client_order_id: str) -> None: ...

    def get_order(self, client_order_id: str) -> OrderStatus: ...

    def open_orders(self, symbol: str) -> list[OrderStatus]: ...

    def balances(self) -> dict[str, Decimal]: ...


@runtime_checkable
class DataFeed(Protocol):
    """Source of CLOSED bars. BacktestFeed replays history; LiveFeed streams it."""

    def bars(self, symbol: str, timeframe: Timeframe) -> Iterator[Bar]: ...
