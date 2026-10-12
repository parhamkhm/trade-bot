"""S0 ``sma100_1d`` (SPEC section 7.1): transitions, sizing, stop, warm-up, replay cache, truncation.

Hand-computed baseline used by most cases: 150 flat bars at 100 (open = high = low = close), then bar 150 (the
151st close, the first tradable one) opens 100, high 101, low 100, closes 101. Then

* SMA100 at bar 150 = (99 x 100 + 101) / 100 = 100.01 < 101 -> the signal is true on the warm-up boundary;
* TR_150 = max(101 - 100, |101 - 100|, |100 - 100|) = 1 and every earlier TR is 0 -> ATR20 = 1 / 20 = 0.05;
* for an entry open (bar 151) of 101.5 the stop is 101.5 - 3 x 0.05 = 101.35 exactly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.strategies.replay import PositionState, ReplayStrategy
from tbot.strategies.s0_sma100 import S0Sma100, make
from tests.sim_helpers import make_bar
from tests.strategies.replay_checks import assert_no_lookahead
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars

D1 = Timeframe.D1
FLAT, LONG, STOPPED = PositionState.FLAT, PositionState.LONG, PositionState.STOPPED
needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")


# ---- helpers (also imported by test_donchian) -------------------------------------------------------


def bar(i: int, o: str, h: str, low: str, c: str) -> Bar:
    return make_bar(i, o, c, high=h, low=low)


def flat_prefix(n: int = 150) -> list[Bar]:
    return [bar(i, "100", "100", "100", "100") for i in range(n)]


def baseline(*tail: tuple[str, str, str, str]) -> list[Bar]:
    """150 flat bars, the entry-signal bar 150, then the given (open, high, low, close) bars 151, 152, ..."""
    bars = [*flat_prefix(), bar(150, "100", "101", "100", "101")]
    bars.extend(bar(151 + k, *ohlc) for k, ohlc in enumerate(tail))
    return bars


def window(bars: Sequence[Bar], n: int | None = None) -> BarWindow:
    return BarWindow.of(bars if n is None else bars[:n])


def states(strategy: ReplayStrategy, bars: Sequence[Bar], first: int = 151) -> list[PositionState]:
    """State after the close of every bar from window length ``first`` on, one growing window at a time."""
    return [strategy.state(window(bars, n)) for n in range(first, len(bars) + 1)]


# ---- construction ----------------------------------------------------------------------------------


def test_defaults_and_registry_factory() -> None:
    s0 = make(timeframe=D1)
    assert (s0.id, s0.warmup_bars, s0.vol_target, s0.sma_n) == ("sma100_1d", 151, 0.30, 100)
    entry = next(e for e in PHASE3_ENTRIES if e.key == "S0")
    loaded = load(entry)
    assert isinstance(loaded, S0Sma100) and loaded.id == "sma100_1d"
    assert validate_strategy(loaded) is loaded


def test_robustness_variants_have_distinct_ids_and_warmups() -> None:
    assert (
        make(timeframe=D1, sma_n=200).id == "sma200_1d" and make(timeframe=D1, sma_n=200).warmup_bars == 200
    )
    assert make(timeframe=D1, sma_n=50).warmup_bars == 151  # sigma_hat still needs 151 closes
    assert make(timeframe=D1, vol_target=0.2).id == "sma100_1d_v20"
    assert make(timeframe=D1, vol_target=0.4).id == "sma100_1d_v40"


@pytest.mark.parametrize("tf", [Timeframe.H4, Timeframe.H1])
def test_only_daily_bars_are_accepted(tf: Timeframe) -> None:
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=tf)


def test_unknown_parameters_and_bad_values_are_refused() -> None:
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, lookbacks=1.0)
    with pytest.raises(ValueError, match="vol_target"):
        make(timeframe=D1, vol_target=0.0)
    with pytest.raises(ValueError, match="sma_n"):
        S0Sma100(sma_n=1)


# ---- hand-computed transitions (SPEC 7.1) --------------------------------------------------------------


def test_warmup_is_151_closes() -> None:
    s0 = S0Sma100()
    bars = baseline()
    with pytest.raises(ValueError, match="needs 151 bars"):
        s0.on_bar(window(bars, 150))
    assert s0.on_bar(window(bars, 151)).target_weight == 1.0


def test_no_entry_before_the_first_tradable_close_then_enter_on_it() -> None:
    # signal true from bar 100 on (rising by 1 per day) but the strategy may only trade from close #151
    closes = [100.0] * 100 + [100.0 + k for k in range(1, 61)]
    bars = [make_bar(i, f"{closes[max(i - 1, 0)]:.2f}", f"{c:.2f}") for i, c in enumerate(closes)]
    s0 = S0Sma100()
    assert s0.state(window(bars, 120)) is FLAT  # before the warm-up the replay still runs, but cannot trade
    assert s0.state(window(bars, 150)) is FLAT
    assert s0.state(window(bars, 151)) is LONG
    assert s0.on_bar(window(bars, 151)).target_weight > 0.0


def test_flat_to_long_at_first_tradable_close_with_full_weight_in_calm_markets() -> None:
    s0 = S0Sma100()
    w = window(baseline())
    assert s0.state(w) is LONG
    intent = s0.on_bar(w)
    assert intent.target_weight == 1.0  # sigma_hat is about 0.048, so 0.30 / sigma_hat is capped at 1
    assert intent.strategy_id == "sma100_1d" and intent.ts == w.as_of
    assert s0.active_stop(w) is None  # the stop is fixed on the entry bar t+1, which has not been seen yet


def test_long_survives_the_entry_bar_and_the_stop_is_fixed_at_open_minus_3_atr() -> None:
    s0 = S0Sma100()
    w = window(baseline(("101.5", "102", "101.4", "102")))  # low 101.4 > stop 101.35
    assert s0.state(w) is LONG
    assert s0.active_stop(w) == Decimal("101.35")
    assert s0.stop_price(window(baseline()), Decimal("101.5")) == Decimal("101.35")


def test_stop_out_on_the_entry_bar_while_the_close_stays_above_the_sma() -> None:
    s0 = S0Sma100()
    # low 101.0 <= 101.35 on bar t+1; close 102 > SMA100 (100.03) -> STOPPED, target 0, stays STOPPED
    bars = baseline(("101.5", "102", "101.0", "102"), ("102", "103.5", "102", "103"))
    assert states(s0, bars, 152) == [STOPPED, STOPPED]
    assert s0.on_bar(window(bars, 152)).target_weight == 0.0
    assert s0.on_bar(window(bars)).target_weight == 0.0
    assert s0.active_stop(window(bars)) is None


def test_stop_triggers_when_low_equals_the_stop() -> None:
    s0 = S0Sma100()
    assert s0.state(window(baseline(("101.5", "102", "101.35", "102")))) is STOPPED


def test_stop_out_bars_own_close_releases_stopped_to_flat_and_a_fresh_close_re_enters() -> None:
    s0 = S0Sma100()
    # stop-out and the close 99.5 <= SMA100 (100.005) on the same bar: STOPPED -> FLAT on that close
    bars = baseline(("101.5", "101.6", "99", "99.5"), ("99.5", "105", "99.5", "105"))
    assert states(s0, bars, 152) == [FLAT, LONG]  # then the next close 105 > SMA is a fresh entry signal
    assert s0.on_bar(window(bars, 152)).target_weight == 0.0
    assert s0.on_bar(window(bars)).target_weight > 0.0


def test_re_entry_needs_a_close_at_or_below_the_sma_after_a_stop_out() -> None:
    s0 = S0Sma100()
    bars = baseline(
        ("101.5", "102", "101.0", "102"),  # stopped on the entry bar, close above the SMA
        ("102", "103.5", "102", "103"),  # still above: stays STOPPED
        ("103", "103", "98", "99"),  # close 99 <= SMA100: STOPPED -> FLAT
        ("99", "106", "99", "105"),  # fresh close above the SMA: LONG again
    )
    assert states(s0, bars, 152) == [STOPPED, STOPPED, FLAT, LONG]


def test_long_to_flat_when_the_close_falls_to_the_sma_without_a_stop() -> None:
    # A close at the SMA with the stop still untouched needs a wide stop: ranging bars (high 105, low 95) give
    # TR = 10 everywhere, ATR20 = 10 and, for an entry open of 101.5, a stop of 71.5 far below the SMA100.
    s0 = S0Sma100()
    wide = [bar(i, "100", "105", "95", "100") for i in range(150)] + [bar(150, "100", "105", "95", "101")]
    assert s0.state(window(wide)) is LONG  # SMA100 = 100.01 < 101
    exit_bar = bar(151, "101.5", "102", "99", "99")  # low 99 > 71.5; close 99 <= SMA100 (100.005)
    after_entry = [*wide, bar(151, "101.5", "102", "99.5", "101")]
    assert s0.state(window(after_entry)) is LONG and s0.active_stop(window(after_entry)) == Decimal("71.5")
    flat = [*wide, exit_bar]
    assert s0.state(window(flat)) is FLAT
    assert s0.active_stop(window(flat)) is None
    assert s0.on_bar(window(flat)).target_weight == 0.0


def test_the_stop_also_applies_after_the_entry_bar_and_is_not_trailing() -> None:
    s0 = S0Sma100()
    bars = baseline(
        ("101.5", "104", "101.4", "103"),  # LONG, stop 101.35 (a trailing stop would have moved up)
        ("103", "103", "101.3", "102"),  # low 101.3 <= 101.35, close 102 > SMA -> STOPPED
    )
    assert states(s0, bars, 152) == [LONG, STOPPED]
    assert s0.active_stop(window(bars, 152)) == Decimal("101.35")


def test_stop_floor_is_one_cent() -> None:
    s0 = S0Sma100()
    assert s0.stop_price(window(baseline()), Decimal("0.10")) == Decimal("0.01")  # 0.10 - 0.15 < 0.01
    # replay: TR_150 = 999, ATR20 = 49.95, open 100 - 149.85 < 0.01 -> stop 0.01; low 1 never reaches it
    bars = [*flat_prefix(), bar(150, "100", "1000", "1", "500"), bar(151, "100", "500", "1", "500")]
    assert s0.state(window(bars)) is LONG
    assert s0.active_stop(window(bars)) == Decimal("0.01")


def test_stop_price_needs_21_bars() -> None:
    with pytest.raises(ValueError, match="needs 21 bars"):
        S0Sma100().stop_price(window(flat_prefix(), 20), Decimal(100))


def test_sizing_is_vol_target_over_sigma_hat() -> None:
    # constant 4 % log returns for 150 days: sigma_hat = 0.04 x sqrt(365) exactly, w = 0.30 / sigma_hat
    closes = [100.0 * math.exp(0.04 * k) for k in range(151)]
    bars = [make_bar(i, repr(closes[max(i - 1, 0)]), repr(c)) for i, c in enumerate(closes)]
    intent = S0Sma100().on_bar(window(bars))
    assert intent.target_weight == pytest.approx(0.30 / (0.04 * math.sqrt(365)), rel=1e-9)
    assert intent.target_weight < 1.0
    assert S0Sma100(vol_target=0.2).on_bar(window(bars)).target_weight == pytest.approx(
        0.20 / (0.04 * math.sqrt(365)), rel=1e-9
    )


def test_sma_variant_uses_its_own_window() -> None:
    # SMA 200 needs 200 closes: bar 150's close is above SMA100 (the S0 baseline) but there is no SMA200 yet
    s200 = make(timeframe=D1, sma_n=200, anchor=None)  # unit fixtures do not start at the anchor
    bars = [*baseline(), *[bar(151 + k, "101", "102", "101", "102") for k in range(60)]]
    assert s200.state(window(bars, 199)) is FLAT
    assert s200.state(window(bars, 200)) is LONG  # close 102 > SMA200 and close #200 is tradable


# ---- the replay cache ---------------------------------------------------------------------------------


def wicky_bars(n: int, seed: int, every: int = 37) -> list[Bar]:
    """Seeded random walk where every ``every``-th bar has a long lower wick (a stop-out trigger)."""
    bars = synthetic_bars(n, seed=seed)
    return [
        replace(b, low=(b.low * Decimal("0.85")).quantize(Decimal("0.01"))) if i % every == every - 1 else b
        for i, b in enumerate(bars)
    ]


def test_incremental_calls_equal_a_fresh_replay_at_every_bar() -> None:
    bars = wicky_bars(420, seed=5)
    incremental = S0Sma100()
    for n in range(151, len(bars) + 1):
        w = window(bars, n)
        fresh = S0Sma100()
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.state(w) is fresh.state(w)
        assert incremental.active_stop(w) == fresh.active_stop(w)


def test_cache_is_discarded_when_history_changes_or_the_window_goes_backwards() -> None:
    bars = wicky_bars(300, seed=9)
    reference = S0Sma100()
    used = S0Sma100()
    used.on_bar(window(bars, 300))
    for n in (200, 151):  # shorter window than the cache
        w = window(bars, n)
        assert used.on_bar(w).target_weight == reference.on_bar(w).target_weight
        assert used.state(w) is reference.state(w)
    altered = list(bars)
    altered[120] = replace(bars[120], close=bars[120].close + 1, high=bars[120].high + 1)  # inside the prefix
    w = window(altered, 300)
    assert used.on_bar(w).target_weight == S0Sma100().on_bar(w).target_weight
    assert used.state(w) is S0Sma100().state(w)


# ---- boundary: exact equality --------------------------------------------------------------------------


def equality_series(last_close: str) -> list[Bar]:
    """Ranging bars (high = close + 5, low = close - 5) whose last close equals SMA100 exactly when 100.

    Closes are 100 except bars 52 and 53 (98) and bar 150 (104). SMA100 at bar 150 is (100 + 98 + 98 +
    96 x 100 + 104) / 100 = 100 < 104 (entry signal); at bar 151 it is (98 + 98 + 96 x 100 + 104 + c) / 100,
    which is exactly c for c = 100. The ranges make ATR20 = 10, so the stop (open 104 - 30 = 74) stays
    untouched
    and only the SMA rule can act on bar 151.
    """
    closes = [100.0] * 150
    closes[52] = closes[53] = 98.0
    closes.append(104.0)
    bars = [
        make_bar(i, str(closes[max(i - 1, 0)]), str(c), high=str(c + 5), low=str(c - 5))
        for i, c in enumerate(closes)
    ]
    c = Decimal(last_close)
    bars.append(make_bar(151, "104", last_close, high=str(max(Decimal(109), c)), low="95"))
    return bars


def test_close_equal_to_the_sma_exits_a_long_position() -> None:
    s0 = S0Sma100()
    bars = equality_series("100")
    assert s0.state(window(bars, 151)) is LONG  # close 104 > SMA100 100 on bar 150
    assert s0.state(window(bars)) is FLAT  # close 100 == SMA100 100 -> LONG -> FLAT (no stop involved)
    assert s0.on_bar(window(bars)).target_weight == 0.0


def test_close_just_above_the_sma_keeps_the_position_long() -> None:
    s0 = S0Sma100()
    bars = equality_series("100.01")  # SMA100 = 100.0001 < close
    assert s0.state(window(bars)) is LONG
    assert s0.active_stop(window(bars)) == Decimal("74")


def test_close_equal_to_the_sma_does_not_enter_a_flat_strategy() -> None:
    s0 = S0Sma100()
    bars = flat_prefix(160)  # every close and the SMA100 are 100 exactly
    assert s0.state(window(bars)) is FLAT
    assert s0.on_bar(window(bars)).target_weight == 0.0


# ---- truncation: nothing at bar > N may change a decision at bar <= N ------------------------------------


def test_no_lookahead_on_a_synthetic_series_with_every_state() -> None:
    bars = wicky_bars(900, seed=3)
    seen = assert_no_lookahead(S0Sma100, bars)
    assert seen == {FLAT, LONG, STOPPED}, f"synthetic series does not exercise every state: {seen}"


@needs_store
def test_no_lookahead_on_real_binance_btcusdt_1d_before_the_holdout() -> None:
    bars = real_bars("BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC))
    assert bars[0].ts == datetime(2018, 1, 2, tzinfo=UTC) and bars[-1].ts < datetime(2025, 10, 1, tzinfo=UTC)
    assert len(bars) > 2800
    seen = assert_no_lookahead(lambda: make(timeframe=D1), bars)
    assert {FLAT, LONG} <= seen
    # first tradable close is #151, i.e. 2018-06-01 00:00 UTC
    assert bars[150].ts == datetime(2018, 6, 1, tzinfo=UTC)


# ---- stopped_out_on (the S1 blend's stop-out tag, SPEC D-092) ----------------------------------------------


def stop_flags(strategy: ReplayStrategy, bars: Sequence[Bar], first: int = 151) -> list[bool]:
    return [strategy.stopped_out_on(window(bars, n)) for n in range(first, len(bars) + 1)]


def test_stopped_out_on_is_true_only_on_the_stop_out_bar() -> None:
    bars = baseline(
        ("101.5", "102", "101.0", "102"),  # stopped on the entry bar, close above the SMA
        ("102", "103.5", "102", "103"),  # still STOPPED, but no new stop-out
        ("103", "103", "98", "99"),  # STOPPED -> FLAT by the close: no stop-out either
    )
    assert stop_flags(S0Sma100(), bars) == [False, True, False, False]


def test_stopped_out_on_includes_a_same_bar_release_to_flat() -> None:
    bars = baseline(("101.5", "101.6", "99", "99.5"), ("99.5", "105", "99.5", "105"))
    s0 = S0Sma100()
    assert stop_flags(s0, bars, 152) == [True, False]
    assert s0.state(window(bars, 152)) is FLAT  # the flag is the only trace of that stop-out


def test_stopped_out_on_is_false_for_an_ordinary_exit() -> None:
    wide = [bar(i, "100", "105", "95", "100") for i in range(150)] + [bar(150, "100", "105", "95", "101")]
    flat = [*wide, bar(151, "101.5", "102", "99", "99")]
    assert stop_flags(S0Sma100(), flat) == [False, False]


def test_stopped_out_on_is_causal_and_equals_a_fresh_replay_at_every_bar() -> None:
    bars = wicky_bars(420, seed=5)
    incremental = S0Sma100()
    seen = False
    for n in range(151, len(bars) + 1):
        w = window(bars, n)
        flag = incremental.stopped_out_on(w)
        assert flag == S0Sma100().stopped_out_on(w)  # a fresh replay of bars 0..n only gives the same flag
        seen = seen or flag
    assert seen, "the wicky series must contain at least one stop-out"
