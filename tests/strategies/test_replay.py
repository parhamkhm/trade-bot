"""``ReplayStrategy``: anchor enforcement, lazy initialisation and the look-ahead check's negative control."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tbot.core.config import DataConfig
from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies import donchian, s0_sma100
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.strategies.replay import HISTORY_START, PositionState, ReplayStrategy, TracePoint
from tbot.strategies.s0_sma100 import S0Sma100
from tests.strategies.replay_checks import assert_no_lookahead
from tests.strategies.test_s0_sma100 import FLAT, LONG, wicky_bars
from tests.validation.data import synthetic_bars

D1 = Timeframe.D1


def anchored_bars(n: int) -> list[Bar]:
    """Synthetic bars whose first bar is the first daily bar after the anchor (opens 2018-01-01 00:00 UTC)."""
    return synthetic_bars(n, seed=2, start=HISTORY_START)


# ---- anchor ---------------------------------------------------------------------------------------------


def test_history_start_matches_the_configured_history_start() -> None:
    assert DataConfig.model_fields["history_start"].default == HISTORY_START
    assert datetime(2018, 1, 1, tzinfo=UTC) == HISTORY_START


def test_every_registry_built_s0_and_c1_is_anchored_at_history_start() -> None:
    entries = [e for e in PHASE3_ENTRIES if e.key == "S0" or e.key.startswith("C1-")]
    assert len(entries) == 4
    for entry in entries:
        strategy = load(entry)
        assert isinstance(strategy, ReplayStrategy) and strategy.anchor == HISTORY_START


@pytest.mark.parametrize("factory", [s0_sma100.make, donchian.make])
def test_a_window_starting_later_than_the_anchor_is_refused(factory: Callable[..., ReplayStrategy]) -> None:
    bars = anchored_bars(200)
    strategy = factory(timeframe=D1)
    assert strategy.on_bar(BarWindow.of(bars)).strategy_id == strategy.id  # starts at the anchor: accepted
    late = BarWindow.of(bars[1:])  # starts one day after the anchor
    for call in (strategy.on_bar, strategy.state, strategy.active_stop, strategy.trace):
        with pytest.raises(ValueError, match="must start at the anchor"):
            call(late)
    with pytest.raises(ValueError, match="must start at the anchor"):
        factory(timeframe=D1).on_bar(late)  # a fresh instance: no cache involved


def test_a_window_starting_earlier_than_the_anchor_is_refused_too() -> None:
    early = synthetic_bars(200, seed=2, start=HISTORY_START - timedelta(days=1))
    with pytest.raises(ValueError, match="must start at the anchor"):
        s0_sma100.make(timeframe=D1).on_bar(BarWindow.of(early))


def test_anchor_none_accepts_any_start_and_a_naive_anchor_is_refused() -> None:
    bars = synthetic_bars(200, seed=2)  # starts 2019-01-01
    assert S0Sma100().on_bar(BarWindow.of(bars)).target_weight >= 0.0
    assert s0_sma100.make(timeframe=D1, anchor=None).anchor is None
    with pytest.raises(ValueError, match="timezone-aware"):
        S0Sma100(anchor=datetime(2018, 1, 1))  # noqa: DTZ001


def test_an_explicit_anchor_is_honoured() -> None:
    bars = synthetic_bars(200, seed=2)  # opens 2019-01-01
    strategy = s0_sma100.make(timeframe=D1, anchor=datetime(2019, 1, 1, tzinfo=UTC))
    assert strategy.on_bar(BarWindow.of(bars)).target_weight >= 0.0


# ---- lazy initialisation ----------------------------------------------------------------------------------


class Momentum(ReplayStrategy):
    """Toy strategy: long while the close is above the close ``n`` bars ago."""

    id = "toy"
    warmup_bars = 151

    def __init__(self, *, set_before_super: bool) -> None:
        if set_before_super:
            self.n = 7
        super().__init__(0.3)
        if not set_before_super:
            self.n = 7  # set after super().__init__(): must work because the reset is lazy

    def _reset_signal(self) -> None:
        self.calls = 0

    def _signal_at(self, i: int) -> bool:
        self.calls += 1
        return i >= self.n and self._close[i] > self._close[i - self.n]


def test_subclass_attributes_may_be_set_before_or_after_super_init() -> None:
    bars = BarWindow.of(wicky_bars(300, seed=4))
    before = Momentum(set_before_super=True).trace(bars)
    after = Momentum(set_before_super=False).trace(bars)
    assert before == after and {p.state for p in before} >= {FLAT, LONG}


def test_construction_does_not_run_the_reset() -> None:
    strategy = Momentum(set_before_super=True)
    assert strategy.anchor is None and not hasattr(strategy, "calls")  # nothing ran at construction


# ---- the look-ahead check must be able to fail ---------------------------------------------------------


class PeekingS0(S0Sma100):
    """Look-ahead mutant: loads every window bar before deciding; its signal reads bar ``i + 1``."""

    def _replay(self, bars: tuple[Bar, ...], start: int, record: list[TracePoint] | None) -> None:
        for bar in bars[start:]:
            self._high.append(float(bar.high))
            self._low.append(float(bar.low))
            self._close.append(float(bar.close))
        for i in range(start, len(bars)):
            self._step(i, bars[i])
            if record is not None:
                record.append(self._point(i))

    def _signal_at(self, i: int) -> bool:
        nxt = min(i + 1, len(self._close) - 1)  # tomorrow's close, when the window has it
        return self._close[nxt] > self._close[i]


def test_the_look_ahead_check_fails_for_a_strategy_that_peeks_at_the_next_bar() -> None:
    bars = wicky_bars(500, seed=3)
    with pytest.raises(AssertionError, match="depend on bars after them"):
        assert_no_lookahead(PeekingS0, bars)


def test_the_look_ahead_check_passes_for_the_causal_toy_and_s0() -> None:
    bars = wicky_bars(500, seed=3)
    assert PositionState.LONG in assert_no_lookahead(lambda: Momentum(set_before_super=True), bars)
    assert PositionState.LONG in assert_no_lookahead(S0Sma100, bars)


def test_replay_base_class_refuses_a_non_daily_window() -> None:
    """The daily sigma-hat / ATR20 rules must never run silently on 4h or 1h bars (SPEC D-090 review)."""
    from tests.strategies.test_volume_invariance import _ohlc

    strategy = PeekingS0()
    with pytest.raises(ValueError, match="daily bars only"):
        strategy.on_bar(BarWindow.of(_ohlc(Timeframe.H4, 400)))
