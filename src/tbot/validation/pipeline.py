"""The T6 selection procedure, steps H1-H5 (docs/SPEC.md section 7.2 H), on top of the walk-forward harness.

H1  S0 (no search) and the 14 grid points through the walk-forward, both fill modes.
H2  The walk-forward gates (G3, section 7.2 E) for each candidate. The regime-overlay keep/drop part of G4 is
    phase 4 and is not computed here.
H3  S1 = equal-weight blend of every strategy that passed G3 (S0 included if it passed); for a candidate C the
    comparison uses the leave-one-out blend S1(without C).
H4  C qualifies only if it passes the gates, its pooled OOS manual-delay Sharpe exceeds S0's and S1's
    (S1 without C), it beats S0 in at least half of the periods, and DSR(C) >= 0.95 with every logged trial
    counted.
H5  X = the qualifying C with the highest pooled OOS Sharpe; else the better of S0/S1 among those that pass
    the gates; else none.

Steps H6-H7 (the holdout band and the one-shot unsealing) are not here, by design. Nothing in this module
loads data: it receives ``Market`` objects. Every number it produces is PROVISIONAL (SPEC 7.2 F).

D-091 sensitivity. When ``EvalInputs.sensitivity_costs`` is set, every contender's (and the benchmark's) FIXED
schedule is re-run under those costs in both fill modes and kept in ``StrategyEval.sensitivity``. It is
reported only ("sensitivity, not gating (D-091)"): it never feeds selection, a gate, DSR, PBO or X.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tbot.core.types import Timeframe
from tbot.validation.gates import (
    GateResult,
    drawdown_gate,
    dsr_gate,
    eth_gate,
    pbo_gate,
    ratio_variants_gate,
    sharpe_gate,
    stress_gate,
)
from tbot.validation.grid import GridPoint
from tbot.validation.overfit import (
    candidate_dsr,
    candidate_min_trl,
    candidate_pbo,
    s0_dsr,
    trial_daily_sharpes,
)
from tbot.validation.robustness import (
    NeighbourResult,
    fixed_pooled,
    fixed_pooled_all,
    neighbour_test,
    reuse_run,
)
from tbot.validation.runs import (
    NORMAL_COSTS,
    CostSpec,
    FillMode,
    Market,
    SeriesRun,
    StrategyFactory,
)
from tbot.validation.stats import Dsr, MinTrl, PboResult, annualised_sharpe
from tbot.validation.walk_forward import (
    Candidate,
    CandidateResult,
    WalkForwardSpec,
    benchmark_runs,
    blend_run,
    pooled_run,
    run_candidate,
    run_fixed_points,
)

__all__ = [
    "S0_POINT",
    "EvalInputs",
    "Phase3Evaluation",
    "StrategyEval",
    "beats_in_periods",
    "choose_x",
    "evaluate",
    "s1_is_contender",
]

S0_POINT = GridPoint("S0", "S0", Timeframe.D1, {}, True, 0)

MODES = (FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN)


@dataclass(frozen=True)
class EvalInputs:
    btc: Market
    spec: WalkForwardSpec
    factory: StrategyFactory
    candidates: Sequence[Candidate]
    first_tradable: Mapping[str, datetime]
    logged_non_s0_trials: int
    eth: Market  # required: a missing input is never scored as a failed gate (the pre-flight refuses first)
    s0_variant_keys: Sequence[str]  # the six section-7.1 variants (S0_VARIANTS keys); NOT trials
    indices: Mapping[str, Market] = field(default_factory=dict)  # informational only; empty = "not run"
    costs: CostSpec = NORMAL_COSTS
    stress_costs: CostSpec | None = None  # default: ``costs.stressed()`` (x2 fees and slippage, curve scaled)
    sensitivity_costs: CostSpec | None = None  # D-091: adverse-p90 curve; REPORTED ONLY, never gating

    @property
    def stress(self) -> CostSpec:
        return self.stress_costs if self.stress_costs is not None else self.costs.stressed()


@dataclass(frozen=True)
class StrategyEval:
    name: str
    kind: str  # "S0" | "candidate" | "S1"
    schedule_members: tuple[tuple[tuple[datetime, str | None], ...], ...]  # one schedule, or S1's several
    pooled: Mapping[FillMode, SeriesRun]
    stress: SeriesRun | None
    eth: Mapping[FillMode, SeriesRun]
    indices: Mapping[str, SeriesRun]
    pbo: PboResult | None
    dsr: Dsr | None
    min_trl_zero: MinTrl | None
    min_trl_bh: MinTrl | None
    neighbour: NeighbourResult | None
    gates: tuple[GateResult, ...]
    reported: tuple[GateResult, ...]  # reported but not gating (candidates' x2 stress)
    walk: CandidateResult | None = None
    members: tuple[str, ...] = ()
    sensitivity: Mapping[FillMode, SeriesRun] = field(default_factory=dict)  # D-091, not gating

    @property
    def passes(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def sharpe(self) -> float | None:
        return self.pooled[FillMode.MANUAL_DELAY].sharpe


@dataclass(frozen=True)
class Phase3Evaluation:
    benchmark: Mapping[FillMode, SeriesRun]
    s0: StrategyEval
    candidates: tuple[StrategyEval, ...]
    s1: StrategyEval | None
    s1_without: Mapping[str, SeriesRun | None]  # leave-one-out blend per candidate (manual delay)
    beats_s0_periods: Mapping[str, int]
    qualified: tuple[str, ...]
    x: str | None
    x_reason: str
    trial_sharpes: Mapping[str, float]
    n_trials: int
    sensitivity_benchmark: Mapping[FillMode, SeriesRun] = field(default_factory=dict)  # D-091, not gating
    costs_label: str = ""  # ``CostSpec.label`` of the run costs, printed by the report
    sensitivity_costs_label: str = ""  # ``CostSpec.label`` of the D-091 sensitivity costs ("" = none)


def _period_score(sharpe: float | None) -> float:
    """A period Sharpe for the "beats S0 in at least half the periods" rule: undefined or negative -> 0."""
    return 0.0 if sharpe is None or sharpe < 0.0 else sharpe


def beats_in_periods(candidate: SeriesRun, s0: SeriesRun, spec: WalkForwardSpec) -> int:
    """Number of test periods in which the candidate's annualised Sharpe is strictly above S0's.

    An undefined (flat, or too short) or negative period Sharpe is scored 0 on BOTH sides, so a candidate
    that loses money cannot "beat" a flat S0 and two flat sides tie (a tie is not a win).
    """
    wins = 0
    for p in spec.periods:
        c = annualised_sharpe(candidate.returns_between(p.start, p.end), candidate.periods_per_year)
        r = annualised_sharpe(s0.returns_between(p.start, p.end), s0.periods_per_year)
        if _period_score(c) > _period_score(r):
            wins += 1
    return wins


_NO_CANDIDATE_NOTE = "no candidate qualifies: the better of S0/S1 among those passing the gates"


def choose_x(
    qualified: Mapping[str, float | None],
    s0: tuple[bool, float | None],
    s1: tuple[bool, float | None] | None,
) -> tuple[str | None, str]:
    """H5. ``qualified`` maps qualifying candidates (registry order) to their pooled Sharpe; s0/s1 are
    (passes G3, Sharpe). ``s1`` is None when S1 is not a contender (see ``s1_is_contender``).

    Ties are never broken on a name: among candidates the first in registry order wins an exact tie, and S1
    must be strictly above S0 to be chosen over it.
    """
    best_name: str | None = None
    best: float | None = None
    for name, sharpe in qualified.items():
        if sharpe is not None and (best is None or sharpe > best):
            best_name, best = name, sharpe
    if best_name is not None:
        return best_name, "highest pooled OOS Sharpe among the qualifying candidates"
    s0_ok = s0[0] and s0[1] is not None
    s1_ok = s1 is not None and s1[0] and s1[1] is not None
    if s0_ok and s1_ok:
        assert s1 is not None and s0[1] is not None and s1[1] is not None
        pick = "S1" if s1[1] > s0[1] else "S0"
        return pick, _NO_CANDIDATE_NOTE
    if s0_ok or s1_ok:
        return ("S0" if s0_ok else "S1"), _NO_CANDIDATE_NOTE
    return None, "no candidate qualifies and neither S0 nor S1 passes the gates: X = none, nothing goes live"


def s1_is_contender(members: Sequence[str]) -> bool:
    """S1 competes only if it blends at least two strategies: an S1 of S0 alone IS S0 (SPEC 7.2 H)."""
    return len(members) >= 2


def _eth_gate(eth: Mapping[FillMode, SeriesRun]) -> GateResult:
    return eth_gate(eth[FillMode.MANUAL_DELAY].sharpe)


def _core_gates(
    pooled: Mapping[FillMode, SeriesRun], bh: Mapping[FillMode, SeriesRun]
) -> tuple[GateResult, GateResult]:
    run, ref = pooled[FillMode.MANUAL_DELAY], bh[FillMode.MANUAL_DELAY]
    return drawdown_gate(run.max_drawdown, ref.max_drawdown), sharpe_gate(run.sharpe, ref.sharpe)


def evaluate(inp: EvalInputs) -> Phase3Evaluation:
    """Run H1-H5 end to end for the given inputs (this is the expensive entry point)."""
    if not inp.s0_variant_keys:
        raise ValueError("the six S0 robustness variants must be supplied (the pre-flight refuses otherwise)")
    spec, btc, factory = inp.spec, inp.btc, inp.factory
    points = [p for c in inp.candidates for p in c.points]
    first = {**inp.first_tradable}

    bh = benchmark_runs(btc, spec, inp.costs, MODES)

    # -- trial series: every grid point held fixed over the pooled OOS days (V[SR] of the DSR)
    trial_runs = fixed_pooled_all(btc, points, factory, spec, FillMode.MANUAL_DELAY, inp.costs)
    trial_sharpes = trial_daily_sharpes(trial_runs)

    # -- H1: S0
    s0 = _evaluate_s0(inp, bh)

    # -- H1/H2: candidates
    fixed = run_fixed_points(btc, points, factory, (FillMode.MANUAL_DELAY,), inp.costs)
    evals: list[StrategyEval] = []
    for cand in inp.candidates:
        walk = run_candidate(
            cand, btc, spec, factory=factory, first_tradable=first, fixed=fixed, costs=inp.costs
        )
        evals.append(_evaluate_candidate(inp, cand, walk, fixed, bh, trial_runs, trial_sharpes))

    # -- H3: S1 and leave-one-out blends
    passing = [e for e in [s0, *evals] if e.passes]
    s1 = _evaluate_s1(inp, passing, bh, trial_sharpes) if passing else None
    s1_without: dict[str, SeriesRun | None] = {}
    for e in evals:
        others = [m for m in passing if m.name != e.name]
        s1_without[e.name] = _blend(inp, others, FillMode.MANUAL_DELAY, inp.btc) if others else None

    # -- H4
    s0_run = s0.pooled[FillMode.MANUAL_DELAY]
    beats = {e.name: beats_in_periods(e.pooled[FillMode.MANUAL_DELAY], s0_run, spec) for e in evals}
    qualified: dict[str, float | None] = {}
    for e in evals:
        sh = e.sharpe
        loo = s1_without[e.name]
        ok = (
            e.passes
            and sh is not None
            and (s0.sharpe is None or sh > s0.sharpe)
            and (loo is None or loo.sharpe is None or sh > loo.sharpe)
            and 2 * beats[e.name] >= len(spec.periods)
            and e.dsr is not None
            and e.dsr.passes
        )
        if ok:
            qualified[e.name] = sh

    # -- H5
    contender = s1 if s1 is not None and s1_is_contender(s1.members) else None
    x, reason = choose_x(
        qualified, (s0.passes, s0.sharpe), (contender.passes, contender.sharpe) if contender else None
    )
    sens_bh: dict[FillMode, SeriesRun] = {}
    if inp.sensitivity_costs is not None:
        s0, evals, s1, sens_bh = _with_sensitivity(inp, s0, evals, s1)
    return Phase3Evaluation(
        benchmark=bh,
        s0=s0,
        candidates=tuple(evals),
        s1=s1,
        s1_without=s1_without,
        beats_s0_periods=beats,
        qualified=tuple(qualified),
        x=x,
        x_reason=reason,
        trial_sharpes=trial_sharpes,
        n_trials=inp.logged_non_s0_trials,
        sensitivity_benchmark=sens_bh,
        costs_label=inp.costs.label,
        sensitivity_costs_label=inp.sensitivity_costs.label if inp.sensitivity_costs is not None else "",
    )


def _with_sensitivity(
    inp: EvalInputs, s0: StrategyEval, evals: list[StrategyEval], s1: StrategyEval | None
) -> tuple[StrategyEval, list[StrategyEval], StrategyEval | None, dict[FillMode, SeriesRun]]:
    """D-091: re-run every contender's fixed schedule under ``inp.sensitivity_costs`` (both fill modes).

    Nothing here is read by a gate, by the selection, by DSR/PBO or by X: the results are attached to the
    evaluation for the report block "sensitivity, not gating (D-091)". The selection stays the one made
    under the run costs.
    """
    costs = inp.sensitivity_costs
    assert costs is not None
    spec, btc, factory = inp.spec, inp.btc, inp.factory
    bench = benchmark_runs(btc, spec, costs, MODES)
    s0_sens = {m: fixed_pooled(btc, S0_POINT, factory, spec, m, costs) for m in MODES}
    out_evals: list[StrategyEval] = []
    for e in evals:
        assert e.walk is not None
        sens = {m: reuse_run(btc, e.walk, factory, spec, m, costs) for m in MODES}
        out_evals.append(dataclasses.replace(e, sensitivity=sens))
    out_s1 = s1
    if s1 is not None:
        sens_s1 = {m: blend_run(btc, list(s1.schedule_members), factory, spec, m, costs) for m in MODES}
        out_s1 = dataclasses.replace(s1, sensitivity=sens_s1)
    return dataclasses.replace(s0, sensitivity=s0_sens), out_evals, out_s1, dict(bench)


# -- pieces ---------------------------------------------------------------------------------------


def _s0_schedule(spec: WalkForwardSpec) -> list[tuple[datetime, str | None]]:
    return [(spec.first_test_start, "S0")]


def _evaluate_s0(inp: EvalInputs, bh: Mapping[FillMode, SeriesRun]) -> StrategyEval:
    spec, btc, factory = inp.spec, inp.btc, inp.factory
    pooled = {m: fixed_pooled(btc, S0_POINT, factory, spec, m, inp.costs) for m in MODES}
    stress = fixed_pooled(btc, S0_POINT, factory, spec, FillMode.MANUAL_DELAY, inp.stress)
    schedule = _s0_schedule(spec)
    eth = _reuse_all(inp, inp.eth, schedule, "S0")
    indices = _index_runs(inp, schedule, "S0")
    run = pooled[FillMode.MANUAL_DELAY]
    dd, sh = _core_gates(pooled, bh)
    variants = _s0_variants(inp, run)
    dsr = s0_dsr(run.returns)
    gates = (
        dd,
        sh,
        dsr_gate(dsr.dsr if dsr else None),
        _eth_gate(eth),
        stress_gate(stress.returns),
        variants,
    )
    zero, vs_bh = candidate_min_trl(run.returns, bh[FillMode.MANUAL_DELAY].returns)
    return StrategyEval(
        "S0", "S0", (tuple(schedule),), pooled, stress, eth, indices, None, dsr, zero, vs_bh, None, gates, ()
    )


def _s0_variants(inp: EvalInputs, run: SeriesRun) -> GateResult:
    sharpes: dict[str, float | None] = {}
    for key in inp.s0_variant_keys:
        point = GridPoint(key, "S0", Timeframe.D1, {}, False, 0)
        sharpes[key] = fixed_pooled(
            inp.btc, point, inp.factory, inp.spec, FillMode.MANUAL_DELAY, inp.costs
        ).sharpe
    return ratio_variants_gate(run.sharpe, sharpes)


def _reuse_all(
    inp: EvalInputs,
    market: Market,
    schedule: Sequence[tuple[datetime, str | None]],
    name: str,
) -> dict[FillMode, SeriesRun]:
    return {
        m: pooled_run(market, schedule, inp.factory, inp.spec, m, inp.costs, strategy_id=name) for m in MODES
    }


def _index_runs(
    inp: EvalInputs,
    schedule: Sequence[tuple[datetime, str | None]],
    name: str,
) -> dict[str, SeriesRun]:
    return {
        label: pooled_run(
            market, schedule, inp.factory, inp.spec, FillMode.NEXT_OPEN, inp.costs, strategy_id=name
        )
        for label, market in inp.indices.items()
    }


def _evaluate_candidate(
    inp: EvalInputs,
    cand: Candidate,
    walk: CandidateResult,
    fixed: Mapping[tuple[str, FillMode], SeriesRun],
    bh: Mapping[FillMode, SeriesRun],
    trial_runs: Mapping[str, SeriesRun],
    trial_sharpes: Mapping[str, float],
) -> StrategyEval:
    spec, btc, factory = inp.spec, inp.btc, inp.factory
    run = walk.pooled[FillMode.MANUAL_DELAY]
    fixed_runs = {p.key: fixed[(p.key, FillMode.MANUAL_DELAY)] for p in cand.points}
    pbo = candidate_pbo(
        cand.points,
        fixed_runs,
        inp.first_tradable,
        btc.daily_closes,
        start=spec.train_start,
        end=spec.last_test_end,
    )
    dsr = candidate_dsr(run.returns, trial_sharpes, inp.logged_non_s0_trials)
    nb = neighbour_test(cand, walk.final_selection, trial_runs)
    eth = {m: reuse_run(inp.eth, walk, factory, spec, m, inp.costs) for m in MODES}
    stress = reuse_run(btc, walk, factory, spec, FillMode.MANUAL_DELAY, inp.stress)
    indices = _index_runs(inp, walk.schedule, cand.name)
    dd, sh = _core_gates(walk.pooled, bh)
    gates = (dd, sh, pbo_gate(pbo.pbo), dsr_gate(dsr.dsr if dsr else None), nb.gate, _eth_gate(eth))
    zero, vs_bh = candidate_min_trl(run.returns, bh[FillMode.MANUAL_DELAY].returns)
    return StrategyEval(
        cand.name,
        "candidate",
        (tuple(walk.schedule),),
        walk.pooled,
        stress,
        eth,
        indices,
        pbo,
        dsr,
        zero,
        vs_bh,
        nb,
        gates,
        (stress_gate(stress.returns),),
        walk=walk,
    )


def _blend(
    inp: EvalInputs,
    members: Sequence[StrategyEval],
    mode: FillMode,
    market: Market,
    costs: CostSpec | None = None,
) -> SeriesRun:
    scheds = [sched for m in members for sched in m.schedule_members[:1]]
    return blend_run(market, scheds, inp.factory, inp.spec, mode, costs or inp.costs)


def _evaluate_s1(
    inp: EvalInputs,
    members: Sequence[StrategyEval],
    bh: Mapping[FillMode, SeriesRun],
    trial_sharpes: Mapping[str, float],
) -> StrategyEval:
    pooled = {m: _blend(inp, members, m, inp.btc) for m in MODES}
    stress = _blend(inp, members, FillMode.MANUAL_DELAY, inp.btc, inp.stress)
    eth = {m: _blend(inp, members, m, inp.eth) for m in MODES}
    run = pooled[FillMode.MANUAL_DELAY]
    dd, sh = _core_gates(pooled, bh)
    dsr = candidate_dsr(run.returns, trial_sharpes, inp.logged_non_s0_trials)
    gates = (dd, sh, dsr_gate(dsr.dsr if dsr else None), _eth_gate(eth), stress_gate(stress.returns))
    zero, vs_bh = candidate_min_trl(run.returns, bh[FillMode.MANUAL_DELAY].returns)
    return StrategyEval(
        "S1",
        "S1",
        tuple(m.schedule_members[0] for m in members),
        pooled,
        stress,
        eth,
        {},
        None,
        dsr,
        zero,
        vs_bh,
        None,
        gates,
        (),
        members=tuple(m.name for m in members),
    )
