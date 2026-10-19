"""C4 volatility-compression breakout (SPEC section 7.2 C4, D-097): percentile gate, channel entry/exit, ties,
integer comparison, own signal state (starts flat, stays STOPPED until the exit fires, fresh entry later).

Synthetic path (``c25_support.linear_rows``): close_k = 100 + k, open = previous close, high = close,
low = open. Realised volatility then falls steadily with the price level, so on the ramp ``rv_t`` is the
lowest
of the trailing window (count = 1) and the channel is high = close: ``max(high_{t-20..t-1}) = close_{t-1}``,
``min(low_{t-20..t-1}) = close_{t-21}``, mid = ``close_{t-1} - 10``.
"""

from __future__ import annotations

import pytest

from tbot.core.types import Bar, Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.strategies.vol_breakout import VolBreakout, make
from tests.strategies.c25_support import (
    FLAT,
    LONG,
    STOPPED,
    Row,
    bars_from_rows,
    linear_bars,
    linear_rows,
    run_lengths,
    states_from,
    window,
)

D1 = Timeframe.D1
PCTS = [10, 20, 30]
FIRST = 385  # first tradable close: rv_{t-364} needs 21 closes ending at t-364 -> 365 + 20


def ramp_to(last_k: int) -> list[Row]:
    return [("100", "100", "100", "100"), *linear_rows(1, last_k)]


# ---- construction -------------------------------------------------------------------------------------


@pytest.mark.parametrize("pct", PCTS)
def test_registry_factories_and_warmups(pct: int) -> None:
    entry = next(e for e in PHASE3_ENTRIES if e.key == f"C4-p{pct}")
    strategy = load(entry)
    assert isinstance(strategy, VolBreakout)
    assert strategy.pct == pct and strategy.warmup_bars == FIRST and strategy.id == f"volbreakout_p{pct}_1d"
    assert validate_strategy(strategy) is strategy


def test_defaults_and_refusals() -> None:
    assert make(timeframe=D1).pct == 20
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=Timeframe.H4)
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, horizons=(1, 2, 3))
    with pytest.raises(TypeError, match="integer"):
        make(timeframe=D1, pct=20.5)
    with pytest.raises(TypeError, match="integer"):
        make(timeframe=D1, pct=True)
    with pytest.raises(ValueError, match="pct"):
        VolBreakout(pct=0)
    with pytest.raises(ValueError, match="pct"):
        VolBreakout(pct=101)


# ---- the percentile: hand-computed ----------------------------------------------------------------------


def test_percentile_count_is_none_until_365_rv_values_exist() -> None:
    bars = linear_bars(400)
    c4 = VolBreakout()
    c4.signal_long(window(bars))
    assert c4.percentile_count(383) is None  # rv_0..rv_19 do not exist: only 364 values in the window
    assert c4.percentile_count(384) == 1  # on the ramp rv falls steadily: only rv_t itself is <= rv_t
    assert c4.percentile_count(399) == 1


def test_percentile_ties_count_so_a_constant_series_has_the_full_count() -> None:
    bars = bars_from_rows([("100", "100", "100", "100")] * 400)  # rv = 0 everywhere: 365 values, all ties
    c4 = VolBreakout()
    c4.signal_long(window(bars))
    assert c4.percentile_count(384) == 365


