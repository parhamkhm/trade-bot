"""Causality of the composite strategy on the real engine, and the S1 stop-out exemption (SPEC D-090, D-092).

Phase 3 is daily only (D-090): every composite runs on the 1d feed.

The earlier prefix test compared only the pooled RETURN series of a short and a long run. A leak that
changes the decision on the last bar of the short run (day N) has not yet changed any return there (the
order fills at the open of N+1), so a returns-only comparison cannot see it. These tests compare, bar by
bar, the target weight and the decision record INCLUDING day N (``harness.compare_prefix``), and they carry a
deliberately leaky child that must be flagged exactly when its peek changes the decision.

The second half covers S1 (D-092): no stop of its own, it follows its members' daily stopped states, and a
close where any member enters STOPPED trades to the full new target (no 25 % band, other members' moves
netted in), while every other change, an ordinary signal exit or a vol resize, keeps the 25 % band.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest

from tbot.backtest.result import NO_ACTION, ORDER, BacktestResult
from tbot.core.types import (
    Approved,
    Bar,
    BarWindow,
    MarketState,
    PortfolioView,
    RefusalReason,
    Refused,
    RiskDecision,
    Strategy,
    TargetIntent,
    Timeframe,
)
from tbot.strategies.sma_filter import SmaFilter
from tbot.validation.runs import (
    NORMAL_COSTS,
    BlendStrategy,
    CompositeStrategy,
    FillMode,
    Market,
    StopOutAware,
    StopOutExemptRiskManager,
    StrategyFactory,
    engine_composite,
)
from tests.sim_helpers import SYMBOL, lbank_like_filters
from tests.validation.harness import compare_prefix
from tests.validation.wf_fixtures import BOUNDARY, StopAwareSma, make_market

D1 = Timeframe.D1
NO, MD = FillMode.NEXT_OPEN, FillMode.MANUAL_DELAY
START = datetime(2020, 1, 1, tzinfo=UTC)

Schedule = list[tuple[datetime, str | None]]
FactoryFor = Callable[[Market], StrategyFactory]


# -- fixtures ------------------------------------------------------------------------------------


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


def _engine(market: Market, factory: StrategyFactory, schedule: Schedule, mode: FillMode) -> BacktestResult:
    """The composite on the engine exactly as ``run_composite`` wires it, but returning the full result."""
    comp = CompositeStrategy(strategy_id="c", market=market, schedule=schedule, factory=factory)
    return engine_composite(
        market, comp, mode, NORMAL_COSTS, baseline=START, end=market.series(D1)[-1].ts, from_anchor=True
    )


def _prefix_diffs(
    full: Market, cut: datetime, factory_for: FactoryFor, schedule: Schedule, mode: FillMode
) -> list[str]:
    """``compare_prefix`` of the run on ``full`` truncated at ``cut`` against the run on ``full``.

    ``factory_for(market)`` is called once per market, so a child that reads its market (the leaky one)
    sees exactly the data that run was given.
    """
    short_market = full.truncated(cut)
    assert short_market.series(D1)[-1].ts == cut
    short = _engine(short_market, factory_for(short_market), schedule, mode)
    long = _engine(full, factory_for(full), schedule, mode)
    assert short.equity[-1].ts == cut
    return compare_prefix(short, long)


def _honest_for(_market: Market) -> StrategyFactory:
    return _honest


SCHEDULE_D: Schedule = [(START, "A"), (BOUNDARY, "B")]


# -- daily composite: the cut day is compared ----------------------------------------------------


@pytest.mark.parametrize("mode", [NO, MD])
def test_leaky_child_is_flagged_exactly_when_its_peek_changes_the_decision(mode: FillMode) -> None:
    market = make_market()
    up, down = _pick_daily_cuts(market)
    flagged_up = _prefix_diffs(market, up, _leaky_for, SCHEDULE_D, mode)
    flagged_down = _prefix_diffs(market, down, _leaky_for, SCHEDULE_D, mode)
    assert "decisions" in flagged_up  # next close higher: long run says 1.0 on day N, the cut run says 0.0
    assert flagged_down == []  # next close not higher: both say 0.0, nothing to see


@pytest.mark.parametrize("mode", [NO, MD])
def test_leak_on_the_cut_day_is_invisible_to_a_returns_only_comparison(mode: FillMode) -> None:
    """Why the old test was not enough: through day N the equity of the leaky run is still identical."""
    market = make_market()
    up, _ = _pick_daily_cuts(market)
    short_market = market.truncated(up)
    short = _engine(short_market, _leaky_for(short_market), SCHEDULE_D, mode)
    long = _engine(market, _leaky_for(market), SCHEDULE_D, mode)
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
        assert _prefix_diffs(market, cut, _honest_for, SCHEDULE_D, mode) == [], cut


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
    assert "decisions" in _prefix_diffs(market, cut, _leaky_for, SCHEDULE_D, mode)


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
        strategy_id="g", market=market, schedule=schedule, factory=lambda _k: _Incremental(30)
    )
    continuous = CompositeStrategy(
        strategy_id="k", market=market, schedule=[(START, "A")], factory=lambda _k: _Incremental(30)
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


# -- S1 and the stop-out exemption (D-092) -------------------------------------------------------------

N_FLAT = 60
STOP_DAY_INDEX = 30  # the day member m0 is stopped out on
MEMBER_WEIGHT = 0.4
KEYS = ("m0", "m1", "m2", "m3", "m4")


def _flat_market(n: int = N_FLAT) -> Market:
    """A market whose price never moves, so the held weight stays at the entry weight (~0.40) and a band
    decision is a pure function of the target path. Daily bars only (the next-open fill needs no 1h feed)."""
    price = Decimal("20000.00")
    bars = [
        Bar(
            symbol=SYMBOL,
            timeframe=D1,
            ts=START + (i + 1) * D1.delta,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(10),
        )
        for i in range(n)
    ]
    return Market(SYMBOL, {D1: bars}, filters=lbank_like_filters())


class _Scripted:
    """A ``StopOutAware`` member with a scripted daily state.

    It targets ``MEMBER_WEIGHT`` until ``stop_ts``; from then on it is STOPPED and targets 0 (a stopped member
    contributes 0). ``stopped_out_on`` is True on ``stop_ts`` only when ``reports_stop`` is set. With
    ``stop_ts`` None the member never changes. ``weight`` is the level before the stop; ``move`` =
    ``(ts, new_weight)`` is an ordinary signal change (not a stop) applied from ``ts`` on.
    """

    warmup_bars = 1

    def __init__(
        self,
        stop_ts: datetime | None,
        *,
        reports_stop: bool,
        weight: float = MEMBER_WEIGHT,
        move: tuple[datetime, float] | None = None,
    ) -> None:
        self.id = "scripted"
        self._stop_ts = stop_ts
        self._reports = reports_stop
        self._weight = weight
        self._move = move
        self.windows: list[tuple[datetime, int, datetime]] = []  # (as_of, n bars, last bar close)

    def on_bar(self, window: BarWindow) -> TargetIntent:
        stopped = self._stop_ts is not None and window.as_of >= self._stop_ts
        level = self._weight
        if self._move is not None and window.as_of >= self._move[0]:
            level = self._move[1]
        return TargetIntent(self.id, window.as_of, 0.0 if stopped else level, "scripted")

    def stopped_out_on(self, window: BarWindow) -> bool:
        self.windows.append((window.as_of, len(window), window.last.ts))
        return self._reports and self._stop_ts is not None and window.as_of == self._stop_ts

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


def _blend(
    market: Market, member_for: Callable[[str], Strategy], keys: Sequence[str] = KEYS
) -> BlendStrategy:
    anchor = market.anchor
    members = [
        CompositeStrategy(strategy_id=k, market=market, schedule=[(anchor, k)], factory=member_for)
        for k in keys
    ]
    return BlendStrategy("S1", members)


def _scripted_factory(
    stop_ts: datetime | None,
    *,
    reports_stop: bool,
    weight: float = MEMBER_WEIGHT,
    m1_move: tuple[datetime, float] | None = None,
) -> Callable[[str], Strategy]:
    """m0 is the member that gets stopped out (when ``stop_ts``); the others never change, except that m1
    makes the ordinary signal change ``m1_move`` when given."""

    def make(key: str) -> Strategy:
        return _Scripted(
            stop_ts if key == "m0" else None,
            reports_stop=reports_stop,
            weight=weight,
            move=m1_move if key == "m1" else None,
        )

    return make


def _run_flat(
    market: Market, member_for: Callable[[str], Strategy], mode: FillMode = NO
) -> tuple[BacktestResult, BlendStrategy]:
    blend = _blend(market, member_for)
    result = engine_composite(
        market, blend, mode, NORMAL_COSTS, baseline=market.anchor, end=market.last_close
    )
    return result, blend


def _decision(result: BacktestResult, ts: datetime) -> tuple[float, str]:
    (d,) = [d for d in result.decisions if d.ts == ts]
    return d.target_weight, d.outcome


def _sells_after(result: BacktestResult, ts: datetime) -> list[object]:
    return [r for r in result.fills if r.fill.side.value.lower() == "sell" and r.fill.ts >= ts]


STOP_TS = START + (STOP_DAY_INDEX + 1) * D1.delta  # close of the stop-out day


def test_a_stop_out_driven_cut_of_the_blend_from_040_to_032_trades_without_the_band() -> None:
    """(a) 5 members at 0.40 each; m0 is stopped out on STOP_TS: the mean falls 0.40 -> 0.32 (a 20 % relative
    cut, inside the 25 % band) and the order is still sent."""
    market = _flat_market()
    result, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=True))
    before_ts = STOP_TS - D1.delta
    assert _decision(result, before_ts)[0] == pytest.approx(0.40)
    held = next(p.weight for p in result.equity if p.ts == STOP_TS)
    assert held == pytest.approx(0.40, abs=0.01)  # the band would see 0.32 vs ~0.40 and block it
    target, outcome = _decision(result, STOP_TS)
    assert target == pytest.approx(0.32) and outcome == ORDER
    sells = _sells_after(result, STOP_TS)
    assert len(sells) == 1  # exactly the exempt reduction (flat price, nothing else to rebalance)
    after = next(p.weight for p in result.equity if p.ts == STOP_TS + D1.delta)
    assert after == pytest.approx(0.32, abs=0.01)


def test_the_same_cut_from_an_ordinary_vol_resize_is_blocked_by_the_band() -> None:
    """(b) the identical 0.40 -> 0.32 target path, but m0 does NOT report a stop-out (a vol resize): the
    25 % band holds the position."""
    market = _flat_market()
    result, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=False))
    target, outcome = _decision(result, STOP_TS)
    assert target == pytest.approx(0.32) and outcome == NO_ACTION
    assert _sells_after(result, STOP_TS) == []
    assert all(p.weight == pytest.approx(0.40, abs=0.01) for p in result.equity if p.ts >= STOP_TS)


def test_an_ordinary_signal_exit_of_one_member_keeps_the_band_but_the_same_cut_by_a_stop_out_trades() -> None:
    """Netting rule, ordinary-exit case: 5 members at 0.5 (each contributes 0.1 to S1). m0 leaves by an
    ordinary signal exit: S1 0.5 -> 0.4 (a 20 % cut) is inside the 25 % band and is NOT traded. The same cut
    caused by m0 being stopped out IS traded."""
    market = _flat_market()
    ordinary, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=False, weight=0.5))
    assert _decision(ordinary, STOP_TS - D1.delta)[0] == pytest.approx(0.5)
    target, outcome = _decision(ordinary, STOP_TS)
    assert target == pytest.approx(0.4) and outcome == NO_ACTION
    assert _sells_after(ordinary, STOP_TS) == []
    assert all(p.weight == pytest.approx(0.5, abs=0.01) for p in _points_from(ordinary, STOP_TS))

    stopped, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=True, weight=0.5))
    target, outcome = _decision(stopped, STOP_TS)
    assert target == pytest.approx(0.4) and outcome == ORDER
    assert len(_sells_after(stopped, STOP_TS)) == 1
    after = next(p.weight for p in stopped.equity if p.ts == STOP_TS + D1.delta)
    assert after == pytest.approx(0.4, abs=0.01)


def _points_from(result: BacktestResult, ts: datetime) -> list[Any]:
    return [p for p in result.equity if p.ts >= ts]


def test_at_a_stop_out_close_the_other_members_moves_are_netted_into_one_trade_to_the_full_target() -> None:
    """Netting rule: at the close where m0 enters STOPPED, m1 also raises its weight by 0.2 (an ordinary
    change). S1 goes 0.40 -> (0 + 0.6 + 3 x 0.4) / 5 = 0.36: a 10 % net cut that the band alone would block.
    With the stop-out tag it trades to the full new target (0.36), the other member's move included."""
    market = _flat_market()
    move = (STOP_TS, 0.6)
    stopped, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=True, m1_move=move))
    target, outcome = _decision(stopped, STOP_TS)
    assert target == pytest.approx(0.36) and outcome == ORDER
    after = next(p.weight for p in stopped.equity if p.ts == STOP_TS + D1.delta)
    assert after == pytest.approx(0.36, abs=0.01)
    untagged, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=False, m1_move=move))
    target, outcome = _decision(untagged, STOP_TS)
    assert target == pytest.approx(0.36) and outcome == NO_ACTION


