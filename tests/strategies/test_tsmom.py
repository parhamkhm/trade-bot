"""C2 time-series momentum (SPEC section 7.2 C2): sign votes, the mean > 0 rule, ties, stop and re-entry.

Hand-computed cases use horizons (1, 2, 3) on the S0 baseline of ``test_s0_sma100`` (150 flat bars at 100,
then
bar 150 = open 100, high 101, low 100, close 101, ATR20 = 0.05; for an entry open of 101.5 the stop is
101.35):
at bar 150 the three signs are +1 (101 against 100 one, two and three bars back).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tbot.core.types import Bar, Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.strategies.tsmom import Tsmom, make
from tests.sim_helpers import make_bar
from tests.strategies.c25_support import bars_from_rows, linear_bars
from tests.strategies.test_s0_sma100 import FLAT, LONG, STOPPED, baseline, flat_prefix, states, window

D1 = Timeframe.D1
GRID = [(15, 45, 90), (30, 90, 180), (60, 180, 360)]
WARMUPS = {(15, 45, 90): 151, (30, 90, 180): 181, (60, 180, 360): 361}


def tail_bars(*closes: str) -> list[Bar]:
    """150 - len(closes) + 1 flat bars at 100 followed by ``closes`` (open = previous close), 151 bars in
    all."""
    prefix = flat_prefix(151 - len(closes))
    bars = list(prefix)
    prev = "100"
    for c in closes:
        bars.append(make_bar(len(bars), prev, c))
        prev = c
    return bars


# ---- construction -------------------------------------------------------------------------------------


@pytest.mark.parametrize("horizons", GRID)
def test_registry_factories_and_warmups(horizons: tuple[int, int, int]) -> None:
    entry = next(e for e in PHASE3_ENTRIES if e.key == "C2-(" + ",".join(map(str, horizons)) + ")")
    strategy = load(entry)
    assert isinstance(strategy, Tsmom)
    assert strategy.horizons == horizons and strategy.warmup_bars == WARMUPS[horizons]
    assert strategy.id == "tsmom_" + "_".join(map(str, horizons)) + "_1d"
    assert strategy.vol_target == 0.30
    assert validate_strategy(strategy) is strategy


def test_defaults_and_refusals() -> None:
    assert make(timeframe=D1).horizons == (30, 90, 180)
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=Timeframe.H4)
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, pct=10)
    with pytest.raises(ValueError, match="three horizons"):
        Tsmom((10, 20))
    with pytest.raises(ValueError, match="three horizons"):
        Tsmom((0, 5, 10))


# ---- hand-computed sign votes ------------------------------------------------------------------------------


def test_all_three_signs_up_is_long_and_enters_on_the_first_tradable_close() -> None:
    c2 = Tsmom((1, 2, 3))
    w = window(baseline())
    assert c2.horizon_signs(w) == (1, 1, 1) and c2.signal_long(w)
    assert c2.state(w) is LONG
    intent = c2.on_bar(w)
    assert intent.target_weight == 1.0 and intent.strategy_id == "tsmom_1_2_3_1d"
    with pytest.raises(ValueError, match="needs 151 bars"):
        c2.on_bar(window(baseline(), 150))


def test_two_up_one_down_is_long_because_the_mean_is_positive() -> None:
    # closes ..., 110, 95, 96, 100: one back +1 (100 > 96), two back +1 (100 > 95), three back -1 (100 < 110)
    bars = tail_bars("110", "95", "96", "100")
    c2 = Tsmom((1, 2, 3))
    w = window(bars)
    assert c2.horizon_signs(w) == (1, 1, -1)  # mean 1/3 > 0
    assert c2.signal_long(w) and c2.state(w) is LONG


def test_one_up_two_down_is_flat_because_the_mean_is_negative() -> None:
    bars = tail_bars(
        "110", "108", "104", "105"
    )  # one back +1 (105 > 104), two back -1 (< 108), three back -1 (< 110)
    c2 = Tsmom((1, 2, 3))
    w = window(bars)
    assert c2.horizon_signs(w) == (1, -1, -1)
    assert not c2.signal_long(w) and c2.state(w) is FLAT


def test_a_mean_of_exactly_zero_is_flat() -> None:
    # closes 100 (three back), 103 (two back), 99 (one back), 100: signs +1 (100 > 99), -1 (100 < 103), 0
    # (100 = 100)
    bars = tail_bars("100", "103", "99", "100")
    c2 = Tsmom((1, 2, 3))
    w = window(bars)
    assert c2.horizon_signs(w) == (1, -1, 0)  # sum 0 -> mean 0
    assert not c2.signal_long(w) and c2.state(w) is FLAT
    assert c2.on_bar(w).target_weight == 0.0


def test_sign_zero_counts_as_zero_in_the_mean() -> None:
    # signs (+1, 0, 0): mean 1/3 > 0 -> long
    bars = tail_bars(
        "100", "100", "99", "100"
    )  # one back: 100 > 99 +1; two back: 100 = 100 -> 0; three back 100 -> 0
    c2 = Tsmom((1, 2, 3))
    assert c2.horizon_signs(window(bars)) == (1, 0, 0)
    assert c2.signal_long(window(bars))


def test_any_positive_move_is_plus_one_and_any_negative_move_is_minus_one() -> None:
    up = tail_bars("100", "100", "100", "100.01")
    down = tail_bars("100", "100", "100", "99.99")
    c2 = Tsmom((1, 2, 3))
    assert c2.horizon_signs(window(up)) == (1, 1, 1)
    assert c2.horizon_signs(window(down)) == (-1, -1, -1)


def test_distinct_horizons_look_back_the_stated_number_of_days() -> None:
    # horizon 5 compares with the close five bars back; horizon 2 with two bars back
    bars = tail_bars(
        "90", "100", "100", "100", "100", "101"
    )  # closes ..., 90(i-5), 100, 100, 100, 100, 101(i)
    c2 = Tsmom((2, 5, 7))
    w = window(bars)
    assert c2.horizon_signs(w) == (1, 1, 1)  # 101 > 100 (2 back), 101 > 90 (5 back), 101 > 100 (7 back)
    bars2 = tail_bars("110", "100", "100", "100", "100", "101")
    assert c2.horizon_signs(window(bars2)) == (1, -1, 1)  # 101 < 110 only five back


# ---- stop, STOPPED, re-entry -------------------------------------------------------------------------------


def test_long_survives_the_entry_bar_and_the_stop_is_open_minus_3_atr() -> None:
    c2 = Tsmom((1, 2, 3))
    w = window(baseline(("101.5", "102", "101.4", "102")))
    assert c2.state(w) is LONG
    assert c2.active_stop(w) == Decimal("101.35")
    assert c2.stop_price(window(baseline()), Decimal("101.5")) == Decimal("101.35")


def test_stopped_stays_stopped_while_the_signal_is_true_then_flat_then_a_fresh_entry() -> None:
    c2 = Tsmom((1, 2, 3))
    bars = baseline(
        ("101.5", "102", "101.0", "102"),  # low 101.0 <= 101.35 -> stop-out; signs (+, +, +): still long
        ("102", "103.5", "102", "103"),  # still true -> stays STOPPED
        ("103", "103", "98", "99"),  # signs (-, -, -) -> STOPPED -> FLAT
        ("99", "106", "99", "105"),  # signs (+, +, +) again -> a fresh entry, LONG
    )
    assert states(c2, bars, 152) == [STOPPED, STOPPED, FLAT, LONG]
    assert c2.on_bar(window(bars)).target_weight > 0.0
    assert c2.on_bar(window(bars, 152)).target_weight == 0.0


def test_stopped_out_on_is_true_only_on_the_stop_out_bar() -> None:
    c2 = Tsmom((1, 2, 3))
    bars = baseline(
        ("101.5", "102", "101.0", "102"), ("102", "103.5", "102", "103"), ("103", "103", "98", "99")
    )
    assert [c2.stopped_out_on(window(bars, n)) for n in range(151, len(bars) + 1)] == [
        False,
        True,
        False,
        False,
    ]


def test_an_ordinary_signal_exit_is_not_a_stop_out() -> None:
    c2 = Tsmom((1, 2, 3))
    wide = [make_bar(i, "100", "100", high="105", low="95") for i in range(150)] + [
        make_bar(150, "100", "101", high="105", low="95")
    ]
    after = [
        *wide,
        make_bar(151, "101.5", "99", high="102", low="99"),
    ]  # stop 71.5 is far away; signs (-, -, -)
    assert c2.state(window(wide)) is LONG
    assert c2.state(window(after)) is FLAT and not c2.stopped_out_on(window(after))


# ---- first tradable close per grid point -------------------------------------------------------------------


@pytest.mark.parametrize("horizons", GRID)
def test_first_tradable_close(horizons: tuple[int, int, int]) -> None:
    first = WARMUPS[horizons]
    bars = linear_bars(first + 40)  # a steady rise: every sign is +1 as soon as it is defined
    c2 = make(timeframe=D1, horizons=horizons)
    assert c2.warmup_bars == first
    assert c2.state(window(bars, first - 1)) is FLAT
    assert c2.state(window(bars, first)) is LONG
    assert c2.on_bar(window(bars, first)).target_weight > 0.0


def test_a_falling_market_never_enters() -> None:
    bars = bars_from_rows(
        [(str(1000 - k + 1), str(1000 - k + 1), str(1000 - k), str(1000 - k)) for k in range(400)]
    )
    c2 = make(timeframe=D1, horizons=(30, 90, 180))
    assert {c2.state(window(bars, n)) for n in range(181, 401)} == {FLAT}


def test_the_signal_before_the_longest_horizon_is_never_traded() -> None:
    # the (60, 180, 360) strategy has no close_{t-360} before close #361; it must not trade even if two
    # signs are +1
    bars = linear_bars(400)
    c2 = make(timeframe=D1, horizons=(60, 180, 360))
    assert {c2.state(window(bars, n)) for n in range(1, 361)} == {FLAT}


def test_incremental_calls_equal_a_fresh_replay() -> None:
    bars = linear_bars(260)
    incremental = make(timeframe=D1, horizons=(30, 90, 180))
    for n in range(181, 261, 7):
        w = window(bars, n)
        fresh = make(timeframe=D1, horizons=(30, 90, 180))
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.horizon_signs(w) == fresh.horizon_signs(w)
