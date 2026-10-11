"""SPEC 7.1 S0 relative rebalance band in the backtest risk layer (StubRiskManager), hand-computed.

Setup for every case: price 100, equity 10 000, no fee and no slippage (sizing is the plain gap).
Band rule while invested: trade only if |target_value - held_value| >= band * max(target_value, held_value).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tbot.backtest.config import BacktestConfig
from tbot.backtest.risk_stub import StubRiskManager
from tbot.core.config import CostConfig
from tbot.core.types import Approved, MarketState, PortfolioView, Refused, Side, TargetIntent, Timeframe
from tests.sim_helpers import SYMBOL, T_START, lbank_like_filters

Dec = Decimal
PRICE = Dec(100)
EQUITY = Dec(10_000)


def stub(band: float = 0.25) -> StubRiskManager:
    return StubRiskManager(taker_fee_bps=Dec(0), slippage_bps=Dec(0), rebalance_band=band)


def decide(held_value: str, target: float, band: float = 0.25) -> Approved | Refused:
    held = Dec(held_value)
    view = PortfolioView(T_START, EQUITY - held, held / PRICE, EQUITY)
    market = MarketState(SYMBOL, T_START, PRICE, lbank_like_filters())
    intent = TargetIntent("s", T_START, target, "t")
    return stub(band).evaluate(intent, view, market)


def assert_trades(decision: Approved | Refused, side: Side, quantity: str) -> None:
    assert isinstance(decision, Approved) and decision.order is not None, decision
    assert (decision.order.side, decision.order.quantity) == (side, Dec(quantity))


def assert_no_trade(decision: Approved | Refused) -> None:
    assert isinstance(decision, Approved) and decision.order is None and "band" in decision.note, decision


def test_upsize_at_exactly_25_percent_trades_and_just_below_does_not() -> None:
    # target 0.8 -> 8000; band edge = 0.25 * 8000 = 2000
    assert_trades(decide("6000", 0.8), Side.BUY, "20")  # gap exactly 2000 = 25.0 %: trades
    assert_no_trade(decide("6008", 0.8))  # gap 1992 = 24.9 %: no trade
    assert_no_trade(decide("6001", 0.8))  # gap 1999
    assert_trades(decide("5999", 0.8), Side.BUY, "20.01")  # gap 2001


def test_downsize_at_exactly_25_percent_trades_and_just_below_does_not() -> None:
    # target 0.6 -> 6000; held is the larger value, so the edge is 0.25 * held
    assert_trades(decide("8000", 0.6), Side.SELL, "20")  # gap 2000 = 25.0 % of 8000: trades
    assert_no_trade(decide("7990", 0.6))  # gap 1990 < 1997.5
    assert_trades(decide("8004", 0.6), Side.SELL, "20.04")  # gap 2004 >= 2001


def test_the_band_is_relative_to_the_larger_side_not_to_equity() -> None:
    # a 5 %-of-equity target and a 6.25 %-of-equity position differ by 0.0125 of equity (tiny in absolute
    # terms) but by 20 % of the larger side: still no trade; at 4 % vs 6.25 % (36 %) it trades
    assert_no_trade(decide("625", 0.05))
    assert_trades(decide("625", 0.04), Side.SELL, "2.25")


def test_entry_from_flat_always_trades_whatever_the_band() -> None:
    for band in (0.0, 0.25, 1.0):
        assert_trades(decide("0", 0.01, band), Side.BUY, "1")  # 100 of 10 000: a 1 % entry
        assert_trades(decide("0", 1.0, band), Side.BUY, "100")


def test_exit_to_zero_always_trades_even_for_a_dust_position_and_band_one() -> None:
    for band in (0.0, 0.25, 1.0):
        assert_trades(decide("100", 0.0, band), Side.SELL, "1")
        assert_trades(decide("9999", 0.0, band), Side.SELL, "99.99")


def test_band_one_never_resizes_while_invested() -> None:
    assert_no_trade(decide("1000", 1.0, 1.0))  # 10 % -> 100 %: gap 9000 < 10000
    assert_no_trade(decide("9000", 0.1, 1.0))


def test_band_zero_resizes_on_any_gap() -> None:
    assert_trades(decide("6000", 0.61, 0.0), Side.BUY, "1")


def test_the_target_already_met_case_is_unchanged() -> None:
    decision = decide("5000", 0.5, 0.0)
    assert isinstance(decision, Approved) and decision.order is None and decision.note == "target already met"


@pytest.mark.parametrize("band", [-0.01, 1.01, float("nan")])
def test_band_must_be_within_zero_and_one(band: float) -> None:
    with pytest.raises(ValueError, match="rebalance_band"):
        stub(band)
    with pytest.raises(ValueError, match="rebalance_band"):
        BacktestConfig(
            symbol=SYMBOL,
            timeframe=Timeframe.D1,
            filters=lbank_like_filters(),
            costs=CostConfig(taker_fee_bps=Dec(10), maker_fee_bps=Dec(10), slippage_bps=Dec(5)),
            rebalance_band=band,
        )


def test_the_band_default_is_a_quarter_and_the_old_absolute_drift_is_gone() -> None:
    cfg = BacktestConfig(
        symbol=SYMBOL,
        timeframe=Timeframe.D1,
        filters=lbank_like_filters(),
        costs=CostConfig(taker_fee_bps=Dec(10), maker_fee_bps=Dec(10), slippage_bps=Dec(5)),
    )
    assert cfg.rebalance_band == 0.25
    assert not hasattr(cfg, "drift_tolerance")