def test_the_exempt_manager_routes_on_the_tag_alone_whatever_the_direction() -> None:
    """A tagged intent goes to the band-0 manager whether it cuts, raises or keeps the weight (the net of all
    members' moves at a stop-out close); an untagged intent always takes the banded path."""
    calls: list[str] = []

    class _Rec:
        def __init__(self, name: str) -> None:
            self._name = name

        def evaluate(
            self,
            intent: TargetIntent,
            portfolio: PortfolioView,  # noqa: ARG002
            market: MarketState,  # noqa: ARG002
        ) -> RiskDecision:
            calls.append(self._name)
            return Refused(intent, RefusalReason.HALTED, self._name)  # a stand-in answer

    portfolio = PortfolioView(
        ts=START,
        cash_quote=Decimal(6000),
        base_qty=Decimal("0.2"),  # x 20000 = 4000 of 10000 equity = weight 0.40
        equity_quote=Decimal(10_000),
    )
    market = MarketState(symbol=SYMBOL, ts=START, last_price=Decimal(20_000), filters=lbank_like_filters())
    assert portfolio.weight(market.last_price) == pytest.approx(0.40)
    tagged = StopOutExemptRiskManager(_Rec("banded"), _Rec("unbanded"), lambda _ts: True)
    for target in (0.32, 0.44, 0.40):
        tagged.evaluate(TargetIntent("S1", START, target, "x"), portfolio, market)
    assert calls == ["unbanded"] * 3

    untagged = StopOutExemptRiskManager(_Rec("banded"), _Rec("unbanded"), lambda _ts: False)
    calls.clear()
    for target in (0.32, 0.44, 0.0):
        untagged.evaluate(TargetIntent("S1", START, target, "x"), portfolio, market)
    assert calls == ["banded"] * 3
    assert isinstance(Approved(TargetIntent("S1", START, 0.0, "x"), None), Approved)


