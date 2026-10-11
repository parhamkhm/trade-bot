"""SimulatedBroker: fill timing, costs, idempotency, filters, delay model."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as Dec

import pytest

from tbot.core.config import CostConfig
from tbot.core.types import Bar, Broker, OrderRequest, OrderState, OrderType, Side, Timeframe
from tbot.execution.simulated import (
    BrokerError,
    FeeSchedule,
    FlatSlippage,
    SimulatedBroker,
    SlippageContext,
    UnknownOrderError,
)
from tests.sim_helpers import SYMBOL, T_START, lbank_like_filters, make_bar


def broker(
    cash: str = "10000", *, fee: str = "35", slip: str = "5", delay: timedelta = timedelta(0)
) -> SimulatedBroker:
    return SimulatedBroker(
        filters=lbank_like_filters(),
        fees=FeeSchedule(taker_bps=Dec(fee), maker_bps=Dec("33")),
        slippage=FlatSlippage(Dec(slip)),
        base_asset="BTC",
        quote_asset="USDT",
        initial_cash=Dec(cash),
        fill_delay=delay,
    )


def req(
    cid: str, side: Side, qty: str, created_i: int = 0, type_: OrderType = OrderType.MARKET
) -> OrderRequest:
    # created at the CLOSE of bar `created_i` (= open of bar created_i + 1)
    return OrderRequest(
        client_order_id=cid,
        symbol=SYMBOL,
        side=side,
        type=type_,
        quantity=Dec(qty),
        created_ts=T_START + timedelta(days=created_i + 1),
        limit_price=Dec("100") if type_ is OrderType.LIMIT else None,
    )


def test_satisfies_broker_protocol() -> None:
    assert isinstance(broker(), Broker)


def test_buy_fills_at_next_open_with_slippage_and_fee() -> None:
    b = broker()
    ack = b.submit(req("a", Side.BUY, "10"))
    assert ack.accepted
    # Bar 0 (the signal bar, already closed) must not fill an order created at its close.
    assert b.advance(make_bar(0, "100", "105")).fills == ()
    assert b.get_order("a").state is OrderState.NEW
    rep = b.advance(make_bar(1, "110", "120"))
    (rec,) = rep.fills
    # open 110, +5 bps -> 110.055 -> ceil to tick 0.01 -> 110.06
    assert rec.fill.price == Dec("110.06") and rec.fill.ts == T_START + timedelta(days=1)
    assert rec.fill.quantity == Dec("10")
    assert rec.fill.fee == Dec("1100.6") * Dec("35") / Dec(10000) == Dec("3.8521")
    assert rec.fill.fee_asset == "USDT"
    assert rec.reference_price == Dec("110")
    assert rec.slippage_cost == Dec("0.06") * 10
    assert b.balances() == {"USDT": Dec("10000") - Dec("1100.6") - Dec("3.8521"), "BTC": Dec("10")}
    st = b.get_order("a")
    assert st.state is OrderState.FILLED and st.avg_fill_price == Dec("110.06")
    assert b.open_orders(SYMBOL) == []


def test_sell_slippage_is_adverse_and_rounds_down() -> None:
    b = broker()
    b.submit(req("buy", Side.BUY, "1"))
    b.advance(make_bar(0, "100", "100"))
    b.advance(make_bar(1, "100", "100"))
    b.submit(req("sell", Side.SELL, "1", created_i=1))
    (rec,) = b.advance(make_bar(2, "100.01", "100")).fills
    # 100.01 * (1 - 0.0005) = 99.959995 -> floor to tick -> 99.95 (never rounds in the seller's favour)
    assert rec.fill.price == Dec("99.95")
    assert rec.fill.fee == Dec("99.95") * Dec("0.0035")
    assert rec.slippage_cost == Dec("0.06")


def test_zero_cost_fills_exactly_at_open() -> None:
    b = broker(fee="0", slip="0")
    b.submit(req("a", Side.BUY, "1"))
    (rec,) = b.advance(make_bar(1, "123.45", "130")).fills
    assert rec.fill.price == Dec("123.45") and rec.fill.fee == 0 and rec.slippage_cost == 0


def test_client_order_id_idempotent() -> None:
    b = broker()
    first = b.submit(req("dup", Side.BUY, "1"))
    second = b.submit(req("dup", Side.BUY, "5"))
    assert first.accepted and not second.accepted
    assert "DUPLICATE_CLIENT_ORDER_ID" in second.message
    assert second.exchange_order_id == first.exchange_order_id
    (rec,) = b.advance(make_bar(1, "100", "100")).fills
    assert rec.fill.quantity == Dec("1")  # only the first request exists
    assert len(b.open_orders(SYMBOL)) == 0
    # resubmitting after the fill still creates nothing
    assert not b.submit(req("dup", Side.BUY, "1")).accepted
    assert b.advance(make_bar(2, "100", "100")).fills == ()


def test_limit_orders_rejected_with_clear_error() -> None:
    b = broker()
    ack = b.submit(req("l", Side.BUY, "1", type_=OrderType.LIMIT))
    assert not ack.accepted and "UNSUPPORTED_ORDER_TYPE" in ack.message
    assert b.get_order("l").state is OrderState.REJECTED
    assert b.advance(make_bar(1, "100", "100")).fills == ()


def test_quantity_must_respect_step_min_qty_max_qty() -> None:
    b = broker()
    assert "INVALID_QUANTITY" in b.submit(req("a", Side.BUY, "0.123456")).message
    assert "INVALID_QUANTITY" in b.submit(req("b", Side.BUY, "0.000001")).message  # below one step
    assert b.submit(req("c", Side.BUY, "0.12345")).accepted
    flt = lbank_like_filters()
    capped = SimulatedBroker(
        filters=type(flt)(
            symbol=flt.symbol,
            tick_size=flt.tick_size,
            step_size=flt.step_size,
            min_qty=Dec("0.001"),
            min_notional=flt.min_notional,
            max_qty=Dec("5"),
        ),
        fees=FeeSchedule(Dec(0), Dec(0)),
        slippage=FlatSlippage(Dec(0)),
        base_asset="BTC",
        quote_asset="USDT",
        initial_cash=Dec(1_000_000),
    )
    assert "MIN_QUANTITY" in capped.submit(req("d", Side.BUY, "0.0005")).message
    assert "MAX_QUANTITY" in capped.submit(req("e", Side.BUY, "5.1")).message
    assert capped.submit(req("f", Side.BUY, "5")).accepted


def test_min_notional_checked_at_fill() -> None:
    b = broker()
    b.submit(req("tiny", Side.BUY, "0.00005"))  # 0.00005 * 100 = 0.005 USDT < 1
    rep = b.advance(make_bar(1, "100", "100"))
    assert rep.fills == () and "MIN_NOTIONAL" in rep.rejections[0].reason
    assert b.get_order("tiny").state is OrderState.REJECTED
    assert b.balances()["USDT"] == Dec("10000")


SIX_HOURS = timedelta(hours=6)
CREATED = T_START + timedelta(days=1)  # close of daily bar 0, the created_ts used by ``req(..., 0)``


def exec_bar(hours_after_created: int, open_: str) -> Bar:
    """1h execution bar whose OPEN is ``hours_after_created`` after ``CREATED``."""
    return make_bar(
        0, open_, open_, tf=Timeframe.H1, start=CREATED + timedelta(hours=hours_after_created)
    )


def test_delay_mode_buy_is_resized_down_rather_than_rejected_when_cash_falls_short() -> None:
    """Manual-delay mode: a BUY whose requested quantity cash no longer covers at fill time is CAPPED, not
    rejected (the delay-mode BLOCKER fix): a manual trader sizes when placing the order, so the broker
    mirrors that at fill time instead of refusing the whole order over a price move. SELLs get no such
    treatment."""
    b = broker("100", delay=SIX_HOURS)
    b.submit(req("big", Side.BUY, "10"))
    b.submit(req("short", Side.SELL, "1"))
    rep = b.advance(exec_bar(6, "100"))
    (rec,) = rep.fills
    # price 100.05 (5 bps slip), unit cost 100.05 * 1.0035 = 100.400175, cash 100 -> floor to step 0.00001
    assert rec.fill.client_order_id == "big" and rec.fill.quantity == Dec("0.99601")
    assert rec.fill.price == Dec("100.05")
    # hand-computed: notional 0.99601 * 100.05 = 99.6508005; fee = 35 bps of that
    assert rec.fill.fee == Dec("99.6508005") * Dec("35") / Dec(10000) == Dec("0.34877780175")
    assert rec.fill.fee_asset == "USDT"
    # cash left = 100 - 99.6508005 - 0.34877780175: the resize left less than one step's cost behind
    assert b.balances() == {"USDT": Dec("0.00042169825"), "BTC": Dec("0.99601")}
    (resize,) = rep.resizes
    assert resize.client_order_id == "big"
    assert resize.requested_quantity == Dec("10") and resize.filled_quantity == Dec("0.99601")
    assert "moved" in resize.reason
    assert [r.client_order_id for r in rep.rejections] == ["short"]  # SELL still rejected whole
    assert "INSUFFICIENT_FUNDS" in rep.rejections[0].reason


def test_resized_order_status_is_filled_with_requested_quantity_and_is_queryable() -> None:
    """get_order reports a resized BUY as FILLED with filled_quantity < quantity; ``OrderStatus`` has no
    flag for it (core/types.py is frozen), so ``resize_of``/``was_resized`` expose the resize."""
    b = broker("100", delay=SIX_HOURS)
    b.submit(req("big", Side.BUY, "10"))
    assert not b.was_resized("big") and b.resize_of("big") is None  # not yet filled
    b.advance(exec_bar(6, "100"))
    st = b.get_order("big")
    assert st.state is OrderState.FILLED
    assert st.quantity == Dec("10") and st.filled_quantity == Dec("0.99601")
    assert st.remaining == Dec("9.00399")  # resized away, not working
    assert b.open_orders(SYMBOL) == []
    assert b.was_resized("big")
    record = b.resize_of("big")
    assert record is not None and record.requested_quantity == Dec("10")
    assert record.filled_quantity == Dec("0.99601")
    with pytest.raises(UnknownOrderError):
        b.was_resized("nope")


def test_unresized_order_is_not_reported_as_resized() -> None:
    b = broker(delay=SIX_HOURS)
    b.submit(req("ok", Side.BUY, "1"))
    b.advance(exec_bar(6, "100"))
    assert b.get_order("ok").filled_quantity == Dec("1") and not b.was_resized("ok")


def test_next_open_mode_does_not_resize_and_rejects_with_insufficient_funds() -> None:
    """fill_delay == 0 (automated next-open fills): the exchange-like rejection is kept. The order is
    rejected whole (INSUFFICIENT_FUNDS), nothing is booked, and there is no ResizeRecord -- the engine's
    INVALID RUN rule then flags the run."""
    b = broker("100")  # no delay
    b.submit(req("big", Side.BUY, "10"))
    rep = b.advance(make_bar(1, "100", "100"))
    assert rep.fills == () and rep.resizes == ()
    (rej,) = rep.rejections
    assert rej.client_order_id == "big" and "INSUFFICIENT_FUNDS" in rej.reason
    assert b.get_order("big").state is OrderState.REJECTED and not b.was_resized("big")
    assert b.balances() == {"USDT": Dec("100"), "BTC": Dec("0")}


def test_delay_mode_buy_resized_to_zero_is_still_rejected() -> None:
    """The resize can land below a tradable size; that must reject (never fill a dust order), but the
    ``ResizeRecord`` is still emitted so the report shows *why* (price moved), not just a bare refusal."""
    b = broker("0.000001", fee="0", slip="0", delay=SIX_HOURS)  # below one step at price 1: floors to zero
    b.submit(req("dust", Side.BUY, "1"))
    rep = b.advance(exec_bar(6, "1"))
    assert rep.fills == ()
    (resize,) = rep.resizes
    assert resize.filled_quantity == Dec("0")
    assert "ZERO_QUANTITY" in rep.rejections[0].reason
    assert b.was_resized("dust") and b.get_order("dust").state is OrderState.REJECTED


def test_buy_resize_does_not_trigger_when_cash_was_sized_correctly() -> None:
    """Sanity check: when cash was sized correctly at close(t) (the stub risk manager's job), the fill-time
    cap must never trigger, in either mode -- the same setup as the hand-computed 5-bar example's first
    fill (fee 50 bps, slippage 100 bps)."""
    for delay, bar in ((timedelta(0), make_bar(1, "100", "100")), (SIX_HOURS, exec_bar(6, "100"))):
        b = broker(fee="50", slip="100", delay=delay)
        b.submit(req("a", Side.BUY, "50"))
        rep = b.advance(bar)
        assert rep.resizes == ()
        assert rep.fills[0].fill.quantity == Dec("50") and rep.fills[0].fill.price == Dec("101.00")


def test_gap_fill_is_flagged_when_the_fill_lands_after_the_expected_delay() -> None:
    b = broker(fee="0", slip="0", delay=timedelta(hours=6))
    b.submit(req("a", Side.BUY, "1"))
    created = T_START + timedelta(days=1)
    # the 05:00 and 06:00 bars are missing (a feed gap): the first bar at/after the 6h deadline is 07:00
    for h in (0, 1, 2, 3, 4, 7):
        bar = make_bar(0, "100", "100", tf=Timeframe.H1, start=created + timedelta(hours=h))
        rep = b.advance(bar)
        if rep.fills:
            assert rep.fills[0].gap_fill is True
            assert rep.fills[0].fill.ts == created + timedelta(hours=7)


def test_no_gap_fill_flag_on_a_contiguous_feed() -> None:
    b = broker(fee="0", slip="0")
    b.submit(req("a", Side.BUY, "1"))
    (rec,) = b.advance(make_bar(1, "100", "100")).fills
    assert rec.gap_fill is False


def test_cancel_and_unknown_order() -> None:
    b = broker()
    b.submit(req("a", Side.BUY, "1"))
    assert [o.client_order_id for o in b.open_orders(SYMBOL)] == ["a"]
    b.cancel("a")
    assert b.get_order("a").state is OrderState.CANCELED
    assert b.advance(make_bar(1, "100", "100")).fills == ()
    with pytest.raises(BrokerError):
        b.cancel("a")
    with pytest.raises(UnknownOrderError):
        b.get_order("nope")


def test_bars_must_increase() -> None:
    b = broker()
    b.advance(make_bar(3, "1", "1"))
    with pytest.raises(BrokerError, match="strictly increasing"):
        b.advance(make_bar(3, "1", "1"))


def test_fill_delay_waits_for_eligible_bar() -> None:
    b = broker(fee="0", slip="0", delay=timedelta(hours=6))
    # created at the close of day 0 = 2024-01-02T00:00Z; eligible from the hourly bar opening at 06:00.
    b.submit(req("a", Side.BUY, "1"))
    created = T_START + timedelta(days=1)
    seen = []
    for h in range(8):
        bar = make_bar(0, "100", "100", tf=Timeframe.H1, start=created + timedelta(hours=h))
        seen.append(len(b.advance(bar).fills))
    assert seen == [0, 0, 0, 0, 0, 0, 1, 0]
    assert b.get_order("a").ts == created + timedelta(hours=6)


def test_slippage_model_hook_and_negative_guard() -> None:
    class Bad:
        def slippage_bps(self, ctx: SlippageContext) -> Dec:  # noqa: ARG002
            return Dec(-1)

    b = SimulatedBroker(
        filters=lbank_like_filters(),
        fees=FeeSchedule(Dec(0), Dec(0)),
        slippage=Bad(),
        base_asset="BTC",
        quote_asset="USDT",
        initial_cash=Dec(1000),
    )
    b.submit(req("a", Side.BUY, "1"))
    with pytest.raises(BrokerError, match="adverse"):
        b.advance(make_bar(1, "100", "100"))


def test_cost_objects_from_config_and_validation() -> None:
    costs = CostConfig(taker_fee_bps=Dec("20"), maker_fee_bps=Dec("10"), slippage_bps=Dec("3"))
    fees = FeeSchedule.from_config(costs)
    assert fees.fee(Dec("1000")) == Dec("2") and fees.fee(Dec("1000"), maker=True) == Dec("1")
    assert FlatSlippage.from_config(costs).bps == Dec("3")
    with pytest.raises(ValueError):
        FeeSchedule(Dec(-1), Dec(0))
    with pytest.raises(ValueError):
        FlatSlippage(Dec(-1))
    with pytest.raises(ValueError):
        SimulatedBroker(
            filters=lbank_like_filters(),
            fees=fees,
            slippage=FlatSlippage(Dec(0)),
            base_asset="BTC",
            quote_asset="BTC",
            initial_cash=Dec(1),
        )
