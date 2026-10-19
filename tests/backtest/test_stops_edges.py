"""SPEC 7.1 stop-path edge cases found in the PR #26 review, all hand-computed.

Common setup (as in ``test_stops.py``): fee 0.5 %, slippage 1 %, cash 10 000, tick 0.01, step 0.00001, stop
rule ``0.9 * P_ref``. Every expected number was worked out outside the engine with Decimal arithmetic:

* a BUY fills at ``ceil_tick(ref * 1.01)``, a SELL at ``floor_tick(ref * 0.99)``, fee 0.5 % of the notional;
* a stop sells the whole position at ``min(open, stop)`` less slippage;
* sizing at a signal close: ``floor_5dp(min(gap, cash / (1.01 * 1.005)) / close)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.report import build_report, is_run_invalid, render_markdown
from tbot.backtest.result import (
    AWAITING_REARM,
    CANCELLED_BY_STOP,
    ENTRY_REFUSED,
    ORDER,
    P_REF_LATE,
    REFUSED,
    STOP_ARMED,
    STOP_FILL,
    STOP_LEVEL_LATER_WINDOW,
    STOP_REJECTED,
    BacktestResult,
)
from tbot.core.types import Bar, BarWindow, RegimeState, Side
from tests.backtest.helpers import Scripted, make_config
from tests.backtest.test_stops import StopScripted, daily_run, day, delay_cfg, hourly
from tests.sim_helpers import T_START, lbank_like_filters, make_bar

Dec = Decimal


def cfg_min_notional(min_notional: str) -> BacktestConfig:
    return replace(make_config(), filters=replace(lbank_like_filters(), min_notional=Dec(min_notional)))


def run_with(bars: list[Bar], strat: StopScripted, cfg: BacktestConfig) -> BacktestResult:
    return run_backtest(BacktestFeed(bars), strat, cfg)


# ---------------------------------------------------------------------------------------------
# MAJOR 1: the re-arm wait applies only if the strategy was still LONG (raw target > 0) at the stop
# ---------------------------------------------------------------------------------------------


def exit_signal_then_stop_bars() -> tuple[list[Bar], list[Bar]]:
    """Day 0 flat; day 1 hourly 100 99 98 97 96 95 | 95 ... (BUY fills at h6); day 2 hourly 96 94 91 89 88 ...
    (stop 90 touched at h2: open 91, next open 89); day 3 flat at 88."""
    days = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "96", low="95"),
        make_bar(2, "96", "88", low="88"),
        make_bar(3, "88", "88"),
    ]
    opens = [
        ["100"] * 24,
        ["100", "99", "98", "97", "96", "95", "95"] + ["96"] * 17,
        ["96", "94", "91", "89"] + ["88"] * 20,
        ["88"] * 24,
    ]
    return days, hourly(days, opens)


def test_stop_beats_a_pending_exit_and_the_next_fresh_entry_is_not_blocked_delay_mode() -> None:
    """The review's reproducer: targets [0.5, 0, 0.5, 0.5] in the 6 h delay mode.

    * day-0 close: BUY 50 (5000 / 100); P_ref = day-1 h0 open 100 -> stop 90; fills at h6: 95.95, fee 23.9875,
      cash 5178.5125;
    * day-1 close: target 0 -> SELL (bt-000002), due at day-2 h6;
    * day-2 h2: the stop fires first: 90 -> 89.10, fee 22.275, cash 9611.2375; the SELL is cancelled;
    * the strategy had ALREADY said 0, so the book is just flat (no re-arm wait): at the day-2 close the 0.5
      target is a fresh entry, sized floor_5dp(0.5 * 9611.2375 / 88) = 54.60930, filling at the day-3 h6 open
      88 * 1.01 = 88.88: notional 4853.674584, fee 24.26837292, cash 4733.29454308.
    """
    days, hours = exit_signal_then_stop_bars()
    strat = StopScripted([0.5, 0.0, 0.5, 0.5])
    res = run_backtest(BacktestFeed(days), strat, delay_cfg(), exec_feed=BacktestFeed(hours))
    assert [d.outcome for d in res.decisions][:3] == [ORDER, ORDER, ORDER]
    assert AWAITING_REARM not in {d.outcome for d in res.decisions}
    buy1, stop, buy2 = (r.fill for r in res.fills)
    assert (stop.client_order_id, stop.ts, stop.price) == (
        "stop-000001",
        T_START + timedelta(days=2, hours=2),
        Dec("89.10"),
    )
    assert buy1.client_order_id == "bt-000001" and buy1.price == Dec("95.95")
    assert (buy2.client_order_id, buy2.ts, buy2.price, buy2.quantity, buy2.fee) == (
        "bt-000003",
        T_START + timedelta(days=3, hours=6),
        Dec("88.88"),
        Dec("54.60930"),
        Dec("24.26837292"),
    )
    assert res.equity[2].cash == Dec("9611.2375") and res.equity[2].quantity == 0
    assert res.equity[3].cash == Dec("4733.29454308") and res.equity[3].quantity == Dec("54.60930")
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_FILL, CANCELLED_BY_STOP, STOP_ARMED]
    assert [c[1] for c in strat.calls] == [Dec("100"), Dec("88")]  # the second entry got its own fresh level


def test_same_scenario_with_the_target_still_above_zero_does_wait_for_the_rearm() -> None:
    """Control for the test above: the target stays 0.5 at the day-1 close (the strategy is still LONG when
    the stop fires), so the SPEC wait applies: AWAITING_REARM at the day-2 and day-3 closes, no 2nd buy."""
    days, hours = exit_signal_then_stop_bars()
    res = run_backtest(
        BacktestFeed(days), StopScripted([0.5, 0.5, 0.5, 0.5]), delay_cfg(), exec_feed=BacktestFeed(hours)
    )
    assert [d.outcome for d in res.decisions][2:] == [AWAITING_REARM, AWAITING_REARM]
    assert [r.fill.side for r in res.fills] == [Side.BUY, Side.SELL]
    assert res.equity[3].quantity == 0


def test_next_open_mode_exit_filling_at_the_open_leaves_nothing_to_stop_and_no_rearm_wait() -> None:
    """Next-open mode: the target-0 SELL fills at the bar-2 open (92 * 0.99 = 91.08) BEFORE the stop test of
    that bar (low 85 <= 90), so the stop never fires and the next 0.5 target is a fresh entry."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "92", "88", low="85"),
        make_bar(3, "88", "90"),
    ]
    res = daily_run(bars, StopScripted([0.5, 0.0, 0.5, 0.5]))
    assert res.stop_fills == ()
    assert [(r.fill.side, r.fill.price) for r in res.fills][:2] == [
        (Side.BUY, Dec("101.00")),
        (Side.SELL, Dec("91.08")),
    ]
    assert res.decisions[2].outcome == ORDER and res.decisions[2].client_order_id == "bt-000003"
    assert AWAITING_REARM not in {d.outcome for d in res.decisions}
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_ARMED]  # the 2nd one: the fresh entry


