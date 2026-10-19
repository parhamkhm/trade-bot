"""SPEC 7.1 protective stops and the re-arm rule, hand-computed (fee 0.5 %, slippage 1 %, cash 10 000).

Every expected number below was worked out outside the engine with Decimal arithmetic from the SPEC rules:

* the stop level is ``strategy.stop_price(window_at_signal, P_ref)`` with ``P_ref`` the open of bar t+1
  BEFORE slippage, computed once per position and never moved;
* a stop sells the whole position at ``min(open, stop)`` less 1 % adverse slippage, fee 0.5 %;
* after a stop-out the book stays flat until the strategy's own target has gone to 0 and above 0 again.

Round-number stop rule used here: ``stop = 0.9 * P_ref``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.report import build_report, is_run_invalid, render_markdown
from tbot.backtest.result import (
    AWAITING_REARM,
    CANCELLED_BY_STOP,
    ENTRY_REFUSED,
    NO_ACTION,
    ORDER,
    STOP_ARMED,
    STOP_FILL,
    BacktestResult,
)
from tbot.core.types import Bar, BarWindow, Side, TargetIntent, Timeframe
from tests.backtest.helpers import make_config
from tests.sim_helpers import T_START, hour_bars_for_day, make_bar

Dec = Decimal
StopRule = Callable[[Decimal], Decimal]


class StopScripted:
    """Scripted target weights plus a recorded, configurable stop rule (default 0.9 * entry price)."""

    warmup_bars = 1

    def __init__(self, weights: Sequence[float], rule: StopRule | None = None) -> None:
        self.id = "stop-scripted"
        self._weights = list(weights)
        self._rule: StopRule = rule or (lambda p: p * Dec("0.9"))
        self.calls: list[tuple[object, Decimal]] = []

    def on_bar(self, window: BarWindow) -> TargetIntent:
        t = len(window) - 1
        return TargetIntent(self.id, window.as_of, self._weights[min(t, len(self._weights) - 1)], "script")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        self.calls.append((window.as_of, entry_price))
        return self._rule(entry_price)


def daily_run(bars: list[Bar], strat: StopScripted, **cfg: Any) -> BacktestResult:
    return run_backtest(BacktestFeed(bars), strat, make_config(**cfg))


def day(i: int) -> datetime:
    return T_START + timedelta(days=i)


# ---------------------------------------------------------------------------------------------
# next-open mode: level at P_ref, fixed; fill at min(open, stop); re-arm
# ---------------------------------------------------------------------------------------------
#
# bar  open close  low   target  what happens
#  0   100   100    -    0.50    BUY 50 sized at 100 (5000 / 100)
#  1   100    98   95    0.50    BUY fills 101.00; P_ref = 100 -> stop 90 (fixed); notional 5050, fee 25.25,
#                              cash 4924.75; low 95 > 90; equity 9824.75; weight 0.4987 inside the band
#  2    92    88   85    0.50    low 85 <= 90: STOP sells 50 at min(92, 90) = 90 -> 89.10; notional 4455,
#                              fee 22.275, cash 9357.475; flat; target 0.5 > 0 -> AWAITING_REARM
#  3    88    90    -     0.50    still AWAITING_REARM
#  4    90    95    -     0.00    strategy target reached 0 -> re-armed (flat, nothing to do)
#  5    95   100    -     0.50    BUY sized at 100: floor(9357.475 * 0.5 / 100, 5dp) = 46.78737
#  6   110   112   105    0.50    BUY fills at 111.10 (open 110 + 1 %); P_ref = 110 -> NEW stop 99, not 90
#                              notional 5198.076807, fee 25.990384035, cash 4133.407808965
#  7   112   100   100    0.50    low 100 > 99: no stop; the level is not re-computed (stop_price: 2 calls)


def long_scenario() -> list[Bar]:
    return [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "92", "88", low="85"),
        make_bar(3, "88", "90"),
        make_bar(4, "90", "95"),
        make_bar(5, "95", "100"),
        make_bar(6, "110", "112", low="105"),
        make_bar(7, "112", "100", low="100"),
    ]


def test_stop_level_comes_from_stop_price_at_p_ref_and_stays_fixed() -> None:
    strat = StopScripted([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5])
    res = daily_run(long_scenario(), strat)
    # stop_price is called exactly once per opened position, with the SIGNAL-bar window and the UNSLIPPED
    # open of the next bar (100 = open of bar 1, 110 = open of bar 6), never the close it was sized at
    assert strat.calls == [(day(1), Dec("100")), (day(6), Dec("110"))]
    armed = [e for e in res.stop_events if e.kind == STOP_ARMED]
    assert [(e.ts, e.level, e.client_order_id) for e in armed] == [
        (day(1), Dec("90.0"), "bt-000001"),
        (day(6), Dec("99.0"), "bt-000002"),
    ]


def test_daily_stop_fills_at_min_open_stop_with_slippage_and_fee() -> None:
    res = daily_run(long_scenario(), StopScripted([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5]))
    buy1, stop, buy2 = (r.fill for r in res.fills)
    assert (buy1.side, buy1.ts, buy1.price, buy1.quantity, buy1.fee) == (
        Side.BUY,
        day(1),
        Dec("101.00"),
        Dec("50.00000"),
        Dec("25.25"),
    )
    rec = res.stop_fills[0]
    assert rec.fill is stop
    # open 92 > stop 90, so the stop is the reference; -1 % slippage -> 89.10
    assert (stop.side, stop.ts, stop.client_order_id) == (Side.SELL, day(2), "stop-000001")
    assert (stop.price, stop.quantity, stop.fee) == (Dec("89.10"), Dec("50.00000"), Dec("22.275"))
    assert rec.reference_price == Dec("90.0") and rec.slippage_cost == Dec("0.9") * 50
    assert (buy2.ts, buy2.price, buy2.quantity, buy2.fee) == (
        day(6),
        Dec("111.10"),
        Dec("46.78737"),
        Dec("25.990384035"),
    )
    cash = [p.cash for p in res.equity]
    assert cash[1] == Dec("4924.75") and cash[2] == Dec("9357.475")
    assert cash[6] == Dec("4133.407808965")
    assert [p.quantity for p in res.equity] == [
        Dec(0),
        Dec("50.00000"),
        Dec(0),
        Dec(0),
        Dec(0),
        Dec(0),
        Dec("46.78737"),
        Dec("46.78737"),
    ]
    assert res.equity[6].equity == Dec("9373.593248965") and res.equity[7].equity == Dec("8812.144808965")
    assert not res.rejections and not res.resizes and not res.unfilled_order_ids
    assert res.fees_paid == Dec("25.25") + Dec("22.275") + Dec("25.990384035")


def test_stop_is_not_blocked_by_the_rebalance_band_and_rearm_waits_for_target_zero() -> None:
    res = daily_run(long_scenario(), StopScripted([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5]))
    # on the stop bar the stub would have answered "within band" (target 0.5 vs weight 0.4987 at bar 1)...
    assert res.decisions[1].outcome == NO_ACTION and "within rebalance band" in res.decisions[1].detail
    # ... yet the position was sold; then no order is placed while the target stays above 0
    assert [d.outcome for d in res.decisions[2:4]] == [AWAITING_REARM, AWAITING_REARM]
    assert res.decisions[4].outcome == NO_ACTION  # target 0 re-arms; flat, so nothing to trade
    assert res.decisions[5].outcome == ORDER and res.decisions[5].client_order_id == "bt-000002"
    kinds = [e.kind for e in res.stop_events]
    assert kinds == [STOP_ARMED, STOP_FILL, STOP_ARMED]
    assert res.stop_events[1].level == Dec("90.0") and res.stop_events[1].client_order_id == "stop-000001"


def test_without_the_target_dropping_to_zero_the_book_never_re_enters() -> None:
    bars = long_scenario()
    res = daily_run(bars, StopScripted([0.5] * len(bars)))
    assert [r.fill.side for r in res.fills] == [Side.BUY, Side.SELL]
    assert all(d.outcome == AWAITING_REARM for d in res.decisions[2:])
    assert all(p.quantity == 0 for p in res.equity[2:])


def test_gap_down_through_the_stop_fills_at_the_open_not_the_stop() -> None:
    """Bar 2 opens at 85, below the stop 90: reference min(85, 90) = 85 -> 85 * 0.99 = 84.15.
    notional 50 * 84.15 = 4207.50, fee 21.0375, cash 4924.75 + 4207.50 - 21.0375 = 9111.2125."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "85", "82", low="80"),
    ]
    res = daily_run(bars, StopScripted([0.5, 0.5, 0.5]))
    (rec,) = res.stop_fills
    assert rec.reference_price == Dec("85") and rec.fill.price == Dec("84.15")
    assert rec.fill.fee == Dec("21.0375")
    assert res.equity[2].cash == Dec("9111.2125") and res.equity[2].quantity == 0
    assert rec.slippage_cost == Dec("0.85") * 50


