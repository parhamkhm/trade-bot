"""T6 steps H1-H5 (``pipeline.evaluate``) end to end on the stand-in family, and the pure decision rules.

The end-to-end run uses three SMA-filter points on seeded synthetic bars; nothing here is a Phase 3 result.
What is checked is the wiring: every mandatory gate is present and applied to the right series, S1 is built
from the strategies that passed, the H4 conditions are evaluated as written, and the report labels every
number.
"""

from __future__ import annotations

import re

import pytest

from tbot.validation.gates import GateResult
from tbot.validation.pipeline import (
    EvalInputs,
    Phase3Evaluation,
    beats_in_periods,
    choose_x,
    evaluate,
    s1_is_contender,
)
from tbot.validation.report import LABEL, render_report
from tbot.validation.runs import STRESS_COSTS, FillMode
from tests.validation.wf_fixtures import (
    CANDIDATE,
    FIRST_TRADABLE,
    POINTS,
    S0_VARIANT_KEYS,
    SPEC,
    factory,
    fake_run,
    make_market,
)

MD, NO = FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN


def _inputs(**extra: object) -> EvalInputs:
    return EvalInputs(
        btc=make_market(),
        spec=SPEC,
        factory=factory,
        candidates=[CANDIDATE],
        first_tradable=FIRST_TRADABLE,
        logged_non_s0_trials=len(POINTS),
        eth=make_market(seed=13, symbol="ETHUSDT"),
        s0_variant_keys=S0_VARIANT_KEYS,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def evaluation() -> Phase3Evaluation:
    return evaluate(_inputs())


# -- pure rules ---------------------------------------------------------------------------------------


def test_choose_x_prefers_the_best_qualifying_candidate() -> None:
    x, why = choose_x({"C1": 0.8, "C3": 1.1}, (True, 0.9), (True, 1.0))
    assert x == "C3"
    assert "qualifying" in why


def test_choose_x_falls_back_to_the_better_of_s0_and_s1_that_pass_the_gates() -> None:
    assert choose_x({}, (True, 0.9), (True, 1.0))[0] == "S1"
    assert choose_x({}, (True, 0.9), (False, 5.0))[0] == "S0"  # S1 failed its gates: it cannot win
    assert choose_x({}, (True, 0.9), None)[0] == "S0"


def test_choose_x_is_none_when_nothing_passes() -> None:
    x, why = choose_x({}, (False, 1.0), (False, 2.0))
    assert x is None
    assert "nothing goes live" in why


def test_beats_in_periods_counts_periods_with_a_higher_sharpe() -> None:
    from tests.validation.test_walk_forward import _closes  # local: the same synthetic calendar

    closes = _closes(400)
    start = SPEC.periods[0].start
    n_after = len([c for c in closes if c >= start])
    # one return per close, stamped on that close
    rets_up = [0.002 + 0.01 * ((i % 2) - 0.5) for i in range(len(closes) - 1)]
    rets_flat_ish = [0.0005 + 0.01 * ((i % 2) - 0.5) for i in range(len(closes) - 1)]
    better = fake_run(closes, rets_up)
    worse = fake_run(closes, rets_flat_ish)
    assert n_after > 0
    assert beats_in_periods(better, worse, SPEC) >= 1
    assert beats_in_periods(worse, better, SPEC) == 0
    assert beats_in_periods(better, better, SPEC) == 0  # equal is not a win


def test_choose_x_first_in_registry_order_wins_an_exact_tie_whatever_the_names() -> None:
    assert choose_x({"C9": 1.0, "C1": 1.0}, (True, 0.5), None)[0] == "C9"
    assert choose_x({"C1": 1.0, "C9": 1.0}, (True, 0.5), None)[0] == "C1"
    assert choose_x({"C1": None, "C2": 0.3}, (True, 0.5), None)[0] == "C2"  # undefined Sharpe never wins


def test_choose_x_s1_must_be_strictly_above_s0_to_replace_it() -> None:
    assert choose_x({}, (True, 0.9), (True, 0.9))[0] == "S0"
    assert choose_x({}, (True, 0.9), (True, 0.9000001))[0] == "S1"
    assert choose_x({}, (False, 0.9), (True, 0.1))[0] == "S1"


def test_a_single_member_s1_is_not_a_contender() -> None:
    assert not s1_is_contender([])
    assert not s1_is_contender(["S0"])  # an S1 of S0 alone IS S0
    assert not s1_is_contender(["C3-(10,28,50)"])
    assert s1_is_contender(["S0", "C3-(10,28,50)"])


def test_beats_in_periods_scores_an_undefined_or_negative_sharpe_as_zero() -> None:
    from tests.validation.test_walk_forward import _closes

    closes = _closes(400)
    n = len(closes) - 1
    noise = [0.01 * ((i % 2) - 0.5) for i in range(n)]
    flat = fake_run(closes, [0.0] * n)
    losing = fake_run(closes, [-0.002 + x for x in noise])
    winning = fake_run(closes, [0.003 + x for x in noise])
    assert flat.sharpe is None
    assert beats_in_periods(losing, flat, SPEC) == 0  # losing money does not "beat" a flat S0
    assert beats_in_periods(flat, losing, SPEC) == 0  # both sides score 0: a tie, not a win
    assert beats_in_periods(flat, flat, SPEC) == 0  # two undefined sides tie, and a tie is not a win
    assert beats_in_periods(winning, flat, SPEC) >= 1
    assert beats_in_periods(losing, losing, SPEC) == 0


def test_evaluate_refuses_to_run_without_the_s0_variants() -> None:
    inp = EvalInputs(
        btc=make_market(),
        spec=SPEC,
        factory=factory,
        candidates=[CANDIDATE],
        first_tradable=FIRST_TRADABLE,
        logged_non_s0_trials=len(POINTS),
        eth=make_market(seed=13, symbol="ETHUSDT"),
        s0_variant_keys=[],
    )
    with pytest.raises(ValueError, match="S0 robustness variants"):
        evaluate(inp)


def test_eval_inputs_require_eth_so_a_missing_input_cannot_become_a_failed_gate() -> None:
    kwargs = {
        "btc": make_market(),
        "spec": SPEC,
        "factory": factory,
        "candidates": [CANDIDATE],
        "first_tradable": FIRST_TRADABLE,
        "logged_non_s0_trials": len(POINTS),
        "s0_variant_keys": S0_VARIANT_KEYS,
    }
    with pytest.raises(TypeError, match="eth"):
        EvalInputs(**kwargs)  # type: ignore[arg-type]


# -- end to end ---------------------------------------------------------------------------------------


def test_every_strategy_has_its_gates(evaluation: Phase3Evaluation) -> None:
    names = {g.name for g in evaluation.s0.gates}
    assert {"maxDD vs B&H", "Sharpe vs B&H", "DSR", "ETH/USDT Sharpe", "x2 cost stress"} <= names
    # the six variants are evaluated (never "not evaluated"): the gate carries one Sharpe per variant
    variants = next(g for g in evaluation.s0.gates if g.name == "S0 robustness variants")
    assert "not evaluated" not in variants.note
    cand = evaluation.candidates[0]
    assert {g.name for g in cand.gates} == {
        "maxDD vs B&H",
        "Sharpe vs B&H",
        "PBO",
        "DSR",
        "neighbours",
        "ETH/USDT Sharpe",
    }
    assert [g.name for g in cand.reported] == ["x2 cost stress"]
    assert cand.passes == all(g.passed for g in cand.gates)


def test_gates_use_the_manual_delay_series(evaluation: Phase3Evaluation) -> None:
    cand = evaluation.candidates[0]
    bh = evaluation.benchmark[MD]
    dd = next(g for g in cand.gates if g.name == "maxDD vs B&H")
    assert dd.value == pytest.approx(cand.pooled[MD].max_drawdown)
    assert dd.threshold == pytest.approx(0.6 * bh.max_drawdown)
    sh = next(g for g in cand.gates if g.name == "Sharpe vs B&H")
    assert bh.sharpe is not None
    assert sh.threshold == pytest.approx(0.8 * bh.sharpe)


def test_both_fill_modes_are_reported_for_every_strategy(evaluation: Phase3Evaluation) -> None:
    for s in (evaluation.s0, *evaluation.candidates):
        assert set(s.pooled) == {MD, NO}
        assert set(s.eth) == {MD, NO}
        assert s.stress is not None
    assert set(evaluation.benchmark) == {MD, NO}


def test_dsr_uses_the_logged_trial_count(evaluation: Phase3Evaluation) -> None:
    assert evaluation.n_trials == len(POINTS)
    assert set(evaluation.trial_sharpes) == {p.key for p in POINTS}
    cand = evaluation.candidates[0]
    if cand.dsr is not None:
        assert cand.dsr.n_trials == len(POINTS)
    if evaluation.s0.dsr is not None:
        assert evaluation.s0.dsr.n_trials == 1 and evaluation.s0.dsr.sr_star == 0.0


def test_pbo_is_computed_over_the_candidate_grid(evaluation: Phase3Evaluation) -> None:
    pbo = evaluation.candidates[0].pbo
    assert pbo is not None
    assert pbo.n_blocks == 16 and pbo.n_strategies == len(POINTS)
    assert 0.0 <= pbo.pbo <= 1.0


def test_s1_is_built_from_the_strategies_that_passed(evaluation: Phase3Evaluation) -> None:
    passers = [s.name for s in (evaluation.s0, *evaluation.candidates) if s.passes]
    if passers:
        assert evaluation.s1 is not None and list(evaluation.s1.members) == passers
    else:
        assert evaluation.s1 is None


def test_x_is_consistent_with_the_qualification_list(evaluation: Phase3Evaluation) -> None:
    if evaluation.qualified:
        assert evaluation.x in evaluation.qualified
        sharpes = {s.name: s.sharpe for s in evaluation.candidates if s.name in evaluation.qualified}
        assert sharpes[evaluation.x or ""] == max(v for v in sharpes.values() if v is not None)
    else:
        assert evaluation.x in {None, "S0", "S1"}
    # a candidate that fails any gate can never qualify
    for s in evaluation.candidates:
        if not s.passes:
            assert s.name not in evaluation.qualified


# -- report -------------------------------------------------------------------------------------------


def _strip_known_constants(text: str, gates: list[GateResult]) -> str:
    """Remove the pre-registered rule texts and the dates: those are the SPEC, not measured numbers."""
    for g in gates:
        text = text.replace(g.rule, "")
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    text = text.replace("section-7.1", "section").replace("section 7.1", "section")
    return re.sub(r"(?<![\d.])(?:0\.7|0\.80|0\.8|0\.6|0\.95|0\.3)(?![\d])", "", text)


def test_report_labels_every_measured_number_provisional(evaluation: Phase3Evaluation) -> None:
    text = render_report(evaluation, SPEC)
    assert "PROVISIONAL" in text.splitlines()[2]
    gates = [
        g
        for s in (evaluation.s0, *evaluation.candidates, *([evaluation.s1] if evaluation.s1 else []))
        for g in (*s.gates, *s.reported)
    ]
    stripped = _strip_known_constants(text, gates)
    # drop every labelled number (a percent sign may sit before the label); no decimal may remain
    unlabelled = re.sub(r"-?\d+\.\d+%? " + re.escape(LABEL), "", stripped)
    bad = re.findall(r"-?\d+\.\d+", unlabelled)
    assert not bad, bad[:10]
    assert LABEL in text


def test_report_shows_turnover_cost_drag_and_both_benchmarks(evaluation: Phase3Evaluation) -> None:
    text = render_report(evaluation, SPEC)
    assert "turnover" in text and "cost drag" in text
    assert "buy-and-hold (manual_delay)" in text and "buy-and-hold (next_open)" in text
    assert "x2 costs" in text


def test_report_states_plainly_when_nothing_qualifies(evaluation: Phase3Evaluation) -> None:
    text = render_report(evaluation, SPEC)
    if evaluation.x is None:
        assert "No edge after costs has been demonstrated" in text
    else:
        assert f"**X = {evaluation.x}.**" in text
    # the phase-4 / holdout items are named as not evaluated
    assert "Not evaluated here" in text


def test_report_period_choices_match_the_schedule(evaluation: Phase3Evaluation) -> None:
    text = render_report(evaluation, SPEC)
    walk = evaluation.candidates[0].walk
    assert walk is not None
    for start, key in walk.schedule:
        assert f"| {start:%Y-%m-%d} | {key or 'flat'} |" in text


# -- D-091: the slippage sensitivity is reported, never gating ----------------------------------------


@pytest.fixture(scope="module")
def sensitive() -> Phase3Evaluation:
    """The same inputs plus a (deliberately different) sensitivity cost model."""
    return evaluate(_inputs(sensitivity_costs=STRESS_COSTS))


def test_without_sensitivity_costs_nothing_is_attached_and_no_block_is_printed(
    evaluation: Phase3Evaluation,
) -> None:
    assert not evaluation.sensitivity_benchmark
    assert all(not s.sensitivity for s in (evaluation.s0, *evaluation.candidates))
    assert "## Slippage sensitivity" not in render_report(evaluation, SPEC)


def test_sensitivity_is_populated_in_both_fill_modes_for_every_contender(sensitive: Phase3Evaluation) -> None:
    assert set(sensitive.sensitivity_benchmark) == {MD, NO}
    for s in (sensitive.s0, *sensitive.candidates):
        assert set(s.sensitivity) == {MD, NO}
        # a costlier model cannot make the same fixed schedule cheaper
        assert s.sensitivity[MD].fees_paid + s.sensitivity[MD].slippage_cost >= (
            s.pooled[MD].fees_paid + s.pooled[MD].slippage_cost
        )


def test_sensitivity_never_changes_a_selection_a_gate_dsr_pbo_or_x(
    evaluation: Phase3Evaluation, sensitive: Phase3Evaluation
) -> None:
    assert sensitive.x == evaluation.x and sensitive.qualified == evaluation.qualified
    assert sensitive.n_trials == evaluation.n_trials
    assert dict(sensitive.trial_sharpes) == dict(evaluation.trial_sharpes)
    pairs = zip((sensitive.s0, *sensitive.candidates), (evaluation.s0, *evaluation.candidates), strict=True)
    for a, b in pairs:
        assert a.gates == b.gates and a.reported == b.reported
        assert a.dsr == b.dsr and a.pbo == b.pbo
        assert (a.walk is None) == (b.walk is None)  # S0 has no walk
        if a.walk is not None and b.walk is not None:
            assert a.walk.schedule == b.walk.schedule
        assert a.pooled[MD].equity == b.pooled[MD].equity  # the run costs still drive the headline series


def test_report_shows_the_sensitivity_block_marked_not_gating(sensitive: Phase3Evaluation) -> None:
    text = render_report(sensitive, SPEC)
    assert "sensitivity, not gating (D-091)" in text
    block = text.split("sensitivity, not gating (D-091)", 1)[1].split("\n## ", 1)[0]
    for s in (sensitive.s0, *sensitive.candidates):
        for mode in (MD, NO):
            assert f"| {s.name} ({mode.value}) |" in block
    assert f"buy-and-hold ({MD.value})" in block and f"buy-and-hold ({NO.value})" in block
    assert "PROPOSED" not in text and "open reading" not in text


def test_report_pbo_line_names_the_gating_convention_and_the_informational_share(
    evaluation: Phase3Evaluation,
) -> None:
    text = render_report(evaluation, SPEC)
    assert "lambda <= 0, the gating value, D-093" in text
    assert "share with lambda < 0, for information only" in text
