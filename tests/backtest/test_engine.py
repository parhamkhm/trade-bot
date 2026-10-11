"""Engine tests: hand-computed 5-bar example, causality, accounting, determinism, delay model, guards."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import EngineError, _fast_window, run_backtest
from tbot.backtest.feed import BacktestFeed, FeedError
from tbot.backtest.report import build_report, is_run_invalid
from tbot.backtest.result import BacktestResult
from tbot.backtest.risk_stub import StubRiskManager
from tbot.core.types import Bar, BarWindow, RegimeState, Side, TargetIntent, Timeframe
from tbot.data.binance_loader import CANONICAL_HOLDOUT_START, HoldoutLockError
from tbot.strategies.base import StrategyRegistrationError
from tests.backtest.helpers import CountingFeed, OversizedRisk, Recorder, Scripted, make_config
from tests.sim_helpers import (
    SYMBOL,
    T_START,
    bars_from_closes,
    hour_bars_for_day,
    lbank_like_filters,
    make_bar,
)

Dec = Decimal


def five_bars() -> list[Bar]:
    return [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "120"),
        make_bar(2, "120", "90"),
        make_bar(3, "90", "95"),
        make_bar(4, "95", "100"),
    ]


# ---------------------------------------------------------------------------------------------
# the hand-computed 5-bar example (fee 0.5 %, slippage 1 %, start cash 10 000, tolerance 2 %)
# ---------------------------------------------------------------------------------------------
#
# bar  open close  decision at close
#  0   100   100   target 0.50 -> BUY  50.00000 (5000 / 100)
#  1   100   120   BUY fills at 100 * 1.01 = 101.00; notional 5050.00; fee 25.25
#                  cash 4924.75, qty 50, equity 4924.75 + 50*120 = 10924.75
#                  target 0.55 vs held 6000/10924.75 = 0.5492 -> |drift| < 0.02 -> no order
#  2   120    90   equity 9424.75; target 0 -> SELL 50
#  3    90    95   SELL fills at 90 * 0.99 = 89.10; notional 4455; fee 22.275; cash 9357.475, flat
#                  target 0.40 -> BUY floor(9357.475*0.4 / 95, 5dp) = 39.39989
#  4    95   100   BUY fills at 95 * 1.01 = 95.95; notional 3780.4194455; fee 18.9020972275
#                  cash 5558.1534572725; equity = 5558.1534572725 + 39.39989*100 = 9498.1424572725


def test_hand_computed_five_bar_example() -> None:
    cfg = make_config()
    res = run_backtest(BacktestFeed(five_bars()), Scripted([0.5, 0.55, 0.0, 0.4, 0.4]), cfg)
    fills = [r.fill for r in res.fills]
    assert [(f.side, f.ts, f.price, f.quantity, f.fee) for f in fills] == [
        (Side.BUY, T_START + timedelta(days=1), Dec("101.00"), Dec("50.00000"), Dec("25.25")),
        (Side.SELL, T_START + timedelta(days=3), Dec("89.10"), Dec("50.00000"), Dec("22.275")),
        (Side.BUY, T_START + timedelta(days=4), Dec("95.95"), Dec("39.39989"), Dec("18.9020972275")),
    ]
    assert [p.equity for p in res.equity] == [
        Dec("10000"),
        Dec("10924.75"),
        Dec("9424.75"),
        Dec("9357.475"),
        Dec("9498.1424572725"),
    ]
    assert [p.cash for p in res.equity] == [
        Dec("10000"),
        Dec("4924.75"),
        Dec("4924.75"),
        Dec("9357.475"),
        Dec("5558.1534572725"),
    ]
    assert res.positions() == [Dec(0), Dec(50), Dec(50), Dec(0), Dec("39.39989")]
    assert res.fees_paid == Dec("66.4270972275")
    assert res.slippage_cost == Dec("132.4298955")  # 50*1.00 + 50*0.90 + 39.39989*0.95
    assert res.traded_notional == Dec("5050") + Dec("4455") + Dec("3780.4194455")
    (trade,) = res.trades  # one closed round trip, the second is still open
    assert trade.pnl == Dec("4455") - Dec("5050") - Dec("25.25") - Dec("22.275") == Dec("-642.525")
    assert [d.outcome for d in res.decisions] == ["ORDER", "NO_ACTION", "ORDER", "ORDER", "NO_ACTION"]
    assert res.first_decision_index == 0 and res.unfilled_order_ids == ()
    assert res.rejections == ()
    assert res.resizes == ()  # next-open mode, correctly-sized fills: the fill-time cap never triggers


# ---------------------------------------------------------------------------------------------
# custom DataFeed bypassing its own checks -- the engine must still catch it (engine.py:230-235)
# ---------------------------------------------------------------------------------------------


class _RawFeed:
    """A minimal ``DataFeed`` that does none of ``BacktestFeed``'s own validation."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def bars(self, symbol: str, timeframe: Timeframe) -> Iterator[Bar]:  # noqa: ARG002
        return iter(self._bars)


def test_engine_rejects_a_custom_feed_that_goes_backwards() -> None:
    bars = bars_from_closes([100, 101, 102])
    out_of_order = [bars[0], bars[2], bars[1]]
    with pytest.raises(EngineError, match="backwards"):
        run_backtest(_RawFeed(out_of_order), Scripted([0.0]), make_config())


def test_engine_rejects_a_custom_feed_that_yields_a_foreign_symbol() -> None:
    bars = bars_from_closes([100, 101, 102])
    foreign = list(bars)
    foreign[1] = Bar(
        symbol="ETHUSDT",
        timeframe=foreign[1].timeframe,
        ts=foreign[1].ts,
        open=foreign[1].open,
        high=foreign[1].high,
        low=foreign[1].low,
        close=foreign[1].close,
        volume=foreign[1].volume,
    )
    with pytest.raises(EngineError, match="ETHUSDT"):
        run_backtest(_RawFeed(foreign), Scripted([0.0]), make_config())


