"""Contract tests for src/tbot/core/types.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbot.core.types import (
    Approved,
    Bar,
    BarWindow,
    Fill,
    MarketState,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    PortfolioView,
    RefusalReason,
    Refused,
    RegimeState,
    Side,
    SymbolFilters,
    TargetIntent,
    Timeframe,
    TradingState,
    ensure_utc,
)

TS = datetime(2024, 1, 1, tzinfo=UTC)


def make_bar(
    ts: datetime = TS,
    close: str = "100",
    *,
    symbol: str = "BTCUSDT",
    timeframe: Timeframe = Timeframe.D1,
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("1.5"),
    )


# --- ensure_utc -------------------------------------------------------------------


def test_ensure_utc_rejects_naive():
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2024, 1, 1))  # noqa: DTZ001


def test_ensure_utc_rejects_non_zero_offset():
    tehran = timezone(timedelta(hours=3, minutes=30))
    with pytest.raises(ValueError, match="UTC"):
        ensure_utc(datetime(2024, 1, 1, tzinfo=tehran))


def test_ensure_utc_accepts_zero_offset_and_normalises():
    assert ensure_utc(datetime(2024, 1, 1, tzinfo=timezone(timedelta(0)))).tzinfo is UTC


# --- Timeframe --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "delta", "per_year"),
    [
        (Timeframe.H1, timedelta(hours=1), 8760.0),
        (Timeframe.H4, timedelta(hours=4), 2190.0),
        (Timeframe.D1, timedelta(days=1), 365.0),
    ],
)
def test_timeframe_metadata(timeframe: Timeframe, delta: timedelta, per_year: float) -> None:
    assert timeframe.delta == delta
    assert timeframe.periods_per_year == per_year


# --- Bar --------------------------------------------------------------------------


def test_bar_open_ts_is_close_minus_one_timeframe():
    bar = make_bar(timeframe=Timeframe.H4)
    assert bar.open_ts == bar.ts - timedelta(hours=4)


def test_bar_rejects_naive_timestamp():
    with pytest.raises(ValueError, match=r"Bar\.ts"):
        make_bar(ts=datetime(2024, 1, 1))  # noqa: DTZ001


def test_bar_rejects_inconsistent_high():
    with pytest.raises(ValueError, match="high"):
        Bar(
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
            ts=TS,
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("90"),
            close=Decimal("95"),
            volume=Decimal("1"),
        )


def test_bar_rejects_float_prices():
    with pytest.raises(TypeError, match="Decimal"):
        Bar(
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
            ts=TS,
            open=100.0,  # type: ignore[arg-type]
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )


def test_bar_allows_zero_volume_but_not_zero_price():
    assert make_bar().volume >= 0
    with pytest.raises(ValueError, match="must be > 0"):
        Bar(
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
            ts=TS,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("0"),
            close=Decimal("0"),
            volume=Decimal("0"),
        )


def test_bar_is_frozen():
    with pytest.raises(AttributeError):
        make_bar().close = Decimal("1")  # type: ignore[misc]


# --- BarWindow --------------------------------------------------------------------


def _window(n: int = 5) -> BarWindow:
    return BarWindow.of([make_bar(TS + timedelta(days=i), close=str(100 + i)) for i in range(n)])


def test_window_as_of_is_last_close_time():
    window = _window(3)
    assert window.as_of == window.last.ts == TS + timedelta(days=2)
    assert len(window) == 3


def test_window_rejects_empty():
    with pytest.raises(ValueError, match="empty sequence"):
        BarWindow.of([])


def test_window_rejects_unsorted_or_duplicate_timestamps():
    bars = [make_bar(TS + timedelta(days=1)), make_bar(TS)]
    with pytest.raises(ValueError, match="strictly increase"):
        BarWindow.of(bars)


def test_window_rejects_mixed_symbols():
    bars = [make_bar(TS), make_bar(TS + timedelta(days=1), symbol="ETHUSDT")]
    with pytest.raises(ValueError, match="foreign bar"):
        BarWindow.of(bars)


def test_window_tail_is_causal_suffix():
    window = _window(5)
    tail = window.tail(2)
    assert [bar.ts for bar in tail] == [bar.ts for bar in window][-2:]
    assert tail.as_of == window.as_of


def test_window_tail_rejects_non_positive():
    with pytest.raises(ValueError, match="n > 0"):
        _window().tail(0)


def test_window_price_series_are_floats_in_order():
    assert _window(3).closes() == (100.0, 101.0, 102.0)


# --- SymbolFilters ----------------------------------------------------------------

FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    tick_size=Decimal("0.01"),
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("10"),
)


def test_round_price_rounds_down_to_tick():
    assert FILTERS.round_price(Decimal("61234.5678")) == Decimal("61234.56")


def test_floor_qty_rounds_down_to_step():
    assert FILTERS.floor_qty(Decimal("0.123456789")) == Decimal("0.12345")


def test_passes_notional():
    assert FILTERS.passes_notional(Decimal("100"), Decimal("0.1"))
    assert not FILTERS.passes_notional(Decimal("100"), Decimal("0.09"))


@given(
    quantity=st.decimals(min_value=Decimal("0"), max_value=Decimal("1000"), places=8),
    step=st.sampled_from([Decimal("0.001"), Decimal("0.00001"), Decimal("1")]),
)
def test_floor_qty_never_rounds_up(quantity: Decimal, step: Decimal) -> None:
    filters = SymbolFilters(
        symbol="X",
        tick_size=Decimal("0.01"),
        step_size=step,
        min_qty=step,
        min_notional=Decimal("10"),
    )
    floored = filters.floor_qty(quantity)
    assert floored <= quantity
    assert quantity - floored < step
    assert floored % step == 0


# --- MarketState ------------------------------------------------------------------


def test_market_state_spread_and_age():
    market = MarketState(
        symbol="BTCUSDT",
        ts=TS,
        last_price=Decimal("100"),
        filters=FILTERS,
        bid=Decimal("99.9"),
        ask=Decimal("100.1"),
    )
    assert market.spread_bps == pytest.approx(20.0, rel=1e-6)
    assert market.age_seconds(TS + timedelta(minutes=5)) == 300.0


def test_market_state_without_book_has_no_spread():
    market = MarketState(symbol="BTCUSDT", ts=TS, last_price=Decimal("100"), filters=FILTERS)
    assert market.spread_bps is None


def test_market_state_rejects_crossed_book():
    with pytest.raises(ValueError, match="crossed book"):
        MarketState(
            symbol="BTCUSDT",
            ts=TS,
            last_price=Decimal("100"),
            filters=FILTERS,
            bid=Decimal("101"),
            ask=Decimal("100"),
        )


# --- TargetIntent / RegimeState ---------------------------------------------------


def test_intent_rejects_weight_outside_unit_interval():
    for weight in (-0.01, 1.01):
        with pytest.raises(ValueError, match="target_weight"):
            TargetIntent(strategy_id="s", ts=TS, target_weight=weight)


def test_intent_rejects_short_and_leverage_by_construction():
    assert TargetIntent(strategy_id="s", ts=TS, target_weight=1.0).target_weight == 1.0
    assert TargetIntent(strategy_id="s", ts=TS, target_weight=0.0).target_weight == 0.0


def test_intent_scaled_by_regime_multiplier():
    intent = TargetIntent(strategy_id="s", ts=TS, target_weight=0.8, reason="breakout")
    scaled = intent.scaled(0.5)
    assert scaled.target_weight == pytest.approx(0.4)
    assert "regime" in scaled.reason and "breakout" in scaled.reason


def test_regime_state_exposure_bounded():
    assert RegimeState(ts=TS, exposure=0.3, label="calm").exposure == 0.3
    with pytest.raises(ValueError, match="exposure"):
        RegimeState(ts=TS, exposure=1.5)


# --- PortfolioView ----------------------------------------------------------------


def test_portfolio_weight():
    view = PortfolioView(
        ts=TS, cash_quote=Decimal("5000"), base_qty=Decimal("0.05"), equity_quote=Decimal("10000")
    )
    assert view.weight(Decimal("100000")) == pytest.approx(0.5)


def test_portfolio_weight_is_zero_on_empty_equity():
    view = PortfolioView(ts=TS, cash_quote=Decimal("0"), base_qty=Decimal("0"), equity_quote=Decimal("0"))
    assert view.weight(Decimal("100")) == 0.0


# --- orders -----------------------------------------------------------------------


def test_limit_order_requires_price_and_market_forbids_it():
    with pytest.raises(ValueError, match="LIMIT order requires"):
        OrderRequest(
            client_order_id="a",
            symbol="BTCUSDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            quantity=Decimal("1"),
            created_ts=TS,
        )
    with pytest.raises(ValueError, match="must not carry"):
        OrderRequest(
            client_order_id="a",
            symbol="BTCUSDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("1"),
            created_ts=TS,
            limit_price=Decimal("100"),
        )


def test_order_request_requires_client_order_id_and_positive_quantity():
    with pytest.raises(ValueError, match="client_order_id"):
        OrderRequest(
            client_order_id="",
            symbol="BTCUSDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("1"),
            created_ts=TS,
        )
    with pytest.raises(ValueError, match="quantity"):
        OrderRequest(
            client_order_id="a",
            symbol="BTCUSDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("0"),
            created_ts=TS,
        )


def test_order_status_remaining_and_overfill_guard():
    status = OrderStatus(
        client_order_id="a",
        symbol="BTCUSDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        state=OrderState.PARTIALLY_FILLED,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0.4"),
        ts=TS,
    )
    assert status.remaining == Decimal("0.6")
    assert status.state.is_open and not status.state.is_terminal
    with pytest.raises(ValueError, match="exceeds quantity"):
        OrderStatus(
            client_order_id="a",
            symbol="BTCUSDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            state=OrderState.FILLED,
            quantity=Decimal("1"),
            filled_quantity=Decimal("1.1"),
            ts=TS,
        )


def test_fill_notional_and_zero_fee_allowed():
    fill = Fill(
        client_order_id="a",
        symbol="BTCUSDT",
        side=Side.SELL,
        ts=TS,
        price=Decimal("100"),
        quantity=Decimal("2"),
        fee=Decimal("0"),
        fee_asset="USDT",
    )
    assert fill.notional == Decimal("200")


# --- risk decisions ---------------------------------------------------------------


def test_approved_may_carry_no_order():
    intent = TargetIntent(strategy_id="s", ts=TS, target_weight=0.5)
    decision = Approved(intent=intent, order=None, note="already at target")
    assert decision.order is None


def test_refused_carries_machine_readable_reason():
    intent = TargetIntent(strategy_id="s", ts=TS, target_weight=0.5)
    decision = Refused(intent=intent, reason=RefusalReason.HALTED, detail="manual halt")
    assert decision.reason is RefusalReason.HALTED
    assert decision.reason.value == "HALTED"


def test_trading_states_exist():
    assert {state.value for state in TradingState} == {"ACTIVE", "REDUCING", "HALTED"}
