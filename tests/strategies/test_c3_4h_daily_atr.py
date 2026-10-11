"""SPEC §7.2 C: the 4h C3 points use S0's daily ATR20 and sigma-hat of the last COMPLETED daily close only.

At a 4h decision inside a UTC day, the current day is not complete. For both the stop level (ATR20) and the
target weight (sigma-hat sizing), changing the 4h bars of that incomplete day must leave the value
unchanged, and changing the last completed day must change it. Skips only while the C3 module does not
exist; no strategy PR may merge while it skips.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry, load
from tests.strategies.test_volume_invariance import _ohlc, skip_if_not_implemented

C3_4H = [e for e in PHASE3_ENTRIES if e.key.startswith("C3-4h")]
BARS_PER_DAY = 6
INCOMPLETE = 3  # the window ends 3 bars (12 h) into an incomplete UTC day
START = datetime(2018, 1, 1, tzinfo=UTC)


def _window_mid_day(n_days: int) -> list[Bar]:
    return _ohlc(Timeframe.H4, n_days * BARS_PER_DAY + INCOMPLETE)


def _uptrend_mid_day(n_days: int, seed: int = 7) -> list[Bar]:
    """A steady uptrend (every EWMAC speed long at the end) with moderate vol: vol-target weight < 1."""
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 10_000.0
    for i in range(n_days * BARS_PER_DAY + INCOMPLETE):
        o = price
        c = o * (1 + rng.gauss(0.0015, 0.01))
        out.append(
            Bar(
                symbol="BTCUSDT",
                timeframe=Timeframe.H4,
                ts=START + (i + 1) * Timeframe.H4.delta,
                open=Decimal(f"{o:.2f}"),
                high=Decimal(f"{max(o, c) * 1.002:.2f}"),
                low=Decimal(f"{min(o, c) * 0.998:.2f}"),
                close=Decimal(f"{c:.2f}"),
                volume=Decimal(10),
            )
        )
        price = c
    return out


def _scaled(
    bar: Bar, *, close: Decimal | None = None, hi: Decimal = Decimal(1), lo: Decimal = Decimal(1)
) -> Bar:
    new_close = bar.close if close is None else close
    return replace(
        bar,
        close=new_close,
        high=max(bar.high * hi, new_close, bar.open),
        low=min(bar.low * lo, new_close, bar.open),
    )


@pytest.mark.parametrize("entry", C3_4H, ids=lambda e: e.key)
def test_4h_stop_uses_only_the_last_completed_daily_atr(entry: Phase3Entry) -> None:
    skip_if_not_implemented(entry)
    strategy = load(entry)
    bars = _window_mid_day(400)
    entry_price = bars[-1].close
    base = strategy.stop_price(BarWindow.of(bars), entry_price)

    # 1) the incomplete day's 4h bars change -> the stop level must not change
    incomplete = [_scaled(b, hi=Decimal("1.2"), lo=Decimal("0.8")) for b in bars[-INCOMPLETE:]]
    assert strategy.stop_price(BarWindow.of(bars[:-INCOMPLETE] + incomplete), entry_price) == base

    # 2) a 4h bar of the last COMPLETED day widens that day's range -> the stop level must change
    i = len(bars) - INCOMPLETE - 1  # last 4h bar of the last completed day
    changed = [*bars[:i], _scaled(bars[i], hi=Decimal("1.5")), *bars[i + 1 :]]
    assert strategy.stop_price(BarWindow.of(changed), entry_price) != base


@pytest.mark.parametrize("entry", C3_4H, ids=lambda e: e.key)
def test_4h_target_weight_uses_only_the_last_completed_daily_sigma(entry: Phase3Entry) -> None:
    skip_if_not_implemented(entry)
    strategy = load(entry)
    bars = _uptrend_mid_day(400)
    base = strategy.on_bar(BarWindow.of(bars)).target_weight
    assert 0.0 < base < 1.0, f"precondition: long with a vol-targeted weight below 1, got {base}"

    # 1) the incomplete day's closes move by 1 % (the trend stays up) -> the target weight must not change
    incomplete = [_scaled(b, close=b.close * Decimal("1.01")) for b in bars[-INCOMPLETE:]]
    assert strategy.on_bar(BarWindow.of(bars[:-INCOMPLETE] + incomplete)).target_weight == base

    # 2) the last COMPLETED daily close moves by 3 % -> sigma-hat, and so the target weight, must change
    i = len(bars) - INCOMPLETE - 1  # last 4h bar of the last completed day = that day's close
    changed = [*bars[:i], _scaled(bars[i], close=bars[i].close * Decimal("1.03")), *bars[i + 1 :]]
    after = strategy.on_bar(BarWindow.of(changed)).target_weight
    assert after > 0.0 and after != base


def _last_completed_day(bars: list[Bar]) -> range:
    """Indices of the 6 4h bars of the last completed UTC day (the window ends INCOMPLETE bars into a day)."""
    end = len(bars) - INCOMPLETE
    return range(end - BARS_PER_DAY, end)


def _non_extreme_mid_bar(bars: list[Bar]) -> int:
    """A 4h bar of the last completed day that sets neither the day's high, low, open nor close."""
    day = list(_last_completed_day(bars))
    hi_i = max(day, key=lambda k: bars[k].high)
    lo_i = min(day, key=lambda k: bars[k].low)
    for k in day[1:-1]:
        if k not in (hi_i, lo_i) and bars[k].high < bars[hi_i].high and bars[k].low > bars[lo_i].low:
            return k
    raise AssertionError("synthetic data has no non-extreme mid-day bar")