# ---------------------------------------------------------------------------------------------
# causality
# ---------------------------------------------------------------------------------------------


def test_signal_at_t_fills_at_open_of_t_plus_1_never_at_signal_close() -> None:
    bars = [make_bar(i, o, c) for i, (o, c) in enumerate([("100", "100"), ("200", "210"), ("210", "210")])]
    res = run_backtest(BacktestFeed(bars), Scripted([0.5, 0.5, 0.5]), make_config(fee="0", slip="0"))
    rec = res.fills[0]  # later fills are rebalances after the price jump
    assert rec.fill.ts == bars[0].ts  # = open_ts of bar 1
    assert rec.fill.price == Dec("200")  # the OPEN of bar 1; the signal close was 100
    assert res.fills[0].fill.ts == bars[1].open_ts


def test_no_future_data_is_pulled_from_the_feed() -> None:
    bars = bars_from_closes([100, 101, 102, 103, 104, 105])
    feed = CountingFeed(bars)
    seen: list[tuple[int, int]] = []
    strat = Recorder(lambda _w: 0.0, on_call=lambda w: seen.append((feed.consumed, len(w))))
    run_backtest(feed, strat, make_config())
    assert seen == [(i, i) for i in range(1, 7)]  # at on_bar(t) exactly t+1 bars were drawn
    for k, w in enumerate(strat.windows):
        assert w.as_of == bars[k].ts and w.bars == tuple(bars[: k + 1])


def test_fast_window_is_equivalent_to_validated_window() -> None:
    bars = bars_from_closes([1, 2, 3, 4])
    assert _fast_window(SYMBOL, tuple(bars), bars[-1]) == BarWindow.of(bars)


def _decisions_prefix(res: BacktestResult, n: int) -> list[float]:
    return [d.target_weight for d in res.decisions[:n]]


def test_peeking_strategy_is_caught_by_a_truncation_comparison() -> None:
    closes = [100.0, 103, 99, 104, 98, 107, 101, 109, 115, 111, 104, 112]
    full_bars = bars_from_closes(closes)

    def honest(data: list[Bar]) -> Recorder:
        return Recorder(lambda w: 1.0 if len(w) > 1 and w.last.close > w[-2].close else 0.0)

    def peeker(data: list[Bar]) -> Recorder:
        def fn(w: BarWindow) -> float:  # cheats: reads bar t+1 from the data it was built with
            t = len(w) - 1
            nxt = data[min(t + 1, len(data) - 1)].close
            return 1.0 if nxt > data[t].close else 0.0

        return Recorder(fn)

    n = 8
    cfg = make_config()
    for make, expect_equal in ((honest, True), (peeker, False)):
        full = run_backtest(BacktestFeed(full_bars), make(full_bars), cfg)
        cut = run_backtest(BacktestFeed(full_bars[:n]), make(full_bars[:n]), cfg)
        same_targets = _decisions_prefix(full, n) == _decisions_prefix(cut, n)
        assert same_targets is expect_equal, make.__name__


def test_intent_with_wrong_timestamp_is_rejected() -> None:
    class Liar(Scripted):
        def on_bar(self, window: BarWindow) -> TargetIntent:
            intent = super().on_bar(window)
            return TargetIntent(intent.strategy_id, intent.ts + timedelta(days=1), intent.target_weight, "")

    with pytest.raises(EngineError, match="stamped"):
        run_backtest(BacktestFeed(five_bars()), Liar([1.0]), make_config())


# ---------------------------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------------------------


def test_accounting_identity_every_bar_and_ledger_replay() -> None:
    closes = [100, 104, 99, 101, 108, 95, 97, 110, 120, 90, 85, 99, 130, 120]
    bars = bars_from_closes(closes)
    weights = [0.0, 0.6, 0.6, 0.3, 0.9, 0.0, 0.0, 0.5, 0.5, 0.0, 0.7, 0.7, 0.2, 0.0]
    res = run_backtest(BacktestFeed(bars), Scripted(weights), make_config())
    for p, b in zip(res.equity, bars, strict=True):
        assert p.mark_price == b.close
        assert p.equity == p.cash + p.quantity * b.close
        assert p.cash >= 0 and p.quantity >= 0
    assert len(res.fills) >= 6
    ledger_fees = sum((r.fill.fee for r in res.fills), Dec(0))
    assert res.fees_paid == ledger_fees > 0


def test_costs_reduce_equity_by_the_cost_amounts() -> None:
    """Round trip, zero price move: the whole equity loss is fees + slippage, to the last digit."""
    bars = [make_bar(i, "100", "100") for i in range(4)]
    cfg = make_config(fee="35", slip="5")
    res = run_backtest(BacktestFeed(bars), Scripted([0.5, 0.0, 0.0, 0.0]), cfg)
    assert len(res.fills) == 2
    final = res.equity[-1]
    assert final.quantity == 0
    assert res.initial_cash - final.equity == res.fees_paid + res.slippage_cost
    assert res.slippage_cost > 0 and res.fees_paid > 0
    assert sum((t.pnl for t in res.trades), Dec(0)) == final.equity - res.initial_cash


def test_zero_costs_zero_drag() -> None:
    bars = [make_bar(i, "100", "100") for i in range(4)]
    res = run_backtest(BacktestFeed(bars), Scripted([0.5, 0.0, 0.0, 0.0]), make_config(fee="0", slip="0"))
    assert res.equity[-1].equity == res.initial_cash
    assert res.fees_paid == 0 and res.slippage_cost == 0


