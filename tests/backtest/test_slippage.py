"""Depth-curve slippage in the backtest layer: the G0 summary builder, the config hook, an engine run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.slippage import DEFAULT_EQUITY_NORMALISER, curve_from_g0_summary, stressed
from tbot.core.config import CostConfig
from tbot.core.types import Side
from tbot.execution.simulated import DepthCurveSlippage, FlatSlippage, SlippageContext
from tests.backtest.helpers import Scripted, make_config
from tests.backtest.test_engine import five_bars

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2024, 1, 1, tzinfo=UTC)
G0_SUMMARY = REPO / "research/reports/g0_lbank_20261011/lbank_probe_20261011T042800Z.summary.json"


def test_builder_reads_the_real_g0_summary() -> None:
    """Points come from book.max_fill[band]["vs_touch"][side]["quote_median"]: the LARGEST MARKET ORDER that
    fills within 0.1 % / 0.5 % of the touch. A BUY eats the ASK side (max_fill "buy"), a SELL the BID side
    ("sell"). Cost at those sizes is the band itself: 10 and 50 bps. Spread median 1.2041733037747128e-05 % of
    mid = 0.0012041733037747128 bps; half of it is 0.0006020866518873564."""
    m = curve_from_g0_summary(G0_SUMMARY)
    assert m.buy_points == (
        (Dec(0), Dec(0)),
        (Dec("791337.7167268150914253939475"), Dec(10)),
        (Dec("1579159.116131694431457999032"), Dec(50)),
    )
    assert m.sell_points == (
        (Dec(0), Dec(0)),
        (Dec("1507954.925878956487830472434"), Dec(10)),
        (Dec("2299814.294177067930772980798"), Dec(50)),
    )
    assert m.half_spread_bps == Dec("0.0006020866518873564")
    assert m.equity_normaliser == Dec(10_000)  # SPEC 7 G3: equity fixed at 10,000 USDT


def test_builder_p10_statistic_is_the_conservative_sensitivity() -> None:
    m = curve_from_g0_summary(G0_SUMMARY, statistic="p10")
    assert m.buy_points == (
        (Dec(0), Dec(0)),
        (Dec("292989.4011110195692658545460"), Dec(10)),
        (Dec("607395.7384117860242833704533"), Dec(50)),
    )
    assert m.sell_points == (
        (Dec(0), Dec(0)),
        (Dec("880697.2835246913241879442317"), Dec(10)),
        (Dec("1785742.864994034092678461543"), Dec(50)),
    )
    assert m.half_spread_bps == Dec("0.0006020866518873564")  # always the median spread
    median = curve_from_g0_summary(G0_SUMMARY)
    # a thinner book is costlier at the same size
    assert m.slippage_bps(_ctx(Side.BUY, "10000")) > median.slippage_bps(_ctx(Side.BUY, "10000"))
    assert m.slippage_bps(_ctx(Side.SELL, "10000")) > median.slippage_bps(_ctx(Side.SELL, "10000"))
    with pytest.raises(ValueError, match="statistic"):
        curve_from_g0_summary(G0_SUMMARY, statistic="mean")  # type: ignore[arg-type]


def _ctx(side: Side, notional: str) -> SlippageContext:
    return SlippageContext(
        side=side, quantity=Dec(notional) / 100, reference_price=Dec(100), ts=T0, equity=Dec(10_000)
    )


def test_cost_of_a_10000_usdt_order_under_each_statistic() -> None:
    """Hand formula on the first segment: bps = 10 * 10000 / N1 + half spread (HS = 0.000602086...)."""
    hs = Dec("0.0006020866518873564")
    median = curve_from_g0_summary(G0_SUMMARY)
    p10 = curve_from_g0_summary(G0_SUMMARY, statistic="p10")
    # median: buy 100000 / 791337.7167... = 0.12636 bps, sell 100000 / 1507954.9258... = 0.06632 bps
    assert (
        median.slippage_bps(_ctx(Side.BUY, "10000"))
        == Dec(100_000) / Dec("791337.7167268150914253939475") + hs
    )
    assert (
        median.slippage_bps(_ctx(Side.SELL, "10000"))
        == Dec(100_000) / Dec("1507954.925878956487830472434") + hs
    )
    assert Dec("0.1269") < median.slippage_bps(_ctx(Side.BUY, "10000")) < Dec("0.1270")
    assert Dec("0.0669") < median.slippage_bps(_ctx(Side.SELL, "10000")) < Dec("0.0670")
    # p10: buy 100000 / 292989.4011... = 0.34131 bps, sell 100000 / 880697.2835... = 0.11354 bps
    assert (
        p10.slippage_bps(_ctx(Side.BUY, "10000")) == Dec(100_000) / Dec("292989.4011110195692658545460") + hs
    )
    assert (
        p10.slippage_bps(_ctx(Side.SELL, "10000")) == Dec(100_000) / Dec("880697.2835246913241879442317") + hs
    )
    assert Dec("0.341") < p10.slippage_bps(_ctx(Side.BUY, "10000")) < Dec("0.342")
    assert Dec("0.1141") < p10.slippage_bps(_ctx(Side.SELL, "10000")) < Dec("0.1142")


def test_builder_does_not_read_the_depth_section(tmp_path: Path) -> None:
    """book.depth is depth WITHIN a band of mid, a different quantity: wrecking it must change nothing, and
    the file does not even need it."""
    expected = curve_from_g0_summary(G0_SUMMARY)
    data = json.loads(G0_SUMMARY.read_text(encoding="utf-8"))
    for band in data["book"]["depth"].values():
        for side in band.values():
            side["quote_median"] = "1"
            side["quote_p10"] = "1"
    wrecked = tmp_path / "wrecked.json"
    wrecked.write_text(json.dumps(data), encoding="utf-8")
    assert curve_from_g0_summary(wrecked) == expected
    del data["book"]["depth"]
    gone = tmp_path / "gone.json"
    gone.write_text(json.dumps(data), encoding="utf-8")
    assert curve_from_g0_summary(gone) == expected
    # and the points are the max_fill ones, not the (much smaller) depth-within-band ones
    assert expected.buy_points[1][0] != Dec("693641.6038858")


def test_builder_uses_the_vs_touch_reference_not_vs_mid(tmp_path: Path) -> None:
    data = json.loads(G0_SUMMARY.read_text(encoding="utf-8"))
    for band in data["book"]["max_fill"].values():
        for side in band["vs_mid"].values():
            side["quote_median"] = "1"
    changed = tmp_path / "vs_mid_changed.json"
    changed.write_text(json.dumps(data), encoding="utf-8")
    assert curve_from_g0_summary(changed) == curve_from_g0_summary(G0_SUMMARY)


def test_builder_defaults_to_the_10000_usdt_equity_normaliser_and_it_is_overridable() -> None:
    assert Dec(10_000) == DEFAULT_EQUITY_NORMALISER
    assert curve_from_g0_summary(G0_SUMMARY).equity_normaliser == Dec(10_000)
    assert curve_from_g0_summary(G0_SUMMARY, equity_normaliser=Dec(50_000)).equity_normaliser == Dec(50_000)
    raw = curve_from_g0_summary(G0_SUMMARY, equity_normaliser=None)
    assert raw.equity_normaliser is None
    # behaviour: at equity 20 000 a 10 000 USDT order is looked up as 5 000 (default) or as 10 000 (None)
    at = SlippageContext(
        side=Side.BUY, quantity=Dec(100), reference_price=Dec(100), ts=T0, equity=Dec(20_000)
    )
    default = curve_from_g0_summary(G0_SUMMARY)
    half = Dec("0.0006020866518873564")
    assert default.slippage_bps(at) == Dec(5000) * 10 / Dec("791337.7167268150914253939475") + half
    assert raw.slippage_bps(at) == Dec(10000) * 10 / Dec("791337.7167268150914253939475") + half


def test_builder_options_and_stress_scaling() -> None:
    m = curve_from_g0_summary(G0_SUMMARY)
    assert m.equity_normaliser == Dec(10_000)
    twice = m.scaled(Dec(2))
    assert twice.half_spread_bps == Dec("0.0012041733037747128")
    assert twice.buy_points[1] == (Dec("791337.7167268150914253939475"), Dec(20))


def test_builder_rejects_files_that_are_not_a_g0_summary(tmp_path: Path) -> None:
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"book": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a G0 probe summary"):
        curve_from_g0_summary(junk)
    data = json.loads(G0_SUMMARY.read_text(encoding="utf-8"))
    del data["book"]["max_fill"]["0.005"]
    cut = tmp_path / "cut.json"
    cut.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"0\.005"):
        curve_from_g0_summary(cut)


def test_default_config_is_flat_and_a_depth_model_is_opt_in() -> None:
    default = make_config()
    assert isinstance(default.slippage(), FlatSlippage) and default.slippage_model is None
    depth = curve_from_g0_summary(G0_SUMMARY)
    cfg = make_config(slippage_model=depth)
    assert isinstance(cfg, BacktestConfig) and cfg.slippage() is depth


def test_engine_with_a_depth_curve_equivalent_to_flat_reproduces_the_flat_run() -> None:
    """A curve that is 100 bps at every positive notional (0 -> 1e-9 rises to 100, then stays) is the
    default test config's flat 1 % slippage, so the hand-computed 5-bar example must come out identical."""
    flat100 = ((Dec(0), Dec(0)), (Dec("1E-9"), Dec(100)), (Dec("1E9"), Dec(100)))
    depth = DepthCurveSlippage(buy_points=flat100, sell_points=flat100, half_spread_bps=Dec(0))
    weights = [0.5, 0.55, 0.0, 0.4, 0.4]
    base = run_backtest(BacktestFeed(five_bars()), Scripted(weights), make_config())
    res = run_backtest(BacktestFeed(five_bars()), Scripted(weights), make_config(slippage_model=depth))
    assert [(r.fill.side, r.fill.price, r.fill.quantity, r.fill.fee) for r in res.fills] == [
        (r.fill.side, r.fill.price, r.fill.quantity, r.fill.fee) for r in base.fills
    ]
    assert [p.equity for p in res.equity] == [p.equity for p in base.equity]
    assert res.slippage_cost == base.slippage_cost == Dec("132.4298955")


