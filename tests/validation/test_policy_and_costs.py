"""The decided items (``validation.policy``, D-090..D-093) and the cost / slippage-model plumbing."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from tbot.backtest.slippage import stressed
from tbot.core.types import Side, Timeframe
from tbot.execution.simulated import DepthCurveSlippage, FlatSlippage, SlippageContext
from tbot.validation import policy, stats
from tbot.validation.runs import (
    NORMAL_COSTS,
    STRESS_COSTS,
    FillMode,
    make_config,
)
from tests.validation.wf_fixtures import make_market

D1 = Timeframe.D1
CTX = SlippageContext(Side.BUY, Decimal(1), Decimal(20_000), datetime(2020, 1, 2, tzinfo=UTC))


def _curve() -> DepthCurveSlippage:
    """A real depth curve: 10 bps at 1 000 USDT, 50 bps at 5 000, half spread 1 bp, equity 10 000."""
    pts = ((Decimal(0), Decimal(0)), (Decimal(1_000), Decimal(10)), (Decimal(5_000), Decimal(50)))
    return DepthCurveSlippage(pts, pts, Decimal(1), Decimal(10_000))


CURVE_CTX = SlippageContext(
    Side.BUY, Decimal("0.05"), Decimal(20_000), datetime(2020, 1, 2, tzinfo=UTC), Decimal(10_000)
)


# -- costs -------------------------------------------------------------------------------------


def test_stressed_flat_costs_double_fee_and_slippage() -> None:
    assert STRESS_COSTS.fee_bps == 2 * NORMAL_COSTS.fee_bps
    assert STRESS_COSTS.slippage_bps == 2 * NORMAL_COSTS.slippage_bps
    assert STRESS_COSTS.is_flat_slippage and NORMAL_COSTS.is_flat_slippage
    assert STRESS_COSTS.slippage_model is None


def test_stress_goes_through_the_engine_helper_for_a_real_depth_curve() -> None:
    curve = _curve()
    costs = NORMAL_COSTS.with_model(curve, "curve")
    out = stressed(costs.to_cost_config(), curve, 2)
    spec = costs.stressed()
    assert spec.slippage_model == out.slippage_model  # the engine's own scaled() copy, not a wrapper
    assert isinstance(spec.slippage_model, DepthCurveSlippage) and not spec.is_flat_slippage
    assert spec.to_cost_config() == out.costs
    # the curve (not only the flat stub) doubled: every cost and the half spread
    assert curve.slippage_bps(CURVE_CTX) == Decimal(11)  # 1 000 USDT -> 10 bps + 1 half spread
    assert spec.slippage_model.slippage_bps(CURVE_CTX) == Decimal(22)
    assert spec.fee_bps == 2 * costs.fee_bps and spec.slippage_bps == 2 * costs.slippage_bps
    # the original is untouched
    assert costs.slippage_model is curve and curve.slippage_bps(CURVE_CTX) == Decimal(11)


def test_a_flat_model_object_is_flat_and_a_foreign_model_cannot_be_stressed() -> None:
    flat = NORMAL_COSTS.with_model(FlatSlippage(Decimal(5)), "flat object")
    assert flat.is_flat_slippage
    stressed_flat = flat.stressed()
    assert stressed_flat.slippage_model == FlatSlippage(Decimal(10))

    class Foreign:
        def slippage_bps(self, ctx: SlippageContext) -> Decimal:  # noqa: ARG002
            return Decimal(1)

    with pytest.raises(TypeError, match="cannot stress"):
        NORMAL_COSTS.with_model(Foreign(), "foreign").stressed()


def test_make_config_passes_both_the_stressed_costs_and_the_stressed_model_to_the_engine() -> None:
    market = make_market()
    curve = _curve()
    costs = NORMAL_COSTS.with_model(curve, "curve")
    normal_cfg = make_config(market, D1, FillMode.NEXT_OPEN, costs)
    assert normal_cfg.slippage_model is curve
    assert normal_cfg.costs == costs.to_cost_config()
    out = stressed(costs.to_cost_config(), curve, 2)
    stress_cfg = make_config(market, D1, FillMode.NEXT_OPEN, costs.stressed())
    assert stress_cfg.costs == out.costs  # fee x2 AND slippage_bps x2, from the helper
    assert stress_cfg.slippage_model == out.slippage_model
    assert stress_cfg.costs.taker_fee_bps == 2 * normal_cfg.costs.taker_fee_bps
    assert stress_cfg.costs.slippage_bps == 2 * normal_cfg.costs.slippage_bps
    assert make_config(market, D1, FillMode.NEXT_OPEN, NORMAL_COSTS).slippage_model is None


def test_run_script_uses_the_median_curve_for_the_run_and_adverse_p90_only_as_the_sensitivity() -> None:
    """D-091: ``curve_from_g0_summary`` is called once with ``statistic="median"`` (the run) and once with
    ``"adverse_p90_cost"`` (the sensitivity); without any option the costs are flat (pre-flight refuses)."""
    import scripts.validation.phase3_run as run_script

    tree = ast.parse(Path(run_script.__file__).read_text(encoding="utf-8"))
    used: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "curve_from_g0_summary":
            (kw,) = [k for k in node.keywords if k.arg == "statistic"]
            assert isinstance(kw.value, ast.Constant)
            used.append(str(kw.value.value))
    assert sorted(used) == ["adverse_p90_cost", "median"]
    assert run_script._slippage_cost(None) is NORMAL_COSTS and NORMAL_COSTS.is_flat_slippage


# -- the decided items (D-090..D-093) ----------------------------------------------------------------------


def test_policy_summary_lists_the_decided_items_with_their_numbers_and_no_open_reading() -> None:
    for token in ("D-090", "D-091", "D-092", "D-093", "14 non-S0", "<= 0"):
        assert token in policy.POLICY_SUMMARY
    for gone in ("open reading", "PROPOSED", "pending Parham", "first_scheduled_member", "4h order"):
        assert gone not in policy.POLICY_SUMMARY
    assert [ref for ref, _ in policy.DECISIONS] == ["D-090", "D-091", "D-092", "D-093"]


def test_the_policy_module_has_no_switches_left() -> None:
    assert not hasattr(policy, "PENDING_ORDER_UNDER_DELAY")
    assert not hasattr(policy, "S1_STOP_SOURCE")
    assert not hasattr(policy, "PBO_ZERO_LOGIT_IS_OVERFIT")
    market = make_market()
    with pytest.raises(TypeError):
        make_config(market, D1, FillMode.MANUAL_DELAY, NORMAL_COSTS, pending_policy="skip")  # type: ignore[call-arg]


def test_pbo_gating_value_is_the_lambda_le_zero_share_and_the_strict_share_is_informational() -> None:
    matrix = np.random.default_rng(1).normal(0.0, 0.02, (160, 3))  # N = 3: the median rank gives logit == 0
    res = stats.pbo_cscv(matrix, n_blocks=8)
    n = len(res.logits)
    assert res.pbo == sum(1 for x in res.logits if x <= 0.0) / n
    assert res.pbo_strict == sum(1 for x in res.logits if x < 0.0) / n
    assert res.pbo > res.pbo_strict