@pytest.mark.parametrize(
    ("pct", "last_passing", "first_failing"), [(10, 36, 37), (20, 73, 74), (30, 109, 110)]
)
def test_the_percentile_comparison_is_done_in_integers(
    pct: int, last_passing: int, first_failing: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pct_t <= p / 100  <=>  count x 100 <= p x 365: 36.5 / 73 / 109.5 of 365 values
    bars = linear_bars(FIRST)
    c4 = VolBreakout(pct=pct)
    assert c4.entry_holds(window(bars))  # breakout holds, count = 1
    for count, expected in ((last_passing, True), (first_failing, False)):
        monkeypatch.setattr(c4, "percentile_count", lambda _i, count=count: count)
        assert c4._entry_at(len(bars) - 1) is expected, (pct, count)


# ---- hand-computed channel rules -------------------------------------------------------------------------


def last_bar_variant(close: str, *, low: str | None = None, high: str | None = None) -> list[Bar]:
    """The ramp to close 483 (k = 383), then bar 384 (n = 385) closing at ``close``."""
    rows = ramp_to(383)
    rows.append(("483", high or max("483", close), low or min("483", close), close))
    return bars_from_rows(rows)


def test_entry_needs_a_close_strictly_above_the_prior_20_highs() -> None:
    c4 = VolBreakout()
    assert c4.entry_holds(window(last_bar_variant("484"))) is True  # 484 > max high = 483
    assert c4.entry_holds(window(last_bar_variant("483"))) is False  # equal: not above
    assert c4.entry_holds(window(last_bar_variant("482"))) is False


def test_the_channel_uses_the_prior_20_bars_and_not_the_bar_itself() -> None:
    # a high on the decision bar must not raise the channel it is compared with
    bars = last_bar_variant("484", high="999")
    assert VolBreakout().entry_holds(window(bars)) is True
    # a high exactly 20 bars back (bar 364) still counts, one more bar back (bar 363) does not
    rows = ramp_to(383)
    rows[364] = ("463", "600", "463", "464")  # bar 364: high 600
    rows.append(("483", "484", "483", "484"))
    assert VolBreakout().entry_holds(window(bars_from_rows(rows))) is False  # t = 384, bar 364 is t - 20
    rows[364] = ("463", "464", "463", "464")
    rows[363] = ("462", "600", "462", "463")  # t - 21: outside the channel
    assert VolBreakout().entry_holds(window(bars_from_rows(rows))) is True


def test_exit_needs_a_close_strictly_below_the_channel_midpoint() -> None:
    c4 = VolBreakout()
    # max high 483, min low 463 (= low of bar 364 = close 463... bar 364 opens 463): mid = 473
    assert c4.exit_holds(window(last_bar_variant("473"))) is False  # equal: not below
    assert c4.exit_holds(window(last_bar_variant("472"))) is True
    assert c4.exit_holds(window(last_bar_variant("484"))) is False


def test_entry_needs_the_percentile_gate_even_when_the_breakout_holds() -> None:
    # same ramp, but the last 20 closes are noisy: rv_t is the highest of the window -> count 365 -> no entry
    rows = ramp_to(364)
    price = 464
    for j in range(20):  # alternating -9 / +13 closes, ending above the ramp's prior highs
        nxt = price + (-9 if j % 2 == 0 else 13)
        rows.append((str(price), str(max(price, nxt)), str(min(price, nxt)), str(nxt)))
        price = nxt
    bars = bars_from_rows(rows)
    c4 = VolBreakout(pct=30)
    assert len(bars) == FIRST
    assert c4.entry_holds(window(bars)) is False
    assert c4.percentile_count(FIRST - 1) == 365
    assert float(bars[-1].close) > max(float(b.high) for b in bars[-21:-1])  # the breakout part does hold


# ---- the own signal state (D-097) ------------------------------------------------------------------------


@pytest.mark.parametrize("pct", PCTS)
def test_first_tradable_close_enters_when_the_entry_condition_holds_and_not_before(pct: int) -> None:
    bars = linear_bars(400)
    c4 = make(timeframe=D1, pct=pct)
    for n in (1, 100, 250, 383, 384):  # the breakout part holds on every one of these bars
        assert c4.state(window(bars, n)) is FLAT and not c4.signal_long(window(bars, n)), n
    assert c4.state(window(bars, FIRST)) is LONG
    assert c4.on_bar(window(bars, FIRST)).target_weight > 0.0
    with pytest.raises(ValueError, match="needs 385 bars"):
        c4.on_bar(window(bars, 384))


@pytest.mark.parametrize("pct", PCTS)
def test_signal_state_starts_flat_at_the_first_tradable_close(pct: int) -> None:
    # D-097 (b): bar 380 carries a spike high of 484 = the close of the first tradable bar, so the entry
    # condition
    # is false there (close is not above the prior highs) while the exit condition is false too (484 is
    # above the
    # midpoint 474). A state "carried in" from the earlier bars, on which the breakout part held, would be
    # long
    # and could not leave; the replayed state starts flat and enters only at the next close that satisfies
    # entry.
    rows = ramp_to(450)
    rows[380] = ("479", "484", "479", "480")
    bars = bars_from_rows(rows)
    c4 = make(timeframe=D1, pct=pct)
    first = window(bars, FIRST)
    assert c4.entry_holds(first) is False and c4.exit_holds(first) is False
    assert c4.state(first) is FLAT and not c4.signal_long(first)
    assert c4.on_bar(first).target_weight == 0.0
    nxt = window(bars, FIRST + 1)  # close 485 > max high 484
    assert c4.entry_holds(nxt) is True and c4.state(nxt) is LONG
    assert run_lengths(states_from(c4, bars, FIRST)) == [FLAT, LONG]


def d097_rows() -> list[Row]:
    """Entry at the first tradable close; a wick stops it out on the next bar while the entry condition holds
    again; the ramp goes on (STOPPED); a crash fires the exit (FLAT); a jump up and a fresh ramp re-enter once
    the 20-day volatility window has forgotten the crash and the jump."""
    rows = ramp_to(384)  # n = 385: close 484, the first tradable close
    rows.append(("484", "485", "480", "485"))  # stop 484 - 3 = 481; low 480 -> stopped out; entry holds again
    rows += linear_rows(386, 390)  # closes 486..490: the signal state stays long, the position STOPPED
    rows.append(("490", "490", "400", "450"))  # crash: close 450 < mid 480 -> exit fires
    rows.append(("450", "700", "450", "700"))  # jump above every old high: breakout, but rv is huge
    rows += [(str(700 + j), str(701 + j), str(700 + j), str(701 + j)) for j in range(0, 40)]
    return rows


@pytest.mark.parametrize("pct", PCTS)
def test_stopped_stays_stopped_until_the_exit_fires_and_a_later_close_satisfies_entry(pct: int) -> None:
    bars = bars_from_rows(d097_rows())
    c4 = make(timeframe=D1, pct=pct)
    seq = states_from(c4, bars, FIRST)
    assert seq[0] is LONG  # n = 385
    assert seq[1] is STOPPED  # n = 386: the stop-out bar; entry holds again at its close
    assert c4.entry_holds(window(bars, 386)) is True  # ... and the position still does not re-enter
    assert seq[2:7] == [STOPPED] * 5  # n = 387..391: the entry condition keeps holding, STOPPED stays
    assert all(c4.entry_holds(window(bars, n)) for n in range(386, 392))
    assert c4.exit_holds(window(bars, 392)) is True and seq[7] is FLAT  # n = 392: the crash fires the exit
    assert c4.entry_holds(window(bars, 392)) is False  # not entering on the same close
    # the jump (n = 393) is above every old high, but the volatility gate is closed until the window
    # forgets it
    assert c4.entry_holds(window(bars, 393)) is False
    reentry = next(FIRST + k for k, s in enumerate(seq) if k > 7 and s is LONG)
    assert reentry == 413
    assert run_lengths(seq) == [LONG, STOPPED, FLAT, LONG]
    assert all(not c4.entry_holds(window(bars, n)) for n in range(392, reentry))
    assert c4.entry_holds(window(bars, reentry)) is True


def test_a_stopped_position_does_not_re_enter_just_because_entry_holds_again() -> None:
    # the plain position machine (signal true -> stays STOPPED) is not enough: here the signal state is long
    # throughout n = 386..391 even though entry_holds is also true each time; no re-entry may appear
    bars = bars_from_rows(d097_rows())
    c4 = VolBreakout()
    assert [c4.state(window(bars, n)) for n in range(386, 392)] == [STOPPED] * 6
    assert [c4.stopped_out_on(window(bars, n)) for n in range(385, 393)] == [False, True] + [False] * 6


def test_an_ordinary_exit_is_not_a_stop_out() -> None:
    rows = ramp_to(384)  # entry at n = 385, stop 484 - 3 = 481
    rows.append(("484", "600", "484", "485"))  # a spike high lifts the channel midpoint to 535
    rows.append(
        ("485", "486", "485", "486")
    )  # close 486 < 535: the exit fires, the low 485 is above the stop
    bars = bars_from_rows(rows)
    c4 = VolBreakout()
    assert run_lengths(states_from(c4, bars, FIRST)) == [LONG, FLAT]
    assert c4.exit_holds(window(bars)) is True and not c4.stopped_out_on(window(bars))


def test_incremental_calls_equal_a_fresh_replay() -> None:
    bars = bars_from_rows(d097_rows())
    incremental = VolBreakout()
    for n in range(FIRST, len(bars) + 1, 3):
        w = window(bars, n)
        fresh = VolBreakout()
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.state(w) is fresh.state(w)