# ---------------------------------------------------------------------------------------------
# MINOR 5a: the re-arm uses the strategy's RAW target, not the regime-scaled one
# ---------------------------------------------------------------------------------------------


class ScriptedRegime:
    warmup_bars = 1

    def __init__(self, exposures: Sequence[float]) -> None:
        self._exposures = list(exposures)

    def update(self, window: BarWindow) -> RegimeState:
        return RegimeState(window.as_of, self._exposures[min(len(window) - 1, len(self._exposures) - 1)])


def test_regime_multiplier_zero_does_not_rearm_while_the_raw_target_is_above_zero() -> None:
    """The stop fires at bar 2 (raw target 0.5). The regime then scales the intent to 0 at bars 3-4 and back
    to 0.5 at bar 5. The strategy's own target never reached 0, so every close stays AWAITING_REARM: a
    re-arm on the scaled intent would have cleared the wait at bar 3 and re-bought at bar 5."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "92", "88", low="85"),
        make_bar(3, "88", "88"),
        make_bar(4, "88", "88"),
        make_bar(5, "88", "88"),
        make_bar(6, "88", "88"),
    ]
    regime = ScriptedRegime([1, 1, 1, 0, 0, 1, 1])
    res = run_backtest(BacktestFeed(bars), StopScripted([0.5] * 7), make_config(), regime=regime)
    assert len(res.stop_fills) == 1
    assert [d.outcome for d in res.decisions[2:]] == [AWAITING_REARM] * 5
    assert [d.target_weight for d in res.decisions[3:6]] == [0.0, 0.0, 0.5]  # logged AFTER the multiplier
    assert [r.fill.side for r in res.fills] == [Side.BUY, Side.SELL]
    assert all(p.quantity == 0 for p in res.equity[2:])


# ---------------------------------------------------------------------------------------------
# MINOR 5b/5c: resizes while invested never move the level; a stop sells whatever is held
# ---------------------------------------------------------------------------------------------


def test_resize_up_while_invested_keeps_the_level_and_the_stop_sells_the_whole_enlarged_position() -> None:
    """Bar 0 BUY 50 (stop 90 from P_ref 100 at bar 1, fixed). Bar 1: target 1.0 -> a resize BUY: equity
    9924.75, gap 4924.75, budget min(4924.75, 4924.75/1.01505) -> floor_5dp(4851.74../100) = 48.51731,
    filled at bar 2: 101.00, fee 24.5012415500, cash 0.00044845. It is NOT a new entry: stop_price is called
    once. Bar 3 (open 95, low 85): the stop sells all 98.51731 at min(95, 90) = 90 -> 89.10: notional
    8777.892321, fee 43.889461605, cash 8734.003307845."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "100", low="99"),
        make_bar(2, "100", "100", low="99"),
        make_bar(3, "95", "88", low="85"),
    ]
    strat = StopScripted([0.5, 1.0, 1.0, 1.0])
    res = daily_run(bars, strat)
    buy1, buy2, stop = (r.fill for r in res.fills)
    assert (buy2.client_order_id, buy2.ts, buy2.price, buy2.quantity) == (
        "bt-000002",
        day(2),
        Dec("101.00"),
        Dec("48.51731"),
    )
    assert strat.calls == [(day(1), Dec("100"))]
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_FILL]
    assert all(e.level == Dec("90.0") for e in res.stop_events)
    assert stop.quantity == buy1.quantity + buy2.quantity == Dec("98.51731")
    assert (stop.price, stop.fee) == (Dec("89.10"), Dec("43.889461605"))
    assert res.equity[3].cash == Dec("8734.003307845") and res.equity[3].quantity == 0


