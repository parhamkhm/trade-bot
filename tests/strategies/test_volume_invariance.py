"""Behavioural D-040 guard (SPEC §7.2 J): no Phase 3 strategy's behaviour may depend on bar volume.

Every registered Phase 3 strategy (S0 + the 17 grid points, ``tbot.strategies.phase3_registry``) runs
through the real engine path three times on the same synthetic OHLC: with the original volume, with
random volume, and with volume set to NaN. Targets, orders/fills and equity must be bit-identical, and the
original run must be non-vacuous (at least 4 fills, with at least 2 entries and 2 exits).

``Bar`` rejects a non-finite volume (``core/types.py``), so the NaN series is injected after construction on
purpose: any read of ``volume`` by a strategy then either raises or changes its output. A volume-dependent
dummy strategy is the negative control; it must make the invariance check fail.
"""

from __future__ import annotations

import importlib.util
import math
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, BarWindow, Side, Strategy, TargetIntent, Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry, load
from tbot.strategies.sma_filter import SmaFilter
from tests.backtest.helpers import make_config

START = datetime(2018, 1, 1, tzinfo=UTC)  # the series must end before the sealed holdout (2025-10-01)
DAYS = 2_000  # the last bar closes 2023-06-24, well before holdout_start
BARS_PER_DAY = {Timeframe.D1: 1, Timeframe.H4: 6}
CYCLE_DAYS = 240  # alternating bull/bear phases so trend and mean-reversion rules all enter and exit
MIN_FILLS, MIN_ENTRIES, MIN_EXITS = 4, 2, 2


