"""The SPEC 7.2 B eligibility table, recomputed from the REAL local Binance BTCUSDT data (every cell).

The table in docs/SPEC.md is parsed from the markdown (so a later edit of the SPEC table changes what is
compared) and compared with ``eligibility_table`` over the 17 grid points: the first-tradable-close column and
every check/dash of the 12 period columns. Skipped when the local Parquet store is absent (CI).

Synthetic tests of the rules themselves (which need no store) are below.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from tbot.core.types import Timeframe
from tbot.validation.eligibility import (
    eligibility_table,
    first_tradable_close,
    is_eligible,
    required_closes,
    tradable_days,
)
from tbot.validation.grid import (
    HISTORY_START,
    HOLDOUT_START,
    SPEC_PERIODS,
    TRAIN_START,
    build_grid,
    grid_points,
)
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars

SPEC = Path(__file__).resolve().parents[2] / "docs" / "SPEC.md"
_ROW = re.compile(
    r"^\|\s*(C[1-5])\s*\|\s*(?P<point>[^|]+?)\s*\|\s*(?P<first>[\d-]+ [\d:]+)\s*\|(?P<cells>.*)\|\s*$"
)


def _key(family: str, point: str) -> str:
    """'(10, 28, 50) base' -> 'C1-(10,28,50)'; '1d f=(8,16,32)' -> 'C3-1d-(8,16,32)'; 'p=10' -> 'C4-p10'."""
    p = point.replace(" base", "").strip()
    if family in {"C1", "C2"}:
        return f"{family}-{p.replace(' ', '')}"
    if family == "C3":
        tf, spans = p.split(" f=")
        return f"C3-{tf}-{spans.replace(' ', '')}"
    return f"{family}-{p.replace('=', '').replace(' ', '')}"


def parse_spec_table() -> dict[str, tuple[datetime, tuple[bool, ...]]]:
    table: dict[str, tuple[datetime, tuple[bool, ...]]] = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group("cells").split("|")]
        flags = tuple(c == "✓" for c in cells)
        assert all(c in {"✓", chr(0x2013)} for c in cells), line  # check mark / en dash
        first = datetime.strptime(m.group("first"), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        table[_key(m.group(1), m.group("point"))] = (first, flags)
    return table


def test_parse_finds_all_17_rows_matching_the_registry() -> None:
    table = parse_spec_table()
    assert set(table) == set(grid_points()), set(table) ^ set(grid_points())
    assert all(len(flags) == 12 for _, flags in table.values())


def test_periods_are_the_twelve_of_the_spec() -> None:
    assert len(SPEC_PERIODS) == 12
    assert SPEC_PERIODS[0].start == datetime(2020, 1, 1, tzinfo=UTC)
    assert SPEC_PERIODS[-1].start == datetime(2025, 7, 1, tzinfo=UTC)
    assert SPEC_PERIODS[-1].end == HOLDOUT_START
    assert all(a.end == b.start for a, b in pairwise(SPEC_PERIODS))
    # eleven half-years and one quarter
    assert [(p.end - p.start).days for p in SPEC_PERIODS[:-1]][:2] == [182, 184]
    assert (SPEC_PERIODS[-1].end - SPEC_PERIODS[-1].start).days == 92


@pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not present")
def test_eligibility_equals_the_spec_table_on_real_data() -> None:
    bars = {
        Timeframe.D1: real_bars("BTCUSDT", Timeframe.D1, HISTORY_START, HOLDOUT_START),
        Timeframe.H4: real_bars("BTCUSDT", Timeframe.H4, HISTORY_START, HOLDOUT_START),
    }
    assert len(bars[Timeframe.D1]) == 2829  # SPEC 7.2 B: 2018-01-02 .. 2025-09-30, no gaps
    assert len(bars[Timeframe.H4]) == 16_963
    spec = parse_spec_table()
    points = list(grid_points().values())
    rows = {r.key: r for r in eligibility_table(points, bars, SPEC_PERIODS)}
    assert set(rows) == set(spec)
    problems = []
    for key, (first, flags) in spec.items():
        if rows[key].first_tradable != first:
            problems.append(f"{key}: first tradable {rows[key].first_tradable} != SPEC {first}")
        if rows[key].eligible != flags:
            problems.append(f"{key}: eligibility {rows[key].eligible} != SPEC {flags}")
    assert not problems, "\n".join(problems)


def test_required_closes_follow_the_spec_text() -> None:
    assert required_closes("C1", {"lookbacks": (40, 110, 200)}) == 201
    assert required_closes("C2", {"horizons": (60, 180, 360)}) == 361
    assert required_closes("C3", {"fast_spans": (16, 32, 64)}) == 768
    assert required_closes("C3", {"fast_spans": (8, 16, 32)}) == 384
    assert required_closes("C4", {"pct": 20}) == 385
    assert required_closes("C5", {"rsi_entry": 10}) == 200


def test_first_tradable_is_floored_at_the_s0_close_and_uses_the_present_bars() -> None:
    daily = synthetic_bars(900, start=HISTORY_START, timeframe=Timeframe.D1)
    pts = grid_points()
    by_tf = {Timeframe.D1: daily}
    # close #n is HISTORY_START + n days
    assert first_tradable_close(pts["C1-(20,55,100)"], by_tf) == TRAIN_START  # 101 < 151: S0 floor
    assert first_tradable_close(pts["C1-(40,110,200)"], by_tf) == HISTORY_START + timedelta(days=201)
    assert first_tradable_close(pts["C4-p20"], by_tf) == HISTORY_START + timedelta(days=385)
    # 4h counts the bars that exist: drop 3 bars and the 1536th present bar is 3 bars later
    h4 = synthetic_bars(1600, start=HISTORY_START, timeframe=Timeframe.H4)
    pt = pts["C3-4h-(32,64,128)"]
    full = first_tradable_close(pt, {Timeframe.H4: h4})
    holes = [b for i, b in enumerate(h4) if i not in {10, 11, 12}]
    assert first_tradable_close(pt, {Timeframe.H4: holes}) == h4[1535 + 3].ts
    assert full == h4[1535].ts


def test_tradable_days_and_the_365_day_threshold_are_exact() -> None:
    closes = [HISTORY_START + timedelta(days=i) for i in range(1, 1200)]
    first = closes[150]  # 2018-06-01
    start = first + timedelta(days=365)  # exactly 365 closes in [first, start)
    assert tradable_days(first, closes, start) == 365
    assert is_eligible(first, closes, start)
    assert not is_eligible(first, closes, start - timedelta(days=1))  # 364
    assert tradable_days(first, closes, first) == 0


def test_build_grid_has_17_points_one_base_per_family() -> None:
    grid = build_grid()
    assert sum(len(v) for v in grid.values()) == 17
    assert [len(grid[f]) for f in ("C1", "C2", "C3", "C4", "C5")] == [3, 3, 5, 3, 3]
    assert all(sum(p.is_base for p in pts) == 1 for pts in grid.values())
