"""Ledger, Portfolio and PassThroughAllocator: exact Decimal accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as Dec

import pytest

from tbot.core.types import Fill, Side, TargetIntent
from tbot.portfolio.allocator import PassThroughAllocator
from tbot.portfolio.ledger import Ledger, LedgerError
from tbot.portfolio.portfolio import Portfolio, PortfolioError

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def fill(
    side: Side,
    price: str,
    qty: str,
    fee: str,
    *,
    n: int = 0,
    symbol: str = "BTCUSDT",
    fee_asset: str = "USDT",
) -> Fill:
    return Fill(
        client_order_id=f"o{n}",
        symbol=symbol,
        side=side,
        ts=T0 + timedelta(days=n),
        price=Dec(price),
        quantity=Dec(qty),
        fee=Dec(fee),
        fee_asset=fee_asset,
    )


def make(cash: str = "1000") -> Portfolio:
    return Portfolio(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", initial_cash=Dec(cash))


def test_buy_then_sell_round_trip_pnl_is_exact() -> None:
    p = make("1000")
    p.apply_fill(fill(Side.BUY, "100", "5", "1.75", n=0))  # cost 500 + 1.75
    assert p.cash == Dec("498.25")
    assert p.quantity == Dec("5")
    assert p.avg_entry_price == Dec("100")
    p.apply_fill(fill(Side.SELL, "110", "5", "1.925", n=1))  # proceeds 550 - 1.925
    assert p.cash == Dec("1046.325")
    assert p.quantity == 0
    assert p.avg_entry_price is None
    assert p.fees_paid == Dec("3.675")
    (trade,) = p.trades
    assert trade.pnl == Dec("550") - Dec("500") - Dec("3.675") == Dec("46.325")
    assert trade.avg_entry_price == Dec("100") and trade.avg_exit_price == Dec("110")
    assert trade.n_fills == 2
    assert trade.return_pct == pytest.approx(46.325 / 500)
    p.verify()


def test_scale_in_updates_vwap_and_partial_exit_keeps_trade_open() -> None:
    p = make("1000")
    p.apply_fill(fill(Side.BUY, "100", "2", "0", n=0))
    p.apply_fill(fill(Side.BUY, "130", "2", "0", n=1))
    assert p.avg_entry_price == Dec("115")
    p.apply_fill(fill(Side.SELL, "120", "1", "0", n=2))
    assert not p.ledger.trades  # still open
    assert p.avg_entry_price == Dec("115")  # selling does not move the cost basis
    assert p.ledger.open_trade_pnl(Dec("120")) == Dec("120") + 3 * Dec("120") - Dec("460")
    p.apply_fill(fill(Side.SELL, "120", "3", "0", n=3))
    trade = p.ledger.trades[0]
    assert trade.quantity == Dec("4") and trade.n_fills == 4
    assert p.ledger.open_trade_pnl(Dec("1")) is None


def test_equity_identity_and_mark() -> None:
    p = make("1000")
    p.apply_fill(fill(Side.BUY, "100", "4", "1.4", n=0))
    pt = p.mark(T0, Dec("125.5"))
    assert pt.equity == pt.cash + pt.quantity * Dec("125.5") == Dec("598.6") + Dec("502")
    assert pt.weight == pytest.approx(502 / 1100.6)
    view = p.view(T0, Dec("125.5"))
    assert view.equity_quote == pt.equity and view.base_qty == Dec("4")
    assert view.avg_entry_price == Dec("100")
    assert p.balances() == {"USDT": Dec("598.6"), "BTC": Dec("4")}


def test_cash_never_negative() -> None:
    p = make("100")
    with pytest.raises(PortfolioError, match="cash negative"):
        p.apply_fill(fill(Side.BUY, "100", "1", "0.5"))
    assert p.cash == Dec("100") and p.quantity == 0 and p.ledger.entries == ()


def test_no_shorts() -> None:
    p = make("100")
    with pytest.raises(PortfolioError, match="short"):
        p.apply_fill(fill(Side.SELL, "100", "1", "0"))
    p.apply_fill(fill(Side.BUY, "10", "1", "0"))
    with pytest.raises(PortfolioError, match="short"):
        p.apply_fill(fill(Side.SELL, "10", "1.5", "0", n=1))


def test_symbol_mismatch_and_foreign_fee_asset_rejected() -> None:
    p = make()
    with pytest.raises(PortfolioError, match="ETHUSDT"):
        p.apply_fill(fill(Side.BUY, "1", "1", "0", symbol="ETHUSDT"))
    with pytest.raises(PortfolioError, match="quote-asset"):
        p.apply_fill(fill(Side.BUY, "1", "1", "0.1", fee_asset="BNB"))


def test_ledger_replay_matches_running_state() -> None:
    p = make("5000")
    p.apply_fill(fill(Side.BUY, "101.37", "1.23456", "0.4378", n=0))
    p.apply_fill(fill(Side.SELL, "99.11", "0.5", "0.1734", n=1))
    p.apply_fill(fill(Side.BUY, "98.9", "2.2", "0.7615", n=2))
    p.verify()
    cash, qty = p.ledger.replay()
    assert (cash, qty) == (p.cash, p.quantity)
    assert [e.seq for e in p.ledger.entries] == [1, 2, 3]
    assert p.ledger.traded_notional == sum((e.notional for e in p.ledger.entries), Dec(0))


def test_ledger_rejects_a_duplicate_client_order_id() -> None:
    """MINOR fix (review round 2): a repeat ``client_order_id`` can only be a replayed/double-booked
    fill in this engine (a MARKET order fills exactly once), so ``record`` raises rather than silently
    booking it twice. A failed (raising) call must not itself count as "seen"."""
    p = make("1000")
    p.apply_fill(fill(Side.BUY, "10", "1", "0", n=0))
    with pytest.raises(LedgerError, match="o0"):
        p.apply_fill(fill(Side.BUY, "10", "1", "0", n=0))
    assert len(p.ledger.entries) == 1
    with pytest.raises(LedgerError, match="o0"):
        p.apply_fill(fill(Side.SELL, "999999", "1", "0", n=0))  # even an otherwise-invalid repeat
    assert len(p.ledger.entries) == 1 and p.quantity == Dec("1")


def test_ledger_rejects_invalid_opening_cash() -> None:
    with pytest.raises(ValueError):
        Ledger(Dec("-1"))
    with pytest.raises(ValueError):
        Portfolio(symbol="X", base_asset="A", quote_asset="A", initial_cash=Dec(1))


def test_verify_detects_tampering() -> None:
    p = make("1000")
    p.apply_fill(fill(Side.BUY, "10", "1", "0"))
    p.ledger._cash += Dec(1)  # simulate corruption
    with pytest.raises(PortfolioError, match="replay"):
        p.verify()
    assert issubclass(PortfolioError, LedgerError)


def test_pass_through_allocator() -> None:
    a = PassThroughAllocator()
    intent = TargetIntent("s", T0, 0.5, "x")
    assert a.allocate([intent]) is intent
    with pytest.raises(ValueError, match="exactly one"):
        a.allocate([])
    with pytest.raises(ValueError, match="exactly one"):
        a.allocate([intent, intent])