def test_stop_touched_exactly_is_a_stop() -> None:
    """low == stop counts (the SPEC says low <= stop): the stop is the reference, 90 * 0.99 = 89.10."""
    bars = [make_bar(0, "100", "100"), make_bar(1, "100", "98", low="95"), make_bar(2, "95", "94", low="90")]
    res = daily_run(bars, StopScripted([0.5, 0.5, 0.5]))
    assert res.stop_fills[0].fill.price == Dec("89.10")
    # one tick above the stop is NOT touched
    bars[2] = make_bar(2, "95", "94", low="90.01")
    assert daily_run(bars, StopScripted([0.5, 0.5, 0.5])).stop_fills == ()


def test_stop_hit_on_the_entry_bar() -> None:
    """The BUY fills at the open of bar 1 (101.00) and the same bar trades down to 85 <= 90: stopped out at
    90 * 0.99 = 89.10 on the entry bar. cash 4924.75 + 4455 - 22.275 = 9357.475, both fills stamped bar 1."""
    bars = [make_bar(0, "100", "100"), make_bar(1, "100", "88", low="85"), make_bar(2, "88", "90")]
    res = daily_run(bars, StopScripted([0.5, 0.5, 0.5]))
    buy, stop = (r.fill for r in res.fills)
    assert (buy.side, buy.ts, buy.price) == (Side.BUY, day(1), Dec("101.00"))
    assert (stop.side, stop.ts, stop.price, stop.quantity) == (Side.SELL, day(1), Dec("89.10"), buy.quantity)
    assert res.equity[1].quantity == 0 and res.equity[1].cash == Dec("9357.475")
    assert len(res.trades) == 1 and res.trades[0].pnl == Dec("9357.475") - 10000
    assert res.decisions[1].outcome == AWAITING_REARM


