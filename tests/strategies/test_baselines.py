"""Baseline strategies: buy-and-hold and the SMA filter."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.core.types import BarWindow
from tbot.strategies.base import is_valid_stop, validate_strategy
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter
from tests.backtest.helpers import make_config
from tests.sim_helpers import bars_from_closes

Dec = Decimal


def window(closes: list[float]) -> BarWindow:
    return BarWindow.of(bars_from_closes(closes))


def test_buy_and_hold_constant_weight_and_valid_stop() -> None:
    s = BuyAndHold()
    validate_strategy(s)
    w = window([100, 101, 102])
    intent = s.on_bar(w)
    assert intent.target_weight == 1.0 and intent.ts == w.as_of and s.id == "buy_and_hold"
    assert is_valid_stop(s.stop_price(w, Dec("102")), Dec("102"))
    assert BuyAndHold(weight=0.4).id == "buy_and_hold_w0.4"
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError, match="weight"):
            BuyAndHold(weight=bad)
    with pytest.raises(ValueError, match="warmup"):
        BuyAndHold(warmup_bars=0)


def test_sma_filter_hand_computed() -> None:
    s = SmaFilter(3)
    validate_strategy(s)
    assert s.warmup_bars == 3
    w_up = window([10, 11, 15])  # sma 12, close 15
    assert s.sma(w_up) == pytest.approx(12.0)
    assert s.on_bar(w_up).target_weight == 1.0
    assert s.on_bar(window([10, 12, 11])).target_weight == 0.0  # sma 11, close 11: not strictly above
    assert s.on_bar(window([20, 14, 10, 11])).target_weight == 0.0  # only the last 3 count: sma 11.67
    with pytest.raises(ValueError, match="need 3"):
        s.sma(window([1, 2]))
    with pytest.raises(ValueError, match="n must"):
        SmaFilter(1)


def test_sma_filter_stop_is_below_entry_either_side_of_the_average() -> None:
    s = SmaFilter(3)
    w = window([10, 11, 15])
    below = s.stop_price(w, Dec("15.5"))
    assert below == Dec("12.0") and is_valid_stop(below, Dec("15.5"))
    clamped = s.stop_price(w, Dec("11"))  # entry under the SMA: stop is clamped below the entry
    assert clamped < Dec("11") and is_valid_stop(clamped, Dec("11"))


def test_sma_filter_trades_through_the_engine_and_is_flat_in_downtrend() -> None:
    closes = [100.0 + i for i in range(10)] + [110.0 - 3 * i for i in range(1, 11)]
    res = run_backtest(BacktestFeed(bars_from_closes(closes)), SmaFilter(3), make_config(fee="0", slip="0"))
    assert res.first_decision_index == 2
    assert res.equity[-1].quantity == 0  # the filter has exited by the end of the downtrend
    assert len(res.fills) == 2
