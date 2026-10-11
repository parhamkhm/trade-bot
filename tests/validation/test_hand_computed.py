"""G2 items 1 and 4: a hand-computed 5-bar example, and the cost identity on a long real-data run.

The expected numbers below are literal and were worked out outside the engine with Decimal arithmetic from
the SPEC rules (see the derivation in the docstring of ``test_five_bar_example_to_the_cent``). They are not
read back from the engine.

Cost identity: whatever the strategy does, a run with fees and slippage ends exactly
``fees_paid + slippage_cost`` below the frictionless run that trades the same quantities at the raw opens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, BarWindow, TargetIntent, Timeframe
from tbot.strategies.sma_filter import SmaFilter
from tests.sim_helpers import make_bar
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars
from tests.validation.harness import BPS_BUFFER_LEGACY, make_cfg, run

D = Decimal
needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")


class Scripted:
    """Emits a fixed target weight per bar close, so the only moving part under test is the engine."""

    warmup_bars = 1

    def __init__(self, weights: list[float], bars: list[Bar]) -> None:
        self.id = "scripted"
        self._by_ts = {b.ts: w for b, w in zip(bars, weights, strict=True)}

    def on_bar(self, window: BarWindow) -> TargetIntent:
        return TargetIntent(self.id, window.as_of, self._by_ts[window.as_of], "scripted")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


def five_bars() -> list[Bar]:
    # (open, close) per daily bar; bar i opens at T_START + i days and closes one day later
    return [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "105"),
        make_bar(2, "106", "110"),
        make_bar(3, "108", "104"),
        make_bar(4, "103", "102"),
    ]


def test_five_bar_example_to_the_cent() -> None:
    """Long at the first close, flat at the third close; 10 bps fee, 5 bps slippage, 10 bps buy buffer.

    The 10 bps buffer is passed EXPLICITLY: it is the configuration this example was derived for (the engine's
    default buffer is 0 since 2026-10-11; the default is hand-computed in the two tests that follow).

    Derivation (initial cash 10 000, tick 0.01, step 0.00001):

    * close(0) = 100: target 1.0. cost_factor = (1 + 0.0005)(1 + 0.001)(1 + 0.001) = 1.0025020005, so the
      BUY budget is 10000 / 1.0025020005 = 9975.0424388..., quantity = floor(budget / 100, step) = 99.75042.
    * open(1) = 100: BUY fills at ceil(100 * 1.0005, tick) = 100.05. Notional 99.75042 * 100.05 =
      9980.029521,
      fee 0.1 % = 9.980029521, cash left = 10000 - 9980.029521 - 9.980029521 = 9.990449479.
    * close(1) = 105: equity = 9.990449479 + 99.75042 * 105 = 10483.784549479. Weight 0.99905: inside the
      0.02 drift tolerance, no trade. close(2) = 110: equity 10982.536649479, target 0.0 -> SELL everything.
    * open(3) = 108: SELL fills at floor(108 * 0.9995, tick) = 107.94. Notional 99.75042 * 107.94 =
      10767.0603348,
      fee 10.76706033480, final cash = 9.990449479 + 10767.0603348 - 10.7670603348 = 10766.2837239442.
    * fees = 9.980029521 + 10.7670603348 = 20.7470898558; slippage = 99.75042 * 0.05 + 99.75042 * 0.06
      = 10.9725462 (a rounded-up buy and a rounded-down sell, each measured against the raw open).
    * frictionless: 10000 - 99.75042 * 100 + 99.75042 * 108 = 10798.00336, and
      10798.00336 - 20.7470898558 - 10.9725462 = 10766.2837239442 exactly.
    """
    bars = five_bars()
    res = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg(buffer_bps=BPS_BUFFER_LEGACY))

    buy, sell = (r.fill for r in res.fills)
    assert (buy.side.value, buy.quantity, buy.price, buy.fee) == (
        "BUY",
        D("99.75042"),
        D("100.05"),
        D("9.980029521"),
    )
    assert buy.ts == bars[0].ts  # the open of bar 1 is the close of bar 0, never the signal bar's own close
    assert (sell.side.value, sell.quantity, sell.price) == ("SELL", D("99.75042"), D("107.94"))
    assert sell.fee == D("10.7670603348")
    assert sell.ts == bars[2].ts

    assert [p.equity for p in res.equity] == [
        D("10000"),
        D("10483.784549479"),
        D("10982.536649479"),
        D("10766.2837239442"),
        D("10766.2837239442"),
    ]
    assert [p.quantity for p in res.equity] == [D(0), D("99.75042"), D("99.75042"), D(0), D(0)]
    assert res.fees_paid == D("20.7470898558")
    assert res.slippage_cost == D("10.9725462")
    assert res.equity[-1].equity.quantize(D("0.01")) == D("10766.28")  # to the cent
    assert len(res.trades) == 1
    assert res.trades[0].pnl == D("10766.2837239442") - D(10000)
    assert not res.rejections and not res.resizes and not res.unfilled_order_ids


def test_five_bar_example_without_costs_is_the_plain_arithmetic() -> None:
    """Zero fee and zero slippage: buy at the raw open, sell at the raw open, only step flooring remains."""
    bars = five_bars()
    cfg = make_cfg(fee_bps=D(0), slip_bps=D(0), buffer_bps=D(0))
    res = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, cfg)
    assert [r.fill.price for r in res.fills] == [D("100"), D("108")]
    assert res.fills[0].fill.quantity == D("100")  # 10000 / 100 exactly
    assert res.equity[-1].equity == D("10800")  # 100 * (108 - 100) + 10000
    assert res.fees_paid == 0 and res.slippage_cost == 0


def test_five_bar_example_with_the_default_zero_buffer() -> None:
    """Default engine config (buffer 0): the same example, derived by hand.

    * close(0) = 100: cost_factor = (1 + 0.0005)(1 + 0.001) = 1.0015005; budget 10000 / 1.0015005 =
      9985.0194...; quantity = floor(budget / 100, step) = 99.85017.
    * open(1) = 100: BUY fills at 100.05 (no gap). The fill-time cap floor(10000 / (100.05 * 1.001), step) is
      the same 99.85017, so nothing is capped. Notional 9990.0095085, fee 9.9900095085, cash left
      0.0004819915.
    """
    bars = five_bars()
    res = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg())
    buy = res.fills[0].fill
    assert (buy.quantity, buy.price, buy.fee) == (D("99.85017"), D("100.05"), D("9.9900095085"))
    assert res.equity[1].cash == D("0.0004819915")
    assert not res.rejections and not res.resizes


def test_gap_up_buy_is_capped_at_fill_time_not_rejected() -> None:
    """The new sizing rule on a hand-made gap: open(1) = 100.5 is 0.5 % above the close it was sized at.

    * Sized at close(0) = 100 exactly as above: requested 99.85017.
    * BUY price at open(1) = ceil(100.5 * 1.0005, tick) = 100.56. At that price the requested order costs
      99.85017 * 100.56 * 1.001 = 10050.97 > 10000 cash, which the old engine rejected (INSUFFICIENT_FUNDS).
    * Cap: floor(10000 / (100.56 * 1.001), step) = floor(10000 / 100.66056) = 99.34377 (-0.5072 % of the
      request, below the 1 % anomaly threshold: kind "price_move"). Notional 9990.0095112, fee
      9.9900095112, cash left 0.0004792888.
    """
    bars = five_bars()
    bars[1] = make_bar(1, "100.5", "105")
    res = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg())
    assert not res.rejections
    (resize,) = res.resizes
    assert (resize.requested_quantity, resize.capped_quantity, resize.kind) == (
        D("99.85017"),
        D("99.34377"),
        "price_move",
    )
    buy = res.fills[0].fill
    assert (buy.quantity, buy.price, buy.fee) == (D("99.34377"), D("100.56"), D("9.9900095112"))
    assert res.equity[1].cash == D("0.0004792888")


@pytest.mark.parametrize(("fee_bps", "slip_bps"), [(0, 0), (10, 0), (0, 5), (10, 5), (20, 10)])
def test_costs_reduce_the_result_by_exactly_fees_plus_slippage(fee_bps: int, slip_bps: int) -> None:
    bars = five_bars()
    res = run(
        Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg(fee_bps=D(fee_bps), slip_bps=D(slip_bps))
    )
    assert _frictionless_equity(res, bars) - res.fees_paid - res.slippage_cost == res.equity[-1].equity
    if fee_bps == 0:
        assert res.fees_paid == 0
    if slip_bps == 0:
        assert res.slippage_cost == 0  # the open is already on the tick grid in this example


def test_the_x2_stress_costs_more_and_ends_lower() -> None:
    bars = five_bars()
    base = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg())
    stress = run(Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg(fee_bps=D(20), slip_bps=D(10)))
    assert stress.fees_paid > base.fees_paid
    assert stress.slippage_cost > base.slippage_cost
    assert stress.equity[-1].equity < base.equity[-1].equity


# ---------------------------------------------------------------------------------------------
# Cost identity on long runs
# ---------------------------------------------------------------------------------------------


def _frictionless_equity(res: BacktestResult, bars: list[Bar]) -> Decimal:
    """Same traded quantities at the raw opens, no fee, no slippage, marked at the final close."""
    open_at_close_of = {bars[i - 1].ts: bars[i].open for i in range(1, len(bars))}  # fill ts -> raw open
    cash, qty = res.config.initial_cash, Decimal(0)
    for rec in res.fills:
        f = rec.fill
        raw = open_at_close_of[f.ts]
        assert rec.reference_price == raw, "the engine must price the fill from the next bar's open"
        sign = 1 if f.side.value == "BUY" else -1
        cash -= sign * f.quantity * raw
        qty += sign * f.quantity
    return cash + qty * bars[-1].close


@needs_store
def test_cost_identity_and_ledger_on_real_btc_daily_sma100() -> None:
    bars = real_bars(
        "BTCUSDT", Timeframe.D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
    )
    _assert_cost_identity(run(SmaFilter(100), bars, make_cfg()), bars)


def test_cost_identity_and_ledger_on_synthetic_sma20() -> None:
    bars = synthetic_bars(800, seed=13, gap_vol=0.0002)
    res = run(SmaFilter(20), bars, make_cfg())
    assert len(res.fills) > 20
    _assert_cost_identity(res, bars)


def _assert_cost_identity(res: BacktestResult, bars: list[Bar]) -> None:
    fee_rate, slip_rate, tick = D("0.001"), D("0.0005"), D("0.01")
    assert res.fees_paid == sum((r.fill.fee for r in res.fills), D(0))
    assert res.slippage_cost == sum((r.slippage_cost for r in res.fills), D(0))
    for rec in res.fills:
        f = rec.fill
        assert f.fee == f.price * f.quantity * fee_rate, "fee is exactly 10 bps of the slipped notional"
        assert rec.slippage_cost == abs(f.price - rec.reference_price) * f.quantity
        if f.side.value == "BUY":
            assert rec.reference_price < f.price <= rec.reference_price * (1 + slip_rate) + tick
        else:
            assert rec.reference_price > f.price >= rec.reference_price * (1 - slip_rate) - tick
    identity = _frictionless_equity(res, bars) - res.fees_paid - res.slippage_cost
    assert identity == res.equity[-1].equity, f"cost identity off by {identity - res.equity[-1].equity}"


@needs_store
def test_x2_cost_stress_on_real_data_roughly_doubles_the_drag() -> None:
    bars = real_bars(
        "BTCUSDT", Timeframe.D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
    )
    base = run(SmaFilter(100), bars, make_cfg())
    stress = run(SmaFilter(100), bars, make_cfg(fee_bps=D(20), slip_bps=D(10)))
    # Absolute cost is not exactly 2x: the stressed run compounds to a smaller equity and so trades less
    # notional. Per unit of traded notional the cost rate is what doubles (10+5 -> 20+10 bps, plus ticks).
    base_rate = (base.fees_paid + base.slippage_cost) / base.traded_notional
    stress_rate = (stress.fees_paid + stress.slippage_cost) / stress.traded_notional
    assert D("1.95") < stress_rate / base_rate < D("2.05"), f"rate ratio {stress_rate / base_rate}"
    assert stress.equity[-1].equity < base.equity[-1].equity
