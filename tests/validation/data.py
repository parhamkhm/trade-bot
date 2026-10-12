"""Data access for the G2 validation tests.

Real data: re-exported from ``tbot.validation.real_data`` (pre-holdout Binance klines from the local Parquet
store; it cannot load the sealed holdout). Tests that need the store skip cleanly when it is absent; the
synthetic fallback below keeps the truncation tests meaningful either way.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tbot.core.types import Bar, Timeframe
from tbot.validation.real_data import find_parquet_root, real_bars
from tests.sim_helpers import SYMBOL

__all__ = ["find_parquet_root", "real_bars", "synthetic_bars", "synthetic_hourly_for_days"]

# synthetic series live well before the sealed holdout so that the engine's own seal checks never trigger
SYNTH_START = datetime(2019, 1, 1, tzinfo=UTC)


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