def test_determinism() -> None:
    bars = bars_from_closes([100, 104, 99, 101, 108, 95, 97, 110, 120, 90])
    weights = [0.0, 0.6, 0.6, 0.3, 0.9, 0.0, 0.0, 0.5, 0.5, 0.0]
    a = run_backtest(BacktestFeed(bars), Scripted(weights), make_config())
    b = run_backtest(BacktestFeed(bars), Scripted(weights), make_config())
    assert a == b


# ---------------------------------------------------------------------------------------------
# exchange filters, warm-up, regime, pending orders
# ---------------------------------------------------------------------------------------------


def test_quantities_follow_step_size_and_min_notional_refusals_are_logged() -> None:
    bars = [make_bar(i, "30000", "30000") for i in range(3)]
    tiny = make_config(initial_cash=Dec("0.5"))  # below the 1 USDT minimum notional
    res = run_backtest(BacktestFeed(bars), Scripted([1.0]), tiny)
    assert res.fills == ()
    assert all(d.outcome == "REFUSED" and "MIN" in d.detail for d in res.decisions)
    ok = run_backtest(BacktestFeed(bars), Scripted([0.3333]), make_config(initial_cash=Dec("1000")))
    (rec,) = ok.fills
    assert rec.fill.quantity % Dec("0.00001") == 0 and rec.fill.price % Dec("0.01") == 0


def test_warmup_defers_first_decision() -> None:
    strat = Recorder(lambda _w: 0.0)
    strat.warmup_bars = 3
    res = run_backtest(BacktestFeed(bars_from_closes([1, 2, 3, 4, 5])), strat, make_config())
    assert res.first_decision_index == 2 and len(res.decisions) == 3
    assert [len(w) for w in strat.windows] == [3, 4, 5]


def test_regime_exposure_scales_the_intent() -> None:
    class Half:
        warmup_bars = 1

        def update(self, window: BarWindow) -> RegimeState:
            return RegimeState(window.as_of, 0.5, "half")

    res = run_backtest(
        BacktestFeed(five_bars()), Scripted([1.0]), make_config(fee="0", slip="0"), regime=Half()
    )
    assert res.decisions[0].target_weight == 0.5
    assert 0.45 < res.equity[1].weight < 0.55


def test_unfilled_order_at_end_of_data_is_reported() -> None:
    res = run_backtest(BacktestFeed(five_bars()), Scripted([0.0, 0.0, 0.0, 0.0, 0.5]), make_config())
    assert res.fills == () and len(res.unfilled_order_ids) == 1
    assert res.decisions[-1].outcome == "ORDER"


def test_strategy_without_stop_price_cannot_run() -> None:
    class NoStop:
        id = "x"
        warmup_bars = 1

        def on_bar(self, window: BarWindow) -> TargetIntent:  # noqa: ARG002
            raise AssertionError("must not be called")

    with pytest.raises(StrategyRegistrationError):
        run_backtest(BacktestFeed(five_bars()), NoStop(), make_config())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# manual-delay fill model (D-051)
# ---------------------------------------------------------------------------------------------


def delay_setup() -> tuple[list[Bar], list[Bar]]:
    d0, d1 = make_bar(0, "100", "124"), make_bar(1, "124", "150")
    opens = [str(100 + h) for h in range(48)]
    hours = hour_bars_for_day(d0, opens[:24]) + hour_bars_for_day(d1, opens[24:])
    return [d0, d1], hours


def test_delay_model_fills_at_first_hourly_open_six_hours_after_the_signal_close() -> None:
    days, hours = delay_setup()
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(BacktestFeed(days), Scripted([0.5, 0.5]), cfg, exec_feed=BacktestFeed(hours))
    (rec,) = res.fills
    signal_close = T_START + timedelta(days=1)
    assert rec.fill.ts == signal_close + timedelta(hours=6)  # exactly close + 6 h on contiguous 1h data
    assert rec.gap_fill is False
    assert rec.reference_price == Dec("130")  # open of the 06:00 hourly bar
    assert rec.fill.price == Dec("131.30")  # adverse +1 %
    # sized at the signal close (124): 5000 / 124 = 40.32258 (floored to the step size)
    assert rec.fill.quantity == Dec("40.32258")
    assert res.equity[0].quantity == 0  # not filled by the first close
    assert res.equity[1].quantity == Dec("40.32258")


def test_delay_fill_lands_on_the_next_available_bar_when_the_deadline_bars_are_missing() -> None:
    """GAP case of Parham's review item (a): the 1h bars opening at created_ts + 6 h and + 7 h are missing, so
    the order fills on the FIRST bar with open_ts >= created_ts + 6 h, which is created_ts + 8 h: not later
    (not at the next signal bar) and not earlier. The fill is flagged gap_fill and the report counts it."""
    days, hours = delay_setup()
    created = T_START + timedelta(days=1)  # close of the first signal bar, when the order is created
    missing = (created + timedelta(hours=6), created + timedelta(hours=7))
    gapped = [b for b in hours if b.open_ts not in missing]
    assert len(gapped) == len(hours) - 2
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(BacktestFeed(days), Scripted([0.5, 0.5]), cfg, exec_feed=BacktestFeed(gapped))
    (rec,) = res.fills
    assert rec.fill.ts == created + timedelta(hours=8)
    assert rec.gap_fill is True
    assert rec.reference_price == Dec("132")  # open of the 08:00 hourly bar (opens are 124 + hour index)
    assert rec.fill.price == Dec("133.32")  # adverse +1 %
    assert rec.fill.quantity == Dec("40.32258")  # still sized at the signal close (124)
    assert res.rejections == ()
    # the only order left open is the rebalance created at the LAST close; the gapped order itself is done
    assert rec.fill.client_order_id == "bt-000001" and res.unfilled_order_ids == ("bt-000002",)
    assert res.equity[0].quantity == 0 and res.equity[1].quantity == Dec("40.32258")
    report = build_report(res)
    assert report["health"]["gap_fills"] == 1
    assert not is_run_invalid(report)
    # the contiguous run on the same data is NOT a gap fill (the counter is not always 1)
    clean = run_backtest(BacktestFeed(days), Scripted([0.5, 0.5]), cfg, exec_feed=BacktestFeed(hours))
    assert build_report(clean)["health"]["gap_fills"] == 0


