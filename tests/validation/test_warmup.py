"""G2 item 2: warm-up stability (CLAUDE.md section 8).

SMA values must be stable to <= 1e-9 once ``warmup_bars`` bars exist, whatever the start offset of the data
the indicator is shown. Three independent views:

1. the strategy's own ``sma(window)`` on windows that start at different offsets vs one another;
2. the same value vs an independent pandas ``rolling(n).mean()`` over the whole history (relative 1e-9);
3. through the engine: the target weights the SMA filter emits after the warm-up are identical for runs
   that start at different offsets, and no decision is taken before ``warmup_bars`` bars exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd  # type: ignore[import-untyped]
import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.strategies.sma_filter import SmaFilter
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars
from tests.validation.harness import make_cfg, run

TOL = 1e-9
OFFSETS = [0, 1, 7, 33, 100, 251]


def _series(source: str) -> list[Bar]:
    if source == "real":
        return real_bars(
            "BTCUSDT", Timeframe.D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
        )
    return synthetic_bars(900, seed=5)


SOURCES = [
    pytest.param("real", marks=pytest.mark.skipif(find_parquet_root() is None, reason="no local store")),
    pytest.param("synthetic"),
]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("n", [20, 100, 200])
def test_sma_is_stable_across_start_offsets_after_warmup(source: str, n: int) -> None:
    bars = _series(source)
    strat = SmaFilter(n)
    reference = [strat.sma(BarWindow.of(bars[t - n + 1 : t + 1])) for t in range(n - 1, len(bars))]
    worst = 0.0
    for offset in OFFSETS:
        sub = bars[offset:]
        for t in range(n - 1, len(sub), 13):  # every 13th bar keeps the test fast and still dense
            value = strat.sma(BarWindow.of(sub[: t + 1]))
            ref = reference[offset + t - (n - 1)]
            worst = max(worst, abs(value - ref))
    assert worst <= TOL, f"max |difference| across start offsets = {worst:.3e}"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("n", [20, 100, 200])
def test_sma_matches_an_independent_pandas_rolling_mean(source: str, n: int) -> None:
    bars = _series(source)
    strat = SmaFilter(n)
    pandas_sma = pd.Series([float(b.close) for b in bars]).rolling(n).mean()
    worst_rel = 0.0
    for t in range(n - 1, len(bars), 7):
        value = strat.sma(BarWindow.of(bars[: t + 1]))
        worst_rel = max(worst_rel, abs(value - pandas_sma.iloc[t]) / pandas_sma.iloc[t])
    assert worst_rel <= TOL, f"max relative difference vs pandas rolling mean = {worst_rel:.3e}"


@pytest.mark.parametrize("source", SOURCES)
def test_sma_filter_weights_do_not_depend_on_where_the_data_starts(source: str) -> None:
    n = 50
    bars = _series(source)[:900]
    cfg = make_cfg()
    by_start: dict[int, dict[datetime, float]] = {}
    for offset in OFFSETS:
        sub = bars[offset:]
        res = run(SmaFilter(n), sub, cfg)
        assert res.first_decision_index == n - 1, "decisions must start exactly when warmup_bars bars exist"
        assert len(res.decisions) == len(sub) - (n - 1)
        by_start[offset] = {d.ts: d.target_weight for d in res.decisions}
    base = by_start[0]
    for offset, weights in by_start.items():
        common = set(weights) & set(base)
        assert len(common) == len(weights), "every decision of a later start must also exist in the base run"
        assert all(weights[ts] == base[ts] for ts in common), f"offset {offset} disagrees with offset 0"


def test_no_decision_is_taken_before_the_warmup_is_complete() -> None:
    bars = synthetic_bars(120, seed=9)
    res = run(SmaFilter(100), bars, make_cfg())
    assert res.first_decision_index == 99
    assert all(p.quantity == 0 for p in res.equity[:100])  # nothing can be held before the first fill
    assert res.decisions[0].ts == bars[99].ts
