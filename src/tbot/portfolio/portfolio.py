"""Portfolio state for one spot symbol: cash, base quantity, equity, cost basis, fee accounting.

The same class runs in backtest, paper and live (CLAUDE.md section 3.2). It is fed only ``Fill`` objects;
it never talks to a broker or a clock. Money and quantities are ``Decimal`` and exact. Marking to
market is always done at a bar's CLOSE, by the caller passing that close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tbot.core.types import Fill, PortfolioView, Side
from tbot.portfolio.ledger import Ledger, LedgerEntry, LedgerError, TradeRecord

__all__ = ["EquityPoint", "Portfolio", "PortfolioError"]

_ZERO = Decimal(0)


class PortfolioError(LedgerError):
    """A fill does not belong to this portfolio, or would break a portfolio invariant."""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """The portfolio marked to market at one instant (a bar close)."""

    ts: datetime
    mark_price: Decimal
    cash: Decimal
    quantity: Decimal
    equity: Decimal  # cash + quantity * mark_price, exactly

    @property
    def weight(self) -> float:
        """Fraction of equity held in the base asset."""
        if self.equity == 0:
            return 0.0
        return float(self.quantity * self.mark_price / self.equity)


class Portfolio:
    """Single-symbol, long-only, no-leverage portfolio."""

    def __init__(self, *, symbol: str, base_asset: str, quote_asset: str, initial_cash: Decimal) -> None:
        if not symbol or not base_asset or not quote_asset or base_asset == quote_asset:
            raise ValueError("symbol, base_asset and quote_asset must be non-empty and distinct")
        self.symbol = symbol
        self.base_asset = base_asset
        self.quote_asset = quote_asset
        self.initial_cash = initial_cash
        self.ledger = Ledger(initial_cash)
        self._avg_entry: Decimal | None = None

    # -- state -----------------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self.ledger.cash

    @property
    def quantity(self) -> Decimal:
        return self.ledger.quantity

    @property
    def avg_entry_price(self) -> Decimal | None:
        """Volume-weighted average price of the open position (fees excluded); ``None`` when flat."""
        return self._avg_entry

    @property
    def fees_paid(self) -> Decimal:
        return self.ledger.fees_paid

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        return self.ledger.trades

    def equity(self, mark_price: Decimal) -> Decimal:
        return self.cash + self.quantity * mark_price

    def balances(self) -> dict[str, Decimal]:
        return {self.quote_asset: self.cash, self.base_asset: self.quantity}

    # -- mutation --------------------------------------------------------------------------

    def apply_fill(self, fill: Fill) -> LedgerEntry:
        """Book a fill. Raises ``PortfolioError`` rather than ever going short or negative on cash."""
        if fill.symbol != self.symbol:
            raise PortfolioError(f"fill for {fill.symbol} booked into a {self.symbol} portfolio")
        prev_qty, prev_avg = self.quantity, self._avg_entry
        try:
            entry = self.ledger.record(fill, self.quote_asset)
        except LedgerError as exc:
            raise PortfolioError(str(exc)) from exc
        if entry.qty_after == 0:
            self._avg_entry = None
        elif fill.side is Side.BUY:
            carried = (prev_avg or _ZERO) * prev_qty
            self._avg_entry = (carried + fill.price * fill.quantity) / entry.qty_after
        return entry

    # -- views -----------------------------------------------------------------------------

    def mark(self, ts: datetime, close: Decimal) -> EquityPoint:
        """Mark to market at a bar close; the identity ``equity == cash + qty * close`` is exact."""
        return EquityPoint(
            ts=ts,
            mark_price=close,
            cash=self.cash,
            quantity=self.quantity,
            equity=self.equity(close),
        )

    def view(self, ts: datetime, close: Decimal) -> PortfolioView:
        """Read-only snapshot for the RiskManager."""
        return PortfolioView(
            ts=ts,
            cash_quote=self.cash,
            base_qty=self.quantity,
            equity_quote=self.equity(close),
            avg_entry_price=self._avg_entry,
        )

    def verify(self) -> None:
        """Re-derive cash and quantity from the ledger entries and compare with the running state."""
        cash, qty = self.ledger.replay()
        if (cash, qty) != (self.cash, self.quantity):
            raise PortfolioError(
                f"ledger replay ({cash}, {qty}) differs from running state ({self.cash}, {self.quantity})"
            )