def test_delay_order_stays_pending_when_no_exec_bar_exists_before_the_next_signal_bar() -> None:
    """Even more extreme gap: not one 1h bar at or after created_ts + 6 h exists before the next signal bar.
    The order must stay pending (no fill at the wrong time, nothing crashes), and the pending-order rule
    keeps the next signal from stacking a second order."""
    days, hours = delay_setup()
    created = T_START + timedelta(days=1)
    before_deadline = [b for b in hours if b.open_ts < created + timedelta(hours=6)]
    assert any(b.open_ts >= created for b in before_deadline)  # day 1's first six hours exist, then nothing
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(BacktestFeed(days), Scripted([0.5, 0.9]), cfg, exec_feed=BacktestFeed(before_deadline))
    assert res.fills == () and res.rejections == ()
    assert [d.outcome for d in res.decisions] == ["ORDER", "SKIPPED_PENDING_ORDER"]
    assert len(res.unfilled_order_ids) == 1
    assert res.equity[-1].quantity == 0
    assert build_report(res)["health"]["gap_fills"] == 0


def test_delay_pending_order_fills_on_the_first_bar_after_a_whole_missing_day_of_exec_data() -> None:
    """Three signal days; day 1 has no 1h data at all, day 2 has all of it. The order created at the close of
    day 0 stays pending through the day-1 signal (SKIPPED_PENDING_ORDER) and fills at the FIRST bar that
    exists, the 00:00 bar of day 2 (a gap fill), before the day-2 decision is taken."""
    d0, d1, d2 = make_bar(0, "100", "124"), make_bar(1, "124", "150"), make_bar(2, "150", "150")
    hours = hour_bars_for_day(d2, [str(150 + h) for h in range(24)])  # opens at d1.ts + h
    assert hours[0].open_ts == d1.ts
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(
        BacktestFeed([d0, d1, d2]), Scripted([0.5, 0.5, 0.5]), cfg, exec_feed=BacktestFeed(hours)
    )
    (rec,) = res.fills
    assert rec.fill.ts == d1.ts  # created_ts + 24 h, the first existing bar at or after created_ts + 6 h
    assert rec.gap_fill is True and rec.reference_price == Dec("150")
    assert res.decisions[1].outcome == "SKIPPED_PENDING_ORDER"
    assert res.equity[1].quantity == 0 and res.equity[2].quantity == rec.fill.quantity
    assert build_report(res)["health"]["gap_fills"] == 1


def test_delay_mode_buy_resizes_and_fills_in_a_rising_market() -> None:
    """BLOCKER from the Phase 2a review: the stub sizes the quantity at close(t); if the price keeps
    rising during the 6h manual delay, the fill-time price is higher than what was sized for. The OLD
    broker rejected the BUY outright every time (INSUFFICIENT_FUNDS) -- weight 1.0 in a rising market never
    entered. The FIX resizes the BUY down to what cash still affords at the fill price instead of
    rejecting it. (Since 2026-10-11 next-open mode does the same, see the gap-up tests below.)"""
    day0 = make_bar(0, "100", "100")
    day1 = make_bar(0, "100", "104.8", start=day0.ts)  # opens exactly where day0 closes, no gap
    hours = hour_bars_for_day(day1, [str(Decimal("100") + Decimal("0.2") * h) for h in range(24)])
    cfg = make_config(
        fee="50", slip="100", buffer="0", fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1
    )
    res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=BacktestFeed(hours))
    assert len(res.fills) == 1  # must fill -- the old code produced zero fills and an INSUFFICIENT_FUNDS
    fill = res.fills[0].fill
    assert fill.ts == day0.ts + timedelta(hours=6)
    assert fill.quantity == Dec("97.34150")  # capped by cash at the fill price, not the sized 98.51731
    assert res.rejections == ()
    (resize,) = res.resizes  # the resize is logged, with the requested and the actually filled quantity
    assert resize.requested_quantity == Dec("98.51731")
    assert resize.capped_quantity == Dec("97.34150")
    # 0.2 %/h for 6 h plus 1 % slippage is a 1.19 % cap. By the cause-based rule that is a price_move: the BUY
    # was affordable at the close it was sized at (100), the price then rose, and the run stays valid.
    assert resize.kind == "price_move" and resize.reason.startswith("price_move")
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")
    assert res.equity[1].quantity == Dec("97.34150") and res.equity[1].quantity > 0
    report = build_report(res)
    assert report["health"]["resizes_anomaly"] == 0 and report["health"]["resizes_price_move"] == 1
    assert not is_run_invalid(report) and report["health"]["fill_rejections"] == 0