def test_partial_resize_sell_then_stop_sells_only_what_is_left() -> None:
    """Bar 0 target 1.0: BUY floor_5dp(9851.72/100) = 98.51731; fills at bar 1: 101.00, fee 49.75124155, cash
    0.00044845 (stop 90 from P_ref 100). Bar 1 close: target 0.5, equity 9851.73144845, gap -4925.865.. beyond
    the 25 % band (2462.93) -> SELL floor_5dp(49.25865..) = 49.25865, filled at bar 2: 99.00, fee 24.38303175,
    cash 4852.2237667. Bar 3 (open 95, low 85): the stop sells the remaining 49.25866 at 89.10: notional
    4388.946606, fee 21.94473303, cash 9219.22563967."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "100", low="99"),
        make_bar(2, "100", "100", low="99"),
        make_bar(3, "95", "85", low="85"),
    ]
    strat = StopScripted([1.0, 0.5, 0.5, 0.5])
    res = daily_run(bars, strat)
    buy, partial, stop = (r.fill for r in res.fills)
    assert buy.quantity == Dec("98.51731")
    assert (partial.side, partial.ts, partial.price, partial.quantity) == (
        Side.SELL,
        day(2),
        Dec("99.00"),
        Dec("49.25865"),
    )
    assert res.equity[2].cash == Dec("4852.2237667")
    assert (stop.client_order_id, stop.quantity, stop.price, stop.fee) == (
        "stop-000001",
        Dec("49.25866"),
        Dec("89.10"),
        Dec("21.94473303"),
    )
    assert res.equity[3].cash == Dec("9219.22563967") and res.equity[3].quantity == 0
    assert strat.calls == [(day(1), Dec("100"))]  # the partial SELL did not touch the level either
    assert res.decisions[3].outcome == AWAITING_REARM


# ---------------------------------------------------------------------------------------------
# MINOR 5d: a stop level that is not on the tick grid
# ---------------------------------------------------------------------------------------------


def test_stop_level_off_the_tick_grid_is_kept_exact_and_only_the_fill_is_tick_rounded() -> None:
    """P_ref = 100.33 -> stop 90.297 (three decimals, off the 0.01 grid). The BUY fills at
    ceil_tick(101.3333) =
    101.34 (notional 5067, fee 25.335, cash 4907.665). On bar 2 (open 95, low 85) the stop reference is
    min(95, 90.297) = 90.297, UNROUNDED; the SELL price is floor_tick(90.297 * 0.99 = 89.39403) = 89.39:
    notional 4469.50, fee 22.3475, cash 9354.8175."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100.33", "98", low="95"),
        make_bar(2, "95", "88", low="85"),
    ]
    res = daily_run(bars, StopScripted([0.5, 0.5, 0.5]))
    assert res.fills[0].fill.price == Dec("101.34")
    (rec,) = res.stop_fills
    assert res.stop_events[0].level == Dec("90.297")
    assert rec.reference_price == Dec("90.297")
    assert (rec.fill.price, rec.fill.fee) == (Dec("89.39"), Dec("22.3475"))
    assert res.equity[1].cash == Dec("4907.665") and res.equity[2].cash == Dec("9354.8175")


