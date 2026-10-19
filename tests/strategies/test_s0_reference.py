"""Independent cross-check of S0 on real Binance BTCUSDT 1d bars (before 2025-10-01 only).

Everything on the reference side is written separately from the production code, straight from SPEC 7.1:
pandas rolling means for SMA100 and ATR20, a pandas / numpy kernel for sigma_hat (D-056), and a plain
per-bar loop for the FLAT / LONG / STOPPED machine. Skips cleanly when the local Parquet store is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
import pytest

from tbot.core.types import Bar, BarWindow, Timeframe
from tbot.indicators import atr_simple, sigma_hat_kernel, sma
from tbot.strategies.replay import TracePoint
from tbot.strategies.s0_sma100 import make
from tests.strategies.test_s0_sma100 import FLAT, LONG, STOPPED, needs_store
from tests.validation.data import real_bars

D1 = Timeframe.D1
TOL = 1e-9
ALPHA, K, SMA_N, ATR_N = 2 / 31, 150, 100, 20


def reference_indicators(bars: list[Bar]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
        }
    )
    prev = frame["close"].shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - prev).abs(), (frame["low"] - prev).abs()], axis=1
    ).max(axis=1)
    weights = (1.0 - ALPHA) ** np.arange(K)  # weight of r_{t-k}
    squared = np.log(frame["close"] / prev) ** 2

    def kernel(window: np.ndarray) -> float:  # window[-1] is r_t
        return float(np.sqrt((weights * window[::-1]).sum() / weights.sum()) * np.sqrt(365.0))

    return pd.DataFrame(
        {
            "sma": frame["close"].rolling(SMA_N).mean(),
            "atr": true_range.rolling(ATR_N).mean(),  # valid from index 20; index 0 has no previous close
            "sigma": squared.rolling(K).apply(kernel, raw=True),
        }
    )


def reference_s0(bars: list[Bar], ind: pd.DataFrame, vol_target: float = 0.30) -> list[TracePoint]:
    """Plain loop over SPEC 7.1: stop first (also on the entry bar), then the close; one state per bar."""
    out: list[TracePoint] = []
    state, stop, pending_atr = FLAT, None, None
    for i, bar in enumerate(bars):
        if state is LONG:
            if pending_atr is not None:
                stop = max(bar.open - 3 * Decimal(repr(pending_atr)), Decimal("0.01"))
                pending_atr = None
            if stop is not None and bar.low <= stop:
                state = STOPPED
        above = i + 1 >= SMA_N and float(bar.close) > ind["sma"].iloc[i]
        if state is not FLAT and not above:
            state, stop = FLAT, None
        elif state is FLAT and above and i >= K:  # the 151st close, index 150, is the first tradable one
            state, stop, pending_atr = LONG, None, float(ind["atr"].iloc[i])
        weight = min(1.0, vol_target / float(ind["sigma"].iloc[i])) if state is LONG else 0.0
        out.append(TracePoint(state, weight, stop if state is LONG else None))
    return out


@needs_store
def test_s0_matches_an_independently_written_pandas_reference_on_real_data() -> None:
    bars = real_bars("BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC))
    assert bars[0].ts == datetime(2018, 1, 2, tzinfo=UTC) and bars[-1].ts < datetime(2025, 10, 1, tzinfo=UTC)
    ind = reference_indicators(bars)

    high, low, close = (np.array([float(getattr(b, f)) for b in bars]) for f in ("high", "low", "close"))
    for name, mine, start in (
        ("sma", sma(close, SMA_N), SMA_N - 1),
        ("atr", atr_simple(high, low, close, ATR_N), ATR_N),
        ("sigma", sigma_hat_kernel(close, alpha=ALPHA, k=K), K),
    ):
        diff = np.abs(mine[start:] - ind[name].to_numpy()[start:])
        assert np.isfinite(mine[start:]).all() and diff.max() <= TOL, (name, diff.max())
        assert np.isnan(mine[:start]).all()

    got = make(timeframe=D1).trace(BarWindow.of(bars))
    expected = reference_s0(bars, ind)
    assert len(got) == len(expected) == len(bars)
    for i, (g, e) in enumerate(zip(got, expected, strict=True)):
        assert g.state is e.state, (i, bars[i].ts, g.state, e.state)
        assert abs(g.weight - e.weight) <= TOL, (i, g.weight, e.weight)
        assert (g.stop is None) == (e.stop is None), (i, g.stop, e.stop)
        if g.stop is not None and e.stop is not None:
            assert abs(g.stop - e.stop) <= Decimal("1e-9"), (i, g.stop, e.stop)

    states = {p.state for p in got}
    # STOPPED needs a 3 x ATR20 drop below the entry open while the close stays above the SMA; the synthetic
    # tests cover that state, real BTC 1d bars (2018-2025-09) never reach it.
    assert {FLAT, LONG} <= states, states
    first_long = next(i for i, p in enumerate(got) if p.state is LONG)
    assert first_long >= 150, first_long  # the 151st close (2018-06-01) is the first tradable one
    assert bars[150].ts == datetime(2018, 6, 1, tzinfo=UTC)
    assert all(p.state is FLAT and p.weight == 0.0 for p in got[:150])


@pytest.mark.parametrize("vol_target", [0.2, 0.4])
@needs_store
def test_s0_vol_target_variants_scale_the_weight_like_the_reference(vol_target: float) -> None:
    bars = real_bars("BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC))
    ind = reference_indicators(bars)
    got = make(timeframe=D1, vol_target=vol_target).trace(BarWindow.of(bars))
    expected = reference_s0(bars, ind, vol_target)
    assert all(
        g.state is e.state and abs(g.weight - e.weight) <= TOL for g, e in zip(got, expected, strict=True)
    )