@pytest.mark.parametrize("mode", [NO, MD])
def test_the_s1_run_has_no_stop_events_but_the_same_members_alone_do(mode: FillMode) -> None:
    """(c) S1 never asks for a stop: no StopEvent at all; the members run standalone do get stopped, so the
    absence is not just a quiet market."""
    market = make_market()
    keys = ("T-sma10", "T-sma20", "T-sma40")
    schedule: Schedule = [(market.anchor, "")]

    def members(key: str) -> Strategy:
        return StopAwareSma(int(key.removeprefix("T-sma")))

    blend = BlendStrategy(
        "S1",
        [
            CompositeStrategy(strategy_id=k, market=market, schedule=[(market.anchor, k)], factory=members)
            for k in keys
        ],
    )
    last = market.last_close
    s1 = engine_composite(market, blend, mode, NORMAL_COSTS, baseline=market.anchor, end=last)
    assert s1.stop_events == () or len(s1.stop_events) == 0
    assert not s1.config.enforce_stops
    assert s1.fills  # it did trade
    del schedule
    alone = CompositeStrategy(
        strategy_id="alone", market=market, schedule=[(market.anchor, keys[0])], factory=members
    )
    solo = engine_composite(market, alone, mode, NORMAL_COSTS, baseline=market.anchor, end=last)
    assert solo.config.enforce_stops
    assert len(solo.stop_events) > 0


