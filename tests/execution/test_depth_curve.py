"""DepthCurveSlippage (SPEC section 7 G3 / D-067), hand-computed.

Curves used (notional in USDT, cost in bps):
  buy : (0, 0) (1000, 10) (3000, 30)     slope 0.010 bps/USDT on both segments
  sell: (0, 0) (1000, 20) (2000, 60)     slope 0.020 then 0.040 bps/USDT
  half spread: 1 bp
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as Dec

import pytest

from tbot.core.types import Side, SymbolFilters
from tbot.execution.simulated import (
    DepthCurveSlippage,
    FeeSchedule,
    SimulatedBroker,
    SlippageContext,
    SlippageModel,
)
from tests.execution.test_simulated import req
from tests.sim_helpers import SYMBOL, make_bar

TS = datetime(2024, 1, 1, tzinfo=UTC)
BUY = ((Dec(0), Dec(0)), (Dec(1000), Dec(10)), (Dec(3000), Dec(30)))
SELL = ((Dec(0), Dec(0)), (Dec(1000), Dec(20)), (Dec(2000), Dec(60)))


def model(**kw: object) -> DepthCurveSlippage:
    args: dict[str, object] = {"buy_points": BUY, "sell_points": SELL, "half_spread_bps": Dec(1)}
    args.update(kw)
    return DepthCurveSlippage(**args)  # type: ignore[arg-type]


def ctx(side: Side, notional: str, *, equity: str | None = None, price: str = "100") -> SlippageContext:
    """A context whose order notional is exactly ``notional`` (quantity = notional / price)."""
    return SlippageContext(
        side=side,
        quantity=Dec(notional) / Dec(price),
        reference_price=Dec(price),
        ts=TS,
        equity=None if equity is None else Dec(equity),
    )


def bps(m: SlippageModel, c: SlippageContext) -> Dec:
    return m.slippage_bps(c)


def test_satisfies_the_slippage_model_protocol() -> None:
    m: SlippageModel = model()
    assert m.slippage_bps(ctx(Side.BUY, "500")) > 0


def test_interpolation_inside_the_curve() -> None:
    m = model()
    # buy 500: 10 * 500 / 1000 = 5, + 1 half spread
    assert bps(m, ctx(Side.BUY, "500")) == Dec(6)
    # buy 2000: second segment, 10 + (30 - 10) * (2000 - 1000) / (3000 - 1000) = 20, + 1
    assert bps(m, ctx(Side.BUY, "2000")) == Dec(21)
    # exactly on a point: 10 + 1; and on the last point: 30 + 1
    assert bps(m, ctx(Side.BUY, "1000")) == Dec(11)
    assert bps(m, ctx(Side.BUY, "3000")) == Dec(31)
    # sell 1500: 20 + (60 - 20) * 500 / 1000 = 40, + 1
    assert bps(m, ctx(Side.SELL, "1500")) == Dec(41)


def test_extrapolation_beyond_the_last_point_uses_the_slope_of_the_last_two() -> None:
    m = model()
    # buy: slope (30 - 10) / (3000 - 1000) = 0.01 -> 5000 costs 30 + 0.01 * 2000 = 50, + 1
    assert bps(m, ctx(Side.BUY, "5000")) == Dec(51)
    # sell: slope (60 - 20) / (2000 - 1000) = 0.04 (NOT the first segment's 0.02):
    # 3000 costs 60 + 0.04 * 1000 = 100, + 1
    assert bps(m, ctx(Side.SELL, "3000")) == Dec(101)


def test_half_spread_is_added_exactly_once() -> None:
    no_spread = model(half_spread_bps=Dec(0))
    with_spread = model(half_spread_bps=Dec("2.5"))
    for side, n in ((Side.BUY, "500"), (Side.BUY, "5000"), (Side.SELL, "1500"), (Side.SELL, "3000")):
        assert bps(with_spread, ctx(side, n)) - bps(no_spread, ctx(side, n)) == Dec("2.5")
    # even a vanishing order pays the half spread once (the curve itself is 0 at the origin)
    assert bps(with_spread, ctx(Side.BUY, "0.0001")) == Dec("2.5") + Dec("0.000001")


def test_each_side_reads_its_own_curve_and_the_price_moves_against_the_trader() -> None:
    m = model()
    # the same 500 USDT notional: buy 6 bps, sell 10 + 1 = 11 bps
    assert bps(m, ctx(Side.BUY, "500")) == Dec(6)
    assert bps(m, ctx(Side.SELL, "500")) == Dec(11)
    # through the broker (zero fees, 5-decimal tick): a buy fills ABOVE the open, a sell BELOW it
    b = _broker(m, cash="1000")
    b.submit(req("buy", Side.BUY, "5"))
    (buy,) = b.advance(make_bar(1, "100", "100")).fills
    assert buy.fill.price == Dec("100.06")  # 100 * (1 + 6/10000)
    b.submit(req("sell", Side.SELL, "5", created_i=1))
    (sell,) = b.advance(make_bar(2, "100", "100")).fills
    assert sell.fill.price == Dec("99.89")  # 100 * (1 - 11/10000)


def test_equity_normaliser_rescales_the_lookup_notional() -> None:
    plain = model()
    norm = model(equity_normaliser=Dec(10_000))
    # equity 20 000: a 2000 USDT order is looked up as 2000 * 10000 / 20000 = 1000 -> 10 + 1
    assert bps(norm, ctx(Side.BUY, "2000", equity="20000")) == Dec(11)
    assert bps(plain, ctx(Side.BUY, "2000", equity="20000")) == Dec(21)  # without it the raw notional is used
    # equity 5000: a 500 USDT order is looked up as 500 * 10000 / 5000 = 1000 -> 11 as well
    assert bps(norm, ctx(Side.BUY, "500", equity="5000")) == Dec(11)
    # equity at the normaliser: a no-op
    assert bps(norm, ctx(Side.SELL, "1500", equity="10000")) == Dec(41)
    # the plain model ignores equity (and works without it)
    assert bps(plain, ctx(Side.BUY, "500")) == Dec(6)


def test_equity_normaliser_needs_a_positive_equity() -> None:
    norm = model(equity_normaliser=Dec(10_000))
    with pytest.raises(ValueError, match="equity"):
        norm.slippage_bps(ctx(Side.BUY, "500"))
    with pytest.raises(ValueError, match="equity"):
        norm.slippage_bps(ctx(Side.BUY, "500", equity="0"))


def test_scaled_multiplies_every_cost_and_the_half_spread() -> None:
    m = model(equity_normaliser=Dec(10_000))
    twice = m.scaled(Dec(2))
    assert twice.buy_points == ((Dec(0), Dec(0)), (Dec(1000), Dec(20)), (Dec(3000), Dec(60)))
    assert twice.sell_points == ((Dec(0), Dec(0)), (Dec(1000), Dec(40)), (Dec(2000), Dec(120)))
    assert twice.half_spread_bps == Dec(2)
    assert twice.equity_normaliser == Dec(10_000)  # the lookup geometry is not scaled
    # buy 500 at 2x: 2 * 5 + 2 = 12 = 2 * (5 + 1); extrapolated sell 3000: 2 * 100 + 2
    assert bps(twice, ctx(Side.BUY, "500", equity="10000")) == Dec(12)
    assert bps(twice, ctx(Side.SELL, "3000", equity="10000")) == Dec(202)
    assert m.buy_points == BUY  # the original is untouched
    assert m.scaled(Dec(1)) == m
    with pytest.raises(ValueError):
        m.scaled(Dec(-1))


def test_curve_validation() -> None:
    with pytest.raises(ValueError, match=r"start at \(0, 0\)"):
        model(buy_points=((Dec(1), Dec(1)), (Dec(2), Dec(2))))
    with pytest.raises(ValueError, match="at least 2 points"):
        model(sell_points=((Dec(0), Dec(0)),))
    with pytest.raises(ValueError, match="strictly increasing"):
        model(buy_points=((Dec(0), Dec(0)), (Dec(5), Dec(1)), (Dec(5), Dec(2))))
    with pytest.raises(ValueError, match="must not decrease"):
        model(buy_points=((Dec(0), Dec(0)), (Dec(5), Dec(3)), (Dec(9), Dec(2))))
    with pytest.raises(ValueError, match="half_spread_bps"):
        model(half_spread_bps=Dec(-1))
    with pytest.raises(ValueError, match="equity_normaliser"):
        model(equity_normaliser=Dec(0))
    with pytest.raises(ValueError, match="finite Decimals"):
        model(buy_points=((Dec(0), Dec(0)), (1000, Dec(1))))  # an int is not a Decimal
    assert model(buy_points=[(Dec(0), Dec(0)), (Dec(5), Dec(1))]).buy_points == (  # lists are normalised
        (Dec(0), Dec(0)),
        (Dec(5), Dec(1)),
    )


# ---------------------------------------------------------------------------------------------
# the broker prices at the FINAL (capped) quantity with this model (D-081)
# ---------------------------------------------------------------------------------------------


def _broker(m: SlippageModel, *, cash: str) -> SimulatedBroker:
    fine_tick = SymbolFilters(  # 5-decimal tick so the price change from re-pricing is not rounded away
        symbol=SYMBOL,
        tick_size=Dec("0.00001"),
        step_size=Dec("0.00001"),
        min_qty=Dec("0.00001"),
        min_notional=Dec(1),
    )
    return SimulatedBroker(
        filters=fine_tick,
        fees=FeeSchedule(Dec(0), Dec(0)),
        slippage=m,
        base_asset="BTC",
        quote_asset="USDT",
        initial_cash=Dec(cash),
    )


def test_broker_reprices_at_the_final_capped_quantity_with_the_depth_curve() -> None:
    """Curve (0,0)-(1000,100 bps), no spread: impact = 0.1 bps per USDT of notional. Cash 1000, open 100,
    request 10 BTC. At 10 BTC the notional is 1000 -> 100 bps -> price 101.00000, so the cash affords
    floor_5(1000 / 101) = 9.90099. At 9.90099 BTC the notional is 990.099 -> 99.0099 bps -> price
    100.990099 -> ceil to tick 100.99010; 9.90099 * 100.99010 = 999.9019701990 <= 1000, so the quantity is
    stable and the fill is priced at 100.99010, not 101."""
    curve = ((Dec(0), Dec(0)), (Dec(1000), Dec(100)))
    b = _broker(model(buy_points=curve, sell_points=curve, half_spread_bps=Dec(0)), cash="1000")
    b.submit(req("a", Side.BUY, "10"))
    rep = b.advance(make_bar(1, "100", "100"))
    (rec,) = rep.fills
    assert rec.fill.quantity == Dec("9.90099") and rec.fill.price == Dec("100.99010")
    assert b.balances()["USDT"] == Dec("0.0980298010")  # 1000 - 9.90099 * 100.99010, never negative
    assert rec.slippage_cost == Dec("9.8029701990")  # (100.99010 - 100) * 9.90099
    (resize,) = rep.resizes
    assert resize.requested_quantity == Dec(10) and resize.capped_quantity == Dec("9.90099")


class _Spy:
    """Records the context of every call and charges nothing."""

    def __init__(self) -> None:
        self.contexts: list[SlippageContext] = []

    def slippage_bps(self, ctx: SlippageContext) -> Dec:
        self.contexts.append(ctx)
        return Dec(0)


def test_broker_hands_the_model_the_equity_marked_at_the_bar_open() -> None:
    spy = _Spy()
    b = _broker(spy, cash="1000")
    b.submit(req("buy", Side.BUY, "5"))
    b.advance(make_bar(1, "100", "100"))  # cash 1000 -> 500, 5 BTC
    b.submit(req("sell", Side.SELL, "5", created_i=1))
    b.advance(make_bar(2, "200", "200"))
    assert [c.equity for c in spy.contexts] == [Dec(1000), Dec(500) + Dec(5) * Dec(200)]  # 1000 then 1500


def test_equity_normaliser_through_the_broker() -> None:
    """Equity 20 000, buy 10 BTC at open 100 (notional 1000): looked up at 1000 * 10000 / 20000 = 500 on the
    curve (0,0)-(1000,100) -> 50 bps -> price 100.50000. Without the normaliser it would be 100 bps."""
    curve = ((Dec(0), Dec(0)), (Dec(1000), Dec(100)))
    norm = model(buy_points=curve, sell_points=curve, half_spread_bps=Dec(0), equity_normaliser=Dec(10_000))
    b = _broker(norm, cash="20000")
    b.submit(req("a", Side.BUY, "10"))
    (rec,) = b.advance(make_bar(1, "100", "100")).fills
    assert rec.fill.price == Dec("100.50000") and rec.fill.quantity == Dec(10)
