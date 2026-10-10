"""Append-only ledger of fills and the flat-to-flat trades derived from it.

Everything here is ``Decimal`` and exact: no rounding happens inside the ledger, so the accounting
identity ``cash_after = cash_before - side * notional - fee`` holds to the last digit and can be
re-derived by replaying the entries (``Ledger.replay``).

A *trade* is one flat -> long -> flat cycle. Scale-ins and partial exits belong to the same trade; the
trade closes when the position returns to exactly zero. Its net P&L is
``sum(sell proceeds) - sum(buy cost) - sum(fees)`` over the cycle, in the quote asset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tbot.core.types import Fill, Side

__all__ = ["Ledger", "LedgerEntry", "LedgerError", "TradeRecord"]

_ZERO = Decimal(0)


class LedgerError(RuntimeError):
    """The ledger would become inconsistent (negative cash, short position, ...)."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One fill as booked: signed cash movement and the resulting balances."""

    seq: int
    ts: datetime
    client_order_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal  # always in the quote asset
    cash_delta: Decimal  # BUY: -(notional + fee); SELL: +(notional - fee)
    cash_after: Decimal
    qty_after: Decimal


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """A completed flat -> flat round trip (net of every fee in the cycle)."""

    entry_ts: datetime
    exit_ts: datetime
    quantity: Decimal  # total base quantity bought during the cycle
    avg_entry_price: Decimal  # VWAP of the buys
    avg_exit_price: Decimal  # VWAP of the sells
    buy_cost: Decimal
    sell_proceeds: Decimal
    fees: Decimal
    pnl: Decimal  # net, quote asset
    n_fills: int

    @property
    def return_pct(self) -> float:
        """Net P&L relative to the capital deployed in the cycle (buy cost + fees on buys is not split
        out: the denominator is the gross buy cost)."""
        if self.buy_cost == 0:
            return 0.0
        return float(self.pnl / self.buy_cost)


@dataclass(slots=True)
class _OpenCycle:
    entry_ts: datetime
    qty_bought: Decimal = _ZERO
    buy_cost: Decimal = _ZERO
    qty_sold: Decimal = _ZERO
    sell_proceeds: Decimal = _ZERO
    fees: Decimal = _ZERO
    n_fills: int = 0


class Ledger:
    """Append-only book of fills for one symbol, starting from ``opening_cash`` and a flat position."""

    def __init__(self, opening_cash: Decimal) -> None:
        if not opening_cash.is_finite() or opening_cash < 0:
            raise ValueError(f"opening_cash must be a finite Decimal >= 0, got {opening_cash}")
        self.opening_cash: Decimal = opening_cash
        self._entries: list[LedgerEntry] = []
        self._trades: list[TradeRecord] = []
        self._cycle: _OpenCycle | None = None
        self._cash: Decimal = opening_cash
        self._qty: Decimal = _ZERO

    # -- read side -------------------------------------------------------------------------

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        """Closed round trips only."""
        return tuple(self._trades)

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def quantity(self) -> Decimal:
        return self._qty

    @property
    def fees_paid(self) -> Decimal:
        return sum((e.fee for e in self._entries), _ZERO)

    @property
    def traded_notional(self) -> Decimal:
        return sum((e.notional for e in self._entries), _ZERO)

    def open_trade_pnl(self, mark_price: Decimal) -> Decimal | None:
        """Unrealised net P&L of the trade still open at ``mark_price`` (``None`` when flat)."""
        cycle = self._cycle
        if cycle is None:
            return None
        remaining = cycle.qty_bought - cycle.qty_sold
        return cycle.sell_proceeds + remaining * mark_price - cycle.buy_cost - cycle.fees

    # -- write side ------------------------------------------------------------------------

    def record(self, fill: Fill, quote_asset: str) -> LedgerEntry:
        """Book ``fill``. Refuses anything that would make cash negative or the position short."""
        if fill.fee_asset != quote_asset:
            raise LedgerError(f"only quote-asset fees are supported, got fee_asset={fill.fee_asset!r}")
        notional = fill.price * fill.quantity
        if fill.side is Side.BUY:
            cash_delta = -(notional + fill.fee)
            qty_after = self._qty + fill.quantity
        else:
            cash_delta = notional - fill.fee
            qty_after = self._qty - fill.quantity
        cash_after = self._cash + cash_delta
        if cash_after < 0:
            raise LedgerError(f"fill {fill.client_order_id} would make cash negative ({cash_after})")
        if qty_after < 0:
            raise LedgerError(f"fill {fill.client_order_id} would make the position short ({qty_after})")
        entry = LedgerEntry(
            seq=len(self._entries) + 1,
            ts=fill.ts,
            client_order_id=fill.client_order_id,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            notional=notional,
            fee=fill.fee,
            cash_delta=cash_delta,
            cash_after=cash_after,
            qty_after=qty_after,
        )
        self._entries.append(entry)
        self._cash, self._qty = cash_after, qty_after
        self._update_cycle(entry)
        return entry

    def _update_cycle(self, entry: LedgerEntry) -> None:
        cycle = self._cycle
        if cycle is None:
            cycle = self._cycle = _OpenCycle(entry_ts=entry.ts)
        cycle.fees += entry.fee
        cycle.n_fills += 1
        if entry.side is Side.BUY:
            cycle.qty_bought += entry.quantity
            cycle.buy_cost += entry.notional
        else:
            cycle.qty_sold += entry.quantity
            cycle.sell_proceeds += entry.notional
        if entry.qty_after == 0:
            self._close_cycle(cycle, entry.ts)

    def _close_cycle(self, cycle: _OpenCycle, exit_ts: datetime) -> None:
        self._trades.append(
            TradeRecord(
                entry_ts=cycle.entry_ts,
                exit_ts=exit_ts,
                quantity=cycle.qty_bought,
                avg_entry_price=cycle.buy_cost / cycle.qty_bought,
                avg_exit_price=cycle.sell_proceeds / cycle.qty_sold,
                buy_cost=cycle.buy_cost,
                sell_proceeds=cycle.sell_proceeds,
                fees=cycle.fees,
                pnl=cycle.sell_proceeds - cycle.buy_cost - cycle.fees,
                n_fills=cycle.n_fills,
            )
        )
        self._cycle = None

    # -- audit -----------------------------------------------------------------------------

    def replay(self) -> tuple[Decimal, Decimal]:
        """Recompute ``(cash, quantity)`` from the entries alone, independent of the running totals."""
        cash, qty = self.opening_cash, _ZERO
        for e in self._entries:
            if e.side is Side.BUY:
                cash -= e.price * e.quantity + e.fee
                qty += e.quantity
            else:
                cash += e.price * e.quantity - e.fee
                qty -= e.quantity
        return cash, qty