def test_crash_through_the_stop_on_the_entry_bar_fills_at_the_stop_when_the_open_is_above_it() -> None:
    """P_ref is the open (100) and the stop 90 is computed from it, so in next-open mode a stop can only be
    hit on the entry bar by intra-bar movement. The hourly delay mode covers the gap-at-fill case."""
    bars = [make_bar(0, "100", "100"), make_bar(1, "100", "70", low="60")]
    res = daily_run(bars, StopScripted([0.5, 0.5]))
    assert res.stop_fills[0].reference_price == Dec("90.0")  # min(open 100, stop 90)


def test_strategy_exit_with_an_open_position_is_a_normal_next_open_exit() -> None:
    """The strategy's own STOPPED state (target 0) while the engine stop is untouched: a normal SELL at the
    next open, no stop fill, and the next entry gets a fresh level."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "98", "96", low="94"),
        make_bar(3, "96", "96"),
        make_bar(4, "96", "97"),
    ]
    strat = StopScripted([0.5, 0.5, 0.0, 0.5, 0.5])
    res = daily_run(bars, strat)
    assert res.stop_fills == ()
    sell = res.fills[1].fill
    assert (sell.side, sell.client_order_id, sell.ts, sell.price) == (
        Side.SELL,
        "bt-000002",
        day(3),
        Dec("95.04"),
    )
    assert [c[1] for c in strat.calls] == [Dec("100"), Dec("96")]  # second entry at the open of bar 4
    assert res.stop_events[-1].level == Dec("86.4")  # 0.9 * 96, not the first level 90


def test_a_stop_cancels_the_order_still_pending_when_it_fires() -> None:
    """Delay mode: the strategy's SELL (target 0 at the day-1 close) would fill at day 2 h6, but the stop
    fires first at day 2 h2 (open 91, next open 89, stop 90 -> 89.10). The engine cancels the SELL so
    nothing can fill into a flat book; it is neither a fill, an unfilled order nor a vanished order."""
    d0, d1, d2 = (
        make_bar(0, "100", "100"),
        make_bar(1, "100", "95", low="95"),
        make_bar(2, "95", "88", low="88"),
    )
    opens1 = ["100", "99", "98", "97", "96", "95", "95"] + ["95"] * 17
    opens2 = ["95", "94", "91", "89"] + ["88"] * 20
    hours = hourly([d0, d1, d2], [["100"] * 24, opens1, opens2])
    res = run_backtest(
        BacktestFeed([d0, d1, d2]), StopScripted([0.5, 0.0, 0.0]), delay_cfg(), exec_feed=BacktestFeed(hours)
    )
    buy, stop = (r.fill for r in res.fills)
    assert buy.side is Side.BUY and stop.client_order_id == "stop-000001"
    assert stop.ts == T_START + timedelta(days=2, hours=2) and stop.price == Dec("89.10")
    cancelled = [e for e in res.stop_events if e.kind == CANCELLED_BY_STOP]
    assert [e.client_order_id for e in cancelled] == ["bt-000002"]
    assert res.unfilled_order_ids == () and res.orders_cancelled_by_engine == 1
    assert res.equity[2].quantity == 0
    assert not is_run_invalid(build_report(res))


# ---------------------------------------------------------------------------------------------
# D-050: no valid stop -> no long
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        lambda p: p,  # not below P_ref
        lambda p: p * 2,
        lambda _p: Dec(0),
        lambda _p: Dec(-1),
        lambda _p: Dec("NaN"),
    ],
    ids=["equal", "above", "zero", "negative", "nan"],
)
def test_invalid_stop_refuses_the_entry(rule: StopRule) -> None:
    bars = [make_bar(i, "100", "100") for i in range(4)]
    res = daily_run(bars, StopScripted([0.5] * 4, rule))
    assert res.fills == ()
    # one entry order per signal close (bars 0-3), the first three cancelled before filling, the last one
    # still pending when the data ends
    assert res.entries_refused_invalid_stop == 3
    assert all(p.quantity == 0 for p in res.equity)
    assert res.unfilled_order_ids == ("bt-000004",)
    report = build_report(res)
    assert report["stops"]["entries_refused_invalid_stop"] == res.entries_refused_invalid_stop
    assert report["stops"]["orders_cancelled_by_engine"] == res.orders_cancelled_by_engine
    assert not is_run_invalid(report)  # refused entries are accounted for, not "vanished orders"


def test_refused_entry_is_retried_at_the_next_signal_and_can_succeed() -> None:
    """First stop_price call is invalid (entry cancelled before filling), the second is valid: the second
    order, sized at bar 1's close, fills at the open of bar 2 with P_ref = 100 -> stop 90."""
    answers = iter([Dec("100"), Dec("90")])
    bars = [make_bar(i, "100", "100") for i in range(4)]
    res = daily_run(bars, StopScripted([0.5] * 4, lambda _p: next(answers)))
    assert [e.kind for e in res.stop_events][:2] == [ENTRY_REFUSED, STOP_ARMED]
    (rec,) = res.fills
    assert rec.fill.client_order_id == "bt-000002" and rec.fill.ts == day(2)
    assert res.orders_cancelled_by_engine == 1
    assert not is_run_invalid(build_report(res))


