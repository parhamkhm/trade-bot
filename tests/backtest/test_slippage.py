"""Depth-curve slippage in the backtest layer: the G0 summary builder, the config hook, an engine run."""

from __future__ import annotations

import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.slippage import curve_from_g0_summary
from tbot.execution.simulated import DepthCurveSlippage, FlatSlippage
from tests.backtest.helpers import Scripted, make_config
from tests.backtest.test_engine import five_bars

REPO = Path(__file__).resolve().parents[2]
G0_SUMMARY = REPO / "research/reports/g0_lbank_20261011/lbank_probe_20261011T042800Z.summary.json"


def test_builder_reads_the_real_g0_summary() -> None:
    """Per side: (0, 0), (median quote within 0.1 %, 10 bps), (median quote within 0.5 %, 50 bps).
    A BUY eats the ASK side, a SELL the BID side. Spread median 1.2041733037747128e-05 % of mid
    = 1.2041733037747128e-05 * 100 bps = 0.0012041733037747128 bps; half of it is 0.0006020866518873564."""
    m = curve_from_g0_summary(G0_SUMMARY)
    assert m.buy_points == (
        (Dec(0), Dec(0)),
        (Dec("693641.6038858"), Dec(10)),
        (Dec("694530.3491568"), Dec(50)),
    )
    assert m.sell_points == (
        (Dec(0), Dec(0)),
        (Dec("1334062.39904865"), Dec(10)),
        (Dec("1334148.0537138"), Dec(50)),
    )
    assert m.half_spread_bps == Dec("0.0006020866518873564")
    assert m.equity_normaliser is None


def test_builder_options_and_stress_scaling() -> None:
    m = curve_from_g0_summary(G0_SUMMARY, equity_normaliser=Dec(10_000))
    assert m.equity_normaliser == Dec(10_000)
    far = curve_from_g0_summary(G0_SUMMARY, near_band="0.005", far_band="0.01")
    assert far.buy_points[1:] == ((Dec("694530.3491568"), Dec(50)), (Dec("1794735.3478725"), Dec(100)))
    twice = m.scaled(Dec(2))
    assert twice.half_spread_bps == Dec("0.0012041733037747128")
    assert twice.buy_points[1] == (Dec("693641.6038858"), Dec(20))


def test_builder_rejects_files_that_are_not_a_g0_summary(tmp_path: Path) -> None:
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"book": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a G0 probe summary"):
        curve_from_g0_summary(junk)
    data = json.loads(G0_SUMMARY.read_text(encoding="utf-8"))
    del data["book"]["depth"]["0.005"]
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
