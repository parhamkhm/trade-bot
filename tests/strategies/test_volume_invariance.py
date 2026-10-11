"""Behavioural D-040 guard (SPEC §7.2 J): no Phase 3 strategy's behaviour may depend on bar volume.

Every registered Phase 3 strategy (S0 + the 18 grid points, ``tbot.strategies.phase3_registry``) runs
through the real engine path three times on the same synthetic OHLC: with the original volume, with
random volume, and with volume set to NaN. Targets, orders/fills and equity must be bit-identical.

``Bar`` rejects a non-finite volume (``core/types.py``), so the NaN series is injected after construction on
purpose: any read of ``volume`` by a strategy then either raises or changes its output. A volume-dependent
dummy strategy is the negative control; it must make the invariance check fail.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, BarWindow, Strategy, TargetIntent, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry, load
from tbot.strategies.sma_filter import SmaFilter
from tests.backtest.helpers import make_config

START = datetime(2018, 1, 1, tzinfo=UTC)  # long series must stay before the sealed holdout (2025-10-01)
N_BARS = {Timeframe.D1: 1_700, Timeframe.H4: 6 * 1_700}


def _ohlc(tf: Timeframe, n: int, seed: int = 20261011) -> list[Bar]:
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 10_000.0
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0.0005, 0.03)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.01)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
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


def _run(strategy: Strategy, tf: Timeframe, mode: str) -> tuple[object, ...]:
    bars = _with_volume(_ohlc(tf, N_BARS[tf]), mode)
    return _fingerprint(run_backtest(BacktestFeed(bars), strategy, make_config(fee="10", slip="5", tf=tf)))


def assert_volume_invariant(make: object, tf: Timeframe) -> None:
    """Runs ``make()`` three times (fresh instance each) and requires bit-identical results."""
    base = _run(make(), tf, "original")  # type: ignore[operator]
    for mode in ("random", "nan"):
        try:
            other = _run(make(), tf, mode)  # type: ignore[operator]
        except Exception as exc:  # any failure on NaN volume means volume was read
            raise AssertionError(f"strategy failed when volume was {mode}: {exc!r}") from exc
        assert other == base, f"behaviour changed when volume was {mode}"


@pytest.mark.parametrize("entry", PHASE3_ENTRIES, ids=lambda e: e.key)
def test_phase3_strategy_is_volume_invariant(entry: Phase3Entry) -> None:
    try:
        load(entry)
    except ModuleNotFoundError:
        pytest.skip(f"{entry.key}: not implemented yet ({entry.module}); blocks any strategy PR")
    assert_volume_invariant(lambda: load(entry), entry.timeframe)


@pytest.mark.parametrize(
    "make",
    [lambda: BuyAndHold(), lambda: SmaFilter(100)],
    ids=["buy_and_hold", "sma_filter_100"],
)
def test_harness_passes_volume_free_baselines(make: object) -> None:
    assert_volume_invariant(make, Timeframe.D1)


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
    with pytest.raises(AssertionError):
        assert_volume_invariant(lambda: _VolumeDependent(), Timeframe.D1)


def test_registry_matches_spec_grid() -> None:
    keys = [e.key for e in PHASE3_ENTRIES]
    assert len(keys) == 19 and len(set(keys)) == 19
    assert keys[0] == "S0"
    assert sum(k.startswith("C3-") for k in keys) == 6
    assert {e.timeframe for e in PHASE3_ENTRIES if e.key.startswith("C3-4h")} == {Timeframe.H4}
