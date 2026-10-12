"""Markdown report for T6 steps H1-H5 (docs/SPEC.md section 7.2 F, H).

Rules this module enforces:

* every MEASURED number carries the label ``[P]`` (PROVISIONAL: the SPEC's costs, band and delay are
  provisional until the G2 re-pricing, so no number here is final). Helpers `_num`, `_pct` and `_cnt` are
  the only way a result reaches the text; ``tests/validation/test_report.py`` checks that no decimal number
  appears without the label;
* turnover and annual cost drag are shown for every strategy and the benchmark (CLAUDE.md section 4.4);
* a failed or undefined gate is printed as FAIL with the reason, never omitted;
* the report says plainly when nothing qualifies ("no edge after costs") and never softens it.

Nothing is computed here: the module only renders a ``Phase3Evaluation``.
"""

from __future__ import annotations

from collections.abc import Sequence

from tbot.validation.gates import GateResult
from tbot.validation.pipeline import Phase3Evaluation, StrategyEval
from tbot.validation.policy import POLICY_SUMMARY
from tbot.validation.runs import FillMode, SeriesRun
from tbot.validation.walk_forward import WalkForwardSpec

__all__ = ["LABEL", "SENSITIVITY_BLOCK", "render_report"]

LABEL = "[P]"
NA = "n/a"


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return NA
    return f"{value:.{digits}f} {LABEL}"


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return NA
    return f"{value * 100:.{digits}f}% {LABEL}"


def _cnt(value: float | int | None) -> str:
    if value is None:
        return NA
    return f"{round(value)} {LABEL}"


def _status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [_row(header), _row(["---"] * len(header)), *(_row(r) for r in rows), ""]


def _total(run: SeriesRun) -> float:
    equity = 1.0
    for r in run.returns:
        equity *= 1.0 + r
    return equity - 1.0


def _series_row(name: str, run: SeriesRun) -> list[str]:
    return [
        name,
        _num(run.sharpe),
        _pct(run.max_drawdown),
        _pct(_total(run)),
        _cnt(run.n_fills),
        _num(run.turnover_notional_per_year, 2),
        _pct(run.cost_drag_annual, 2),
    ]


_SERIES_HEADER = [
    "strategy",
    "Sharpe(365)",
    "maxDD",
    "total return",
    "fills",
    "turnover (x equity/yr)",
    "cost drag/yr",
]


def _gate_line(g: GateResult) -> list[str]:
    value = _num(g.value, 4) if g.value is not None else NA
    thr = _num(g.threshold, 4) if g.threshold is not None else NA
    return [g.name, g.rule, value, thr, _status(g.passed), g.note or "-"]


def _header(spec: WalkForwardSpec, ev: Phase3Evaluation) -> list[str]:
    return [
        "# Phase 3 walk-forward evaluation (T6 steps H1-H5)",
        "",
        f"> **PROVISIONAL.** Every number below carries the label `{LABEL}`: costs (fee 10 bps per side plus",
        "> the run's slippage model, the G0 median depth curve of D-091),",
        "> the 25 % rebalance band and the 6 h manual delay are provisional until the G2 re-pricing, so",
        "> none of",
        "> these results is final and none may be used for a go / no-go decision on its own.",
        "",
        f"Periods: {len(spec.periods)} test periods, "
        f"{spec.first_test_start:%Y-%m-%d} to {spec.last_test_end:%Y-%m-%d} (exclusive). "
        "Training is anchored and expanding. The sealed holdout (from 2025-10-01) was not loaded.",
        f"Logged non-S0 trials used for the DSR: {ev.n_trials}.",
        f"Decided items (Parham, 2026-10-12): {POLICY_SUMMARY}.",
        "S1 has no intraday stop of its own; it follows its members' daily stopped states (D-092).",
        "",
    ]


def _benchmark_section(ev: Phase3Evaluation) -> list[str]:
    rows = [
        _series_row(f"buy-and-hold ({mode.value})", ev.benchmark[mode])
        for mode in (FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN)
        if mode in ev.benchmark
    ]
    return [
        "## Benchmark: buy-and-hold over the pooled OOS days",
        "",
        "Weight 1, continuous, same fee and slippage, entry charged once. Gating ratios use manual delay.",
        "",
        *_table(_SERIES_HEADER, rows),
    ]