# ---------------------------------------------------------------------------------------------
# stressed(): the x2 cost stress in one place
# ---------------------------------------------------------------------------------------------

COSTS = CostConfig(taker_fee_bps=Dec(10), maker_fee_bps=Dec(8), slippage_bps=Dec(5))


def test_stressed_with_a_curve_doubles_fees_and_scales_the_curve() -> None:
    curve = curve_from_g0_summary(G0_SUMMARY)
    costs, model = stressed(COSTS, curve)
    assert (costs.taker_fee_bps, costs.maker_fee_bps) == (Dec(20), Dec(16))
    assert costs.slippage_bps == Dec(10)  # the sizing-estimate stays consistent with the stress
    assert isinstance(model, DepthCurveSlippage)
    assert model == curve.scaled(Dec(2))
    assert model.half_spread_bps == curve.half_spread_bps * 2
    assert model.buy_points[1] == (curve.buy_points[1][0], Dec(20))
    assert model.equity_normaliser == Dec(10_000)
    assert COSTS.taker_fee_bps == Dec(10)  # inputs are not mutated
    # the broker really prices twice the cost: same order, twice the bps
    c = SlippageContext(side=Side.BUY, quantity=Dec(100), reference_price=Dec(100), ts=T0, equity=Dec(10_000))
    assert abs(model.slippage_bps(c) - curve.slippage_bps(c) * 2) < Dec("1e-20")


def test_stressed_without_a_curve_doubles_the_flat_bps() -> None:
    costs, model = stressed(COSTS, None)
    assert model is None  # the engine then uses FlatSlippage.from_config(costs) = the doubled 10 bps
    assert (costs.taker_fee_bps, costs.maker_fee_bps, costs.slippage_bps) == (Dec(20), Dec(16), Dec(10))
    assert FlatSlippage.from_config(costs).bps == Dec(10)
    _, flat = stressed(COSTS, FlatSlippage(bps=Dec(7)), factor=3)
    assert flat == FlatSlippage(bps=Dec(21))


def test_stressed_rejects_unknown_models_and_bad_factors() -> None:
    class Other:
        def slippage_bps(self, ctx: SlippageContext) -> Dec:  # noqa: ARG002
            return Dec(0)

    with pytest.raises(TypeError, match="Other"):
        stressed(COSTS, Other())
    with pytest.raises(ValueError, match="factor"):
        stressed(COSTS, None, factor=-1)