# ---------------------------------------------------------------------------------------------
# delay mode: stop checked on 1h bars from the delayed fill on
# ---------------------------------------------------------------------------------------------


def hourly(days: list[Bar], opens_by_day: list[list[str]]) -> list[Bar]:
    out: list[Bar] = []
    for d, opens in zip(days, opens_by_day, strict=True):
        out += hour_bars_for_day(d, opens)
    return out


def delay_cfg() -> BacktestConfig:
    return make_config(fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)


def test_delay_mode_stop_is_checked_on_hourly_bars_after_the_delayed_fill() -> None:
    """Day 1 hourly opens: 100 99 98 97 96 95 | h6 95 | 93 91 89 88 ... 88.
    * order created at the day-0 close; P_ref = open of the first hourly bar of day 1 = 100 -> stop 90.
    * BUY fills at the h6 open: 95 * 1.01 = 95.95, qty 50, notional 4797.50, fee 23.9875, cash 5178.5125.
    * hourly bar h7 (open 93, next open 91) has low 91 > 90; bar h8 (open 91, next open 89) has low 89 <= 90:
      the stop fires on h8 at min(open 91, stop 90) = 90 -> 89.10, notional 4455, fee 22.275,
      cash 5178.5125 + 4455 - 22.275 = 9611.2375.
    """
    d0, d1 = make_bar(0, "100", "100"), make_bar(1, "100", "88", low="88")
    opens1 = ["100", "99", "98", "97", "96", "95", "95", "93", "91", "89"] + ["88"] * 14
    hours = hourly([d0, d1], [["100"] * 24, opens1])
    strat = StopScripted([0.5, 0.5])
    res = run_backtest(BacktestFeed([d0, d1]), strat, delay_cfg(), exec_feed=BacktestFeed(hours))
    created = T_START + timedelta(days=1)
    buy, stop = (r.fill for r in res.fills)
    assert (buy.ts, buy.price, buy.quantity, buy.fee) == (
        created + timedelta(hours=6),
        Dec("95.95"),
        Dec("50.00000"),
        Dec("23.9875"),
    )
    assert (stop.ts, stop.price, stop.fee) == (created + timedelta(hours=8), Dec("89.10"), Dec("22.275"))
    assert res.stop_fills[0].reference_price == Dec("90.0")
    assert strat.calls == [(created, Dec("100"))]  # P_ref is the day-1 open (= first hourly open)
    assert res.equity[1].quantity == 0 and res.equity[1].cash == Dec("9611.2375")
    assert res.decisions[1].outcome == AWAITING_REARM
    assert not is_run_invalid(build_report(res))


