"""Reusable look-ahead check for every ``ReplayStrategy`` (C2-C5 copy the call, not the code).

``trace(full)[m]`` is recorded during ONE full replay over the whole series. A causal strategy must give the
same decision after bar ``m`` when it is shown only the prefix ``bars[:m + 1]``. A strategy that reads bar
``m + 1`` (or any statistic of the full series) differs from its own prefix decision at the end of nearly
every prefix. Comparing two runs that make identical calls (a "truncation" test of ``trace(n)`` against
``trace(n + k)`` on fresh instances) proves only determinism and cannot fail for look-ahead.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from tbot.core.types import Bar, BarWindow
from tbot.strategies.replay import PositionState, ReplayStrategy, TracePoint


def decision_on_prefix(strategy: ReplayStrategy, bars: Sequence[Bar], m: int) -> TracePoint:
    """(state, weight, stop) after bar ``m`` computed from ``bars[:m + 1]`` only."""
    window = BarWindow.of(bars[: m + 1])
    weight = strategy.on_bar(window).target_weight if m + 1 >= strategy.warmup_bars else 0.0
    return TracePoint(strategy.state(window), weight, strategy.active_stop(window))


def assert_no_lookahead(
    make_strategy: Callable[[], ReplayStrategy],
    bars: Sequence[Bar],
    *,
    fresh_every: int = 41,
) -> set[PositionState]:
    """Assert ``trace(full)[m] == decision on bars[:m + 1]`` for EVERY ``m``; return the states seen.

    The prefix decisions come from one instance fed growing windows (the production call pattern) and, every
    ``fresh_every`` bars and at the end, from a brand-new instance (a pure replay of the prefix).
    """
    full = make_strategy().trace(BarWindow.of(bars))
    assert len(full) == len(bars)
    incremental = make_strategy()
    bad: list[int] = []
    for m in range(len(bars)):
        differs = decision_on_prefix(incremental, bars, m) != full[m]
        if not differs and (m % fresh_every == 0 or m == len(bars) - 1):
            differs = decision_on_prefix(make_strategy(), bars, m) != full[m]
        if differs:
            bad.append(m)
    assert not bad, f"{len(bad)} of {len(bars)} decisions depend on bars after them; first at bar {bad[:5]}"
    return {p.state for p in full}