def _ohlc(tf: Timeframe, n: int, seed: int = 20261011) -> list[Bar]:
    rng = random.Random(seed)
    per_day = BARS_PER_DAY[tf]
    out: list[Bar] = []
    price = 10_000.0
    for i in range(n):
        drift = 0.004 * math.sin(2 * math.pi * (i / per_day) / CYCLE_DAYS) / per_day
        vol = 0.03 / math.sqrt(per_day)
        o = price
        c = max(1.0, o * (1 + rng.gauss(drift, vol)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append(
            Bar(
                symbol="BTCUSDT",
                timeframe=tf,
                ts=START + (i + 1) * tf.delta,
                open=Decimal(f"{o:.2f}"),
                high=Decimal(f"{hi:.2f}"),
                low=Decimal(f"{lo:.2f}"),
                close=Decimal(f"{c:.2f}"),
                volume=Decimal(10 + i % 7),
            )
        )
        price = c
    return out


def _with_volume(bars: list[Bar], mode: str) -> list[Bar]:
    if mode == "original":
        return bars
    if mode == "random":
        rng = random.Random(7)
        return [replace(b, volume=Decimal(f"{rng.uniform(0, 1e6):.4f}")) for b in bars]
    assert mode == "nan"
    out = [replace(b) for b in bars]
    for b in out:
        object.__setattr__(b, "volume", Decimal("NaN"))  # deliberately past Bar's finite-volume check
    return out


def _fingerprint(res: BacktestResult) -> tuple[object, ...]:
    return (
        tuple((d.ts, d.target_weight, d.outcome, d.client_order_id) for d in res.decisions),
        tuple(res.fills),
        tuple(res.rejections),
        tuple(res.equity),
    )


def _entries_exits(res: BacktestResult) -> tuple[int, int]:
    position, entries, exits = Decimal(0), 0, 0
    for rec in res.fills:
        before = position
        position += rec.fill.quantity if rec.fill.side is Side.BUY else -rec.fill.quantity
        if before == 0 and position > 0:
            entries += 1
        elif before > 0 and position == 0:
            exits += 1
    return entries, exits


def _run(strategy: Strategy, tf: Timeframe, mode: str) -> BacktestResult:
    bars = _with_volume(_ohlc(tf, DAYS * BARS_PER_DAY[tf]), mode)
    return run_backtest(BacktestFeed(bars), strategy, make_config(fee="10", slip="5", tf=tf))


def assert_volume_invariant(make: Callable[[], Strategy], tf: Timeframe) -> None:
    """Runs ``make()`` three times (fresh instance each): non-vacuous, and bit-identical across volumes."""
    base_res = _run(make(), tf, "original")
    entries, exits = _entries_exits(base_res)
    assert len(base_res.fills) >= MIN_FILLS and entries >= MIN_ENTRIES and exits >= MIN_EXITS, (
        f"vacuous run: {len(base_res.fills)} fills, {entries} entries, {exits} exits "
        f"(need >= {MIN_FILLS}, {MIN_ENTRIES}, {MIN_EXITS})"
    )
    base = _fingerprint(base_res)
    for mode in ("random", "nan"):
        try:
            other = _fingerprint(_run(make(), tf, mode))
        except Exception as exc:  # any failure on NaN volume means volume was read
            raise AssertionError(f"strategy failed when volume was {mode}: {exc!r}") from exc
        assert other == base, f"behaviour changed when volume was {mode}"


def skip_if_not_implemented(entry: Phase3Entry) -> None:
    """Skip ONLY when the strategy's module does not exist; any other import error must fail the test."""
    if importlib.util.find_spec(entry.module) is None:
        pytest.skip(f"{entry.key}: not implemented yet ({entry.module}); blocks any strategy PR")


@pytest.mark.parametrize("entry", PHASE3_ENTRIES, ids=lambda e: e.key)
def test_phase3_strategy_is_volume_invariant(entry: Phase3Entry) -> None:
    skip_if_not_implemented(entry)
    assert_volume_invariant(lambda: load(entry), entry.timeframe)


def test_harness_passes_a_volume_free_baseline() -> None:
    assert_volume_invariant(lambda: SmaFilter(50), Timeframe.D1)


class _VolumeDependent:
    """Negative control: goes long only on above-average volume."""

    id = "volume_dependent_dummy"
    warmup_bars = 5

    def on_bar(self, window: BarWindow) -> TargetIntent:
        recent = [b.volume for b in window.bars[-5:]]
        weight = 1.0 if window.last.volume > sum(recent) / len(recent) else 0.0
        return TargetIntent(self.id, window.as_of, weight, "volume")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


def test_negative_control_volume_dependent_strategy_is_caught() -> None:
    with pytest.raises(AssertionError, match="volume was"):
        assert_volume_invariant(lambda: _VolumeDependent(), Timeframe.D1)


def test_skip_rule_does_not_skip_an_existing_but_broken_entry() -> None:
    """A module that exists but whose strategy cannot be built must FAIL, not skip."""
    entry = Phase3Entry("broken", "tbot.strategies.sma_filter", "no_such_factory", Timeframe.D1)
    skip_if_not_implemented(entry)  # the module exists, so this must not skip
    with pytest.raises(AttributeError):
        load(entry)


D1, H4 = Timeframe.D1, Timeframe.H4

SPEC_72C_GRID: list[tuple[str, str, Timeframe, dict[str, object]]] = [
    ("S0", "tbot.strategies.s0_sma100", D1, {}),
    ("C1-(10,28,50)", "tbot.strategies.donchian", D1, {"lookbacks": (10, 28, 50)}),
    ("C1-(20,55,100)", "tbot.strategies.donchian", D1, {"lookbacks": (20, 55, 100)}),
    ("C1-(40,110,200)", "tbot.strategies.donchian", D1, {"lookbacks": (40, 110, 200)}),
    ("C2-(15,45,90)", "tbot.strategies.tsmom", D1, {"horizons": (15, 45, 90)}),
    ("C2-(30,90,180)", "tbot.strategies.tsmom", D1, {"horizons": (30, 90, 180)}),
    ("C2-(60,180,360)", "tbot.strategies.tsmom", D1, {"horizons": (60, 180, 360)}),
    ("C3-1d-(8,16,32)", "tbot.strategies.ewmac", D1, {"fast_spans": (8, 16, 32)}),
    ("C3-1d-(16,32,64)", "tbot.strategies.ewmac", D1, {"fast_spans": (16, 32, 64)}),
    ("C3-4h-(8,16,32)", "tbot.strategies.ewmac", H4, {"fast_spans": (8, 16, 32)}),
    ("C3-4h-(16,32,64)", "tbot.strategies.ewmac", H4, {"fast_spans": (16, 32, 64)}),
    ("C3-4h-(32,64,128)", "tbot.strategies.ewmac", H4, {"fast_spans": (32, 64, 128)}),
    ("C4-p10", "tbot.strategies.vol_breakout", D1, {"pct": 10}),
    ("C4-p20", "tbot.strategies.vol_breakout", D1, {"pct": 20}),
    ("C4-p30", "tbot.strategies.vol_breakout", D1, {"pct": 30}),
    ("C5-x5", "tbot.strategies.pullback_mr", D1, {"rsi_entry": 5}),
    ("C5-x10", "tbot.strategies.pullback_mr", D1, {"rsi_entry": 10}),
    ("C5-x15", "tbot.strategies.pullback_mr", D1, {"rsi_entry": 15}),
]


def test_registry_matches_spec_grid() -> None:
    """Every entry's (key, module, timeframe, params) equals a literal copy of SPEC §7.2 C (v3, D-087)."""
    actual = [(e.key, e.module, e.timeframe, dict(e.params)) for e in PHASE3_ENTRIES]
    assert actual == SPEC_72C_GRID
    assert all(e.factory == "make" for e in PHASE3_ENTRIES)
    assert len(SPEC_72C_GRID) == 18  # S0 + 17 non-S0 trials
