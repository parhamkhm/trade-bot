"""The three open-reading switches (``validation.policy``) and the cost / slippage-model plumbing."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from tbot.backtest.slippage import stressed
from tbot.core.types import BarWindow, Side, TargetIntent, Timeframe
from tbot.execution.simulated import DepthCurveSlippage, FlatSlippage, SlippageContext
from tbot.validation import policy, stats
from tbot.validation.runs import (
    NORMAL_COSTS,
    STRESS_COSTS,
    BlendStrategy,
    CompositeStrategy,
    FillMode,
    make_config,
)
from tests.validation.wf_fixtures import BOUNDARY, make_market

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


def test_curve_from_g0_summary_is_an_explicit_argument_never_a_default() -> None:
    """The PROPOSED builder (pending Parham) is only reachable via --slippage-model; never a default."""
    import scripts.validation.phase3_run as run_script

    tree = ast.parse(Path(run_script.__file__).read_text(encoding="utf-8"))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "curve_from_g0_summary" not in used | imported


# -- reading 6: pending order under the delay ----------------------------------------------------------


def test_the_pending_policy_is_one_named_switch_and_only_skip_is_implemented() -> None:
    assert policy.PENDING_ORDER_UNDER_DELAY == "skip"
    market = make_market()
    make_config(market, D1, FillMode.MANUAL_DELAY, NORMAL_COSTS, pending_policy="skip")
    with pytest.raises(NotImplementedError, match="cancel_and_replace"):
        make_config(market, D1, FillMode.MANUAL_DELAY, NORMAL_COSTS, pending_policy="cancel_and_replace")


# -- reading 7: S1 stop source ---------------------------------------------------------------------------


class _Stop:
    """A child whose stop is a fixed fraction of the entry price."""

    warmup_bars = 1

    def __init__(self, fraction: str) -> None:
        self.id = "s"
        self._fraction = Decimal(fraction)

    def on_bar(self, window: BarWindow) -> TargetIntent:  # noqa: ARG002
        raise AssertionError("not used")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price * self._fraction


def _blend(source: policy.StopSource | None, *, second_scheduled_from: datetime) -> BlendStrategy:
    market = make_market()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    fractions = {"A": "0.90", "B": "0.85", "C": "0.80", "D": "0.95"}

    def member(key: str, at: datetime) -> CompositeStrategy:
        return CompositeStrategy(
            strategy_id=key,
            market=market,
            schedule=[(at, key)],
            factory=lambda k: _Stop(fractions[k]),
            timeframes={key: D1},
            feed_timeframe=D1,
        )

    members = [member("A", second_scheduled_from), member("B", start), member("C", start), member("D", start)]
    if source is None:
        return BlendStrategy("s1", members)
    return BlendStrategy("s1", members, stop_source=source)


def _window_at(ts: datetime) -> BarWindow:
    window = make_market().window_to(D1, ts)
    assert window is not None
    return window


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("first_scheduled_member", "17000"),  # A is not scheduled yet: the first SCHEDULED one is B (0.85)
        ("tightest_member", "19000"),  # highest stop of B, C, D (D: 0.95)
        ("widest_member", "16000"),  # lowest stop (C: 0.80)
    ],
)
def test_s1_stop_source_switch(source: policy.StopSource, expected: str) -> None:
    blend = _blend(source, second_scheduled_from=BOUNDARY)
    ts = datetime(2020, 3, 1, tzinfo=UTC)  # before BOUNDARY: member A has no scheduled point
    assert blend.stop_price(_window_at(ts), Decimal(20_000)) == Decimal(expected)


def test_s1_stop_default_is_the_policy_constant_and_is_the_first_scheduled_member() -> None:
    blend = _blend(None, second_scheduled_from=datetime(2020, 1, 1, tzinfo=UTC))
    ts = datetime(2020, 3, 1, tzinfo=UTC)
    assert policy.S1_STOP_SOURCE == "first_scheduled_member"
    assert blend.stop_price(_window_at(ts), Decimal(20_000)) == Decimal(18_000)  # A (0.90) is first


def test_s1_stop_with_no_scheduled_member_is_an_error() -> None:
    blend = _blend("first_scheduled_member", second_scheduled_from=BOUNDARY)
    with pytest.raises(ValueError, match="no member has a scheduled point"):
        blend.stop_price(_window_at(datetime(2019, 12, 1, tzinfo=UTC)), Decimal(20_000))


# -- PBO logit convention ----------------------------------------------------------------------------------


def test_pbo_logit_convention_is_one_switch_and_changes_only_the_zero_logit_cases() -> None:
    assert policy.PBO_ZERO_LOGIT_IS_OVERFIT is True
    matrix = np.random.default_rng(1).normal(0.0, 0.02, (160, 3))  # N = 3: the median rank gives logit == 0
    default = stats.pbo_cscv(matrix, n_blocks=8)
    inclusive = stats.pbo_cscv(matrix, n_blocks=8, zero_logit_is_overfit=True)
    strict = stats.pbo_cscv(matrix, n_blocks=8, zero_logit_is_overfit=False)
    assert default.pbo == inclusive.pbo
    zero = sum(1 for x in default.logits if x == 0.0)
    assert zero > 0
    assert inclusive.pbo - strict.pbo == pytest.approx(zero / len(default.logits))


def test_policy_summary_names_all_three_switches() -> None:
    for token in ("skip", "first_scheduled_member", "logit == 0"):
        assert token in policy.POLICY_SUMMARY
