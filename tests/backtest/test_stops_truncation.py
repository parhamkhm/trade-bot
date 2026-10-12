"""Truncation test (CLAUDE.md section 8) for the STOP and RE-ARM path (PR #26 review, MAJOR 2).

The G2 truncation tests in ``tests/validation`` run with ``enforce_stops=False``, so they never exercised the
stop machinery. Here the same property is checked with ``enforce_stops=True``: a run on data[:N] and a run on
data[:N+k] must agree on everything observable up to bar N -- fills (incl. stop fills), stop events (armed,
refused, rejected, filled, cancelled), positions, equity, decisions (incl. AWAITING_REARM) -- in the next-open
fill model and in the 6 h manual-delay model.

The strategy is the real ``SmaFilter(10)``: weight 1.0 above its SMA and a stop AT the SMA, so the runs
contain many stop-outs and re-arm waits (asserted below as vacuity guards). Cuts are placed around the
stop fills of a full run, where a bug would show first. ``StopPeeker`` proves the comparison has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from tbot.backtest.result import AWAITING_REARM, STOP_ARMED, STOP_FILL, BacktestResult
from tbot.core.types import Bar, BarWindow, Strategy
from tbot.strategies.sma_filter import SmaFilter
from tests.validation.data import synthetic_bars, synthetic_hourly_for_days
from tests.validation.harness import compare_prefix, make_cfg, run

SMA_N = 10
NEXT_OPEN_BARS = synthetic_bars(400, seed=7)
DELAY_DAYS = synthetic_bars(200, seed=23)
DELAY_HOURS = synthetic_hourly_for_days(DELAY_DAYS, seed=24)
KS = [1, 2, 4]


def hours_until(hours: list[Bar], days: list[Bar]) -> list[Bar]:
    limit = days[-1].ts
    return [h for h in hours if h.ts <= limit]


def compare_stop_prefix(short: BacktestResult, long: BacktestResult) -> list[str]:
    """``compare_prefix`` plus the stop bookkeeping. Stop events and fills are stamped at the open time of
    an execution bar, so (as for fills in ``snapshot``) only those strictly before the short run's last
    close are comparable; an event at exactly that close belongs to the next bar, which the short run
    lacks."""
    upto = short.equity[-1].ts
    diffs = compare_prefix(short, long)

    def events(res: BacktestResult) -> list[tuple[object, ...]]:
        return [(e.ts, e.kind, e.client_order_id, e.level, e.detail) for e in res.stop_events if e.ts < upto]

    def stop_flags(res: BacktestResult) -> list[tuple[object, ...]]:
        return [
            (r.fill.client_order_id, r.stop_fill, r.reference_price) for r in res.fills if r.fill.ts < upto
        ]

    if events(short) != events(long):
        diffs.append("stop_events")
    if stop_flags(short) != stop_flags(long):
        diffs.append("stop_fill_flags")
    return diffs


def sma_strategy() -> Strategy:
    return SmaFilter(SMA_N)


def cuts_around_stop_fills(res: BacktestResult, bars: list[Bar], limit: int) -> list[int]:
    """Cut sizes N such that the stop fill's own signal bar is just outside / at the edge / inside."""
    out: set[int] = set()
    for rec in res.stop_fills[:limit]:
        containing = next(i for i, b in enumerate(bars) if b.ts > rec.fill.ts)  # bar whose span has the fill
        out.update({containing, containing + 1, containing + 2})
    return sorted(n for n in out if SMA_N + 2 <= n < len(bars) - max(KS))


# ---------------------------------------------------------------------------------------------
# vacuity guards: the data really exercises stop-outs, re-arm waits and re-entries
# ---------------------------------------------------------------------------------------------


def _assert_exercises_stop_path(res: BacktestResult) -> None:
    assert len(res.stop_fills) >= 8
    assert sum(d.outcome == AWAITING_REARM for d in res.decisions) >= 3
    kinds = [e.kind for e in res.stop_events]
    first_stop = kinds.index(STOP_FILL)
    assert STOP_ARMED in kinds[first_stop:], "no position was re-opened (re-armed) after a stop-out"


def test_next_open_data_exercises_stops_and_rearm() -> None:
    _assert_exercises_stop_path(run(sma_strategy(), NEXT_OPEN_BARS, make_cfg(enforce_stops=True)))


def test_delay_data_exercises_stops_and_rearm() -> None:
    cfg = make_cfg(delay_hours=6, enforce_stops=True)
    _assert_exercises_stop_path(run(sma_strategy(), DELAY_DAYS, cfg, DELAY_HOURS))


# ---------------------------------------------------------------------------------------------
# honest strategy: invariant under truncation with stops enforced
# ---------------------------------------------------------------------------------------------