def test_delay_mode_stop_touched_before_the_delayed_fill_is_invisible_and_the_exit_is_the_strategys() -> None:
    """The stop (90) is touched at hour 0 (open 100, next open 89) but the BUY only fills at hour 6 (95.95):
    the stop is not live yet, so the engine does not stop. The strategy itself saw the stop touched on the
    day bar (low 89), is STOPPED and emits target 0 at the day-1 close. With the 6 h delay the SELL fills
    at the h6 open of day 2 (96 * 0.99 = 95.04): notional 4752, fee 23.76,
    cash 5178.5125 + 4752 - 23.76 = 9906.7525.
    """
    d0 = make_bar(0, "100", "100")
    d1 = make_bar(1, "100", "96", low="89")
    d2 = make_bar(2, "96", "96")
    opens1 = ["100", "89", "92", "94", "95", "95", "95"] + ["96"] * 17
    hours = hourly([d0, d1, d2], [["100"] * 24, opens1, ["96"] * 24])
    res = run_backtest(
        BacktestFeed([d0, d1, d2]), StopScripted([0.5, 0.0, 0.0]), delay_cfg(), exec_feed=BacktestFeed(hours)
    )
    assert res.stop_fills == ()
    buy, sell = (r.fill for r in res.fills)
    assert buy.ts == T_START + timedelta(days=1, hours=6) and buy.price == Dec("95.95")
    assert res.equity[1].quantity == Dec("50.00000")  # still long at the day-1 close
    assert sell.side is Side.SELL and sell.ts == T_START + timedelta(days=2, hours=6)  # close + 6 h
    assert sell.price == Dec("95.04") and sell.fee == Dec("23.76")
    assert res.equity[2].cash == Dec("9906.7525") and res.equity[2].quantity == 0
    assert [e.kind for e in res.stop_events] == [STOP_ARMED]  # armed, never fired


