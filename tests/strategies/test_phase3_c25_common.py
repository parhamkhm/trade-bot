"""Cross-checks for the eleven Phase 3 C2-C5 trials (registry T4-T14): parameters, anchoring, warm-up,
sizing and
stop parity with S0, the D-097 signal-state machine, and the look-ahead (truncation) check on synthetic and on
real pre-holdout data.

No performance number is computed anywhere here: the real-data test compares decisions of a full replay with
decisions of prefix replays (state, weight, stop), nothing else.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.indicators import atr_simple, sigma_hat_kernel
from tbot.strategies.hysteresis import HysteresisStrategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry, load
from tbot.strategies.replay import HISTORY_START, ReplayStrategy, stop_level
from tbot.strategies.s0_sma100 import S0Sma100
from tests.strategies.c25_support import FLAT, LONG, STOPPED, cyclic_wicky_bars, window
from tests.strategies.replay_checks import assert_no_lookahead
from tests.validation.data import find_parquet_root, real_bars

D1 = Timeframe.D1
C25 = [e for e in PHASE3_ENTRIES if e.key.startswith(("C2-", "C3-", "C4-", "C5-"))]
KEYS = [e.key for e in C25]
EXPERIMENTS = Path(__file__).resolve().parents[2] / "research" / "EXPERIMENTS.md"
needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")
FIRST_CLOSE = datetime(2018, 1, 2, tzinfo=UTC)  # bar 0 of an anchored series closes here


def replay_strategy(entry: Phase3Entry) -> ReplayStrategy:
    strategy = load(entry)
    assert isinstance(strategy, ReplayStrategy)
    return strategy


def experiments_rows() -> dict[str, list[str]]:
    """Registry key -> the cells of its row in research/EXPERIMENTS.md (read-only)."""
    out: dict[str, list[str]] = {}
    for line in EXPERIMENTS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > 5 and re.fullmatch(r"T\d+", cells[0]):
            found = re.search(r"`([^`]+)`", cells[3])
            if found:
                out[found.group(1)] = cells
    return out


def expected_param_text(entry: Phase3Entry) -> str:
    ((name, value),) = entry.params.items()
    return {
        "horizons": f"horizons {value}",
        "fast_spans": f"fast spans {value}",
        "pct": f"pct = {value}",
        "rsi_entry": f"rsi_entry = {value}",
    }[name]


# ---- registry <-> strategy <-> EXPERIMENTS -----------------------------------------------------------------


def test_there_are_eleven_trials_t4_to_t14() -> None:
    assert len(C25) == 11
    assert [k.split("-")[0] for k in KEYS] == ["C2"] * 3 + ["C3"] * 2 + ["C4"] * 3 + ["C5"] * 3


@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_params_equal_the_registry_entry_and_the_experiments_row(entry: Phase3Entry) -> None:
    strategy = replay_strategy(entry)
    attribute = {"horizons": "horizons", "fast_spans": "fast_spans", "pct": "pct", "rsi_entry": "rsi_entry"}
    for name, value in entry.params.items():
        assert getattr(strategy, attribute[name]) == value, (entry.key, name)
    rows = experiments_rows()
    assert entry.key in rows, f"{entry.key} is not logged in research/EXPERIMENTS.md"
    cells = rows[entry.key]
    assert cells[4].startswith(expected_param_text(entry)), (entry.key, cells[4])
    # the first tradable close of the row is the close of bar (warmup_bars - 1) of the anchored series
    found = re.search(r"first tradable close (\d{4}-\d{2}-\d{2}) 00:00 UTC", cells[9])
    assert found, cells[9]
    expected = (FIRST_CLOSE + timedelta(days=strategy.warmup_bars - 1)).strftime("%Y-%m-%d")
    assert found.group(1) == expected, (entry.key, strategy.warmup_bars)


def test_the_eleven_ids_are_distinct_and_the_rows_are_all_logged() -> None:
    ids = [replay_strategy(e).id for e in C25]
    assert len(set(ids)) == 11
    assert set(KEYS) <= set(experiments_rows())


@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_registry_built_strategies_are_anchored_and_refuse_a_late_window(entry: Phase3Entry) -> None:
    strategy = replay_strategy(entry)
    assert strategy.anchor == HISTORY_START
    bars = cyclic_wicky_bars(60, seed=1)
    assert strategy.state(BarWindow.of(bars)) is FLAT  # the anchored window is accepted
    with pytest.raises(ValueError, match="must start at the anchor"):
        replay_strategy(entry).state(BarWindow.of(bars[1:]))


@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_every_trial_is_flat_with_zero_weight_before_its_first_tradable_close(entry: Phase3Entry) -> None:
    strategy = replay_strategy(entry)
    bars = cyclic_wicky_bars(strategy.warmup_bars + 5, seed=2)
    points = strategy.trace(BarWindow.of(bars))
    assert all(
        p.state is FLAT and p.weight == 0.0 and p.stop is None for p in points[: strategy.warmup_bars - 1]
    )
    with pytest.raises(ValueError, match=f"needs {strategy.warmup_bars} bars"):
        strategy.on_bar(window(bars, strategy.warmup_bars - 1))


# ---- sizing and stop are S0's ------------------------------------------------------------------------------


@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_sizing_and_stop_are_the_s0_daily_rules(entry: Phase3Entry) -> None:
    bars = cyclic_wicky_bars(1500, seed=7)
    strategy = replay_strategy(entry)
    points = strategy.trace(BarWindow.of(bars))
    s0_points = S0Sma100().trace(BarWindow.of(bars))
    high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]
    close = [float(b.close) for b in bars]
    longs = both = stops = 0
    for i, p in enumerate(points):
        if p.state is not LONG:
            assert p.weight == 0.0
            continue
        longs += 1
        sigma = float(
            sigma_hat_kernel(close[i - 150 : i + 1], k=150)[-1]
        )  # S0's D-056 kernel at the last close
        assert p.weight == (1.0 if sigma <= 0.0 else min(1.0, 0.30 / sigma)), i
        if s0_points[i].state is LONG:  # both long: the weight depends on closes only, so it is S0's weight
            both += 1
            assert p.weight == s0_points[i].weight
        if p.stop is not None:  # the entry bar e is the last FLAT -> LONG change before i
            e = max(j for j in range(i) if points[j].state is LONG and points[j - 1].state is not LONG)
            atr = float(atr_simple(high[e - 20 : e + 1], low[e - 20 : e + 1], close[e - 20 : e + 1], 20)[-1])
            assert p.stop == stop_level(bars[e + 1].open, atr) and p.stop < bars[e + 1].open
            stops += 1
    assert longs > 0 and stops > 0, "synthetic series must contain long bars with a known stop"


def test_the_stop_is_open_minus_three_atr20_with_the_floor() -> None:
    entry = next(e for e in C25 if e.key == "C5-x10")
    strategy = replay_strategy(entry)
    bars = cyclic_wicky_bars(260, seed=3)
    w = window(bars, 240)
    atr = float(
        atr_simple(
            [float(b.high) for b in bars[219:240]],
            [float(b.low) for b in bars[219:240]],
            [float(b.close) for b in bars[219:240]],
            20,
        )[-1]
    )
    entry_price = Decimal("10000")
    assert strategy.stop_price(w, entry_price) == max(entry_price - 3 * Decimal(repr(atr)), Decimal("0.01"))
    assert strategy.stop_price(w, Decimal("1")) == Decimal("0.01")  # the floor


# ---- D-097: the own signal state of C4 and C5 --------------------------------------------------------------


HYSTERESIS = [e for e in C25 if e.key.startswith(("C4-", "C5-"))]


@pytest.mark.parametrize("entry", HYSTERESIS, ids=[e.key for e in HYSTERESIS])
def test_signal_state_follows_the_entry_exit_rule_and_the_position_follows_the_signal(
    entry: Phase3Entry,
) -> None:
    bars = cyclic_wicky_bars(1500, seed=11, every=23, cycle=180, daily_vol=0.03)
    strategy = replay_strategy(entry)
    assert isinstance(strategy, HysteresisStrategy)
    points = strategy.trace(BarWindow.of(bars))
    reference = replay_strategy(entry)
    assert isinstance(reference, HysteresisStrategy)
    long_signal = False  # reference machine, driven by the public entry_holds / exit_holds at each prefix
    entries = exits = reentries = 0
    first = strategy.warmup_bars - 1
    prev_state = FLAT
    for i in range(len(bars)):
        w = window(bars, i + 1)
        if i >= first:
            if long_signal and reference.exit_holds(w):
                long_signal, exits = False, exits + 1
            elif not long_signal and reference.entry_holds(w):
                long_signal, entries = True, entries + 1
            assert reference.signal_long(w) is long_signal, i
        assert (points[i].state in (LONG, STOPPED)) is long_signal, i  # the position follows the signal state
        if prev_state is STOPPED and points[i].state is LONG:
            raise AssertionError(f"STOPPED -> LONG without passing through FLAT at bar {i}")
        if prev_state is FLAT and points[i].state is LONG:
            reentries += 1
        prev_state = points[i].state
    assert entries > 0 and exits > 0 and reentries == entries


# ---- look-ahead (truncation) on every trial ----------------------------------------------------------------


SYNTH_BARS = {"C2": 1500, "C3": 2000, "C4": 1500, "C5": 1000}


@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_no_lookahead_on_a_synthetic_series_with_every_state(entry: Phase3Entry) -> None:
    bars = cyclic_wicky_bars(SYNTH_BARS[entry.key[:2]], seed=6, every=17, cycle=180, daily_vol=0.03)
    seen = assert_no_lookahead(lambda: replay_strategy(entry), bars, fresh_every=157)
    assert seen == {FLAT, LONG, STOPPED}, f"{entry.key}: the series does not exercise every state: {seen}"


@needs_store
@pytest.mark.parametrize("entry", C25, ids=KEYS)
def test_no_lookahead_on_real_binance_btcusdt_1d_before_the_holdout(entry: Phase3Entry) -> None:
    bars: list[Bar] = real_bars(
        "BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
    )
    assert bars[0].ts == FIRST_CLOSE and bars[-1].ts < datetime(2025, 10, 1, tzinfo=UTC)
    seen = assert_no_lookahead(lambda: replay_strategy(entry), bars, fresh_every=700)
    assert LONG in seen and FLAT in seen