def test_stop_path_is_invariant_to_truncation_next_open() -> None:
    cfg = make_cfg(enforce_stops=True)
    full = run(sma_strategy(), NEXT_OPEN_BARS, cfg)
    cuts = cuts_around_stop_fills(full, NEXT_OPEN_BARS, limit=10)
    assert len(cuts) >= 15
    for n in cuts:
        short_run = run(sma_strategy(), NEXT_OPEN_BARS[:n], cfg)
        for k in KS:
            long_run = run(sma_strategy(), NEXT_OPEN_BARS[: n + k], cfg)
            assert compare_stop_prefix(short_run, long_run) == [], f"N={n} k={k}"


@pytest.mark.parametrize("exec_view", ["trimmed", "full"])
def test_stop_path_is_invariant_to_truncation_6h_delay(exec_view: str) -> None:
    """``full`` gives the short run ALL hourly bars: the engine must neither consume nor let a stop or a
    fill depend on an hourly bar past the last signal close."""
    cfg = make_cfg(delay_hours=6, enforce_stops=True)
    full = run(sma_strategy(), DELAY_DAYS, cfg, DELAY_HOURS)
    cuts = cuts_around_stop_fills(full, DELAY_DAYS, limit=8)
    assert len(cuts) >= 12
    for n in cuts:
        short_days = DELAY_DAYS[:n]
        short_exec = hours_until(DELAY_HOURS, short_days) if exec_view == "trimmed" else DELAY_HOURS
        short_run = run(sma_strategy(), short_days, cfg, short_exec)
        for k in KS:
            long_days = DELAY_DAYS[: n + k]
            long_run = run(sma_strategy(), long_days, cfg, hours_until(DELAY_HOURS, long_days))
            assert compare_stop_prefix(short_run, long_run) == [], f"{exec_view} N={n} k={k}"


# ---------------------------------------------------------------------------------------------
# teeth: a strategy whose STOP LEVEL peeks at the future must be flagged
# ---------------------------------------------------------------------------------------------


class StopPeeker(SmaFilter):
    """SMA entries, but the stop level looks two bars ahead in the full array it was built with: if the close
    two bars later is lower, the stop is put just below P_ref (so it fires at once), otherwise far away. With
    only data[:N] the lookup finds nothing near the cut and falls back to 'far away'."""

    def __init__(self, data: list[Bar]) -> None:
        super().__init__(SMA_N)
        self.id = "stop_peeker"
        self._index = {b.ts: i for i, b in enumerate(data)}
        self._closes = [b.close for b in data]

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        i = self._index[window.as_of]
        if i + 2 < len(self._closes) and self._closes[i + 2] < self._closes[i]:
            return entry_price * Decimal("0.9999")
        return entry_price / 2


def _peeker_flags(
    make: Callable[[list[Bar]], Strategy],
    days: list[Bar],
    hours: list[Bar] | None,
    cuts: range,
    delay: int,
) -> tuple[int, int]:
    """(cuts flagged by compare_stop_prefix, cuts flagged by the stop-blind compare_prefix)."""
    cfg = make_cfg(delay_hours=delay, enforce_stops=True)
    flagged = flagged_blind = 0
    for n in cuts:
        long_days, short_days = days[: n + 3], days[:n]
        long_run = run(make(long_days), long_days, cfg, hours_until(hours, long_days) if hours else None)
        short_run = run(make(short_days), short_days, cfg, hours_until(hours, short_days) if hours else None)
        diffs = compare_stop_prefix(short_run, long_run)
        flagged += bool(diffs)
        flagged_blind += bool(compare_prefix(short_run, long_run))
    return flagged, flagged_blind


def test_stop_peeker_is_detected_next_open() -> None:
    flagged, _ = _peeker_flags(StopPeeker, NEXT_OPEN_BARS, None, range(40, 200), delay=0)
    assert flagged >= 5


def test_stop_peeker_is_detected_6h_delay() -> None:
    flagged, _ = _peeker_flags(StopPeeker, DELAY_DAYS, DELAY_HOURS, range(40, 190), delay=6)
    assert flagged >= 5


def test_honest_strategy_is_never_flagged_over_the_same_scan() -> None:
    """Control for the two tests above: the same scan with the honest SMA stop reports nothing."""
    flagged, flagged_blind = _peeker_flags(lambda _d: sma_strategy(), NEXT_OPEN_BARS, None, range(40, 100), 0)
    assert flagged == 0 and flagged_blind == 0


def test_comparison_sees_a_stop_event_difference_that_the_plain_prefix_check_misses() -> None:
    """A stop event is invisible to ``compare_prefix`` (it compares fills, not events). Two runs on the same
    bars with different stop rules differ in stop events at the same horizon: ``compare_stop_prefix``
    says so."""
    cfg = make_cfg(enforce_stops=True)
    bars = NEXT_OPEN_BARS[:120]
    a = run(SmaFilter(SMA_N), bars, cfg)
    b = run(StopPeeker(bars), bars, cfg)
    assert "stop_events" in compare_stop_prefix(a, b)
