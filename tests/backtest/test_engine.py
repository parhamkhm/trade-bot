"""Engine tests: hand-computed 5-bar example, causality, accounting, determinism, delay model, guards."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import EngineError, _fast_window, run_backtest
from tbot.backtest.feed import BacktestFeed, FeedError
from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, BarWindow, RegimeState, Side, TargetIntent, Timeframe
from tbot.data.binance_loader import CANONICAL_HOLDOUT_START, HoldoutLockError
from tbot.strategies.base import StrategyRegistrationError
from tests.backtest.helpers import CountingFeed, Recorder, Scripted, make_config
from tests.sim_helpers import SYMBOL, T_START, bars_from_closes, hour_bars_for_day, make_bar

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
    assert rec.fill.ts == signal_close + timedelta(hours=6)
    assert rec.reference_price == Dec("130")  # open of the 06:00 hourly bar
    assert rec.fill.price == Dec("131.30")  # adverse +1 %
    # sized at the signal close (124): 5000 / 124 = 40.32258 (floored to the step size)
    assert rec.fill.quantity == Dec("40.32258")
    assert res.equity[0].quantity == 0  # not filled by the first close
    assert res.equity[1].quantity == Dec("40.32258")


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
    bars = [make_bar(i, "100", "100", start=start) for i in range(4)]  # last bars open inside the holdout
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT", raising=False)
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars)
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars, allow_holdout=True)  # one lock is not enough
    monkeypatch.setenv("TBOT_UNSEAL_HOLDOUT", "G4")
    with pytest.raises(HoldoutLockError):
        BacktestFeed(bars)  # the other lock alone is not enough either
    assert len(BacktestFeed(bars, allow_holdout=True)) == 4
    before = [make_bar(i, "100", "100", start=start) for i in range(2)]  # ends exactly at the boundary
    monkeypatch.delenv("TBOT_UNSEAL_HOLDOUT")
    assert before[-1].ts == CANONICAL_HOLDOUT_START
    assert len(BacktestFeed(before)) == 2
