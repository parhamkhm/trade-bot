"""Causality of the composite strategy on the real engine: prefix property incl. the cut day, 4h feeds, gaps.

The earlier prefix test compared only the pooled RETURN series of a short and a long run. A leak that
changes the decision on the last bar of the short run (day N) has not yet changed any return there (the
order fills at the open of N+1), so a returns-only comparison cannot see it. These tests compare, bar by
bar, the target weight and the decision record INCLUDING day N (``harness.compare_prefix``), and they carry a
deliberately leaky child that must be flagged exactly when its peek changes the decision.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from itertools import pairwise

import pytest

from tbot.backtest.engine import run_backtest
from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, BarWindow, Strategy, TargetIntent, Timeframe
from tbot.strategies.sma_filter import SmaFilter
from tbot.validation.runs import (
    NORMAL_COSTS,
    CompositeStrategy,
    FillMode,
    GatingRiskManager,
    Market,
    StrategyFactory,
    make_config,
    make_stub_risk,
    run_composite,
)
from tests.validation.harness import compare_prefix
from tests.validation.wf_fixtures import BOUNDARY, make_market

D1, H4, H1 = Timeframe.D1, Timeframe.H4, Timeframe.H1
NO, MD = FillMode.NEXT_OPEN, FillMode.MANUAL_DELAY
START = datetime(2020, 1, 1, tzinfo=UTC)

Schedule = list[tuple[datetime, str | None]]
FactoryFor = Callable[[Market], StrategyFactory]


# -- fixtures ------------------------------------------------------------------------------------


def _aggregate_h4(hours: tuple[Bar, ...]) -> list[Bar]:
    """4h bars (``ts`` = close) from consecutive hourly bars; 24 per day, so groups never straddle a day."""
    out: list[Bar] = []
    for i in range(0, len(hours) - 3, 4):
        group = hours[i : i + 4]
        out.append(
            Bar(
                symbol=group[0].symbol,
                timeframe=H4,
                ts=group[-1].ts,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum((b.volume for b in group), Decimal(0)),
            )
        )
    return out


@cache
def _market_h4() -> Market:
    base = make_market()
    return Market(
        base.symbol,
        {D1: base.series(D1), H4: _aggregate_h4(base.series(H1)), H1: base.series(H1)},
        base_asset=base.base_asset,
    )


def _next_close_is_higher(market: Market, ts: datetime) -> bool:
    closes = {b.ts: b.close for b in market.series(D1)}
    return closes[ts + timedelta(days=1)] > closes[ts]


def _pick_daily_cuts(market: Market) -> tuple[datetime, datetime]:
    """One daily close whose NEXT close is higher and one whose next close is lower or equal (Feb 2020)."""
    window = [b.ts for b in market.series(D1) if datetime(2020, 2, 1, tzinfo=UTC) <= b.ts < BOUNDARY]
    up = next(ts for ts in window if _next_close_is_higher(market, ts))
    down = next(ts for ts in window if not _next_close_is_higher(market, ts))
    return up, down


class _Leaky:
    """Cheating child: goes long iff the NEXT daily close (read from the market it was built on) is higher.

    Built on a truncated market it cannot see past the cut, so on the last bar it differs from the same
    child built on the long market -- the exact look-ahead a ``shift(-1)`` indicator would have.
    """

    warmup_bars = 1

    def __init__(self, market: Market) -> None:
        self.id = "leaky"
        bars = market.series(D1)
        self._close = {b.ts: b.close for b in bars}
        self._next = {a.ts: b.ts for a, b in pairwise(bars)}

    def on_bar(self, window: BarWindow) -> TargetIntent:
        nxt = self._next.get(window.as_of)
        peek = self._close[nxt] if nxt is not None else window.last.close
        return TargetIntent(self.id, window.as_of, 1.0 if peek > window.last.close else 0.0, "peek")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


def _honest(_key: str) -> Strategy:
    return SmaFilter(30)


def _leaky_for(market: Market) -> StrategyFactory:
    return lambda _key: _Leaky(market)


def _engine(
    market: Market,
    factory: StrategyFactory,
    schedule: Schedule,
    timeframes: dict[str, Timeframe],
    feed_tf: Timeframe,
    mode: FillMode,
) -> BacktestResult:
    """The composite on the engine exactly as ``run_composite`` wires it, but returning the full result."""
    comp = CompositeStrategy(
        strategy_id="c",
        market=market,
        schedule=schedule,
        factory=factory,
        timeframes=timeframes,
        feed_timeframe=feed_tf,
    )
    cfg = make_config(market, feed_tf, mode, NORMAL_COSTS)
    last = market.series(feed_tf)[-1].ts
    exec_feed = market.feed(H1, None, last) if mode is MD else None
    risk = None if feed_tf is D1 else GatingRiskManager(make_stub_risk(cfg), comp.is_decision_bar)
    return run_backtest(market.feed(feed_tf, None, last), comp, cfg, risk=risk, exec_feed=exec_feed)


def _prefix_diffs(
    full: Market,
    cut: datetime,
    factory_for: FactoryFor,
    schedule: Schedule,
    timeframes: dict[str, Timeframe],
    feed_tf: Timeframe,
    mode: FillMode,
) -> list[str]:
    """``compare_prefix`` of the run on ``full`` truncated at ``cut`` against the run on ``full``.

    ``factory_for(market)`` is called once per market, so a child that reads its market (the leaky one)
    sees exactly the data that run was given.
    """
    short_market = full.truncated(cut)
    assert short_market.series(feed_tf)[-1].ts == cut
    short = _engine(short_market, factory_for(short_market), schedule, timeframes, feed_tf, mode)
    long = _engine(full, factory_for(full), schedule, timeframes, feed_tf, mode)
    assert short.equity[-1].ts == cut
    return compare_prefix(short, long)


def _honest_for(_market: Market) -> StrategyFactory:
    return _honest


SCHEDULE_D: Schedule = [(START, "A"), (BOUNDARY, "B")]
TF_D = {"A": D1, "B": D1}


# -- daily composite: the cut day is compared ----------------------------------------------------


@pytest.mark.parametrize("mode", [NO, MD])
def test_leaky_child_is_flagged_exactly_when_its_peek_changes_the_decision(mode: FillMode) -> None:
    market = make_market()
    up, down = _pick_daily_cuts(market)
    flagged_up = _prefix_diffs(market, up, _leaky_for, SCHEDULE_D, TF_D, D1, mode)
    flagged_down = _prefix_diffs(market, down, _leaky_for, SCHEDULE_D, TF_D, D1, mode)
    assert "decisions" in flagged_up  # next close higher: long run says 1.0 on day N, the cut run says 0.0
    assert flagged_down == []  # next close not higher: both say 0.0, nothing to see


@pytest.mark.parametrize("mode", [NO, MD])
def test_leak_on_the_cut_day_is_invisible_to_a_returns_only_comparison(mode: FillMode) -> None:
    """Why the old test was not enough: through day N the equity of the leaky run is still identical."""
    market = make_market()
    up, _ = _pick_daily_cuts(market)
    short_market = market.truncated(up)
    short = _engine(short_market, _leaky_for(short_market), SCHEDULE_D, TF_D, D1, mode)
    long = _engine(market, _leaky_for(market), SCHEDULE_D, TF_D, D1, mode)
    assert [p.equity for p in short.equity] == [p.equity for p in long.equity if p.ts <= up]
    assert "decisions" in compare_prefix(short, long)


def _all_cuts(market: Market) -> list[datetime]:
    up, down = _pick_daily_cuts(market)
    return [up, down, BOUNDARY - timedelta(days=1), BOUNDARY, BOUNDARY + timedelta(days=3)]


@pytest.mark.parametrize("mode", [NO, MD])
def test_honest_composite_has_no_prefix_difference_incl_the_cut_day_and_a_schedule_boundary(
    mode: FillMode,
) -> None:
    market = make_market()
    for cut in _all_cuts(market):
        assert _prefix_diffs(market, cut, _honest_for, SCHEDULE_D, TF_D, D1, mode) == [], cut


@pytest.mark.parametrize("mode", [NO, MD])
def test_leaky_child_after_the_schedule_boundary_is_flagged_too(mode: FillMode) -> None:
    """The key switches at BOUNDARY; a leak in the NEW key must be seen at a cut after the switch."""
    market = make_market()
    window = [
        b.ts
        for b in market.series(D1)
        if BOUNDARY + timedelta(days=2) <= b.ts < BOUNDARY + timedelta(days=60)
    ]
    cut = next(ts for ts in window if _next_close_is_higher(market, ts))
    assert "decisions" in _prefix_diffs(market, cut, _leaky_for, SCHEDULE_D, TF_D, D1, mode)


# -- 4h and mixed-timeframe composites (GatingRiskManager) -----------------------------------------


class _Recorder:
    """An honest SMA child that remembers every close it was consulted at."""

    def __init__(self, n: int = 30) -> None:
        self.id = "rec"
        self.warmup_bars = n
        self._inner = SmaFilter(n)
        self.calls: list[datetime] = []

    def on_bar(self, window: BarWindow) -> TargetIntent:
        self.calls.append(window.as_of)
        return self._inner.on_bar(window)

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        return self._inner.stop_price(window, entry_price)


def test_the_h4_fixture_is_consistent_with_the_daily_and_hourly_bars() -> None:
    market = _market_h4()
    daily = {b.ts: b for b in market.series(D1)}
    h4 = market.series(H4)
    assert len(h4) == 6 * len(daily)
    midnight = [b for b in h4 if b.ts in daily]
    assert len(midnight) == len(daily)
    assert all(b.close == daily[b.ts].close for b in midnight)  # the 00:00 4h close is the daily close


def test_a_daily_child_on_a_4h_feed_is_consulted_only_at_the_midnight_closes_and_held_between() -> None:
    market = _market_h4()
    rec = _Recorder()
    comp = CompositeStrategy(
        strategy_id="c",
        market=market,
        schedule=[(START, "A")],
        factory=lambda _k: rec,
        timeframes={"A": D1},
        feed_timeframe=H4,
    )
    reference = SmaFilter(30)
    midnight_target = 0.0
    seen = [b.ts for b in market.series(H4) if START <= b.ts < START + timedelta(days=60)]
    assert len(seen) == 6 * 60
    for ts in seen:
        window = market.window_to(H4, ts)
        assert window is not None
        intent = comp.on_bar(window)
        if ts.hour == 0:
            daily_window = market.window_to(D1, ts)
            assert daily_window is not None
            midnight_target = reference.on_bar(daily_window).target_weight
            assert comp.is_decision_bar(ts)
        else:
            assert not comp.is_decision_bar(ts)
        assert intent.target_weight == midnight_target, ts  # held between the daily closes
    assert rec.calls and all(c.hour == 0 and c.minute == 0 for c in rec.calls)
    assert len(rec.calls) == len(set(rec.calls)) == 60  # once per daily close, nothing else


def test_a_4h_child_is_consulted_at_every_4h_close() -> None:
    market = _market_h4()
    rec = _Recorder(n=12)
    comp = CompositeStrategy(
        strategy_id="c",
        market=market,
        schedule=[(START, "F")],
        factory=lambda _k: rec,
        timeframes={"F": H4},
        feed_timeframe=H4,
    )
    seen = [b.ts for b in market.series(H4) if START <= b.ts < START + timedelta(days=10)]
    for ts in seen:
        window = market.window_to(H4, ts)
        assert window is not None
        comp.on_bar(window)
        assert comp.is_decision_bar(ts)
    assert rec.calls == seen


def test_a_4h_composite_cannot_hold_a_child_finer_than_its_feed() -> None:
    with pytest.raises(ValueError, match="finer than the feed"):
        CompositeStrategy(
            strategy_id="c",
            market=_market_h4(),
            schedule=[(START, "F")],
            factory=_honest,
            timeframes={"F": H1},
            feed_timeframe=H4,
        )


SCHEDULE_X: Schedule = [(START, "D"), (BOUNDARY, "F")]  # a daily point, then a 4h point
TF_X = {"D": D1, "F": H4}


def _noon_cuts(market: Market) -> list[datetime]:
    """H4 cuts: a 12:00 close in February, the boundary midnight, and a 12:00 close after the switch."""
    pick = [datetime(2020, 2, 11, 12, tzinfo=UTC), BOUNDARY, BOUNDARY + timedelta(days=5, hours=12)]
    have = {b.ts for b in market.series(H4)}
    assert all(c in have for c in pick)
    return pick


@pytest.mark.parametrize("mode", [NO, MD])
def test_mixed_timeframe_honest_composite_has_no_prefix_difference_on_a_4h_feed(mode: FillMode) -> None:
    market = _market_h4()
    for cut in _noon_cuts(market):
        diffs = _prefix_diffs(market, cut, _honest_for, SCHEDULE_X, TF_X, H4, mode)
        assert diffs == [], (cut, mode)


@pytest.mark.parametrize("mode", [NO, MD])
def test_leaky_daily_child_on_a_4h_feed_is_flagged_at_a_noon_cut(mode: FillMode) -> None:
    """The cut is 12:00; the leaking decision was taken at the 00:00 close of that day (3 bars earlier).

    The truncated market has no daily close after that midnight, so the child's peek falls back to 'flat'
    there, while the long run peeks the next daily close. The held target of the 04:00, 08:00 and 12:00
    bars differs as well, so the 4h decisions record it.
    """
    market = _market_h4()
    midnight = datetime(2020, 2, 1, tzinfo=UTC)
    # find a day in February whose next daily close is higher, and cut at that day's 12:00
    day = next(
        ts
        for ts in (midnight + timedelta(days=i) for i in range(28))
        if ts in {b.ts for b in market.series(D1)} and _next_close_is_higher(market, ts)
    )
    cut = day + timedelta(hours=12)
    assert "decisions" in _prefix_diffs(market, cut, _leaky_for, SCHEDULE_X, TF_X, H4, mode)


def test_run_composite_on_a_4h_feed_returns_a_daily_series_for_both_fill_modes() -> None:
    market = _market_h4()
    baseline = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2020, 3, 1, tzinfo=UTC)
    for mode in (NO, MD):
        comp = CompositeStrategy(
            strategy_id="c",
            market=market,
            schedule=[(baseline, "D")],
            factory=_honest,
            timeframes={"D": D1},
            feed_timeframe=H4,
        )
        run = run_composite(market, comp, H4, mode, NORMAL_COSTS, baseline=baseline, end=end)
        assert run.ts[0] == baseline and run.ts[-1] == end
        assert len(run.ts) == 61  # one point per daily close, 2020-01-01 .. 2020-03-01 inclusive


# -- a child that is skipped for a while gives the same answer afterwards ------------------------------


class _Incremental:
    """A cumulative-sum SMA that catches up from the window it is given (the way the replay strategies do).

    It keeps state between calls, so a gap in the calls would corrupt it if the composite fed it anything
    other than the full causal history.
    """

    def __init__(self, n: int) -> None:
        self.id = "inc"
        self.warmup_bars = n
        self._n = n
        self._seen = 0
        self._sum = [0.0]

    def on_bar(self, window: BarWindow) -> TargetIntent:
        for bar in window.bars[self._seen :]:
            self._sum.append(self._sum[-1] + float(bar.close))
        self._seen = len(window.bars)
        sma = (self._sum[-1] - self._sum[-1 - self._n]) / self._n
        return TargetIntent(self.id, window.as_of, 1.0 if float(window.last.close) > sma else 0.0, "inc")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


def test_a_child_gives_the_same_answer_after_a_gap_as_when_consulted_every_day() -> None:
    market = make_market()
    gap_start, gap_end = datetime(2020, 2, 1, tzinfo=UTC), datetime(2020, 4, 15, tzinfo=UTC)
    schedule: Schedule = [(START, "A"), (gap_start, None), (gap_end, "A")]
    gapped = CompositeStrategy(
        strategy_id="g",
        market=market,
        schedule=schedule,
        factory=lambda _k: _Incremental(30),
        timeframes={"A": D1},
        feed_timeframe=D1,
    )
    continuous = CompositeStrategy(
        strategy_id="k",
        market=market,
        schedule=[(START, "A")],
        factory=lambda _k: _Incremental(30),
        timeframes={"A": D1},
        feed_timeframe=D1,
    )
    reference = SmaFilter(30)
    checked = 0
    for bar in market.series(D1):
        if not START <= bar.ts <= datetime(2020, 7, 1, tzinfo=UTC):
            continue
        window = market.window_to(D1, bar.ts)
        assert window is not None
        a, b = gapped.on_bar(window).target_weight, continuous.on_bar(window).target_weight
        if gap_start <= bar.ts < gap_end:
            assert a == 0.0  # flat while no point is scheduled
        else:
            assert a == b == reference.on_bar(window).target_weight, bar.ts
            checked += 1
    assert checked > 100
