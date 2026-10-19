"""C3 EWMAC on 1d bars (SPEC section 7.2 C3, D-097): scalars, caps, sigma_p = 0, stop, re-entry.

The forecast reference below is written independently of ``tbot.indicators`` (numpy dot products instead of
``fsum``, ``np.std`` instead of the hand-written stdev), so a shared slip cannot hide.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tbot.core.types import Bar, Timeframe
from tbot.strategies.base import validate_strategy
from tbot.strategies.ewmac import FORECAST_SCALARS, Ewmac, forecast, make
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tests.sim_helpers import make_bar
from tests.strategies.c25_support import (
    FLAT,
    LONG,
    START,
    STOPPED,
    bars_from_rows,
    linear_bars,
    linear_rows,
    run_lengths,
    states_from,
    stop_flags_from,
    window,
)

D1 = Timeframe.D1
GRID = [(8, 16, 32), (16, 32, 64)]
WARMUPS = {(8, 16, 32): 384, (16, 32, 64): 768}


def reference_ema(closes: list[float], span: int) -> float:
    """Finite-window (K = 3 x span) EWMA at the last close, weights (1 - a)^lag, a = 2 / (span + 1)."""
    k = 3 * span
    weights = (1.0 - 2.0 / (span + 1)) ** np.arange(k)
    return float(np.dot(weights, np.array(closes[::-1][:k])) / weights.sum())


def reference_forecasts(closes: list[float], fast_spans: tuple[int, ...]) -> list[float]:
    """Carver EWMAC per SPEC 7.2 C3, from the closes up to and including the decision bar."""
    c = np.array(closes)
    sigma_p = c[-1] * float(np.std(np.diff(np.log(c))[-36:], ddof=1))
    out: list[float] = []
    for f in fast_spans:
        raw = reference_ema(closes, f) - reference_ema(closes, 4 * f)
        out.append(float(np.clip(raw / sigma_p * FORECAST_SCALARS[f], -20.0, 20.0)) if sigma_p else 0.0)
    return out


# ---- construction -------------------------------------------------------------------------------------


def test_forecast_scalars_are_carvers_published_values_exactly() -> None:
    assert dict(FORECAST_SCALARS) == {8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}
    assert 128 not in FORECAST_SCALARS  # the 4h point (32, 64, 128) was dropped (D-090)


@pytest.mark.parametrize("fast_spans", GRID)
def test_registry_factories_and_warmups(fast_spans: tuple[int, int, int]) -> None:
    entry = next(e for e in PHASE3_ENTRIES if e.key == "C3-1d-(" + ",".join(map(str, fast_spans)) + ")")
    strategy = load(entry)
    assert isinstance(strategy, Ewmac)
    assert strategy.fast_spans == fast_spans and strategy.warmup_bars == WARMUPS[fast_spans]
    assert strategy.id == "ewmac_" + "_".join(map(str, fast_spans)) + "_1d"
    assert validate_strategy(strategy) is strategy


def test_defaults_and_refusals() -> None:
    assert make(timeframe=D1).fast_spans == (16, 32, 64)
    with pytest.raises(ValueError, match="1d bars only"):
        make(timeframe=Timeframe.H4)
    with pytest.raises(TypeError, match="unexpected"):
        make(timeframe=D1, horizons=(1, 2, 3))
    with pytest.raises(ValueError, match="three fast spans"):
        Ewmac((16, 32))
    with pytest.raises(ValueError, match="three fast spans"):
        Ewmac((32, 64, 128))  # no published scalar in the pre-registered set


# ---- the forecast function ------------------------------------------------------------------------------


def test_forecast_function_hand_values() -> None:
    assert forecast(1.0, 2.0, 3.75) == pytest.approx(1.875)  # raw / sigma_p x c
    assert forecast(-1.0, 2.0, 3.75) == pytest.approx(-1.875)
    assert forecast(0.0, 2.0, 3.75) == 0.0
    assert forecast(100.0, 1.0, 3.75) == 20.0  # capped
    assert forecast(-100.0, 1.0, 3.75) == -20.0
    assert forecast(20.0 / 3.75, 1.0, 3.75) == pytest.approx(20.0)  # exactly at the cap


@pytest.mark.parametrize("raw", [-5.0, 0.0, 5.0])
def test_forecast_is_zero_when_sigma_p_is_zero(raw: float) -> None:
    # D-097 (c): the pair's F is 0, never +-inf (-> +-20) or NaN
    assert forecast(raw, 0.0, 3.75) == 0.0


# ---- the forecasts against the independent reference ----------------------------------------------------


def walk_bars(n: int, seed: int) -> list[Bar]:
    rng = np.random.default_rng(seed)
    price, rows = 1000.0, []
    for _ in range(n):
        o, c = price, price * math.exp(float(rng.normal(0.0005, 0.02)))
        rows.append((f"{o:.2f}", f"{max(o, c) * 1.002:.2f}", f"{min(o, c) * 0.998:.2f}", f"{c:.2f}"))
        price = float(f"{c:.2f}")
    return bars_from_rows(rows)


@pytest.mark.parametrize("fast_spans", GRID)
def test_forecasts_match_the_independent_reference(fast_spans: tuple[int, int, int]) -> None:
    bars = walk_bars(900, seed=21)
    c3 = Ewmac(fast_spans)
    closes = [float(b.close) for b in bars]
    checked = 0
    for n in (c3.warmup_bars, c3.warmup_bars + 1, 800, 900):
        got = c3.forecasts(window(bars, n))
        assert got is not None
        np.testing.assert_allclose(got, reference_forecasts(closes[:n], fast_spans), rtol=1e-9, atol=1e-9)
        checked += 1
    assert checked == 4


def test_signal_is_the_mean_of_the_three_forecasts_being_positive() -> None:
    bars = walk_bars(900, seed=33)
    c3 = Ewmac((16, 32, 64))
    seen = set()
    for n in range(768, 901):
        w = window(bars, n)
        f = c3.forecasts(w)
        assert f is not None
        assert c3.signal_long(w) == (sum(f) / 3 > 0.0)
        seen.add(c3.signal_long(w))
    assert seen == {True, False}  # both outcomes occur on this walk, so the equality above is not vacuous


def test_forecasts_are_undefined_until_every_window_is_full() -> None:
    bars = linear_bars(400)
    c3 = Ewmac((8, 16, 32))  # the slowest EWMA (span 128, K = 384) needs 384 closes
    assert c3.forecasts(window(bars, 383)) is None
    assert c3.forecasts(window(bars, 384)) is not None


# ---- D-097 (c): sigma_p = 0 --------------------------------------------------------------------------------


def stalled_bars() -> list[Bar]:
    """A rise, then 60 identical closes: the last 36 log returns are exactly 0 (sigma_p = 0) while E_f - E_4f
    is still positive (the fast average sits at the top, the slow one remembers the climb)."""
    rows = [("100", "100", "100", "100"), *linear_rows(1, 400)]
    top = rows[-1][3]
    rows += [(top, top, top, top)] * 60
    return bars_from_rows(rows)


def test_sigma_p_zero_gives_forecast_zero_for_every_pair_and_a_flat_signal() -> None:
    bars = stalled_bars()
    c3 = Ewmac((8, 16, 32))
    w = window(bars)
    closes = [float(b.close) for b in bars]
    assert len(set(closes[-37:])) == 1  # sigma_p is exactly 0
    # without the rule every raw would be divided by 0 and the forecast would be +-inf (capped to +-20)
    ema_fast, ema_slow = (reference_ema(closes, span) for span in (8, 32))
    assert ema_fast - ema_slow > 0.0
    assert c3.forecasts(w) == (0.0, 0.0, 0.0)
    assert not c3.signal_long(w)
    assert c3.on_bar(w).target_weight == 0.0


def test_a_zero_sigma_p_does_not_make_the_strategy_long_after_a_stop_free_rise() -> None:
    bars = stalled_bars()
    c3 = Ewmac((8, 16, 32))
    assert c3.state(window(bars, 401)) is LONG  # still rising: long
    assert c3.state(window(bars)) is FLAT  # sigma_p = 0 -> F = 0 -> mean 0 -> flat


# ---- hand-computed behaviour on the linear ramp -----------------------------------------------------------


@pytest.mark.parametrize("fast_spans", GRID)
def test_ramp_forecasts_are_capped_and_the_strategy_enters_on_the_first_tradable_close(
    fast_spans: tuple[int, int, int],
) -> None:
    first = WARMUPS[fast_spans]
    bars = linear_bars(first + 30)
    c3 = make(timeframe=D1, fast_spans=fast_spans)
    assert c3.warmup_bars == first
    assert c3.state(window(bars, first - 1)) is FLAT
    assert c3.state(window(bars, first)) is LONG
    assert c3.forecasts(window(bars, first)) == (20.0, 20.0, 20.0)  # tiny sigma_p: every pair hits the cap
    assert c3.on_bar(window(bars, first)).target_weight > 0.0


def test_a_falling_ramp_gives_capped_negative_forecasts_and_stays_flat() -> None:
    rows = [(str(2000 - k + 1), str(2000 - k + 1), str(2000 - k), str(2000 - k)) for k in range(500)]
    bars = bars_from_rows(rows)
    c3 = make(timeframe=D1, fast_spans=(8, 16, 32))
    assert c3.forecasts(window(bars)) == (-20.0, -20.0, -20.0)
    assert {c3.state(window(bars, n)) for n in range(384, 501)} == {FLAT}


def crash_scenario() -> list[Bar]:
    """Ramp to the first tradable close; a wick that stops the position out on its entry bar; the ramp goes on
    (STOPPED while the signal stays true); a crash flips the signal (STOPPED -> FLAT); a long recovery ramp
    makes the signal true again (a fresh entry)."""
    rows = [("100", "100", "100", "100"), *linear_rows(1, 383)]  # 384 closes: close #384 is c = 483
    rows.append(("484", "485", "470", "484"))  # entry bar: low 470 <= 484 - 3 x ATR20 (1) = 481 -> stopped
    rows += linear_rows(385, 389)  # keeps rising: the signal stays true, the position stays STOPPED
    rows.append(("489", "489", "300", "300"))  # crash: mean forecast < 0 -> flat
    rows += [(str(300 + j - 1), str(300 + j), str(300 + j - 1), str(300 + j)) for j in range(1, 500)]
    return bars_from_rows(rows)


def test_stop_out_stays_stopped_while_true_then_flat_after_the_crash_then_fresh_entry() -> None:
    bars = crash_scenario()
    c3 = make(timeframe=D1, fast_spans=(8, 16, 32))
    seq = states_from(c3, bars, 384)
    assert seq[0] is LONG  # first tradable close
    assert seq[1:6] == [STOPPED] * 5  # stop-out on bar 384, then the signal stays true
    assert run_lengths(seq) == [LONG, STOPPED, FLAT, LONG]  # the crash exits; the recovery is a fresh entry
    assert stop_flags_from(c3, bars, 384)[:3] == [False, True, False]
    # the re-entry needs the signal itself to be true again, not only a close above the stop
    first_long_again = 384 + max(i for i, s in enumerate(seq) if s is not LONG) + 1
    assert c3.signal_long(window(bars, first_long_again))
    assert not c3.signal_long(window(bars, first_long_again - 1))


def test_stop_price_is_the_s0_rule() -> None:
    from decimal import Decimal

    bars = linear_bars(400)
    c3 = Ewmac((8, 16, 32))
    assert c3.stop_price(window(bars), Decimal("500")) == Decimal("497")  # ATR20 = 1 on the ramp: 500 - 3


def test_incremental_calls_equal_a_fresh_replay() -> None:
    bars = walk_bars(800, seed=5)
    incremental = Ewmac((8, 16, 32))
    for n in range(384, 801, 29):
        w = window(bars, n)
        fresh = Ewmac((8, 16, 32))
        assert incremental.on_bar(w) == fresh.on_bar(w)
        assert incremental.forecasts(w) == fresh.forecasts(w)


def test_first_tradable_close_dates() -> None:
    bars = linear_bars(800)
    assert bars[383].ts == datetime(2019, 1, 20, tzinfo=UTC)  # (8, 16, 32): close #384
    assert bars[767].ts == datetime(2020, 2, 8, tzinfo=UTC)  # (16, 32, 64): close #768
    assert bars[0].ts == datetime(2018, 1, 2, tzinfo=UTC) and datetime(2018, 1, 1, tzinfo=UTC) == START
    assert make_bar(0, "1", "1", start=START).ts == bars[0].ts