def _strategies(ev: Phase3Evaluation) -> list[StrategyEval]:
    return [ev.s0, *ev.candidates, *([ev.s1] if ev.s1 else [])]


def _pooled_section(ev: Phase3Evaluation) -> list[str]:
    rows: list[list[str]] = []
    for s in _strategies(ev):
        for mode in (FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN):
            if mode in s.pooled:
                rows.append(_series_row(f"{s.name} ({mode.value})", s.pooled[mode]))
        if s.stress is not None:
            rows.append(_series_row(f"{s.name} (x2 costs, manual-delay)", s.stress))
    return [
        "## Pooled out-of-sample series (turnover and annual cost drag included)",
        "",
        *_table(_SERIES_HEADER, rows),
    ]


SENSITIVITY_BLOCK = "sensitivity, not gating (D-091)"


def _sensitivity_section(ev: Phase3Evaluation) -> list[str]:
    """D-091: adverse-p90 curve, pooled OOS Sharpe and maxDD per contender, both fill modes. Not gating."""
    if not ev.sensitivity_benchmark and not any(s.sensitivity for s in _strategies(ev)):
        return []
    header = ["strategy", "Sharpe(365)", "maxDD"]
    rows: list[list[str]] = []
    for mode in (FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN):
        if mode in ev.sensitivity_benchmark:
            run = ev.sensitivity_benchmark[mode]
            rows.append([f"buy-and-hold ({mode.value})", _num(run.sharpe), _pct(run.max_drawdown)])
        for s in _strategies(ev):
            if mode in s.sensitivity:
                run = s.sensitivity[mode]
                rows.append([f"{s.name} ({mode.value})", _num(run.sharpe), _pct(run.max_drawdown)])
    return [
        f"## Slippage sensitivity, adverse p90 cost curve ({SENSITIVITY_BLOCK})",
        "",
        "The same fixed schedules re-run with the adverse-p90-cost depth curve. These rows never feed a "
        "selection, a gate, the DSR, the PBO or X; the numbers above use the median curve.",
        "",
        *_table(header, rows),
    ]


def _gates_section(ev: Phase3Evaluation) -> list[str]:
    out = ["## Gates (G3, applied literally, manual-delay series)", ""]
    for s in _strategies(ev):
        out += [f"### {s.name}: {_status(s.passes)}", ""]
        gate_rows = [_gate_line(g) for g in (*s.gates, *s.reported)]
        out += _table(["gate", "rule", "value", "threshold", "result", "note"], gate_rows)
        extras: list[str] = []
        if s.pbo is not None:
            extras.append(
                f"PBO (lambda <= 0, the gating value, D-093) {_num(s.pbo.pbo)} over "
                f"{_cnt(s.pbo.n_combinations)} combinations, "
                f"{_cnt(s.pbo.n_blocks)} blocks, {_cnt(s.pbo.dropped_leading_days)} leading days dropped; "
                f"share with lambda < 0, for information only: {_num(s.pbo.pbo_strict)}"
            )
        if s.dsr is not None:
            d = s.dsr
            extras.append(
                f"DSR {_num(d.dsr)} (PSR vs 0 {_num(d.psr_vs_zero)}, daily SR {_num(d.sr_hat, 5)}, "
                f"SR* {_num(d.sr_star, 5)}, N = {d.n_trials}, {_cnt(d.n_obs)} observations)"
            )
        for label, m in (("MinTRL vs 0", s.min_trl_zero), ("MinTRL vs B&H", s.min_trl_bh)):
            if m is not None:
                extras.append(
                    f"{label}: {_num(m.years, 2)} years needed, {_num(m.n_obs / 365.0, 2)} available "
                    f"({'enough' if m.sufficient else 'NOT enough'})"
                )
        if s.members:
            extras.append("members: " + ", ".join(s.members))
        out += [f"- {line}" for line in extras] + ([""] if extras else [])
    return out


