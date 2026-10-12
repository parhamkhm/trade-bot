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
)
from tbot.validation.report import LABEL, render_report
from tbot.validation.runs import FillMode
from tests.validation.wf_fixtures import (
    CANDIDATE,
    FIRST_TRADABLE,
    POINTS,
    SPEC,
    factory,
    fake_run,
    make_market,
)

MD, NO = FillMode.MANUAL_DELAY, FillMode.NEXT_OPEN


@pytest.fixture(scope="module")
def evaluation() -> Phase3Evaluation:
    inp = EvalInputs(
        btc=make_market(),
        spec=SPEC,
        factory=factory,
        candidates=[CANDIDATE],
        first_tradable=FIRST_TRADABLE,
        logged_non_s0_trials=len(POINTS),
        eth=make_market(seed=13, symbol="ETHUSDT"),
    )
    return evaluate(inp)


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


# -- end to end ---------------------------------------------------------------------------------------


def test_every_strategy_has_its_gates(evaluation: Phase3Evaluation) -> None:
    names = {g.name for g in evaluation.s0.gates}
    assert {"maxDD vs B&H", "Sharpe vs B&H", "DSR", "ETH/USDT Sharpe", "x2 cost stress"} <= names
    # the section-7.1 variants are not registered here: the gate exists and fails with a reason
    variants = next(g for g in evaluation.s0.gates if g.name == "S0 robustness variants")
    assert not variants.passed and "not evaluated" in variants.note
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