def _daily_hlc(bars: list[Bar]) -> tuple[Decimal, Decimal, Decimal]:
    day = list(_last_completed_day(bars))
    return max(bars[k].high for k in day), min(bars[k].low for k in day), bars[day[-1]].close


@pytest.mark.parametrize("entry", C3_4H, ids=lambda e: e.key)
def test_4h_only_change_inside_a_completed_day_leaves_the_stop_unchanged(entry: Phase3Entry) -> None:
    """A 4h bar of a completed day is changed (high, low AND close) while that day's daily high, low and
    close stay unchanged: the stop level must not change. Control: raising the bar that sets the day's high
    changes the daily range, so the stop level must change."""
    skip_if_not_implemented(entry)
    strategy = load(entry)
    bars = _window_mid_day(400)
    entry_price = bars[-1].close
    base = strategy.stop_price(BarWindow.of(bars), entry_price)
    day_high, day_low, day_close = _daily_hlc(bars)

    k = _non_extreme_mid_bar(bars)
    b = bars[k]
    new_close = (b.close + day_low) / 2 if b.close > day_low else b.close  # moves the close, stays inside
    mid = replace(b, close=new_close, high=max(b.open, new_close), low=min(b.open, new_close))
    changed = [*bars[:k], mid, *bars[k + 1 :]]
    assert _daily_hlc(changed) == (day_high, day_low, day_close)  # the daily bar is untouched
    assert changed[k] != bars[k]
    assert strategy.stop_price(BarWindow.of(changed), entry_price) == base

    hi_i = max(_last_completed_day(bars), key=lambda j: bars[j].high)
    control = [*bars[:hi_i], _scaled(bars[hi_i], hi=Decimal("1.5")), *bars[hi_i + 1 :]]
    assert _daily_hlc(control)[0] != day_high
    assert strategy.stop_price(BarWindow.of(control), entry_price) != base


@pytest.mark.parametrize("entry", C3_4H, ids=lambda e: e.key)
def test_4h_only_intrabar_range_change_leaves_the_target_weight_unchanged(entry: Phase3Entry) -> None:
    """Only the intrabar high/low of a non-extreme 4h bar of a completed day changes (not its close:
    4h closes feed the C3 signal by design); the day's daily high, low and close stay unchanged, so the
    target weight must not change."""
    skip_if_not_implemented(entry)
    strategy = load(entry)
    bars = _uptrend_mid_day(400)
    base = strategy.on_bar(BarWindow.of(bars)).target_weight
    assert 0.0 < base < 1.0, f"precondition: long with a vol-targeted weight below 1, got {base}"
    day = _daily_hlc(bars)

    k = _non_extreme_mid_bar(bars)
    b = bars[k]
    widened = replace(b, high=(b.high + day[0]) / 2, low=(b.low + day[1]) / 2)  # wider, still inside the day
    changed = [*bars[:k], widened, *bars[k + 1 :]]
    assert _daily_hlc(changed) == day and changed[k] != bars[k]
    assert strategy.on_bar(BarWindow.of(changed)).target_weight == base


@pytest.mark.parametrize("series", ["cyclic", "uptrend"])
def test_4h_only_edits_leave_the_daily_bar_unchanged_and_the_control_does_not(series: str) -> None:
    """Strategy-independent check of the edits used above, on both synthetic series: the mid-bar edits change
    the 4h bar but not the completed day's daily high/low/close; the control changes the daily high."""
    bars = _window_mid_day(400) if series == "cyclic" else _uptrend_mid_day(400)
    day = _daily_hlc(bars)
    k = _non_extreme_mid_bar(bars)
    b = bars[k]
    new_close = (b.close + day[1]) / 2
    shrunk = replace(b, close=new_close, high=max(b.open, new_close), low=min(b.open, new_close))
    widened = replace(b, high=(b.high + day[0]) / 2, low=(b.low + day[1]) / 2)
    for edited in (shrunk, widened):
        assert edited != b
        assert _daily_hlc([*bars[:k], edited, *bars[k + 1 :]]) == day
    hi_i = max(_last_completed_day(bars), key=lambda j: bars[j].high)
    assert _daily_hlc([*bars[:hi_i], _scaled(bars[hi_i], hi=Decimal("1.5")), *bars[hi_i + 1 :]])[0] != day[0]


def test_c3_4h_entries_are_registered() -> None:
    assert [e.key for e in C3_4H] == ["C3-4h-(8,16,32)", "C3-4h-(16,32,64)", "C3-4h-(32,64,128)"]
