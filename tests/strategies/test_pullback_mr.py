"""C5 RSI(2) pullback (SPEC section 7.2 C5, D-097): Cutler RSI2, SMA200 gate, exit only on RSI2 > 70, ties,
own signal state (starts flat, stays STOPPED until the exit fires, fresh entry on a later close).

Synthetic path (``c25_support.linear_rows``): close_k = 100 + k, open = previous close; on the ramp every
RSI2 is
100 (no down move) and the close is far above its SMA200, so only the dip rows below can trigger entries.
"""

from __future__ import annotations

import pytest

from tbot.core.types import Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.strategies.pullback_mr import PullbackMr, make
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
XS = [5, 10, 15]
FIRST = 200  # first tradable close: SMA200 needs 200 closes


def closes_to_rows(first_open: float, closes: list[float]) -> list[Row]:
    """Rows with open = previous close and high / low = max / min of the two."""
    rows: list[Row] = []
    prev = first_open
    for c in closes:
        rows.append((f"{prev:g}", f"{max(prev, c):g}", f"{min(prev, c):g}", f"{c:g}"))
        prev = c
    return rows


def ramp_then(*closes: float) -> list[Row]:
    """The linear ramp up to close 297 (k = 197; 198 bars), then the given closes."""
    return [("100", "100", "100", "100"), *linear_rows(1, 197), *closes_to_rows(297, list(closes))]


# ---- construction -------------------------------------------------------------------------------------


@pytest.mark.parametrize("x", XS)
def test_registry_factories_and_warmups(x: int) -> None:
    entry = next(e for e in PHASE3_ENTRIES if e.key == f"C5-x{x}")
    strategy = load(entry)
    assert isinstance(strategy, PullbackMr)
    assert (
        strategy.rsi_entry == x and strategy.warmup_bars == FIRST and strategy.id == f"pullback_rsi2_x{x}_1d"
    )
    assert validate_strategy(strategy) is strategy


def test_defaults_and_refusals() -> None:
    assert make(timeframe=D1).rsi_entry == 10
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=Timeframe.H4)
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, pct=10)
    with pytest.raises(TypeError, match="number"):
        make(timeframe=D1, rsi_entry="10")
    with pytest.raises(ValueError, match="rsi_entry"):
        PullbackMr(rsi_entry=0)
    with pytest.raises(ValueError, match="rsi_entry"):
        PullbackMr(rsi_entry=101)


# ---- hand-computed entry rule: RSI2 < x, strictly ----------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "up", "tie_down", "below_down"), [(5, 1, 19, 20), (10, 1, 9, 10), (15, 3, 17, 18)]
)
def test_entry_needs_rsi2_strictly_below_x(x: int, up: int, tie_down: int, below_down: int) -> None:
    # changes (+up, -down): mean U = up / 2, mean D = down / 2; RSI2 = 100 - 100 / (1 + up / down) = x for
    # tie_down
    c5 = PullbackMr(rsi_entry=x)
    for down, expected in ((tie_down, False), (below_down, True)):
        bars = bars_from_rows(ramp_then(297 + up, 297 + up - down))  # close ~280 >> SMA200 ~ 200
        assert len(bars) == FIRST
        assert c5.entry_holds(window(bars)) is expected, (x, down)


# ---- the SMA200 gate ---------------------------------------------------------------------------------------


def sma_gate_rows(first_close: float) -> list[Row]:
    """Closes: ``first_close``, then 196 x 100, p = 109, q = 110, c = 100. RSI2 = 100 - 100 / (1 + 0.5 / 5)
    = 9.09;
    SMA200 = (first_close + 19600 + 109 + 110 + 100) / 200 = 100 when ``first_close`` is 81."""
    closes = [first_close, *([100.0] * 196), 109.0, 110.0, 100.0]
    return closes_to_rows(first_close, closes)


@pytest.mark.parametrize("x", [10, 15])
def test_entry_needs_the_close_strictly_above_sma200(x: int) -> None:
    c5 = PullbackMr(rsi_entry=x)
    assert c5.entry_holds(window(bars_from_rows(sma_gate_rows(80.0)))) is True  # SMA 99.995 < 100
    assert c5.entry_holds(window(bars_from_rows(sma_gate_rows(81.0)))) is False  # SMA = 100 = close: a tie
    assert c5.entry_holds(window(bars_from_rows(sma_gate_rows(82.0)))) is False  # SMA 100.005 > 100


def test_the_rsi_gate_applies_too_at_x_5() -> None:
    # same bars: RSI2 = 9.09 is not < 5 even though the close is above the SMA200
    assert PullbackMr(rsi_entry=5).entry_holds(window(bars_from_rows(sma_gate_rows(80.0)))) is False


# ---- the exit rule: RSI2 > 70, nothing else ----------------------------------------------------------------


def test_exit_needs_rsi2_strictly_above_70() -> None:
    c5 = PullbackMr()
    tie = bars_from_rows(
        closes_to_rows(100, [100.0] * 197 + [100.0, 107.0, 104.0])
    )  # changes +7, -3 -> RSI2 = 70.0
    assert c5.exit_holds(window(tie)) is False
    below = bars_from_rows(closes_to_rows(100, [100.0] * 197 + [100.0, 107.0, 103.0]))  # +7, -4 -> 63.6
    assert c5.exit_holds(window(below)) is False
    above = bars_from_rows(closes_to_rows(100, [100.0] * 197 + [100.0, 107.0, 105.0]))  # +7, -2 -> 77.8
    assert c5.exit_holds(window(above)) is True
    rising = bars_from_rows(closes_to_rows(100, [100.0] * 198 + [101.0, 102.0]))  # no down move -> RSI2 = 100
    assert c5.exit_holds(window(rising)) is True


