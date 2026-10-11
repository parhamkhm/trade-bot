"""G2 item 1: the truncation test (CLAUDE.md section 8).

Run the engine on data[:N] and on data[:N+k]. Everything observable for bars <= N (positions, weights, equity,
cash, fills, decisions, fill-time resizes and rejections) must be identical. Honest strategies must pass,
deliberately cheating ones must be flagged, in both fill modes (next open, and the 6 h manual delay).

Data: real Binance BTCUSDT bars before the sealed holdout (skipped when the local Parquet store is absent)
and a seeded synthetic random walk with opening gaps. ``tests.validation.data`` cannot load the holdout.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.core.types import Bar, BarWindow, Strategy, TargetIntent, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars, synthetic_hourly_for_days
from tests.validation.harness import Peeker, compare_prefix, make_cfg, run, snapshot

D1, H1 = Timeframe.D1, Timeframe.H1

needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")


# ---------------------------------------------------------------------------------------------
# data sets (module-level cache: loading Parquet and building Decimal bars is the slow part)
# ---------------------------------------------------------------------------------------------

_CACHE: dict[str, object] = {}


def _daily(source: str) -> list[Bar]:
    key = f"daily-{source}"
    if key not in _CACHE:
        if source == "real":
            _CACHE[key] = real_bars(
                "BTCUSDT", D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
            )
        else:
            _CACHE[key] = synthetic_bars(700, seed=7)
    return _CACHE[key]  # type: ignore[return-value]


def _delay_data(source: str) -> tuple[list[Bar], list[Bar]]:
    key = f"delay-{source}"
    if key not in _CACHE:
        if source == "real":
            start, end = datetime(2022, 7, 1, tzinfo=UTC), datetime(2023, 7, 2, tzinfo=UTC)
            days = real_bars("BTCUSDT", D1, start, end)
            hours = real_bars("BTCUSDT", H1, start, end)
        else:
            days = synthetic_bars(330, seed=21)
            hours = synthetic_hourly_for_days(days, seed=22)
        _CACHE[key] = (days, hours)
    return _CACHE[key]  # type: ignore[return-value]


SOURCES = [pytest.param("real", marks=needs_store), pytest.param("synthetic")]
HONEST: dict[str, Callable[[], Strategy]] = {
    "buy_and_hold": lambda: BuyAndHold(),
    "sma20": lambda: SmaFilter(20),
    "sma100": lambda: SmaFilter(100),
}
CUTS_NEXT_OPEN = {"real": [130, 300, 700, 1500, 2500], "synthetic": [120, 250, 400, 600]}
KS_NEXT_OPEN = [1, 2, 7, 40]
CUTS_DELAY = [110, 180, 260]
KS_DELAY = [1, 2, 5, 25]


def _hours_until(hours: list[Bar], days: list[Bar]) -> list[Bar]:
    """Execution bars a run over ``days`` can legitimately reference: closed by the last signal close."""
    limit = days[-1].ts
    return [h for h in hours if h.ts <= limit]


# ---------------------------------------------------------------------------------------------
# honest strategies must be invariant under truncation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", sorted(HONEST))
def test_honest_strategies_are_invariant_to_truncation_next_open(source: str, name: str) -> None:
    bars, cfg = _daily(source), make_cfg()
    checked = 0
    for n in CUTS_NEXT_OPEN[source]:
        for k in KS_NEXT_OPEN:
            long_run = run(HONEST[name](), bars[: n + k], cfg)
            short_run = run(HONEST[name](), bars[:n], cfg)
            assert compare_prefix(short_run, long_run) == [], f"{name} {source} N={n} k={k}"
            assert len(snapshot(short_run, short_run.equity[-1].ts).positions) == n
            checked += 1
    assert checked == len(CUTS_NEXT_OPEN[source]) * len(KS_NEXT_OPEN)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", ["buy_and_hold", "sma20"])
@pytest.mark.parametrize("exec_view", ["trimmed", "full"])
def test_honest_strategies_are_invariant_to_truncation_6h_delay(
    source: str, name: str, exec_view: str
) -> None:
    """``trimmed`` hands the short run only the hourly bars closed by its last signal bar; ``full`` hands it
    ALL hourly bars (a stronger check: the engine must not consume, or let a fill depend on, an hourly bar
    past the last signal close)."""
    days, hours = _delay_data(source)
    cfg = make_cfg(delay_hours=6)
    for n in CUTS_DELAY:
        for k in KS_DELAY:
            long_days, short_days = days[: n + k], days[:n]
            long_run = run(HONEST[name](), long_days, cfg, _hours_until(hours, long_days))
            short_exec = _hours_until(hours, short_days) if exec_view == "trimmed" else hours
            short_run = run(HONEST[name](), short_days, cfg, short_exec)
            assert compare_prefix(short_run, long_run) == [], f"{name} {source} {exec_view} N={n} k={k}"
            if name == "sma20":
                assert long_run.fills, "a delay-mode SMA run with no fills would make this test vacuous"


@pytest.mark.parametrize("source", SOURCES)
def test_truncation_runs_actually_trade(source: str) -> None:
    """Guard against a vacuous pass: the SMA runs compared above must contain fills and flips."""
    bars = _daily(source)[: CUTS_NEXT_OPEN[source][-1]]
    res = run(SmaFilter(20), bars, make_cfg())
    assert len(res.fills) >= 10
    assert {r.fill.side.value for r in res.fills} == {"BUY", "SELL"}


# ---------------------------------------------------------------------------------------------
# cheating strategies must be caught (this proves the harness has teeth)
# ---------------------------------------------------------------------------------------------


class FullSampleMeanCheater:
    """Long when close > the mean of ALL closes in the data it was built with (a full-column aggregate)."""

    warmup_bars = 1

    def __init__(self, data: list[Bar]) -> None:
        self.id = "full_sample_mean"
        self._mean = float(sum(b.close for b in data) / len(data))

    def on_bar(self, window: BarWindow) -> TargetIntent:
        weight = 1.0 if float(window.last.close) > self._mean else 0.0
        return TargetIntent(self.id, window.as_of, weight, "close vs full-sample mean")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2


@pytest.mark.parametrize("source", SOURCES)
def test_peeker_is_detected_exactly_when_its_peek_changes_the_decision(source: str) -> None:
    """Peeker(data[:N]) falls back to its own close at the cut (weight 0), Peeker(data[:N+k]) sees close(N+1).
    The comparison must flag the pair exactly when those two weights differ: no misses, no false alarms."""
    bars, cfg = _daily(source), make_cfg()
    flagged = expected = 0
    cuts = list(range(130, 130 + 60)) if source == "synthetic" else list(range(300, 300 + 60))
    for n in cuts:
        long_run = run(Peeker(bars[: n + 2]), bars[: n + 2], cfg)
        short_run = run(Peeker(bars[:n]), bars[:n], cfg)
        long_weight_at_cut = next(
            d.target_weight for d in long_run.decisions if d.ts == short_run.equity[-1].ts
        )
        diffs = compare_prefix(short_run, long_run)
        will_differ = long_weight_at_cut != 0.0
        assert bool(diffs) == will_differ, f"N={n}: diffs={diffs} long weight at cut={long_weight_at_cut}"
        flagged += bool(diffs)
        expected += will_differ
    assert flagged == expected > 5  # a vacuous scan (peeker always flat) would prove nothing


@pytest.mark.parametrize("source", SOURCES)
def test_peeker_is_detected_in_6h_delay_mode(source: str) -> None:
    days, hours = _delay_data(source)
    cfg = make_cfg(delay_hours=6)
    flagged = 0
    for n in range(150, 190):
        long_days, short_days = days[: n + 2], days[:n]
        long_run = run(Peeker(long_days), long_days, cfg, _hours_until(hours, long_days))
        short_run = run(Peeker(short_days), short_days, cfg, _hours_until(hours, short_days))
        diffs = compare_prefix(short_run, long_run)
        long_weight = next(d.target_weight for d in long_run.decisions if d.ts == short_run.equity[-1].ts)
        assert bool(diffs) == (long_weight != 0.0), f"N={n}: {diffs}"
        flagged += bool(diffs)
    assert flagged > 5


@pytest.mark.parametrize("source", SOURCES)
def test_full_sample_aggregate_cheater_is_detected(source: str) -> None:
    bars, cfg = _daily(source), make_cfg()
    n = CUTS_NEXT_OPEN[source][1]
    long_run = run(FullSampleMeanCheater(bars[: n + 40]), bars[: n + 40], cfg)
    short_run = run(FullSampleMeanCheater(bars[:n]), bars[:n], cfg)
    assert compare_prefix(short_run, long_run), "a full-column aggregate must change decisions before the cut"


def test_comparison_reports_nothing_for_identical_runs_and_something_for_different_data() -> None:
    """Sanity of the harness: a run compared with itself reports nothing, the same strategy on different
    data reports differences."""
    bars = synthetic_bars(300, seed=3)
    a = run(SmaFilter(20), bars[:250], make_cfg())
    assert compare_prefix(a, a) == []
    b = run(SmaFilter(20), synthetic_bars(300, seed=4)[:250], make_cfg())
    assert compare_prefix(a, b) != []
