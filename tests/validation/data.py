"""Data access for the G2 validation tests.

Real data: Binance public klines from the local Parquet store, loaded through the normal sealed-holdout
loader (``load_bars`` with ``allow_holdout=False``). On top of that every helper here asserts that no
returned bar closes on or after ``CANONICAL_HOLDOUT_START`` and never passes ``allow_holdout``: this module
cannot load the sealed holdout.

The Parquet store is not committed. A worktree therefore looks for it in (1) the ``TBOT_VALIDATION_DATA``
environment variable, (2) ``<repo>/data/parquet`` and (3) every ancestor directory of the repo (a git
worktree lives under ``<main checkout>/.claude/worktrees/<name>``). Tests that need it skip cleanly when it
is absent; the synthetic fallback below keeps the truncation tests meaningful either way.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tbot.core.config import DataConfig
from tbot.core.types import Bar, Timeframe
from tbot.data.binance_loader import CANONICAL_HOLDOUT_START, load_bars
from tests.sim_helpers import SYMBOL

__all__ = ["find_parquet_root", "real_bars", "synthetic_bars", "synthetic_hourly_for_days"]

_REPO = Path(__file__).resolve().parents[2]
# synthetic series live well before the sealed holdout so that the engine's own seal checks never trigger
SYNTH_START = datetime(2019, 1, 1, tzinfo=UTC)


def find_parquet_root() -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("TBOT_VALIDATION_DATA")
    if env:
        candidates.append(Path(env))
    for base in (_REPO, *_REPO.parents):
        candidates.append(base / "data" / "parquet")
    for path in candidates:
        if (path / "klines" / "source=binance").is_dir():
            return path
    return None


def real_bars(
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> list[Bar]:
    """Pre-holdout Binance bars with ``start <= bar.ts < end``. Raises ``FileNotFoundError`` if no store."""
    if end > CANONICAL_HOLDOUT_START:
        raise ValueError(f"validation tests must not read past the holdout start (end={end})")
    root = find_parquet_root()
    if root is None:
        raise FileNotFoundError("no Parquet store found (set TBOT_VALIDATION_DATA)")
    bars = load_bars(
        symbol,
        timeframe,
        start=start,
        end=end,
        config=DataConfig(parquet_root=root),
        caller="tests.validation.data",
        reason="G2 validation (pre-holdout only)",
    )
    bars = [b for b in bars if start <= b.ts < end]
    assert all(b.ts < CANONICAL_HOLDOUT_START for b in bars)
    return bars


def synthetic_bars(
    n: int,
    *,
    seed: int = 7,
    timeframe: Timeframe = Timeframe.D1,
    start: datetime = SYNTH_START,
    start_price: float = 20_000.0,
    daily_vol: float = 0.035,
    gap_vol: float = 0.004,
    symbol: str = SYMBOL,
) -> list[Bar]:
    """Seeded random walk with opening gaps (open != previous close), two-decimal prices."""
    rng = random.Random(seed)
    out: list[Bar] = []
    prev_close = start_price
    for i in range(n):
        o = max(1.0, prev_close * (1.0 + rng.gauss(0.0, gap_vol)))
        c = max(1.0, o * (1.0 + rng.gauss(0.0002, daily_vol)))
        hi = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.005)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.005)))
        ts = start + (i + 1) * timeframe.delta
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=Decimal(f"{o:.2f}"),
                high=Decimal(f"{hi:.2f}"),
                low=Decimal(f"{lo:.2f}"),
                close=Decimal(f"{c:.2f}"),
                volume=Decimal("10"),
            )
        )
        prev_close = c
    return out


def synthetic_hourly_for_days(days: list[Bar], *, seed: int = 11) -> list[Bar]:
    """24 hourly bars per day: a seeded path from the day's open to the day's close (high/low envelope)."""
    rng = random.Random(seed)
    out: list[Bar] = []
    for day in days:
        o_day, c_day = float(day.open), float(day.close)
        prices = [o_day]
        for h in range(1, 24):
            frac = h / 24.0
            prices.append(max(1.0, o_day + (c_day - o_day) * frac + rng.gauss(0.0, 0.004) * o_day))
        prices.append(c_day)
        for h in range(24):
            o, c = Decimal(f"{prices[h]:.2f}"), Decimal(f"{prices[h + 1]:.2f}")
            out.append(
                Bar(
                    symbol=day.symbol,
                    timeframe=Timeframe.H1,
                    ts=day.open_ts + timedelta(hours=h + 1),
                    open=o,
                    high=max(o, c),
                    low=min(o, c),
                    close=c,
                    volume=Decimal("1"),
                )
            )
    return out
