"""C1 Donchian ensemble (SPEC section 7.2 C1): channel hysteresis, 2-of-3 vote, stop and re-entry, truncation.

Hand-computed channel cases use lookbacks (2, 3, 4) with bars given as (high, low, close); the position-level
cases reuse the S0 baseline of ``test_s0_sma100`` (150 flat bars at 100, then bar 150 = open 100, high 101,
low 100, close 101, so ATR20 = 0.05 and, for an entry open of 101.5, the stop is 101.35).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.core.types import Bar, Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.donchian import DonchianEnsemble, make
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tests.sim_helpers import make_bar
from tests.strategies.replay_checks import assert_no_lookahead
from tests.strategies.test_s0_sma100 import (
    FLAT,
    LONG,
    STOPPED,
    baseline,
    flat_prefix,
    needs_store,
    states,
    wicky_bars,
    window,
)
from tests.validation.data import real_bars

D1 = Timeframe.D1
GRID = [(10, 28, 50), (20, 55, 100), (40, 110, 200)]
Lookbacks = tuple[int, int, int]


def hlc_bars(rows: list[tuple[str, str, str]]) -> list[Bar]:
    """Bars from (high, low, close); the open equals the close (only high / low / close matter for C1)."""
    return [make_bar(i, c, c, high=h, low=lo) for i, (h, lo, c) in enumerate(rows)]


def channels(lookbacks: Lookbacks, rows: list[tuple[str, str, str]]) -> list[tuple[bool, ...]]:
    c1 = DonchianEnsemble(lookbacks)
    bars = hlc_bars(rows)
    return [c1.channel_states(window(bars, n)) for n in range(1, len(bars) + 1)]


# ---- construction ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("lookbacks", "warmup"), [(GRID[0], 151), (GRID[1], 151), (GRID[2], 201)])
def test_registry_factories_and_warmups(lookbacks: Lookbacks, warmup: int) -> None:
    entry = next(e for e in PHASE3_ENTRIES if e.key == "C1-(" + ",".join(map(str, lookbacks)) + ")")
    strategy = load(entry)
    assert isinstance(strategy, DonchianEnsemble)
    assert strategy.lookbacks == lookbacks and strategy.warmup_bars == warmup
    assert strategy.id == "donchian_" + "_".join(map(str, lookbacks)) + "_1d"
    assert validate_strategy(strategy) is strategy


def test_defaults_and_refusals() -> None:
    assert make(timeframe=D1).lookbacks == (20, 55, 100)
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=Timeframe.H4)
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, horizons=(1, 2, 3))
    with pytest.raises(ValueError, match="three lookbacks"):
        DonchianEnsemble((10, 20))
    with pytest.raises(ValueError, match="three lookbacks"):
        DonchianEnsemble((0, 5, 10))


# ---- hand-computed channel logic -----------------------------------------------------------------------

ROWS = [
    ("10", "8", "9"),  # i0
    ("11", "9", "10"),  # i1
    ("12", "10", "11.5"),  # i2
    ("12", "10", "11"),  # i3
    ("13", "11", "12.5"),  # i4
    ("12", "9", "10"),  # i5
]


def test_channel_turns_long_on_a_close_above_the_prior_highs_and_keeps_state_in_between() -> None:
    got = channels((2, 3, 4), ROWS)
    # i0, i1: no channel has n prior bars yet -> flat
    assert got[0] == (False, False, False) and got[1] == (False, False, False)
    # i2: n=2 -> U = max(10, 11) = 11 < close 11.5 -> long; n=3 and n=4 are undefined
    assert got[2] == (True, False, False)
    # i3: n=2 -> U = 12, L = 9, M = 10.5, close 11 is inside (not > U, not < M) -> keeps long;
    #     n=3 -> U = max(10, 11, 12) = 12 > close 11 -> stays flat
    assert got[3] == (True, False, False)
    # i4: close 12.5 > U for n=2 (highs 12, 12), n=3 (highs 11, 12, 12) and n=4 (highs 10, 11, 12, 12)
    assert got[4] == (True, True, True)


def test_channel_turns_flat_only_below_the_mid_channel() -> None:
    got = channels((2, 3, 4), ROWS)
    # i5: n=2 -> U = 13, L = 10, M = 11.5 and close 10 < M -> flat; n=3 -> L = 10, M = 11.5 -> flat;
    #     n=4 -> U = 13, L = min(9, 10, 10, 11) = 9, M = 11, close 10 < 11 -> flat
    assert got[5] == (False, False, False)
    # the same bar closing at 11.6 is above every mid-channel (11.5, 11.5, 11) and below every upper
    # -> all keep long
    inside = channels((2, 3, 4), [*ROWS[:5], ("12", "10", "11.6")])
    assert inside[5] == (True, True, True)


def test_the_vote_needs_two_of_three_channels() -> None:
    # the old high of 20 at i0 stops the n=4 channel from breaking out at i4 while n=2 and n=3 do
    rows = [("20", "8", "9"), ("11", "9", "10"), ("12", "10", "11"), ("12", "10", "11"), ("13", "11", "12.9")]
    got = channels((2, 3, 4), rows)
    assert got[2] == (False, False, False)  # n=2: U = max(20, 11) = 20 -> no breakout
    assert got[4] == (True, True, False)  # n=2: U = 12, n=3: U = 12 -> long; n=4: U = 20, M = 14 -> flat
    assert sum(got[4]) == 2


# ---- hand-computed position logic (vote, stop, hysteresis, fresh re-entry) ----------------------------


def test_enters_on_the_first_tradable_close_when_the_vote_is_true() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    w = window(baseline())  # close 101 > every prior high (100) -> all three channels long
    assert c1.channel_states(w) == (True, True, True)
    assert c1.state(w) is LONG
    assert c1.on_bar(w).target_weight == 1.0  # sigma_hat is about 0.048, so 0.30 / sigma_hat is capped at 1
    with pytest.raises(ValueError, match="needs 151 bars"):
        c1.on_bar(window(baseline(), 150))


def test_two_of_three_channels_keep_the_strategy_long_while_one_turns_flat() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    bars = baseline(
        ("101.5", "104", "101.4", "103"),  # all channels stay long; stop 101.35 untouched (low 101.4)
        # n=1: M = (104 + 101.4) / 2 = 102.7 > close 102.2 -> flat;
        # n=2, n=3: M = (104 + 100) / 2 = 102 -> keep
        ("103", "103", "101.5", "102.2"),
    )
    assert c1.channel_states(window(bars)) == (False, True, True)
    assert states(c1, bars, 152) == [LONG, LONG]
    assert c1.active_stop(window(bars)) == Decimal("101.35")


def test_all_channels_flat_exits_and_a_new_breakout_after_a_stop_out_is_a_fresh_entry() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    bars = baseline(
        ("101.5", "104", "101.4", "103"),
        ("103", "103", "101.5", "102.2"),  # (False, True, True) -> still LONG
        # low 100.4 <= 101.35 -> stop-out; closes below M1 = 102.25, M2 = 102.7, M3 = 102
        # -> every channel flat
        ("102.2", "102.5", "100.4", "100.5"),
        ("100.6", "106", "100.6", "105"),  # close 105 is above all three upper channels (102.5, 103, 104)
    )
    assert c1.channel_states(window(bars, 153)) == (False, True, True)
    assert c1.channel_states(window(bars, 154)) == (False, False, False)
    assert states(c1, bars, 152) == [LONG, LONG, FLAT, LONG]
    assert (
        c1.active_stop(window(bars)) is None
    )  # the new stop is fixed on its entry bar, which is not seen yet


def test_no_re_entry_while_the_entry_condition_stays_true_after_a_stop_out() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    bars = baseline(
        (
            "101.5",
            "102",
            "101.0",
            "102",
        ),  # stop-out on the entry bar (low 101.0 <= 101.35); close above U = 101
        ("102", "103.5", "102", "103"),  # the vote is still 3 of 3 and never went false -> remains STOPPED
        ("103", "104.5", "103", "104"),
    )
    assert states(c1, bars, 152) == [STOPPED, STOPPED, STOPPED]
    assert c1.on_bar(window(bars)).target_weight == 0.0


def test_stop_price_and_floor_follow_the_common_rules() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    assert c1.stop_price(window(baseline()), Decimal("101.5")) == Decimal("101.35")
    assert c1.stop_price(window(baseline()), Decimal("0.10")) == Decimal("0.01")


# ---- first tradable close per grid point (SPEC 7.2 eligibility table) --------------------------------------


@pytest.mark.parametrize(("lookbacks", "first_close"), [(GRID[0], 151), (GRID[1], 151), (GRID[2], 201)])
def test_first_tradable_close(lookbacks: Lookbacks, first_close: int) -> None:
    closes = [100.0 + k for k in range(260)]  # a steady rise: every channel is long as soon as it is defined
    bars = [make_bar(i, f"{closes[max(i - 1, 0)]:.2f}", f"{c:.2f}") for i, c in enumerate(closes)]
    c1 = DonchianEnsemble(lookbacks)
    assert c1.state(window(bars, first_close - 1)) is FLAT
    assert c1.state(window(bars, first_close)) is LONG
    assert c1.on_bar(window(bars, first_close)).target_weight > 0.0


# ---- the replay cache ----------------------------------------------------------------------------------


@pytest.mark.parametrize("lookbacks", GRID)
def test_incremental_calls_equal_a_fresh_replay(lookbacks: Lookbacks) -> None:
    bars = wicky_bars(480, seed=11)
    incremental = DonchianEnsemble(lookbacks)
    for n in range(incremental.warmup_bars, len(bars) + 1, 3):
        w = window(bars, n)
        fresh = DonchianEnsemble(lookbacks)
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.channel_states(w) == fresh.channel_states(w)
        assert incremental.active_stop(w) == fresh.active_stop(w)


# ---- boundary: exact equality --------------------------------------------------------------------------


def test_close_equal_to_the_upper_channel_does_not_turn_the_channel_long() -> None:
    rows = [("10", "8", "9"), ("11", "9", "10"), ("12", "10", "11")]  # n=2 at i2: U = max(10, 11) = 11
    assert channels((2, 3, 4), rows)[2] == (False, False, False)  # close 11 == U: not a breakout
    above = [*rows[:2], ("12", "10", "11.01")]
    assert channels((2, 3, 4), above)[2] == (True, False, False)


def test_close_equal_to_the_mid_channel_keeps_the_channel_state() -> None:
    # i2: n=2 turns long; at i3 U = 12, L = 9, M = 10.5 for n=2
    head = [("10", "8", "9"), ("11", "9", "10"), ("12", "10", "11.5")]
    assert channels((2, 3, 4), [*head, ("12", "10", "10.5")])[3][0] is True  # close == M: keeps long
    assert channels((2, 3, 4), [*head, ("12", "10", "10.49")])[3][0] is False  # just below M: flat


def test_a_flat_strategy_does_not_enter_on_a_close_equal_to_every_upper_channel() -> None:
    c1 = DonchianEnsemble((1, 2, 3))
    bars = [*flat_prefix(), make_bar(150, "100", "100")]  # 150 flat bars at 100: U = 100 for every n
    assert c1.channel_states(window(bars)) == (False, False, False)
    assert c1.state(window(bars)) is FLAT


# ---- truncation -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("lookbacks", GRID)
def test_no_lookahead_on_a_synthetic_series(lookbacks: Lookbacks) -> None:
    bars = wicky_bars(900, seed=3)
    seen = assert_no_lookahead(lambda: DonchianEnsemble(lookbacks), bars)
    assert {FLAT, LONG} <= seen, seen


@needs_store
@pytest.mark.parametrize("lookbacks", GRID)
def test_no_lookahead_on_real_binance_btcusdt_1d_before_the_holdout(lookbacks: Lookbacks) -> None:
    bars = real_bars("BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC))
    assert bars[-1].ts < datetime(2025, 10, 1, tzinfo=UTC)
    seen = assert_no_lookahead(lambda: make(timeframe=D1, lookbacks=lookbacks), bars)
    assert LONG in seen
