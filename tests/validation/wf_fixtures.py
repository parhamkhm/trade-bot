"""Stand-in families and synthetic markets for the walk-forward harness tests.

The real Phase 3 strategy modules are not on this branch, so the harness takes an injectable strategy factory
and the tests use ``SmaFilter(n)`` points as a three-point family "T" on seeded synthetic data. A short
calendar
(three periods, 2020-01-01 .. 2020-10-01) keeps every engine run fast; the SPEC calendar itself is covered by
``test_eligibility_vs_spec.py`` and ``test_walk_forward.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache

from tbot.core.types import Bar, Strategy, Timeframe
from tbot.strategies.sma_filter import SmaFilter
from tbot.validation.grid import GridPoint, make_periods
from tbot.validation.runs import Market, SeriesRun
from tbot.validation.walk_forward import Candidate, WalkForwardSpec
from tests.validation.data import synthetic_bars, synthetic_hourly_for_days

__all__ = [
    "BOUNDARY",
    "CANDIDATE",
    "FIRST_TRADABLE",
    "N_BARS",
    "POINTS",
    "SPEC",
    "factory",
    "fake_run",
    "make_market",
]

D1, H1 = Timeframe.D1, Timeframe.H1
N_BARS = 700  # 2019-01-02 .. 2020-12-01 closes
BOUNDARY = datetime(2020, 4, 1, tzinfo=UTC)  # start of the second test period

SPEC = WalkForwardSpec(
    periods=make_periods(
        [datetime(2020, 1, 1, tzinfo=UTC), BOUNDARY, datetime(2020, 7, 1, tzinfo=UTC)],
        datetime(2020, 10, 1, tzinfo=UTC),
    ),
    train_start=datetime(2019, 2, 1, tzinfo=UTC),
    min_train_days=60,
    final_train_end=datetime(2020, 10, 1, tzinfo=UTC),
)

POINTS: tuple[GridPoint, ...] = tuple(
    GridPoint(f"T-sma{n}", "T", D1, {"n": n}, n == 20, i) for i, n in enumerate((10, 20, 40))
)
CANDIDATE = Candidate("T", POINTS)


def factory(key: str) -> Strategy:
    """``S0`` is a 30-day SMA filter; ``T-smaN`` is an N-day SMA filter."""
    if key == "S0":
        return SmaFilter(30)
    return SmaFilter(int(key.removeprefix("T-sma")))


@cache
def _days(seed: int, symbol: str) -> tuple[Bar, ...]:
    return tuple(synthetic_bars(N_BARS, seed=seed, symbol=symbol))


def make_market(
    *,
    seed: int = 7,
    symbol: str = "BTCUSDT",
    tail_seed: int | None = None,
    tail_from: datetime | None = None,
) -> Market:
    """Daily + hourly synthetic market; bars closing at or after ``tail_from`` come from ``tail_seed``."""
    days = list(_days(seed, symbol))
    if tail_seed is not None:
        assert tail_from is not None
        other = _days(tail_seed, symbol)
        days = [b for b in days if b.ts < tail_from] + [b for b in other if b.ts >= tail_from]
    hours = synthetic_hourly_for_days(days, seed=11)
    base = "BTC" if symbol.startswith("BTC") else "ETH"
    return Market(symbol, {D1: days, H1: hours}, base_asset=base)


def _first_tradable() -> dict[str, datetime]:
    closes = [b.ts for b in _days(7, "BTCUSDT")]
    return {p.key: closes[int(p.params["n"])] for p in POINTS}  # type: ignore[call-overload]


FIRST_TRADABLE = _first_tradable()


def fake_run(closes: list[datetime], returns: list[float], *, periods_per_year: float = 365.0) -> SeriesRun:
    """A ``SeriesRun`` over ``closes`` whose return stamped ``closes[i+1]`` is ``returns[i]``."""
    assert len(closes) == len(returns) + 1
    equity = [1.0]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    return SeriesRun(
        ts=tuple(closes),
        equity=tuple(equity),
        periods_per_year=periods_per_year,
        n_fills=0,
        fees_paid=0.0,
        slippage_cost=0.0,
        traded_notional=0.0,
        n_stop_events=0,
    )