def test_a_blend_refuses_to_ask_for_a_stop() -> None:
    market = _flat_market()
    blend = _blend(market, _scripted_factory(None, reports_stop=False))
    window = market.window_to(D1, STOP_TS)
    assert window is not None
    with pytest.raises(NotImplementedError, match="no intraday stop of its own"):
        blend.stop_price(window, Decimal(100))


def test_the_stop_out_tag_at_close_t_uses_only_bars_up_to_t() -> None:
    """(d) look-ahead, part 1: every window handed to ``stopped_out_on`` ends at the as-of close."""
    market = _flat_market()
    members: dict[str, _Scripted] = {}

    def make(key: str) -> Strategy:
        members[key] = _Scripted(STOP_TS if key == "m0" else None, reports_stop=True)
        return members[key]

    result, blend = _run_flat(market, make)
    assert len(result.decisions) == N_FLAT
    seen = members["m0"].windows
    assert [w[0] for w in seen] == [b.ts for b in market.series(D1)]  # consulted once per daily close
    for as_of, n_bars, last_ts in seen:
        assert last_ts == as_of  # the last bar of the window is the bar that just closed
        assert n_bars == (as_of - market.anchor) // D1.delta + 1  # nothing beyond it
    assert blend.stop_out_at(market.last_close) is False  # the tag is per close, not sticky