def test_delay_mode_realistic_drift_is_a_price_move_resize_and_not_invalid() -> None:
    """The same rising market at a realistic 0.02 %/h (opens 100.00 ... 100.46): over the 6 h delay the
    price drifts 0.12 %, the BUY is capped by 0.13 % of the requested quantity: a price_move."""
    day0 = make_bar(0, "100", "100")
    day1 = make_bar(0, "100", "100.48", start=day0.ts)
    hours = hour_bars_for_day(day1, [str(Decimal("100") + Decimal("0.02") * h) for h in range(24)])
    cfg = make_config(fee="35", slip="5", fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=BacktestFeed(hours))
    assert res.rejections == ()
    (resize,) = res.resizes
    # open 100.12 -> *1.0005 = 100.17006 -> ceil tick 100.18; unit cost 100.18 * 1.0035 = 100.53063
    assert resize.requested_quantity == Dec("99.60142")
    assert resize.capped_quantity == Dec("99.47217")  # floor_5(10000 / 100.53063)
    assert resize.kind == "price_move" and resize.reason.startswith("price_move")
    (rec,) = res.fills
    assert rec.fill.price == Dec("100.18") and rec.fill.fee == Dec("34.8779269671")
    report = build_report(res)
    assert report["health"]["resizes"] == 1 and report["health"]["resizes_price_move"] == 1
    assert report["health"]["resizes_anomaly"] == 0
    assert not is_run_invalid(report)


def gap_up_run(gap: str, delay: timedelta) -> tuple[BacktestResult, Decimal]:
    """Sized at close(0) = 100 with fee 35 bps, slippage 5 bps and NO extra buffer; the fill price is then
    100 * (1 + gap): at the next open (``delay == 0``) or at the open of the 06:00 hourly bar (6 h delay),
    where the price has jumped from 100 to the gapped level. Returns the result and the fill-time open."""
    fill_open = Dec("100") * (1 + Dec(gap) / 100)
    day0 = make_bar(0, "100", "100")
    if delay == timedelta(0):
        day1 = make_bar(0, str(fill_open), str(fill_open), start=day0.ts)
        res0 = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), make_config(fee="35", slip="5"))
        return res0, fill_open
    day1 = make_bar(0, "100", str(fill_open), start=day0.ts)
    opens = ["100"] * 6 + [str(fill_open)] * 18
    cfg = make_config(fee="35", slip="5", fill_delay=delay, exec_timeframe=Timeframe.H1)
    exec_feed = BacktestFeed(hour_bars_for_day(day1, opens))
    res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=exec_feed)
    return res, fill_open


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
@pytest.mark.parametrize(
    ("gap", "price", "quantity", "fee", "cash"),
    [
        # +0.43 %: the largest real open gap in the data. open 100.43 -> 100.480215 -> ceil tick 100.49;
        # unit cost 100.49 * 1.0035 = 100.841715; floor_5(10000 / 100.841715) = 99.16531
        ("0.43", "100.49", "99.16531", "34.87792700665", "0.00007109335"),
        # +5 %: open 105 -> 105.0525 -> 105.06; unit cost 105.06 * 1.0035 = 105.42771; 10000/that -> 94.85172.
        # Under the old fixed threshold this was an "anomaly"; by cause it is a price_move.
        ("5", "105.06", "94.85172", "34.8779259612", "0.0003708388"),
        # +2.5 % and +3 %: the size of the large real 6 h moves (BTC dropped/jumped 2-3 % inside a 6 h delay)
        ("2.5", "102.56", "97.16382", "34.8779248272", "0.0006959728"),
        ("3", "103.06", "96.69243", "34.8779264253", "0.0002377747"),
    ],
)
def test_gap_up_open_is_capped_not_rejected_in_both_fill_modes(
    delay: timedelta, gap: str, price: str, quantity: str, fee: str, cash: str
) -> None:
    """(a) D-051 as revised 2026-10-11: a BUY is sized at fill time from the real fill price and the cash on
    hand in BOTH modes. A gap-up between sizing (close 100, requested 99.60142) and the fill never produces
    INSUFFICIENT_FUNDS; the cap is logged as a ResizeRecord, the position is filled, fee and cash are exact.
    Both the +0.43 % gap (0.44 % of the quantity) and the +5 % gap (4.8 %) are price_move resizes: the
    order was affordable at the sizing close (100), only the price moved. No anomaly, the run stays valid."""
    res, _ = gap_up_run(gap, delay)
    assert res.rejections == ()
    (resize,) = res.resizes
    assert resize.requested_quantity == Dec("99.60142")  # floor_5(10000 / (1.0005 * 1.0035) / 100)
    assert resize.capped_quantity == Dec(quantity)
    (rec,) = res.fills
    assert rec.fill.price == Dec(price) and rec.fill.quantity == Dec(quantity)
    assert rec.fill.fee == Dec(fee)
    assert rec.fill.price * rec.fill.quantity * Dec("35") / Dec(10000) == rec.fill.fee
    assert res.equity[1].quantity == Dec(quantity)
    assert res.equity[1].cash == Dec("10000") - rec.fill.price * rec.fill.quantity - rec.fill.fee == Dec(cash)
    assert res.fees_paid == Dec(fee)
    assert resize.kind == "price_move" and resize.reason.startswith("price_move")
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")
    health = build_report(res)["health"]
    assert health["resizes"] == 1 and health["fill_rejections"] == 0
    assert health["resizes_price_move"] == 1 and health["resizes_anomaly"] == 0
    assert not is_run_invalid(build_report(res))