# ---------------------------------------------------------------------------------------------
# MINOR 3 + 5e: a rejected stop exit keeps the stop armed and is retried
# ---------------------------------------------------------------------------------------------
#
# min_notional 4600: the entry (50 * 100 = 5000) and the resize BUY (88 * 55.13331 = 4851.7) pass, the stop
# exit of the first position (50 * 89.10 = 4455) does not.


def test_rejected_stop_exit_stays_armed_and_succeeds_once_the_position_has_grown() -> None:
    """bar 0 BUY 50; bar 1 fill 101.00 (stop 90; cash 4924.75). Bar 2 (open 92, low 85): stop exit 50 * 89.10
    = 4455 < 4600 -> STOP_REJECTED (stop-000001), position kept, stop still 90. Bar 2 close: target 1.0,
    equity 9324.75, gap 4924.75, qty floor_5dp(min(4924.75, 4924.75/1.01505) / 88) = 55.13331. Bar 3 (open 88,
    low 87): the BUY fills at 88.88 (notional 4900.2485928, fee 24.501242964, cash 0.000164236) and the SAME
    bar's stop test retries with 105.13331 units at min(88, 90) = 88 -> 87.12: notional 9159.2139672, fee
    45.796069836, cash 9113.4180616 (stop-000002)."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "92", "88", low="85"),
        make_bar(3, "88", "89", low="87"),
    ]
    strat = StopScripted([0.5, 0.5, 1.0, 1.0])
    res = run_with(bars, strat, cfg_min_notional("4600"))
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_REJECTED, STOP_FILL]
    rejected, filled = res.stop_events[1], res.stop_events[2]
    assert (rejected.ts, rejected.client_order_id, rejected.level) == (day(2), "stop-000001", Dec("90.0"))
    assert (filled.ts, filled.client_order_id, filled.level) == (day(3), "stop-000002", Dec("90.0"))
    assert len(res.rejections) == 1 and "MIN_NOTIONAL" in res.rejections[0].reason
    assert res.equity[2].quantity == Dec("50.00000")  # still long after the rejected exit
    assert strat.calls == [(day(1), Dec("100"))]  # the level was not recomputed
    buy2 = res.fills[1].fill
    assert (buy2.price, buy2.quantity, buy2.fee) == (Dec("88.88"), Dec("55.13331"), Dec("24.501242964"))
    stop = res.fills[2].fill
    assert (stop.quantity, stop.price, stop.fee) == (Dec("105.13331"), Dec("87.12"), Dec("45.796069836"))
    assert res.equity[3].cash == Dec("9113.4180616") and res.equity[3].quantity == 0
    assert is_run_invalid(build_report(res))  # a rejected exit is a visible failure, not a silent one


def test_rejected_stop_exit_is_retried_on_every_later_touch() -> None:
    """Same entry, constant target 0.5 (the position stays 50 units): bar 2 stop exit 4455 < 4600 rejected;
    bar 3 (open 88, low 87): reference min(88, 90) = 88 -> 87.12 * 50 = 4356 < 4600 rejected again. Both
    attempts are events with their own ids; the position and the level are untouched."""
    bars = [
        make_bar(0, "100", "100"),
        make_bar(1, "100", "98", low="95"),
        make_bar(2, "92", "88", low="85"),
        make_bar(3, "88", "89", low="87"),
    ]
    res = run_with(bars, StopScripted([0.5] * 4), cfg_min_notional("4600"))
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_REJECTED, STOP_REJECTED]
    assert [e.client_order_id for e in res.stop_events[1:]] == ["stop-000001", "stop-000002"]
    assert [e.level for e in res.stop_events[1:]] == [Dec("90.0")] * 2
    assert len(res.rejections) == 2 and res.stop_fills == ()
    assert [p.quantity for p in res.equity[1:]] == [Dec("50.00000")] * 3


# ---------------------------------------------------------------------------------------------
# MINOR 6: an entry that only worked after an earlier attempt failed is counted
# ---------------------------------------------------------------------------------------------


def test_entry_retried_after_an_invalid_stop_is_counted_as_a_level_from_a_later_window() -> None:
    answers = iter([Dec("100"), Dec("90")])  # first stop_price answer is invalid (not below P_ref)
    bars = [make_bar(i, "100", "100") for i in range(4)]
    res = daily_run(bars, StopScripted([0.5] * 4, lambda _p: next(answers)))
    assert [e.kind for e in res.stop_events][:3] == [ENTRY_REFUSED, STOP_ARMED, STOP_LEVEL_LATER_WINDOW]
    assert res.stop_levels_from_later_window == 1
    report = build_report(res)
    assert report["stops"]["stop_levels_from_later_window"] == 1
    assert "stop levels from a later window 1" in render_markdown(report)


def test_entry_after_a_risk_refusal_is_counted_and_a_target_zero_in_between_resets_it() -> None:
    """min_notional 4600: target 0.4 sizes to 40 units = 4000 < 4600 -> REFUSED at bar 0 while the strategy
    wants to be long; target 0.6 at bar 1 (60 units = 6000) is accepted, so the position's stop level comes
    from bar 1's window: counted. With a target 0 at bar 1 the next entry is a fresh one: not counted."""
    cfg = cfg_min_notional("4600")
    bars = [make_bar(i, "100", "100") for i in range(5)]
    res = run_with(bars, StopScripted([0.4, 0.6, 0.6, 0.6, 0.6]), cfg)
    assert res.decisions[0].outcome == REFUSED and res.decisions[1].outcome == ORDER
    assert res.stop_levels_from_later_window == 1
    assert [e.kind for e in res.stop_events] == [STOP_ARMED, STOP_LEVEL_LATER_WINDOW]
    reset = run_with(bars, StopScripted([0.4, 0.0, 0.6, 0.6, 0.6]), cfg)
    assert reset.decisions[0].outcome == REFUSED and reset.decisions[2].outcome == ORDER
    assert reset.stop_levels_from_later_window == 0
    assert [e.kind for e in reset.stop_events] == [STOP_ARMED]