def test_delay_mode_gap_down_at_the_delayed_fill_stops_on_the_same_bar_at_the_open() -> None:
    """Hour 6 opens at 80, below the stop 90 (P_ref 100): the BUY fills at 80 * 1.01 = 80.80 and the same
    hourly bar is already beyond the stop, so it is sold at min(80, 90) = 80 -> 79.20."""
    d0, d1 = make_bar(0, "100", "100"), make_bar(1, "100", "78", low="78")
    opens1 = ["100", "99", "98", "97", "96", "95"] + ["80"] * 17 + ["78"]
    hours = hourly([d0, d1], [["100"] * 24, opens1])
    res = run_backtest(
        BacktestFeed([d0, d1]), StopScripted([0.5, 0.5]), delay_cfg(), exec_feed=BacktestFeed(hours)
    )
    buy, stop = (r.fill for r in res.fills)
    assert buy.ts == stop.ts == T_START + timedelta(days=1, hours=6)
    assert buy.price == Dec("80.80") and stop.price == Dec("79.20")
    assert res.stop_fills[0].reference_price == Dec("80")


# ---------------------------------------------------------------------------------------------
# benchmarks and the report
# ---------------------------------------------------------------------------------------------


def test_buy_and_hold_benchmarks_are_never_stopped_or_rebalanced() -> None:
    bars = long_scenario()
    feed = BacktestFeed(bars)
    cfg = make_config()
    strat = StopScripted([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5])
    res = run_backtest(feed, strat, cfg)
    assert len(res.stop_fills) == 1  # the strategy IS stopped ...
    bench = run_benchmarks(feed, strat, cfg, res)
    for run in (bench.buy_and_hold, bench.vol_matched):
        assert run.stop_fills == () and run.stop_events == ()  # ... buy & hold is not (SPEC 7.2 E)
        assert [f.fill.side for f in run.fills] == [Side.BUY]
        assert run.equity[-1].quantity > 0


def test_report_counts_stop_fills_and_says_so_in_markdown() -> None:
    res = daily_run(long_scenario(), StopScripted([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5]))
    report = build_report(res)
    assert report["stops"] == {
        "enforced": True,
        "stop_fills": 1,
        "entries_refused_invalid_stop": 0,
        "orders_cancelled_by_engine": 0,
        "stop_levels_from_later_window": 0,
        "p_ref_from_late_bar": 0,
    }
    assert report["stub_risk_manager"]["rebalance_band"] == 0.25
    assert not is_run_invalid(report)  # a stop exit is neither a rejection nor a vanished order
    assert "Protective stops: enforced; stop fills 1" in render_markdown(report)
    off = build_report(daily_run(long_scenario(), StopScripted([0.5] * 8), enforce_stops=False))
    assert off["stops"]["enforced"] is False and off["stops"]["stop_fills"] == 0
    assert "NOT enforced" in render_markdown(off)


def test_enforce_stops_false_is_the_old_stopless_engine() -> None:
    res = daily_run(long_scenario(), StopScripted([0.5] * 8), enforce_stops=False)
    assert res.stop_fills == () and res.stop_events == ()
    assert [r.fill.side for r in res.fills] == [Side.BUY]  # bought once, held through the crash bar