def oversized_run(delay: timedelta, factor: str = "2", fill_open: str = "100") -> BacktestResult:
    """A sizing BUG: a risk manager that asks for ``factor`` x what cash covers (sized at close 100, cash
    10 000), in next-open or 6 h delay mode. By default the prices do not move at all (open == close == 100);
    ``fill_open`` is the price level at the fill (next-open fill, or from the 06:00 hourly bar on)."""
    day0 = make_bar(0, "100", "100")
    exec_feed = None
    if delay == timedelta(0):
        day1 = make_bar(0, fill_open, fill_open, start=day0.ts)
        cfg = make_config(fee="35", slip="5")
    else:
        day1 = make_bar(0, "100", fill_open, start=day0.ts)
        cfg = make_config(fee="35", slip="5", fill_delay=delay, exec_timeframe=Timeframe.H1)
        exec_feed = BacktestFeed(hour_bars_for_day(day1, ["100"] * 6 + [fill_open] * 18))
    return run_backtest(
        BacktestFeed([day0, day1]),
        Scripted([1.0, 1.0]),
        cfg,
        risk=OversizedRisk(cfg, Dec(factor)),
        exec_feed=exec_feed,
    )


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_unaffordable_request_at_the_sizing_price_is_an_anomaly_and_invalidates_the_run(
    delay: timedelta,
) -> None:
    """Sizing-bug cause: the requested 199.20284 BTC (2 x 99.60142) costs 19 990 USDT at the sizing close of
    100, twice the 10 000 of cash. The broker still caps and fills it (no INSUFFICIENT_FUNDS rejection) but
    the resize is an anomaly and the run is INVALID, with no price movement involved at all."""
    res = oversized_run(delay)
    assert res.rejections == ()
    (resize,) = res.resizes
    assert resize.requested_quantity == Dec("199.20284") and resize.kind == "anomaly"
    assert "not affordable even at the sizing price" in resize.reason
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")
    assert resize.capped_quantity == Dec("99.60142")  # 100 * 1.0005 -> 100.05; 10000 / (100.05 * 1.0035)
    (rec,) = res.fills
    assert rec.fill.quantity == resize.capped_quantity
    report = build_report(res)
    assert report["health"]["resizes_anomaly"] == 1 and report["health"]["resizes_price_move"] == 0
    assert report["health"]["fill_rejections"] == 0 and is_run_invalid(report)


def test_buy_buffer_defaults_to_zero() -> None:
    """The stub's sizing divisor already includes fee and slippage and the broker caps at fill time, so the
    default extra buffer is 0 (it was 10 bps before the 2026-10-11 decision)."""
    cfg = BacktestConfig(
        symbol=SYMBOL,
        timeframe=Timeframe.D1,
        filters=lbank_like_filters(),
        costs=make_config().costs,
    )
    assert cfg.buy_buffer_bps == 0
    stub = StubRiskManager(taker_fee_bps=Dec("35"), slippage_bps=Dec("5"))
    assert stub._cost_factor == Dec("1.0005") * Dec("1.0035")


@pytest.mark.parametrize("exec_tf", [Timeframe.H4, Timeframe.D1])
def test_delay_mode_refuses_coarser_than_hourly_exec_bars(exec_tf: Timeframe) -> None:
    """Item 1: with 4h/1d execution bars the 6 h delay would silently stretch (fill at the first bar OPENING
    at or after close + 6 h = up to 24 h later). The config refuses it instead of accepting it."""
    with pytest.raises(ValueError, match=r"1h|finer"):
        BacktestConfig(
            symbol=SYMBOL,
            timeframe=Timeframe.D1,
            filters=lbank_like_filters(),
            costs=make_config().costs,
            fill_delay=timedelta(hours=6),
            exec_timeframe=exec_tf,
        )


def test_engine_refuses_an_exec_feed_that_yields_the_wrong_timeframe() -> None:
    """Item 1, engine level: a custom exec feed that ignores the requested timeframe and hands back daily bars
    (the config says 1h) is an EngineError, never a silently stretched delay."""
    days, _ = delay_setup()
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    with pytest.raises(EngineError, match="exec feed yielded"):
        run_backtest(BacktestFeed(days), Scripted([0.5]), cfg, exec_feed=_RawFeed(days))


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_exit_after_a_resized_buy_sells_exactly_the_held_quantity(delay: timedelta) -> None:
    """Item 2: the exit is sized from the ACTUAL held quantity (the portfolio is booked from fills, which
    carry the capped quantity -- engine.py `_Run.execute`), not from the originally requested one. The BUY
    is capped by a +0.43 % gap, the next signal flattens: the SELL is exactly the held 99.16531."""
    day0 = make_bar(0, "100", "100")
    if delay == timedelta(0):
        day1 = make_bar(0, "100.43", "100.43", start=day0.ts)
        day2 = make_bar(0, "100.43", "100.43", start=day1.ts)
        res = run_backtest(
            BacktestFeed([day0, day1, day2]), Scripted([1.0, 0.0, 0.0]), make_config(fee="35", slip="5")
        )
    else:
        day1 = make_bar(0, "100", "100.43", start=day0.ts)
        day2 = make_bar(0, "100.43", "100.43", start=day1.ts)
        hours = hour_bars_for_day(day1, ["100"] * 6 + ["100.43"] * 18) + hour_bars_for_day(
            day2, ["100.43"] * 24
        )
        cfg = make_config(fee="35", slip="5", fill_delay=delay, exec_timeframe=Timeframe.H1)
        res = run_backtest(
            BacktestFeed([day0, day1, day2]), Scripted([1.0, 0.0, 0.0]), cfg, exec_feed=BacktestFeed(hours)
        )
    assert res.rejections == ()
    (resize,) = res.resizes  # only the BUY was resized
    buy, sell = (r.fill for r in res.fills)
    assert (buy.side, sell.side) == (Side.BUY, Side.SELL)
    assert buy.quantity == resize.capped_quantity == Dec("99.16531") < resize.requested_quantity
    assert sell.quantity == buy.quantity  # exactly what was held, not the requested 99.60142
    assert res.equity[1].quantity == Dec("99.16531") and res.equity[2].quantity == Dec(0)
    assert res.unfilled_order_ids == ()
    (trade,) = res.trades
    assert trade.quantity == Dec("99.16531")


