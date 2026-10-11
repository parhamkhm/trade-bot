"""SimulatedBroker: fill timing, costs, idempotency, filters, delay model."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as Dec

import pytest

from tbot.core.config import CostConfig
from tbot.core.types import (
    Bar,
    Broker,
    OrderRequest,
    OrderState,
    OrderType,
    Side,
    SymbolFilters,
    Timeframe,
)
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
    cash: str = "10000",
    *,
    fee: str = "35",
    slip: str = "5",
    delay: timedelta = timedelta(0),
    filters: SymbolFilters | None = None,
) -> SimulatedBroker:
    return SimulatedBroker(
        filters=filters or lbank_like_filters(),
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


# D-051 (as revised 2026-10-11): a BUY is sized at fill time from the real fill price and the available
# cash in BOTH fill modes. Each test below runs once per mode.
MODES = pytest.mark.parametrize("delay", [timedelta(0), SIX_HOURS], ids=["next_open", "delay_6h"])


def fill_bar(delay: timedelta, open_: str, created_i: int = 0) -> Bar:
    """The execution bar on which an order created at the close of daily bar ``created_i`` becomes eligible:
    the next daily bar in next-open mode, the 1h bar opening ``delay`` after the creation time otherwise."""
    if delay == timedelta(0):
        return make_bar(created_i + 1, open_, open_)
    created = T_START + timedelta(days=created_i + 1)
    return make_bar(0, open_, open_, tf=Timeframe.H1, start=created + delay)


@MODES
def test_buy_is_capped_rather_than_rejected_when_cash_falls_short(delay: timedelta) -> None:
    """A BUY whose requested quantity cash does not cover at the fill price is CAPPED, not rejected, in both
    fill modes (live: a market buy is sized at the open from the real price and cash; a manual order is
    sized when placed). SELLs get no such treatment."""
    b = broker("100", delay=delay)
    b.submit(req("big", Side.BUY, "10"), sizing_price=Dec("100"), sizing_cash=Dec("100"))  # 10 BTC, 100 USDT
    b.submit(req("short", Side.SELL, "1"))
    rep = b.advance(fill_bar(delay, "100"))
    (rec,) = rep.fills
    # price 100.05 (5 bps slip), unit cost 100.05 * 1.0035 = 100.400175, cash 100 -> floor to step 0.00001
    assert rec.fill.client_order_id == "big" and rec.fill.quantity == Dec("0.99601")
    assert rec.fill.price == Dec("100.05")
    # hand-computed: notional 0.99601 * 100.05 = 99.6508005; fee = 35 bps of that
    assert rec.fill.fee == Dec("99.6508005") * Dec("35") / Dec(10000) == Dec("0.34877780175")
    assert rec.fill.fee_asset == "USDT"
    # cash left = 100 - 99.6508005 - 0.34877780175: the cap left less than one step's cost behind
    assert b.balances() == {"USDT": Dec("0.00042169825"), "BTC": Dec("0.99601")}
    (resize,) = rep.resizes
    assert resize.client_order_id == "big"
    assert resize.requested_quantity == Dec("10") and resize.filled_quantity == Dec("0.99601")
    assert resize.kind == "anomaly" and resize.reason.startswith("anomaly")  # 1000 USDT requested vs 100 cash
    assert resize.capped_fraction == Dec("0.900399")
    assert [r.client_order_id for r in rep.rejections] == ["short"]  # SELL still rejected whole
    assert "INSUFFICIENT_FUNDS" in rep.rejections[0].reason


@MODES
def test_capped_order_status_is_filled_with_requested_quantity_and_is_queryable(delay: timedelta) -> None:
    """get_order reports a capped BUY as FILLED with filled_quantity < quantity; ``OrderStatus`` has no
    flag for it (core/types.py is frozen), so ``resize_of``/``was_resized`` expose the resize."""
    b = broker("100", delay=delay)
    b.submit(req("big", Side.BUY, "10"))
    assert not b.was_resized("big") and b.resize_of("big") is None  # not yet filled
    b.advance(fill_bar(delay, "100"))
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


@MODES
def test_unresized_order_is_not_reported_as_resized(delay: timedelta) -> None:
    b = broker(delay=delay)
    b.submit(req("ok", Side.BUY, "1"))
    b.advance(fill_bar(delay, "100"))
    assert b.get_order("ok").filled_quantity == Dec("1") and not b.was_resized("ok")


@MODES
def test_sell_is_never_capped(delay: timedelta) -> None:
    """(b) A SELL larger than the position is rejected whole (INSUFFICIENT_FUNDS) with no ResizeRecord, and a
    SELL the position covers fills in full regardless of the cash balance -- in both fill modes."""
    b = broker("1000", fee="0", slip="0", delay=delay)
    b.submit(req("buy", Side.BUY, "1"))
    b.advance(fill_bar(delay, "100"))  # now long 1 BTC, 900 USDT
    b.submit(req("oversell", Side.SELL, "2", created_i=1))
    rep = b.advance(fill_bar(delay, "100", created_i=1))
    assert rep.fills == () and rep.resizes == ()
    (rej,) = rep.rejections
    assert rej.client_order_id == "oversell" and "INSUFFICIENT_FUNDS" in rej.reason
    assert not b.was_resized("oversell") and b.get_order("oversell").state is OrderState.REJECTED
    assert b.balances() == {"USDT": Dec("900"), "BTC": Dec("1")}
    b.submit(req("exit", Side.SELL, "1", created_i=2))
    rep = b.advance(fill_bar(delay, "100", created_i=2))
    (rec,) = rep.fills
    assert rec.fill.quantity == Dec("1") and rep.resizes == () and rep.rejections == ()
    assert b.balances() == {"USDT": Dec("1000"), "BTC": Dec("0")}


@MODES
def test_buy_capped_to_zero_is_still_rejected(delay: timedelta) -> None:
    """(c) The cap can land below a tradable size; that must reject (never fill a dust order), but the
    ``ResizeRecord`` is still emitted so the report shows *why* (price moved), not just a bare refusal."""
    b = broker("0.000001", fee="0", slip="0", delay=delay)  # below one step at price 1: floors to zero
    b.submit(req("dust", Side.BUY, "1"))
    rep = b.advance(fill_bar(delay, "1"))
    assert rep.fills == ()
    (resize,) = rep.resizes
    assert resize.filled_quantity == Dec("0")
    assert "ZERO_QUANTITY" in rep.rejections[0].reason
    assert b.was_resized("dust") and b.get_order("dust").state is OrderState.REJECTED
    assert b.balances() == {"USDT": Dec("0.000001"), "BTC": Dec("0")}


@MODES
def test_buy_capped_below_min_qty_is_still_rejected(delay: timedelta) -> None:
    """(c) Cash affords 0.0005 BTC at the fill price but min_qty is 0.001: the capped BUY is rejected."""
    flt = SymbolFilters(
        symbol=SYMBOL,
        tick_size=Dec("0.01"),
        step_size=Dec("0.00001"),
        min_qty=Dec("0.001"),
        min_notional=Dec("0.01"),
    )
    b = broker("50", fee="0", slip="0", delay=delay, filters=flt)
    b.submit(req("small", Side.BUY, "0.002"))  # valid at submit; cash 50 / price 100000 = 0.0005 at fill
    rep = b.advance(fill_bar(delay, "100000"))
    assert rep.fills == ()
    (resize,) = rep.resizes
    assert resize.requested_quantity == Dec("0.002") and resize.filled_quantity == Dec("0.0005")
    (rej,) = rep.rejections
    assert "MIN_QUANTITY" in rej.reason
    assert b.balances() == {"USDT": Dec("50"), "BTC": Dec("0")}


@MODES
def test_buy_capped_below_min_notional_is_still_rejected(delay: timedelta) -> None:
    """(c) Cash affords only 0.5 USDT of BTC but min_notional is 1: the capped BUY is rejected."""
    b = broker("0.5", fee="0", slip="0", delay=delay)
    b.submit(req("small", Side.BUY, "0.1"))  # 0.1 * 100 = 10 USDT intended; only 0.5 USDT of cash
    rep = b.advance(fill_bar(delay, "100"))
    assert rep.fills == ()
    (resize,) = rep.resizes
    assert resize.requested_quantity == Dec("0.1") and resize.filled_quantity == Dec("0.005")
    (rej,) = rep.rejections
    assert "MIN_NOTIONAL" in rej.reason
    assert b.balances() == {"USDT": Dec("0.5"), "BTC": Dec("0")}


# Resize kinds are decided by CAUSE (Parham, 2026-10-11): affordable at the sizing price => price_move,
# however large the move; not affordable even there => anomaly (sizing bug); no sizing reference => anomaly
# (fail safe).


@MODES
@pytest.mark.parametrize(
    ("open_", "quantity"),
    [
        # sized at close 100 (99.60142); open 100.43 -> price 100.49 -> 99.16531: 0.44 % capped
        ("100.43", "99.16531"),
        # open 105 -> price 105.06 -> 94.85172: 4.77 % capped; not an anomaly (affordable at 100).
        ("105", "94.85172"),
        # +2.5 % (the size of a large real 6 h move): open 102.5 -> 102.55125 -> 102.56; unit cost
        # 102.56 * 1.0035 = 102.91896; floor_5(10000 / 102.91896) = 97.16382
        ("102.5", "97.16382"),
    ],
)
def test_price_move_cap_is_not_an_anomaly_whatever_the_size(
    delay: timedelta, open_: str, quantity: str
) -> None:
    b = broker("10000", delay=delay)
    b.submit(req("a", Side.BUY, "99.60142"), sizing_price=Dec("100"), sizing_cash=Dec("10000"))
    rep = b.advance(fill_bar(delay, open_))
    assert rep.rejections == ()
    (resize,) = rep.resizes
    assert resize.kind == "price_move" and resize.reason.startswith("price_move")
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")
    # the reason states the cause with the numbers: requested cost at the sizing price vs cash at sizing
    # requested cost at the sizing price: 99.60142 * 100 * 1.0035 = 9995.002497000 <= cash 10000
    assert "9995.002497000" in resize.reason and "cash at sizing 10000" in resize.reason
    assert resize.requested_quantity == Dec("99.60142") and resize.filled_quantity == Dec(quantity)
    assert b.resize_of("a") == resize


@MODES
def test_request_not_affordable_at_the_sizing_price_is_an_anomaly(delay: timedelta) -> None:
    """A request of 2x cash is a sizing bug whatever the price does afterwards: anomaly, and the broker still
    fills the capped order (no INSUFFICIENT_FUNDS)."""
    b = broker("10000", fee="0", slip="0", delay=delay)
    b.submit(req("big", Side.BUY, "200"), sizing_price=Dec("100"), sizing_cash=Dec("10000"))  # 20000 > 10000
    rep = b.advance(fill_bar(delay, "100"))
    assert rep.rejections == ()
    (rec,) = rep.fills
    assert rec.fill.quantity == Dec("100")
    (resize,) = rep.resizes
    assert resize.kind == "anomaly" and resize.reason.startswith("anomaly")
    assert "not affordable even at the sizing price" in resize.reason
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")


@MODES
def test_fail_safe_no_sizing_reference_is_an_anomaly(delay: timedelta) -> None:
    """A direct broker user with no sizing reference cannot prove the cap was a price move: anomaly."""
    b = broker("10000", delay=delay)
    b.submit(req("a", Side.BUY, "99.60142"))  # would be an ordinary price_move if the reference were known
    rep = b.advance(fill_bar(delay, "100.43"))
    (resize,) = rep.resizes
    assert resize.kind == "anomaly" and "no sizing reference" in resize.reason
    assert resize.sizing_price is None and resize.sizing_cash is None
    assert rep.rejections == () and rep.fills[0].fill.quantity == Dec("99.16531")


def test_affordability_boundary_exactly_cash_is_a_price_move() -> None:
    """The rule is `cost > cash` => anomaly, so a request costing exactly the cash is still affordable."""
    for qty, kind in (("1", "price_move"), ("1.00001", "anomaly")):
        b = broker("100", fee="0", slip="0")
        b.submit(req("a", Side.BUY, qty), sizing_price=Dec("100"), sizing_cash=Dec("100"))
        (resize,) = b.advance(make_bar(1, "101", "101")).resizes  # price moved up: cap to floor_5(100 / 101)
        assert resize.kind == kind, qty


def test_taker_fee_is_part_of_the_affordability_test() -> None:
    """1 BTC at 100 costs exactly 100 without fees (affordable) but 100.35 with the 35 bps taker fee."""
    b = broker("100", fee="35", slip="0")
    b.submit(req("a", Side.BUY, "1"), sizing_price=Dec("100"), sizing_cash=Dec("100"))
    (resize,) = b.advance(make_bar(1, "100", "100")).resizes
    assert resize.kind == "anomaly" and "= 100.3500 USDT vs cash at sizing 100 USDT" in resize.reason


def test_sizing_reference_must_be_complete_and_sane() -> None:
    b = broker()
    with pytest.raises(ValueError, match="together"):
        b.submit(req("a", Side.BUY, "1"), sizing_price=Dec("100"))
    with pytest.raises(ValueError, match="together"):
        b.submit(req("b", Side.BUY, "1"), sizing_cash=Dec("100"))
    for bad_price in (Dec("0"), Dec("-1"), Dec("NaN")):
        with pytest.raises(ValueError, match="sizing_price"):
            b.submit(req("c", Side.BUY, "1"), sizing_price=bad_price, sizing_cash=Dec("100"))
    with pytest.raises(ValueError, match="sizing_cash"):
        b.submit(req("d", Side.BUY, "1"), sizing_price=Dec("100"), sizing_cash=Dec("-1"))
    assert b.open_orders(SYMBOL) == []  # nothing was created by the refused calls


def test_duplicate_submit_ignores_the_second_sizing_reference() -> None:
    b = broker("10000", fee="0", slip="0")
    b.submit(req("a", Side.BUY, "200"), sizing_price=Dec("100"), sizing_cash=Dec("10000"))
    assert not b.submit(req("a", Side.BUY, "200"), sizing_price=Dec("1"), sizing_cash=Dec("1000000")).accepted
    (resize,) = b.advance(make_bar(1, "100", "100")).resizes
    assert resize.sizing_price == Dec("100") and resize.kind == "anomaly"


def test_the_threshold_argument_is_gone() -> None:
    with pytest.raises(TypeError, match="resize_anomaly_threshold"):
        SimulatedBroker(  # type: ignore[call-arg]
            filters=lbank_like_filters(),
            fees=FeeSchedule(taker_bps=Dec("35"), maker_bps=Dec("33")),
            slippage=FlatSlippage(Dec("5")),
            base_asset="BTC",
            quote_asset="USDT",
            initial_cash=Dec(10000),
            resize_anomaly_threshold=Dec("0.01"),
        )


def test_submit_with_sizing_reference_still_satisfies_the_broker_protocol() -> None:
    b: Broker = broker()
    assert b.submit(req("a", Side.BUY, "1")).accepted  # the Protocol-typed call has no sizing arguments


class _SizeSlippage:
    """Size-dependent impact: 10 bps per BTC of order size (a bigger order walks further up the book)."""

    def slippage_bps(self, ctx: SlippageContext) -> Dec:
        return ctx.quantity * Dec(10)


class _EverWorseSlippage:
    """Pathological: every pricing call is worse than the last, so no quantity ever becomes affordable."""

    def __init__(self) -> None:
        self.calls = 0

    def slippage_bps(self, ctx: SlippageContext) -> Dec:  # noqa: ARG002
        self.calls += 1
        return Dec(100) * self.calls


def _slip_broker(model: object, cash: str = "1000") -> SimulatedBroker:
    fine_tick = SymbolFilters(  # a 5-decimal tick so the price change from re-pricing is not rounded away
        symbol=SYMBOL,
        tick_size=Dec("0.00001"),
        step_size=Dec("0.00001"),
        min_qty=Dec("0.00001"),
        min_notional=Dec(1),
    )
    return SimulatedBroker(
        filters=fine_tick,
        fees=FeeSchedule(Dec(0), Dec(0)),
        slippage=model,  # type: ignore[arg-type]
        base_asset="BTC",
        quote_asset="USDT",
        initial_cash=Dec(cash),
    )


def test_cap_reprices_slippage_at_the_final_quantity() -> None:
    """Item 3: with a size-dependent model the fill price must be derived at the CAPPED quantity, not left
    at the price of the requested (larger) one. Request 10 BTC with 1000 USDT: at 10 BTC the impact is 100 bps
    (price 101.00000, affordable floor_5(1000 / 101) = 9.90099); at 9.90099 BTC it is 99.0099 bps (price
    100.990099 -> ceil tick 100.99010) and 9.90099 * 100.99010 = 999.9019701990 <= 1000, so the quantity is
    stable and the fill is priced at 100.99010, not 101."""
    b = _slip_broker(_SizeSlippage())
    b.submit(req("a", Side.BUY, "10"))
    rep = b.advance(make_bar(1, "100", "100"))
    (rec,) = rep.fills
    assert rec.fill.quantity == Dec("9.90099") and rec.fill.price == Dec("100.99010")
    cash_left = b.balances()["USDT"]
    assert cash_left == Dec("0.0980298010") and cash_left >= 0  # 1000 - 9.90099 * 100.99010
    assert rec.slippage_cost == Dec("9.8029701990")  # (100.99010 - 100) * 9.90099
    (resize,) = rep.resizes
    assert resize.requested_quantity == Dec("10") and resize.filled_quantity == Dec("9.90099")


def test_cap_never_leaves_notional_plus_fee_above_cash() -> None:
    """Item 3: when no stable affordable quantity exists within the bound, the BUY is rejected -- the books
    never go negative."""
    model = _EverWorseSlippage()
    b = _slip_broker(model)
    b.submit(req("a", Side.BUY, "10"))
    rep = b.advance(make_bar(1, "100", "100"))
    assert rep.fills == ()
    (rej,) = rep.rejections
    assert "INSUFFICIENT_FUNDS" in rej.reason
    assert b.balances() == {"USDT": Dec("1000"), "BTC": Dec("0")}
    assert model.calls <= 9  # bounded: the first pricing plus at most 8 re-pricings


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
