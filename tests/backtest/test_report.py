"""Report rendering and the run_backtest CLI (synthetic bars only; the store is never touched)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_backtest as cli
from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.report import build_report, render_markdown
from tbot.core.types import Bar, Timeframe
from tests.backtest.helpers import Scripted, make_config
from tests.backtest.test_engine import five_bars
from tests.sim_helpers import bars_from_closes


def test_report_dict_is_json_serialisable_and_complete() -> None:
    feed = BacktestFeed(five_bars())
    cfg = make_config()
    strat = Scripted([0.5, 0.55, 0.0, 0.4, 0.4])
    res = run_backtest(feed, strat, cfg)
    report = build_report(res, run_benchmarks(feed, strat, cfg, res))
    json.dumps(report)  # no Decimal / datetime objects leak out
    assert set(report["metrics"]) == {"strategy", "buy_and_hold", "vol_matched_buy_and_hold"}
    assert report["costs"]["taker_fee_bps"] == "50" and report["costs"]["slippage_bps"] == "100"
    assert report["health"]["decisions"] == {"ORDER": 3, "NO_ACTION": 2}
    assert report["health"]["fill_rejections"] == 0 and report["health"]["unfilled_orders_at_end"] == 0
    assert report["metrics"]["strategy"]["fees_paid"] == pytest.approx(66.4270972275)


def test_markdown_shows_na_for_undefined_metrics_and_health_counters() -> None:
    res = run_backtest(BacktestFeed(five_bars()), Scripted([0.0]), make_config())
    text = render_markdown(build_report(res))
    assert "n/a" in text  # zero-variance ratios are undefined, never printed as 0
    assert "Run health" in text and "Caveats" in text and "| CAGR |" in text
    assert "vol-matched" not in text  # no benchmarks requested


def test_cli_end_to_end_on_synthetic_bars(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    closes = [100.0 + (i % 7) * 3 + i for i in range(40)]
    calls: list[tuple[str, Timeframe]] = []

    def fake_load(symbol: str, timeframe: Timeframe, **_: object) -> list[Bar]:
        calls.append((symbol, timeframe))
        return bars_from_closes(closes)

    monkeypatch.setattr(cli, "load_bars", fake_load)
    out = tmp_path / "r.json"
    code = cli.main(["--strategy", "sma_filter", "--sma-n", "5", "--json", str(out)])
    assert code == 0 and calls == [("BTCUSDT", Timeframe.D1)]
    text = capsys.readouterr().out
    assert "sma_filter_5" in text and "buy & hold" in text and "vol-matched B&H" in text
    assert json.loads(out.read_text(encoding="utf-8"))["strategy"] == "sma_filter_5"


def test_cli_rejects_unknown_strategy_name() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        cli.make_strategy("nope", 5)