def test_delay_requires_exec_feed_and_finer_timeframe() -> None:
    days, _ = delay_setup()
    with pytest.raises(ValueError, match="exec_timeframe"):
        make_config(fill_delay=timedelta(hours=6))
    with pytest.raises(ValueError, match="finer"):
        make_config(exec_timeframe=Timeframe.D1)
    with pytest.raises(EngineError, match="exec_feed"):
        delayed = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
        run_backtest(BacktestFeed(days), Scripted([0.5]), delayed)
    with pytest.raises(EngineError, match="exec_feed"):
        run_backtest(BacktestFeed(days), Scripted([0.5]), make_config(), exec_feed=BacktestFeed(days))


def test_pending_order_blocks_stacking_when_delay_exceeds_signal_spacing() -> None:
    days, hours = delay_setup()
    cfg = make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    # Signals every day, but a (artificially) 30 h delay keeps the day-0 order pending at the day-1 close.
    cfg = BacktestConfig(
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        filters=cfg.filters,
        costs=cfg.costs,
        fill_delay=timedelta(hours=30),
        exec_timeframe=Timeframe.H1,
        buy_buffer_bps=Dec(0),
    )
    res = run_backtest(BacktestFeed(days), Scripted([0.5, 0.9]), cfg, exec_feed=BacktestFeed(hours))
    assert [d.outcome for d in res.decisions] == ["ORDER", "SKIPPED_PENDING_ORDER"]
    assert len(res.unfilled_order_ids) == 1 and res.fills == ()


# ---------------------------------------------------------------------------------------------
# feed guards
# ---------------------------------------------------------------------------------------------


def test_feed_rejects_unordered_or_mixed_bars() -> None:
    bars = five_bars()
    with pytest.raises(FeedError, match="strictly increase"):
        BacktestFeed([bars[1], bars[0]])
    with pytest.raises(FeedError, match="at least one"):
        BacktestFeed([])
    with pytest.raises(FeedError):
        BacktestFeed([bars[0], hour_bars_for_day(bars[0], [str(100 + h) for h in range(24)])[0]])
    with pytest.raises(FeedError, match="holds"):
        BacktestFeed(bars).bars("ETHUSDT", Timeframe.D1)


def test_feed_is_reiterable() -> None:
    feed = BacktestFeed(five_bars())
    assert list(feed.bars(SYMBOL, Timeframe.D1)) == list(feed.bars(SYMBOL, Timeframe.D1))
    assert len(feed) == 5


def test_sealed_holdout_bars_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    start = CANONICAL_HOLDOUT_START - timedelta(days=2)
    bars = [make_bar(i, "100", "100", start=start) for i in range(4)]  # last bar closes inside the holdout
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT", raising=False)
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars)
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars, allow_holdout=True)  # one lock is not enough
    monkeypatch.setenv("TBOT_UNSEAL_HOLDOUT", "G4")
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars)  # the other lock alone is not enough either
    assert len(BacktestFeed(bars, allow_holdout=True)) == 4
    # the shared predicate is by CLOSE time (``ts``), matching the loader (SPEC: "ts < holdout_start"):
    # a bar whose close is exactly the boundary is already sealed, so "before" must close strictly earlier.
    before = [make_bar(i, "100", "100", start=start - timedelta(days=1)) for i in range(2)]
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT")
    assert before[-1].ts == CANONICAL_HOLDOUT_START - timedelta(days=1)
    assert len(BacktestFeed(before)) == 2
    at_boundary = [make_bar(0, "100", "100", start=CANONICAL_HOLDOUT_START - timedelta(days=1))]
    assert at_boundary[0].ts == CANONICAL_HOLDOUT_START
    with pytest.raises(HoldoutLockError):
        BacktestFeed(at_boundary)  # closing exactly at the boundary is already sealed, not "before" it


