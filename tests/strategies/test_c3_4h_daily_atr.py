"""SPEC §7.2 C: the 4h C3 points use S0's daily ATR20 of the last COMPLETED daily bar for their stop.

At a 4h decision inside a UTC day, the current day is not complete. Changing the 4h bars of that incomplete
day must not change the stop level; changing a bar of the last completed day (its high/low, hence its true
range) must change it. Skips while the C3 module is not implemented; no strategy PR may merge while it skips.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry, load
from tests.strategies.test_volume_invariance import _ohlc

C3_4H = [e for e in PHASE3_ENTRIES if e.key.startswith("C3-4h")]
BARS_PER_DAY = 6


def _window_mid_day(n_days: int) -> list[Bar]:
    bars = _ohlc(Timeframe.H4, n_days * BARS_PER_DAY + 3)  # ends 3 bars (12 h) into an incomplete day
    return bars


@pytest.mark.parametrize("entry", C3_4H, ids=lambda e: e.key)
def test_4h_stop_uses_only_the_last_completed_daily_atr(entry: Phase3Entry) -> None:
    try:
        strategy = load(entry)
    except ModuleNotFoundError:
        pytest.skip(f"{entry.key}: not implemented yet ({entry.module}); blocks any strategy PR")
    bars = _window_mid_day(400)
    entry_price = bars[-1].close
    base = strategy.stop_price(BarWindow.of(bars), entry_price)

    # 1) the incomplete day's 4h bars (last 3) change -> the stop level must not change
    incomplete = [replace(b, high=b.high * Decimal("1.2"), low=b.low * Decimal("0.8")) for b in bars[-3:]]
    assert strategy.stop_price(BarWindow.of(bars[:-3] + incomplete), entry_price) == base

    # 2) a 4h bar of the last COMPLETED day widens that day's range -> the stop level must change
    i = len(bars) - 3 - 1  # last 4h bar of the last completed day
    widened = replace(bars[i], high=bars[i].high * Decimal("1.5"))
    changed = [*bars[:i], widened, *bars[i + 1 :]]
    assert strategy.stop_price(BarWindow.of(changed), entry_price) != base


def test_c3_4h_entries_are_registered() -> None:
    assert [e.key for e in C3_4H] == ["C3-4h-(8,16,32)", "C3-4h-(16,32,64)", "C3-4h-(32,64,128)"]