def _selection_section(ev: Phase3Evaluation) -> list[str]:
    out = ["## Per-period choices", ""]
    for s in ev.candidates:
        if s.walk is None:
            continue
        rows = [[f"{p[0]:%Y-%m-%d}", p[1] or "flat"] for p in s.walk.schedule]
        out += [f"### {s.name}", "", *_table(["period start", "chosen point"], rows)]
        ties = [sel for sel in s.walk.selections if len(sel.tied) > 1]
        if ties:
            out.append(f"Ties broken to the base point or lowest grid position in {_cnt(len(ties))} periods.")
            out.append("")
    return out


def _neighbour_section(ev: Phase3Evaluation) -> list[str]:
    out = ["## Neighbour test", ""]
    for s in ev.candidates:
        n = s.neighbour
        if n is None:
            continue
        rows = [[k, _num(v)] for k, v in n.neighbour_sharpes.items()]
        out += [f"### {s.name}: P = {n.point_key or 'flat'}, Sharpe(P) = {_num(n.reference_sharpe)}", ""]
        out += (
            _table(["neighbour", "fixed-parameter pooled OOS Sharpe"], rows)
            if rows
            else ["No neighbours.", ""]
        )
    return out


def _robustness_section(ev: Phase3Evaluation) -> list[str]:
    rows: list[list[str]] = []
    for s in _strategies(ev):
        for mode, run in s.eth.items():
            rows.append([f"{s.name} ETH/USDT ({mode.value})", _num(run.sharpe), _pct(run.max_drawdown)])
        for label, run in s.indices.items():
            rows.append([f"{s.name} {label} (next-open, 252-day)", _num(run.sharpe), _pct(run.max_drawdown)])
    note = (
        []
        if any(s.indices for s in _strategies(ev))
        else ["Stooq indices: not run (informational only; an absent index never fails a gate).", ""]
    )
    return [
        "## Robustness: ETH/USDT and indices (BTC choices reused unchanged)",
        "",
        *_table(["series", "Sharpe", "maxDD"], rows),
        *note,
    ]


def _selection_procedure(ev: Phase3Evaluation, spec: WalkForwardSpec) -> list[str]:
    out = ["## Selection procedure (H3-H5)", ""]
    s0_sharpe = ev.s0.sharpe
    rows = []
    for s in ev.candidates:
        loo = ev.s1_without.get(s.name)
        rows.append(
            [
                s.name,
                _status(s.passes),
                _num(s.sharpe),
                _num(s0_sharpe),
                _num(loo.sharpe) if loo is not None else "no other passer",
                f"{_cnt(ev.beats_s0_periods.get(s.name, 0))} of {_cnt(len(spec.periods))}",
                _num(s.dsr.dsr) if s.dsr is not None else NA,
                "QUALIFIES" if s.name in ev.qualified else "does not qualify",
            ]
        )
    out += _table(
        ["candidate", "gates", "Sharpe", "S0 Sharpe", "S1 without it", "periods beating S0", "DSR", "H4"],
        rows,
    )
    s1_text = "none (nothing passed the gates)" if ev.s1 is None else ", ".join(ev.s1.members)
    out += [f"S1 members: {s1_text}.", ""]
    if ev.x is None:
        out += [
            f"**X = none.** {ev.x_reason}.",
            "",
            "**No edge after costs has been demonstrated by this evaluation.** The pre-registered "
            "result stands "
            "as measured: stopping here is a valid outcome of the process.",
            "",
        ]
    else:
        out += [f"**X = {ev.x}.** {ev.x_reason}.", ""]
    out += [
        "Not evaluated here: the regime-overlay keep/drop part of G4 (phase 4), the holdout band and the "
        "one-shot "
        "unsealing (H6-H7).",
        "",
    ]
    return out


def render_report(ev: Phase3Evaluation, spec: WalkForwardSpec) -> str:
    """The full Markdown text. Pure: no I/O."""
    parts = [
        *_header(spec, ev),
        *_benchmark_section(ev),
        *_pooled_section(ev),
        *_sensitivity_section(ev),
        *_gates_section(ev),
        *_selection_section(ev),
        *_neighbour_section(ev),
        *_robustness_section(ev),
        *_selection_procedure(ev, spec),
    ]
    return "\n".join(parts).rstrip() + "\n"
