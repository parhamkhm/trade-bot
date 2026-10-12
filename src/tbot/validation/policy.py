"""The Phase 3 decisions of 2026-10-12 (SPEC D-090..D-093) as the harness implements them.

Nothing here is open: Parham decided the four items, and the harness does what they say. Every number the
harness produces is still PROVISIONAL (SPEC 7.2 F); that word is about the data and the run, not about
these rules. ``POLICY_SUMMARY`` is printed in the header of every report.

D-090  Daily only, 14 non-S0 trials
    The three C3 4h points left Phase 3. The non-S0 grid is C1 3 + C2 3 + C3 2 + C4 3 + C5 3 = 14 points; S0
    is the locked default and is not a trial. Signals use 1d bars only, executed on 1h bars under the 6 h
    manual-delay fill. ATR20 and the vol estimate are always the daily ones.

D-091  Slippage curve
    The run uses the depth curve built from the G0 summary with the median statistic. The adverse-p90-cost
    curve is a sensitivity: it is reported (pooled OOS Sharpe and maxDD per contender, both fill modes) as
    "sensitivity, not gating (D-091)" and never feeds a selection, a gate, DSR, PBO or X. A flat slippage
    figure is refused by the preflight.

D-092  S1 has no protective stop of its own
    S1 runs with ``enforce_stops=False`` and follows its members' daily stopped states through their target
    weights (a stopped member contributes 0). Netting rule: at a close where ANY member enters STOPPED, S1
    trades to its full new target (including the other members' moves at that close), ignoring the 25 %
    rebalance band. Every other change of S1's target, a member's ordinary signal exit included, keeps S0's
    rebalance rule on the netted position (25 % band; entries from 0 and exits to 0 always trade).

D-093  PBO convention
    A CSCV combination counts as overfit when its logit is <= 0 (the in-sample winner is at or below the
    out-of-sample median). That is THE gating value. The share of logits strictly below 0 is reported next to
    it, for information only. Rank convention: omega = r / (N + 1), r = the in-sample winner's out-of-sample
    rank (1 = worst, N = best, ties at the average rank), logit = ln(omega / (1 - omega)).
"""

from __future__ import annotations

from typing import Final

__all__ = ["DECISIONS", "POLICY_SUMMARY"]

DECISIONS: Final[tuple[tuple[str, str], ...]] = (
    ("D-090", "daily signals only (1h execution); 14 non-S0 trials (C1 3, C2 3, C3 2, C4 3, C5 3)"),
    (
        "D-091",
        "run slippage = G0 median depth curve; adverse-p90-cost curve is a sensitivity, not gating",
    ),
    (
        "D-092",
        "S1 has no protective stop of its own; it follows its members' daily stopped states; at a close "
        "where any member enters STOPPED it trades to its full new target (no 25 % band), otherwise "
        "S0's band applies",
    ),
    (
        "D-093",
        "PBO counts logit <= 0 as overfit (gating; omega = r/(N+1), 1 = worst); the logit < 0 share is "
        "shown for information",
    ),
)

POLICY_SUMMARY: Final[str] = "; ".join(f"{ref}: {text}" for ref, text in DECISIONS)