def test_a_clean_first_entry_is_not_counted() -> None:
    res = daily_run([make_bar(i, "100", "100") for i in range(3)], StopScripted([0.5] * 3))
    assert res.stop_levels_from_later_window == 0 and res.p_ref_from_late_bar == 0


# ---------------------------------------------------------------------------------------------
# MINOR 7: delay mode with a missing execution bar at the signal close
# ---------------------------------------------------------------------------------------------


def test_missing_first_hour_is_recorded_and_counted() -> None:
    """The day-1 h0 bar (closing at day 1 + 1 h) is missing, so the first execution bar at/after the order's
    creation is h1 (open 99). P_ref is therefore 99, not the day-1 open: stop 0.9 * 99 = 89.1, and the event
    says so. The BUY still fills at the h6 open 95 * 1.01 = 95.95."""
    d0, d1 = make_bar(0, "100", "100"), make_bar(1, "100", "96", low="95")
    opens1 = ["100", "99", "98", "97", "96", "95", "95"] + ["96"] * 17
    hours = [
        h for h in hourly([d0, d1], [["100"] * 24, opens1]) if h.ts != T_START + timedelta(days=1, hours=1)
    ]
    strat = StopScripted([0.5, 0.5])
    res = run_backtest(BacktestFeed([d0, d1]), strat, delay_cfg(), exec_feed=BacktestFeed(hours))
    late, armed = res.stop_events[0], res.stop_events[1]
    assert (late.kind, late.ts, late.client_order_id) == (
        P_REF_LATE,
        T_START + timedelta(days=1, hours=1),
        "bt-000001",
    )
    assert "1:00:00 after the signal close" in late.detail
    assert (armed.kind, armed.level) == (STOP_ARMED, Dec("89.1"))
    assert strat.calls == [(day(1), Dec("99"))]
    assert res.fills[0].fill.price == Dec("95.95")
    assert res.p_ref_from_late_bar == 1
    report = build_report(res)
    assert report["stops"]["p_ref_from_late_bar"] == 1
    assert "P_ref from a late bar 1" in render_markdown(report)