def test_a_close_below_sma200_does_not_exit_a_long_signal() -> None:
    # enter on the dip, then keep falling far below the SMA200 with RSI2 low: no exit, the signal state
    # stays long
    rows = ramp_then(299, 297, 250, 150, 120)  # bar 200 enters; closes 150, 120 are below the SMA200 (~205)
    bars = bars_from_rows(rows)
    c5 = PullbackMr(rsi_entry=10)
    w = window(bars)
    assert c5.exit_holds(w) is False and c5.signal_long(w) is True


# ---- the own signal state (D-097) --------------------------------------------------------------------------


@pytest.mark.parametrize("x", XS)
def test_first_tradable_close_on_a_pure_ramp_is_flat_since_rsi2_is_100(x: int) -> None:
    bars = linear_bars(260)
    c5 = make(timeframe=D1, rsi_entry=x)
    assert {c5.state(window(bars, n)) for n in range(1, 261)} == {FLAT}
    with pytest.raises(ValueError, match="needs 200 bars"):
        c5.on_bar(window(bars, 199))
    assert c5.on_bar(window(bars, FIRST)).target_weight == 0.0


@pytest.mark.parametrize("x", XS)
def test_signal_state_starts_flat_at_the_first_tradable_close(x: int) -> None:
    # D-097 (b): at the first tradable close (n = 200) RSI2 = 50 (changes +2, -2) and close 297 > SMA200:
    # neither
    # the entry (RSI2 < x) nor the exit (RSI2 > 70) holds. Nothing is carried in from the warm-up bars, so the
    # state is flat -- the strategy does not become long merely because the trend filter is satisfied.
    bars = bars_from_rows(ramp_then(299, 297))
    assert len(bars) == FIRST
    c5 = make(timeframe=D1, rsi_entry=x)
    w = window(bars)
    assert c5.entry_holds(w) is False and c5.exit_holds(w) is False
    assert c5.state(w) is FLAT and c5.signal_long(w) is False and c5.on_bar(w).target_weight == 0.0
    assert all(c5.signal_long(window(bars, n)) is False for n in range(1, FIRST + 1))


def d097_rows() -> list[Row]:
    """n = 200: RSI2 50 (flat). Bar 200 (n = 201): dip -> entry. Bar 201: wick below the stop (STOPPED)
    while the
    entry condition holds again. Bar 202: still holds. Bar 203: the bounce fires the exit (RSI2 75). Bar 204:
    neither condition. Bar 205 (n = 206): a new dip -> a fresh entry."""
    closes = [299.0, 297.0, 250.0, 240.0, 230.0, 260.0, 250.0, 225.0]
    rows = ramp_then(*closes)
    rows[-5] = rows[-5][0], rows[-5][1], "100", rows[-5][3]  # bar 201 (close 240): low 100 hits the stop
    return rows


@pytest.mark.parametrize("x", XS)
def test_stopped_stays_stopped_until_the_exit_fires_and_a_later_close_satisfies_entry(x: int) -> None:
    bars = bars_from_rows(d097_rows())
    assert len(bars) == 206
    c5 = make(timeframe=D1, rsi_entry=x)
    seq = states_from(c5, bars, FIRST)  # n = 200 ... 206
    assert seq == [FLAT, LONG, STOPPED, STOPPED, FLAT, FLAT, LONG]
    # the entry condition itself holds on the stop-out bar and the one after, yet the position stays STOPPED
    assert c5.entry_holds(window(bars, 202)) is True and c5.entry_holds(window(bars, 203)) is True
    assert c5.exit_holds(window(bars, 202)) is False and c5.exit_holds(window(bars, 203)) is False
    # the exit fires at n = 204 (RSI2 = 75) -- not an entry on that same close
    assert c5.exit_holds(window(bars, 204)) is True and c5.entry_holds(window(bars, 204)) is False
    assert c5.entry_holds(window(bars, 205)) is False  # n = 205: neither
    assert c5.entry_holds(window(bars, 206)) is True  # a later close satisfies the entry condition
    assert run_lengths(seq) == [FLAT, LONG, STOPPED, FLAT, LONG]
    assert [c5.stopped_out_on(window(bars, n)) for n in range(201, 207)] == [
        False,
        True,
        False,
        False,
        False,
        False,
    ]


def test_the_stop_is_three_atr20_below_the_entry_open() -> None:
    from decimal import Decimal

    # ATR20 at bar 200 = (17 x 1 + 2 + 2 + 47) / 20 = 3.4 (true ranges of bars 181..200) -> 250 - 10.2
    bars = bars_from_rows(d097_rows())
    assert PullbackMr().stop_price(window(bars, 201), Decimal("250")) == Decimal("239.8")


def test_incremental_calls_equal_a_fresh_replay() -> None:
    bars = bars_from_rows(d097_rows())
    incremental = PullbackMr()
    for n in range(FIRST, len(bars) + 1):
        w = window(bars, n)
        fresh = PullbackMr()
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.state(w) is fresh.state(w)
