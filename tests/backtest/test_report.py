"""Report rendering and the run_backtest CLI (synthetic bars only; the store is never touched)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts import run_backtest as cli
from tbot.backtest.benchmarks import run_benchmarks
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.report import build_report, is_run_invalid, render_markdown
from tbot.backtest.result import BacktestResult
from tbot.core.config import CostConfig
from tbot.core.types import Bar, Strategy, Timeframe
from tests.backtest.helpers import OversizedRisk, Scripted, make_config
from tests.backtest.test_engine import five_bars, gap_up_run, oversized_run
from tests.sim_helpers import (
    SYMBOL,
    T_START,
    bars_from_closes,
    hour_bars_for_day,
    lbank_like_filters,
    make_bar,
)


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


# ---------------------------------------------------------------------------------------------
# MAJOR 2 (review round 2): provisional-slippage flag
# ---------------------------------------------------------------------------------------------


def _config_with_slippage_provisional(provisional: bool, **kwargs: object) -> BacktestConfig:
    return BacktestConfig(
        symbol=SYMBOL,
        timeframe=Timeframe.D1,
        filters=lbank_like_filters(),
        costs=CostConfig(
            taker_fee_bps=Decimal("10"), maker_fee_bps=Decimal("10"), slippage_bps=Decimal("5"),
            slippage_provisional=provisional,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def test_provisional_slippage_is_flagged_by_default_and_clears_when_measured() -> None:
    res_provisional = run_backtest(
        BacktestFeed(five_bars()), Scripted([0.0]), _config_with_slippage_provisional(True)
    )
    report = build_report(res_provisional)
    assert report["costs"]["slippage_provisional"] is True
    assert any("PROVISIONAL" in c for c in report["caveats"])
    text = render_markdown(report)
    assert "PROVISIONAL SLIPPAGE" in text

    res_measured = run_backtest(
        BacktestFeed(five_bars()), Scripted([0.0]), _config_with_slippage_provisional(False)
    )
    measured_report = build_report(res_measured)
    assert measured_report["costs"]["slippage_provisional"] is False
    assert not any("PROVISIONAL" in c for c in measured_report["caveats"])
    assert "PROVISIONAL SLIPPAGE" not in render_markdown(measured_report)


# ---------------------------------------------------------------------------------------------
# MAJOR 3 (review round 2): invalid-run warning and the CLI's non-zero exit
# ---------------------------------------------------------------------------------------------


def test_invalid_run_warning_on_fill_rejections_and_markdown_block() -> None:
    # sized at close(0)=1000 (ample notional there); bar1 gaps down to an open of 0.0001, so the fill
    # price collapses and the SAME quantity's notional falls below min_notional only at fill time --
    # a genuine broker-level rejection, not the risk manager's own (pre-submission) refusal.
    bars = [make_bar(0, "1000", "1000"), make_bar(1, "0.0001", "0.0001")]
    res = run_backtest(BacktestFeed(bars), Scripted([1.0]), make_config())
    assert res.rejections != () and "MIN_NOTIONAL" in res.rejections[0].reason
    report = build_report(res)
    assert is_run_invalid(report)
    assert report["health"]["issues"] != []
    assert any("rejected" in issue for issue in report["health"]["issues"])
    assert "INVALID RUN WARNING" in render_markdown(report)


def test_clean_run_has_no_issues_and_is_not_flagged_invalid() -> None:
    res = run_backtest(BacktestFeed(five_bars()), Scripted([0.5, 0.55, 0.0, 0.4, 0.4]), make_config())
    report = build_report(res)
    assert report["health"]["issues"] == [] and not is_run_invalid(report)
    assert "INVALID RUN WARNING" not in render_markdown(report)


def test_benchmark_health_issues_make_the_run_invalid_even_if_the_strategy_is_clean() -> None:
    """MINOR (re-review): the strategy never trades (clean), but buy & hold's BUY is rejected at fill time
    (a genuine MIN_NOTIONAL rejection: sized at 1000, the open collapses to 0.0001). A comparison against a
    benchmark that silently never entered is meaningless, so the benchmark's issue must reach
    ``health.issues`` / ``is_run_invalid`` / the markdown. (A gap-UP no longer rejects: the BUY is capped.)"""
    bars = [make_bar(0, "1000", "1000"), make_bar(1, "0.0001", "0.0001"), make_bar(2, "0.0001", "0.0001")]
    cfg = make_config(fee="50", slip="100", buffer="0")
    feed, strat = BacktestFeed(bars), Scripted([0.0])
    res = run_backtest(feed, strat, cfg)
    assert res.rejections == ()
    assert not is_run_invalid(build_report(res))  # strategy alone: clean
    bench = run_benchmarks(feed, strat, cfg, res)
    assert bench.buy_and_hold.rejections != ()
    assert "MIN_NOTIONAL" in bench.buy_and_hold.rejections[0].reason
    report = build_report(res, bench)
    assert is_run_invalid(report)
    assert any(i.startswith("buy & hold:") and "rejected" in i for i in report["health"]["issues"])
    assert not any(i.startswith("vol-matched") for i in report["health"]["issues"])  # weight 0: no orders
    assert "INVALID RUN WARNING" in render_markdown(report)


def test_cli_exits_non_zero_on_an_invalid_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # same gap-down construction as the engine-level test above, reached end-to-end through the CLI.
    bars = [make_bar(0, "1000", "1000"), make_bar(1, "0.0001", "0.0001")]

    def fake_load(symbol: str, timeframe: Timeframe, **_: object) -> list[Bar]:
        return bars

    monkeypatch.setattr(cli, "load_bars", fake_load)
    code = cli.main(["--strategy", "buy_and_hold"])
    assert code == 3
    assert "INVALID RUN WARNING" in capsys.readouterr().out


def test_cli_exits_zero_on_a_clean_run(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [100.0 + (i % 7) * 3 + i for i in range(40)]
    monkeypatch.setattr(cli, "load_bars", lambda *_a, **_k: bars_from_closes(closes))
    assert cli.main(["--strategy", "buy_and_hold"]) == 0


# ---------------------------------------------------------------------------------------------
# MINOR 9 / 10 (review round 2): resize and gap-fill counts actually reach the report
# ---------------------------------------------------------------------------------------------


def test_report_health_counts_a_resize() -> None:
    """Reuses the BLOCKER repro (rising market during a 6h delay): one BUY is resized down at fill
    time, and that must show up in the report's health counters, not just in ``result.resizes``."""
    day0 = make_bar(0, "100", "100")
    day1 = make_bar(0, "100", "104.8", start=day0.ts)
    hours = hour_bars_for_day(day1, [str(Decimal("100") + Decimal("0.2") * h) for h in range(24)])
    cfg = make_config(
        fee="50", slip="100", buffer="0", fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1
    )
    res = run_backtest(BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=BacktestFeed(hours))
    assert len(res.resizes) == 1
    report = build_report(res)
    assert report["health"]["resizes"] == 1
    assert "resizes 1" in render_markdown(report)
    # 0.2 %/h with 1 % slippage is a 1.19 % cap: affordable at the sizing close, so a price_move
    assert report["health"]["resizes_anomaly"] == 0 and report["health"]["resizes_price_move"] == 1
    assert "(price_move 1, anomaly 0)" in render_markdown(report)
    assert not is_run_invalid(report)


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
@pytest.mark.parametrize("gap", ["0.43", "3", "5"])
def test_resizes_alone_do_not_make_a_run_invalid(delay: timedelta, gap: str) -> None:
    """(d) A price_move resize is counted but the run is clean (no issue, no INVALID RUN block), whatever the
    size of the move: +0.43 % is the largest real open-vs-close gap, +3 % a large 6 h move, +5 % a stress."""
    res, _ = gap_up_run(gap, delay)
    assert len(res.resizes) == 1 and res.rejections == ()
    report = build_report(res)
    assert report["health"]["issues"] == [] and not is_run_invalid(report)
    assert report["health"]["resizes_price_move"] == 1 and report["health"]["resizes_anomaly"] == 0
    markdown = render_markdown(report)
    assert "INVALID RUN WARNING" not in markdown and "(price_move 1, anomaly 0)" in markdown


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_anomaly_resize_marks_the_run_invalid_without_any_rejection(delay: timedelta) -> None:
    """A request twice what cash covers at the sizing price is a sizing bug -- an anomaly. The BUY is NOT
    rejected (no INSUFFICIENT_FUNDS) but the run is INVALID, in the report dict and the markdown block."""
    res = oversized_run(delay)
    assert res.rejections == () and len(res.fills) == 1
    report = build_report(res)
    assert report["health"]["fill_rejections"] == 0 and report["health"]["resizes_anomaly"] == 1
    assert is_run_invalid(report)
    assert any("resize anomaly" in issue for issue in report["health"]["issues"])
    markdown = render_markdown(report)
    assert "INVALID RUN WARNING" in markdown and "(price_move 0, anomaly 1)" in markdown


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(hours=6)], ids=["next_open", "delay_6h"])
def test_uncapped_anomaly_is_not_counted_as_a_resize_but_invalidates_the_run(delay: timedelta) -> None:
    """An oversized request whose price then falls fills in full: no cap, so ``resizes`` stays 0, but the
    D-082 anomaly record is counted under ``resizes_anomaly`` and the run is INVALID."""
    res = oversized_run(delay, factor="1.02", fill_open="95")
    assert res.rejections == () and len(res.fills) == 1
    (rec,) = res.resizes
    assert rec.kind == "anomaly" and rec.capped_quantity == rec.requested_quantity and rec.filled
    report = build_report(res)
    health = report["health"]
    assert health["resizes"] == 0
    assert health["resizes_anomaly"] == 1 and health["resizes_price_move"] == 0
    assert is_run_invalid(report)
    markdown = render_markdown(report)
    assert "resizes 0 [actual caps]" in markdown and "(price_move 0, anomaly 1)" in markdown
    assert "INVALID RUN WARNING" in markdown