def test_complete_hours_record_no_late_p_ref() -> None:
    d0, d1 = make_bar(0, "100", "100"), make_bar(1, "100", "96", low="95")
    hours = hourly([d0, d1], [["100"] * 24, ["100", "99", "98", "97", "96", "95", "95"] + ["96"] * 17])
    res = run_backtest(
        BacktestFeed([d0, d1]), StopScripted([0.5, 0.5]), delay_cfg(), exec_feed=BacktestFeed(hours)
    )
    assert res.p_ref_from_late_bar == 0 and P_REF_LATE not in {e.kind for e in res.stop_events}


# ---------------------------------------------------------------------------------------------
# MINOR 4: only plain buy-and-hold is band-free; the vol-matched benchmark keeps S0's 25 % band
# ---------------------------------------------------------------------------------------------


def test_vol_matched_benchmark_keeps_the_rebalance_band_while_plain_buy_and_hold_never_rebalances() -> None:
    """The strategy holds 0.5, so the vol-matched weight is about 0.5. A tripling of the price (100 -> 300)
    drifts that weight to about 0.74: more than 25 % away from 0.5, so the vol-matched run (band 0.25)
    rebalances with a SELL, while plain buy-and-hold (weight 1, band 1.0) holds its single BUY."""
    closes = ["100", "100", "100", "300", "300", "300"]
    bars = [make_bar(i, closes[i - 1] if i else "100", c) for i, c in enumerate(closes)]
    cfg = make_config()
    feed = BacktestFeed(bars)
    strat = Scripted([0.5] * len(bars))
    res = run_backtest(feed, strat, cfg)
    bench = run_benchmarks(feed, strat, cfg, res)
    assert 0.4 < bench.vol_match_weight < 0.6
    assert bench.buy_and_hold.config.rebalance_band == 1.0
    assert bench.vol_matched.config.rebalance_band == cfg.rebalance_band == 0.25
    assert not bench.buy_and_hold.config.enforce_stops and not bench.vol_matched.config.enforce_stops
    assert [f.fill.side for f in bench.buy_and_hold.fills] == [Side.BUY]
    assert [f.fill.side for f in bench.vol_matched.fills] == [Side.BUY, Side.SELL]
    assert bench.vol_matched.stop_events == () and bench.buy_and_hold.stop_events == ()
