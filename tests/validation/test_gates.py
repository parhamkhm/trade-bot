"""The pre-registered G3 gates (docs/SPEC.md section 7.2 E) applied literally, including the corners."""

from __future__ import annotations

import pytest

from tbot.validation import gates as g


def test_thresholds_are_the_spec_numbers() -> None:
    assert (g.DD_RATIO_MAX, g.SHARPE_RATIO_MIN, g.PBO_MAX, g.DSR_MIN, g.NEIGHBOUR_KEEP_MIN) == (
        0.60,
        0.80,
        0.30,
        0.95,
        0.70,
    )


def test_drawdown_gate_boundaries() -> None:
    assert g.drawdown_gate(0.2999, 0.50).passed
    assert not g.drawdown_gate(0.3001, 0.50).passed
    res = g.drawdown_gate(0.25, 0.50)
    assert res.threshold == pytest.approx(0.30) and res.value == 0.25


def test_sharpe_gate_boundary_and_positive_benchmark() -> None:
    assert g.sharpe_gate(0.81, 1.0).passed
    assert not g.sharpe_gate(0.79, 1.0).passed
    assert g.sharpe_gate(0.79, 1.0).threshold == pytest.approx(0.80)


def test_sharpe_gate_with_a_non_positive_benchmark_is_literal_and_flagged() -> None:
    # B&H Sharpe -1.0 -> 0.8 * -1.0 = -0.8, which is HIGHER than -1.0: the literal rule is stricter
    res = g.sharpe_gate(-0.9, -1.0)
    assert res.threshold == pytest.approx(-0.8)
    assert not res.passed
    assert "stricter" in res.note
    assert g.sharpe_gate(-0.5, -1.0).passed


def test_sharpe_gate_undefined_fails() -> None:
    assert not g.sharpe_gate(None, 1.0).passed
    assert not g.sharpe_gate(1.0, None).passed


def test_pbo_gate_is_strict() -> None:
    assert g.pbo_gate(0.2999).passed
    assert not g.pbo_gate(0.30).passed


def test_dsr_gate_boundary_and_undefined() -> None:
    assert g.dsr_gate(0.95).passed
    assert not g.dsr_gate(0.9499).passed
    undefined = g.dsr_gate(None)
    assert not undefined.passed and "undefined" in undefined.note


def test_neighbour_gate_keeps_seventy_percent() -> None:
    ok = g.neighbour_gate(1.0, {"a": 0.71, "b": 1.4})
    assert ok.passed and ok.threshold == pytest.approx(0.70)
    bad = g.neighbour_gate(1.0, {"a": 0.69, "b": 1.4})
    assert not bad.passed and "a" in bad.note


def test_neighbour_gate_corners_fail() -> None:
    assert not g.neighbour_gate(0.0, {"a": 1.0}).passed
    assert not g.neighbour_gate(-0.5, {"a": 1.0}).passed
    assert not g.neighbour_gate(None, {"a": 1.0}).passed
    assert not g.neighbour_gate(1.0, {"a": None}).passed
    empty = g.neighbour_gate(1.0, {})
    assert not empty.passed and "no neighbour" in empty.note


def test_eth_gate_needs_a_strictly_positive_sharpe() -> None:
    assert g.eth_gate(0.01).passed
    assert not g.eth_gate(0.0).passed
    assert not g.eth_gate(None).passed


def test_stress_gate_is_compounded_net_return() -> None:
    assert g.stress_gate([0.10, -0.05]).passed  # 1.1 * 0.95 - 1 = +4.5 %
    assert not g.stress_gate([0.05, -0.10]).passed  # -5.5 %
    assert g.stress_gate([0.10, -0.05]).value == pytest.approx(0.045)
    assert not g.stress_gate([]).passed  # nothing earned is not "profitable"


def test_ratio_variants_gate_is_the_neighbour_rule_with_its_own_name() -> None:
    res = g.ratio_variants_gate(1.0, {"v": 0.71})
    assert res.name == "S0 robustness variants" and res.passed
    assert not g.ratio_variants_gate(1.0, {"v": 0.6}).passed


def test_total_return_and_sharpe_of() -> None:
    assert g.total_return([0.1, 0.1]) == pytest.approx(0.21)
    assert g.sharpe_of([0.0, 0.0, 0.0]) is None