def test_the_stop_out_tag_survives_a_truncation_test_with_an_honest_flag() -> None:
    """(d) look-ahead, part 2: cutting the data at any day gives the same decisions up to the cut."""
    full = _flat_market()

    def make(key: str) -> Strategy:
        return _Scripted(STOP_TS if key == "m0" else None, reports_stop=True)

    cuts = [STOP_TS - D1.delta, STOP_TS, STOP_TS + D1.delta, STOP_TS + 5 * D1.delta]
    for cut in cuts:
        short_market = full.truncated(cut)
        short, _ = _run_flat(short_market, make)
        long, _ = _run_flat(full, make)
        assert compare_prefix(short, long) == [], cut


class _LeakyFlag(_Scripted):
    """Member whose stop-out flag peeks at tomorrow: True iff the market it was built on has a next bar.

    Its target path is the same as ``_Scripted`` with a stop at STOP_TS; only the flag cheats, and it
    cheats on EVERY day, so on the cut day of a truncated market it says False while the long one says True.
    """

    def __init__(self, market: Market, stop_ts: datetime | None) -> None:
        super().__init__(stop_ts, reports_stop=True)
        closes = [b.ts for b in market.series(D1)]
        self._has_next = {a for a, _ in pairwise(closes)}

    def stopped_out_on(self, window: BarWindow) -> bool:
        return window.as_of in self._has_next


def test_a_look_ahead_in_the_stop_out_flag_is_caught_by_the_prefix_comparison() -> None:
    """The truncation comparison has power over the new tag: a flag that peeks changes the decision on the
    cut day (the reduction of the blend trades in the long run and is band-blocked in the short one)."""
    full = _flat_market()

    def factory_for(market: Market) -> Callable[[str], Strategy]:
        return lambda key: _LeakyFlag(market, STOP_TS if key == "m0" else None)

    cut = STOP_TS  # on this day the target drops 0.40 -> 0.32: the exemption decides trade vs no trade
    short_market = full.truncated(cut)
    short, _ = _run_flat(short_market, factory_for(short_market))
    long, _ = _run_flat(full, factory_for(full))
    assert "decisions" in compare_prefix(short, long)
    assert _decision(short, cut)[1] == NO_ACTION and _decision(long, cut)[1] == ORDER


def test_a_stopped_member_contributes_zero_to_the_blend_on_every_later_day() -> None:
    """(e) the blend target is the mean over ALL members with the stopped one at 0, not the mean of the
    members still in a position."""
    market = _flat_market()
    result, _ = _run_flat(market, _scripted_factory(STOP_TS, reports_stop=True))
    for d in result.decisions:
        expected = MEMBER_WEIGHT if d.ts < STOP_TS else MEMBER_WEIGHT * 4 / 5
        assert d.target_weight == pytest.approx(expected), d.ts
    only_stopped = _blend(market, _scripted_factory(STOP_TS, reports_stop=True), keys=("m0",))
    window = market.window_to(D1, STOP_TS + D1.delta)
    assert window is not None
    assert only_stopped.on_bar(window).target_weight == 0.0  # a lone stopped member: flat


def test_a_member_without_stopped_out_on_is_rejected_loudly() -> None:
    """No silent state-based fallback: a member that cannot report a stop-out would hide a same-bar
    release (STOPPED -> FLAT on the stop-out bar)."""
    market = _flat_market()
    assert not isinstance(SmaFilter(30), StopOutAware)
    assert isinstance(StopAwareSma(30), StopOutAware)
    blend = _blend(market, lambda _k: SmaFilter(5))
    window = market.window_to(D1, market.last_close)
    assert window is not None
    with pytest.raises(TypeError, match=r"stopped_out_on.*D-092"):
        blend.on_bar(window)