def test_engine_level_holdout_guard_catches_a_custom_feed_that_skips_its_own_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom ``DataFeed`` (not ``BacktestFeed``) does none of the seal checking on its own; the
    engine's own per-bar guard must still catch it, so the seal cannot be bypassed just by writing a
    feed that does not call ``BacktestFeed.__init__``."""
    sealed = [make_bar(0, "100", "100", start=CANONICAL_HOLDOUT_START)]
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT", raising=False)
    with pytest.raises(HoldoutLockError):
        run_backtest(_RawFeed(sealed), Scripted([0.0]), make_config())
    with pytest.raises(HoldoutLockError):
        run_backtest(_RawFeed(sealed), Scripted([0.0]), make_config(), allow_holdout=True)  # one lock
    monkeypatch.setenv("TBOT_UNSEAL_HOLDOUT", "G4")
    assert run_backtest(_RawFeed(sealed), Scripted([0.0]), make_config(), allow_holdout=True).n_bars == 1


def _unseal_rows(log: Path) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if "unsealed" in line]


def _sealed_bars() -> list[Bar]:
    # every bar closes inside the holdout (the first closes exactly at the boundary)
    return bars_from_closes([100, 101, 103, 102], start=CANONICAL_HOLDOUT_START - timedelta(days=1))


def test_unsealing_is_logged_once_per_run_not_once_per_bar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TBOT_UNSEAL_HOLDOUT", "G4")
    bars = _sealed_bars()
    res = run_backtest(_RawFeed(bars), Scripted([0.5]), make_config(), allow_holdout=True)
    assert res.n_bars == 4
    assert len(_unseal_rows(tmp_path / "HOLDOUT_LOG.md")) == 1  # 4 sealed bars, 1 log row


def test_benchmarks_take_allow_holdout_so_a_g4_run_unseals_exactly_once_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MAJOR (re-review): run_benchmarks re-runs the engine twice on the same feed; without
    ``allow_holdout`` it raised HoldoutLockError right after the strategy run had unsealed, forcing a
    second unsealing. With both locks set it must run, logging one unsealing per engine run."""
    feed, cfg, strat = _RawFeed(_sealed_bars()), make_config(), Scripted([0.5])
    monkeypatch.setenv("TBOT_UNSEAL_HOLDOUT", "G4")
    res = run_backtest(feed, strat, cfg, allow_holdout=True)
    with pytest.raises(HoldoutLockError):  # the first lock is missing for the benchmarks
        run_benchmarks(feed, strat, cfg, res)
    bench = run_benchmarks(feed, strat, cfg, res, allow_holdout=True)
    assert bench.buy_and_hold.n_bars == 4 and bench.vol_matched.n_bars == 4
    # strategy + B&H + vol-matched = three engine runs; the refused attempt is logged as a refusal
    assert len(_unseal_rows(tmp_path / "HOLDOUT_LOG.md")) == 3
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT")  # the env lock alone must not be enough either
    with pytest.raises(HoldoutLockError):
        run_benchmarks(feed, strat, cfg, res, allow_holdout=True)


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_oversized_request_is_an_anomaly_even_if_the_price_falls_and_nothing_is_capped(
    delay: timedelta,
) -> None:
    """The anomaly rule does not depend on price direction: the buggy risk manager asks for 1.02 x what cash
    covers at the sizing close (101.59344 BTC x 100 x 1.0035 > 10 000), the price then FALLS 5 % to 95, so the
    request fits at the fill price and fills in full. It is still an anomaly and the run is INVALID."""
    res = oversized_run(delay, factor="1.02", fill_open="95")
    assert res.rejections == ()
    (rec,) = res.fills
    assert rec.fill.quantity == Dec("101.59344")  # not capped
    (resize,) = res.resizes
    assert resize.kind == "anomaly" and resize.filled is True
    assert resize.requested_quantity == resize.capped_quantity == rec.fill.quantity
    assert resize.sizing_price == Dec("100") and resize.sizing_cash == Dec("10000")
    report = build_report(res)
    assert report["health"]["resizes_anomaly"] == 1 and report["health"]["resizes_price_move"] == 0
    assert is_run_invalid(report) and any("resize anomaly" in issue for issue in report["health"]["issues"])


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_sizing_price_is_the_signal_bar_close_not_its_open(delay: timedelta) -> None:
    """The signal bar opens at 90 and closes at 100, and the stub sizes at the CLOSE (99.60142 BTC). The
    recorded sizing price must be 100. Had the open (90) been used, the request (99.60142 x 90 x 1.0035 =
    8996 USDT) would look affordable either way, so the test also runs with a sizing open ABOVE the close
    (110 -> 100): there the open would make the request look unaffordable (an anomaly), the close does not."""
    for open_, close in (("90", "100"), ("110", "100")):
        day0 = make_bar(0, open_, close)
        fill_open = "100.43"
        if delay == timedelta(0):
            day1 = make_bar(0, fill_open, fill_open, start=day0.ts)
            cfg = make_config(fee="35", slip="5")
            res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg)
        else:
            day1 = make_bar(0, close, fill_open, start=day0.ts)
            cfg = make_config(fee="35", slip="5", fill_delay=delay, exec_timeframe=Timeframe.H1)
            exec_feed = BacktestFeed(hour_bars_for_day(day1, [close] * 6 + [fill_open] * 18))
            res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=exec_feed)
        (resize,) = res.resizes
        assert resize.sizing_price == Dec("100") != day0.open
        assert resize.kind == "price_move" and not is_run_invalid(build_report(res))


def test_sizing_cash_is_the_cash_at_submit_time_not_the_initial_cash_nor_the_equity() -> None:
    """Buy half, sell it, buy half again, then top up to full on a gap-up. At the top-up the portfolio holds
    a position, so cash (about half of equity) differs from both the initial 10 000 and the equity. The cap
    on that last BUY records exactly the cash that the equity curve shows at the signal bar."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "110"),
        make_bar(2, "110", "105"),
        make_bar(3, "105", "108"),  # signal bar of the top-up: close 108
        make_bar(4, "110", "110"),  # gap up between the signal close (108) and this open
        make_bar(5, "110", "110"),
    ]
    cfg = make_config(fee="35", slip="5")
    res = run_backtest(BacktestFeed(bars), Scripted([0.5, 0.0, 0.5, 1.0, 1.0, 1.0]), cfg)
    assert len([f for f in res.fills if f.fill.side is Side.SELL]) == 1  # one completed round trip
    (resize,) = [z for z in res.resizes if z.sizing_price == Dec("108")]
    signal_point = next(p for p in res.equity if p.ts == bars[3].ts)  # the bar whose close sized the BUY
    assert resize.sizing_cash == signal_point.cash
    assert resize.sizing_cash != cfg.initial_cash and resize.sizing_cash != signal_point.equity
    assert resize.kind == "price_move"


@pytest.mark.parametrize("hours", [6.5, 0.5, 1.25])
def test_non_integer_fill_delays_are_refused(hours: float) -> None:
    with pytest.raises(ValueError, match="whole number of hours"):
        make_config(fill_delay=timedelta(hours=hours), exec_timeframe=Timeframe.H1)


@pytest.mark.parametrize("hours", [0, 1, 6, 12])
def test_whole_hour_fill_delays_are_accepted(hours: int) -> None:
    cfg = make_config(
        fill_delay=timedelta(hours=hours), exec_timeframe=Timeframe.H1 if hours else None
    )
    assert cfg.fill_delay == timedelta(hours=hours)
