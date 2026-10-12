"""The three open-reading switches (``validation.policy``) and the cost / slippage-model plumbing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from tbot.core.types import BarWindow, Side, TargetIntent, Timeframe
from tbot.execution.simulated import SlippageContext
from tbot.validation import policy, stats
from tbot.validation.runs import (
    NORMAL_COSTS,
    STRESS_COSTS,
    BlendStrategy,
    CompositeStrategy,
    FillMode,
    ScaledSlippage,
    make_config,
)
from tests.validation.wf_fixtures import BOUNDARY, make_market

D1 = Timeframe.D1
CTX = SlippageContext(Side.BUY, Decimal(1), Decimal(20_000), datetime(2020, 1, 2, tzinfo=UTC))


class _Curve:
    """Impact grows with notional: 1 bp per 10 000 USDT."""

    def slippage_bps(self, ctx: SlippageContext) -> Decimal:
        return ctx.quantity * ctx.reference_price / Decimal(10_000)


# -- costs -------------------------------------------------------------------------------------


def test_scaled_slippage_multiplies_the_inner_impact() -> None:
    assert _Curve().slippage_bps(CTX) == Decimal(2)
    assert ScaledSlippage(_Curve(), Decimal(2)).slippage_bps(CTX) == Decimal(4)


def test_stressed_flat_costs_double_fee_and_slippage() -> None:
    assert STRESS_COSTS.fee_bps == 2 * NORMAL_COSTS.fee_bps
    assert STRESS_COSTS.slippage_bps == 2 * NORMAL_COSTS.slippage_bps
    assert STRESS_COSTS.is_flat_slippage and NORMAL_COSTS.is_flat_slippage


def test_stressed_curve_costs_scale_the_curve_and_stay_non_flat() -> None:
    costs = NORMAL_COSTS.with_model(_Curve(), "curve")
    stressed = costs.stressed()
    assert not stressed.is_flat_slippage and stressed.slippage_model is not None
    assert stressed.slippage_model.slippage_bps(CTX) == 2 * _Curve().slippage_bps(CTX)
    assert stressed.fee_bps == 2 * costs.fee_bps
    # the original is untouched, and scaling twice composes
    assert costs.slippage_model is not None and costs.slippage_model.slippage_bps(CTX) == Decimal(2)
    assert costs.stressed(3).slippage_model.slippage_bps(CTX) == Decimal(6)  # type: ignore[union-attr]


def test_make_config_passes_the_slippage_model_to_the_engine() -> None:
    market = make_market()
    model = _Curve()
    cfg = make_config(market, D1, FillMode.NEXT_OPEN, NORMAL_COSTS.with_model(model, "curve"))
    assert cfg.slippage_model is model
    assert make_config(market, D1, FillMode.NEXT_OPEN, NORMAL_COSTS).slippage_model is None


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