def test_genuine_rejection_still_fires_next_to_a_resize() -> None:
    """(d) A genuine fill-time rejection keeps the INVALID RUN rule, whatever the resize counts say."""
    bars = [make_bar(0, "1000", "1000"), make_bar(1, "0.0001", "0.0001")]
    res = run_backtest(BacktestFeed(bars), Scripted([1.0]), make_config())
    assert res.rejections != () and res.resizes == ()
    report = build_report(res)
    assert is_run_invalid(report) and report["health"]["resizes"] == 0
    assert any("rejected" in issue for issue in report["health"]["issues"])


def test_cli_exits_0_on_a_large_price_move_resize(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: buy & hold through the CLI on a +5 % open gap is a price_move resize: valid, exit 0."""
    bars = [make_bar(0, "100", "100"), make_bar(1, "105", "105"), make_bar(2, "105", "105")]
    monkeypatch.setattr(cli, "load_bars", lambda *_a, **_k: bars)
    assert cli.main(["--strategy", "buy_and_hold"]) == 0
    out = capsys.readouterr().out
    assert "INVALID RUN WARNING" not in out and "(price_move 1, anomaly 0)" in out


def test_cli_exits_3_on_an_anomaly_resize(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: a sizing bug (a risk manager requesting 2x cash) reached through the CLI exits 3 with the
    INVALID RUN block, even though the prices do not move and nothing is rejected."""
    bars = [make_bar(0, "100", "100"), make_bar(1, "100", "100"), make_bar(2, "100", "100")]
    monkeypatch.setattr(cli, "load_bars", lambda *_a, **_k: bars)

    def oversized(feed: BacktestFeed, strat: Strategy, cfg: BacktestConfig, **kw: Any) -> BacktestResult:
        return run_backtest(feed, strat, cfg, risk=OversizedRisk(cfg), **kw)

    monkeypatch.setattr(cli, "run_backtest", oversized)
    assert cli.main(["--strategy", "buy_and_hold"]) == 3
    out = capsys.readouterr().out
    assert "INVALID RUN WARNING" in out and "resize anomaly" in out and "(price_move 0, anomaly 1)" in out


def test_report_health_counts_a_gap_fill() -> None:
    """A delayed fill that lands later than ``created_ts + fill_delay`` because the exec feed has a gap
    must be counted in the report, not only visible on the individual ``FillRecord``."""
    day0 = make_bar(0, "100", "100")
    day1 = make_bar(0, "100", "100", start=day0.ts)
    # hourly bars for day1, but hours 5 and 6 are missing: the deadline (close(day0) + 6h) falls in the
    # gap, so the first eligible bar is hour 7, later than the configured delay.
    all_hours = hour_bars_for_day(day1, [str(100 + h) for h in range(24)])
    gapped_hours = [b for b in all_hours if b.open_ts not in (T_START + timedelta(days=1, hours=5),
                                                               T_START + timedelta(days=1, hours=6))]
    cfg = make_config(fee="0", slip="0", fill_delay=timedelta(hours=6), exec_timeframe=Timeframe.H1)
    res = run_backtest(
        BacktestFeed([day0, day1]), Scripted([1.0, 1.0]), cfg, exec_feed=BacktestFeed(gapped_hours)
    )
    assert len(res.fills) == 1 and res.fills[0].gap_fill is True
    report = build_report(res)
    assert report["health"]["gap_fills"] == 1
    assert "gap fills 1" in render_markdown(report)
