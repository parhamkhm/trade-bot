"""Tests for scripts/tabdeal_probe.py.

All HTTP is mocked with respx; no test may reach a real exchange and no test may send an order.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from scripts.tabdeal_probe import (
    best_bid_ask,
    build_symbol_filters,
    compute_clock_skew_ms,
    cumulative_depth,
    describe_unreachable_failure,
    discover_max_trades_limit,
    find_base_asset_markets,
    find_symbol_entry,
    is_saturated,
    main,
    probe_both_prefixes,
    spread_bps_and_pct,
    summarize_spreads,
    trades_window_stats,
    unsafe_key_permissions,
)

from tbot.execution.tabdeal_client import ProbeResult, TabdealClient
from tbot.monitoring import logging as tbot_logging
from tbot.monitoring.logging import REDACTED

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://api1.tabdeal.org"
READ_PREFIX = "/r/api/v1"
WRITE_PREFIX = "/api/v1"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make_client(clock: FakeClock | None = None, **overrides: Any) -> TabdealClient:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "read_prefix": READ_PREFIX,
        "write_prefix": WRITE_PREFIX,
        "clock": clock or FakeClock(),
        "requests_per_second": 1000.0,
        "timeout_seconds": 1.0,
        "max_retries": 2,
    }
    kwargs.update(overrides)
    return TabdealClient(**kwargs)


# ---------------------------------------------------------------------------------
# clock skew
# ---------------------------------------------------------------------------------


def test_clock_skew_zero_when_server_matches_midpoint() -> None:
    before = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    after = datetime(2026, 1, 1, 12, 0, 0, 200_000, tzinfo=UTC)  # +200ms latency
    midpoint_ms = (before.timestamp() + after.timestamp()) / 2 * 1000
    skew = compute_clock_skew_ms(int(midpoint_ms), before, after)
    assert abs(skew) < 1e-6


def test_clock_skew_detects_server_ahead() -> None:
    before = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    after = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    local_ms = before.timestamp() * 1000
    server_time_ms = int(local_ms) + 5_000  # server reports 5s ahead
    skew = compute_clock_skew_ms(server_time_ms, before, after)
    assert abs(skew - 5000) < 1.0


# ---------------------------------------------------------------------------------
# spread and cumulative depth maths
# ---------------------------------------------------------------------------------


def test_spread_bps_and_pct() -> None:
    bps, pct = spread_bps_and_pct(Decimal("100.00"), Decimal("100.10"))
    mid = Decimal("100.05")
    expected_pct = float((Decimal("0.10") / mid) * 100)
    assert pct == pytest.approx(expected_pct)
    assert bps == pytest.approx(expected_pct * 100)


def test_spread_rejects_crossed_book() -> None:
    with pytest.raises(ValueError, match="crossed book"):
        spread_bps_and_pct(Decimal("100.10"), Decimal("100.00"))


def test_best_bid_ask_picks_extremes() -> None:
    bids = [(Decimal("99"), Decimal("1")), (Decimal("100"), Decimal("2"))]
    asks = [(Decimal("101"), Decimal("1")), (Decimal("102"), Decimal("1"))]
    result = best_bid_ask(bids, asks)
    assert result == (Decimal("100"), Decimal("101"))


def test_best_bid_ask_none_when_one_side_empty() -> None:
    assert best_bid_ask([], [(Decimal("1"), Decimal("1"))]) is None


def test_cumulative_depth_sums_levels_within_threshold() -> None:
    mid = Decimal("100")
    bids = [(Decimal("99.95"), Decimal("1")), (Decimal("98"), Decimal("5"))]  # 0.05% and 2% away
    base_total, quote_total = cumulative_depth(bids, mid, Decimal("0.001"), side="bid")
    assert base_total == Decimal("1")
    assert quote_total == Decimal("99.95")


def test_cumulative_depth_excludes_levels_beyond_threshold() -> None:
    mid = Decimal("100")
    asks = [(Decimal("100.50"), Decimal("3"))]  # 0.5% away
    base_total, _ = cumulative_depth(asks, mid, Decimal("0.001"), side="ask")
    assert base_total == Decimal("0")


def test_summarize_spreads_reports_median_p95_min_max() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    summary = summarize_spreads(samples)
    assert summary["median_bps"] == 3.0
    assert summary["min_bps"] == 1.0
    assert summary["max_bps"] == 5.0
    assert summary["samples"] == 5.0


def test_summarize_spreads_empty() -> None:
    assert summarize_spreads([]) == {}


# ---------------------------------------------------------------------------------
# saturation / trades window stats / max-limit discovery
# ---------------------------------------------------------------------------------


def test_is_saturated_true_when_returned_equals_limit() -> None:
    assert is_saturated(500, 500) is True
    assert is_saturated(500, 499) is False
    assert is_saturated(0, 0) is False


def test_trades_window_stats_computes_span_and_recommended_interval() -> None:
    trades = [
        {"id": 10, "time": 1_000_000},
        {"id": 11, "time": 1_010_000},
        {"id": 12, "time": 1_020_000},
    ]
    stats = trades_window_stats(trades, requested_limit=500)
    assert stats.count == 3
    assert stats.min_id == 10
    assert stats.max_id == 12
    assert stats.span_seconds == pytest.approx(20.0)
    assert stats.saturated is False
    # trade_rate = 3 / 20 = 0.15/s; recommended = 500 / (3 * 0.15) ~= 1111s
    assert stats.recommended_poll_interval_seconds == pytest.approx(500 / (3.0 * (3 / 20.0)))


def test_trades_window_stats_flags_saturation() -> None:
    trades = [{"id": i, "time": 1_000_000 + i} for i in range(5)]
    stats = trades_window_stats(trades, requested_limit=5)
    assert stats.saturated is True


def test_trades_window_stats_handles_non_list_body() -> None:
    stats = trades_window_stats(None, requested_limit=500)
    assert stats.count == 0
    assert stats.saturated is False
    assert stats.recommended_poll_interval_seconds is None


@respx.mock
def test_discover_max_trades_limit_stops_at_first_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(dict(httpx.QueryParams(request.url.query))["limit"])
        if limit <= 1000:
            return httpx.Response(200, json=[{"id": i, "time": i} for i in range(min(limit, 800))])
        return httpx.Response(400, json={"msg": "limit too large"})

    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(side_effect=handler)
    client = make_client()

    candidates = [100, 500, 1000, 2000, 5000]
    attempts, max_accepted = discover_max_trades_limit(client, "BTCUSDT", READ_PREFIX, candidates)

    assert max_accepted == 1000
    assert [a.requested for a in attempts] == [100, 500, 1000, 2000]
    assert attempts[-1].ok is False
    assert attempts[0].returned == 100
    assert attempts[2].returned == 800  # server returned fewer than requested
    assert [a.outcome for a in attempts] == ["accepted", "accepted", "accepted", "rejected"]
    client.close()


@respx.mock
def test_discover_max_trades_limit_persistent_429_is_inconclusive_not_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for M19: an exhausted 429 (rate limit) is not evidence that ``limit`` itself
    was rejected -- it must be classified "inconclusive", distinct from an actual 4xx
    limit-too-large rejection, so it never understates ``max_accepted_limit`` or poisons the
    recorder's poll-interval design."""
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(dict(httpx.QueryParams(request.url.query))["limit"])
        if limit == 100:
            return httpx.Response(200, json=[{"id": i, "time": i} for i in range(100)])
        return httpx.Response(429)

    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(side_effect=handler)
    client = make_client(max_retries=1)

    candidates = [100, 500, 1000]
    attempts, max_accepted = discover_max_trades_limit(client, "BTCUSDT", READ_PREFIX, candidates)

    assert max_accepted == 100
    assert [a.requested for a in attempts] == [100, 500]
    assert attempts[0].outcome == "accepted"
    assert attempts[1].outcome == "inconclusive"
    assert attempts[1].status_code == 429
    client.close()


@respx.mock
def test_discover_max_trades_limit_timeout_is_inconclusive_not_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for M19: a timeout (no response at all) must also be "inconclusive", not
    treated as proof the limit was rejected."""
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(dict(httpx.QueryParams(request.url.query))["limit"])
        if limit == 100:
            return httpx.Response(200, json=[{"id": i, "time": i} for i in range(100)])
        raise httpx.ReadTimeout("boom")

    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(side_effect=handler)
    client = make_client(max_retries=0)

    candidates = [100, 500]
    attempts, max_accepted = discover_max_trades_limit(client, "BTCUSDT", READ_PREFIX, candidates)

    assert max_accepted == 100
    assert attempts[1].outcome == "inconclusive"
    assert attempts[1].status_code is None
    client.close()


# ---------------------------------------------------------------------------------
# symbol / filter discovery
# ---------------------------------------------------------------------------------


EXCHANGE_INFO_BODY: dict[str, Any] = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "tabdealSymbol": "BTC_USDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
            ],
        },
        {
            "symbol": "ETHIRT",
            "tabdealSymbol": "ETH_IRT",
            "status": "TRADING",
            "baseAsset": "ETH",
            "filters": [],
        },
    ]
}


def test_find_symbol_entry_matches_symbol_field() -> None:
    entry = find_symbol_entry(EXCHANGE_INFO_BODY, "BTC", "USDT")
    assert entry is not None
    assert entry["symbol"] == "BTCUSDT"


def test_find_symbol_entry_matches_tabdeal_symbol_field_with_underscore() -> None:
    body = {"symbols": [{"tabdealSymbol": "BTC_USDT", "status": "TRADING", "filters": []}]}
    entry = find_symbol_entry(body, "BTC", "USDT")
    assert entry is not None


def test_find_symbol_entry_none_when_missing() -> None:
    assert find_symbol_entry(EXCHANGE_INFO_BODY, "XRP", "USDT") is None


def test_find_base_asset_markets_lists_alternatives() -> None:
    markets = find_base_asset_markets(EXCHANGE_INFO_BODY, "ETH")
    assert len(markets) == 1
    assert markets[0]["symbol"] == "ETHIRT"


def test_build_symbol_filters_success() -> None:
    entry = EXCHANGE_INFO_BODY["symbols"][0]
    filters = build_symbol_filters(entry, "BTCUSDT")
    assert filters is not None
    assert filters.tick_size == Decimal("0.01")
    assert filters.step_size == Decimal("0.00001")
    assert filters.min_qty == Decimal("0.00001")
    assert filters.min_notional == Decimal("10")


def test_build_symbol_filters_none_when_incomplete() -> None:
    entry = EXCHANGE_INFO_BODY["symbols"][1]
    assert build_symbol_filters(entry, "ETHIRT") is None


# ---------------------------------------------------------------------------------
# key permissions / balances
# ---------------------------------------------------------------------------------


def test_unsafe_key_permissions_flags_trade_or_withdraw() -> None:
    unsafe, found = unsafe_key_permissions({"canTrade": True, "canWithdraw": False})
    assert unsafe is True
    assert found == {"canTrade": True, "canWithdraw": False}


def test_unsafe_key_permissions_safe_when_read_only() -> None:
    unsafe, _ = unsafe_key_permissions({"canTrade": False, "canWithdraw": False})
    assert unsafe is False


def test_unsafe_key_permissions_checks_permissions_list() -> None:
    unsafe, _ = unsafe_key_permissions({"permissions": ["SPOT", "WITHDRAW"]})
    assert unsafe is True


# ---------------------------------------------------------------------------------
# both path prefixes (probe-level)
# ---------------------------------------------------------------------------------


@respx.mock
def test_probe_both_prefixes_prefers_read_when_both_answer() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    client = make_client()

    summary, chosen = probe_both_prefixes(client, lambda p: client.ping(prefix=p), "ping")

    assert summary["answering_prefix"] == "read"
    assert chosen.ok is True
    client.close()


@respx.mock
def test_probe_both_prefixes_falls_back_to_write() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    client = make_client()

    summary, chosen = probe_both_prefixes(client, lambda p: client.ping(prefix=p), "ping")

    assert summary["answering_prefix"] == "write"
    assert chosen.ok is True
    client.close()


@respx.mock
def test_probe_both_prefixes_none_when_neither_answers() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(403))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(403))
    client = make_client()

    summary, chosen = probe_both_prefixes(client, lambda p: client.ping(prefix=p), "ping")

    assert summary["answering_prefix"] is None
    assert chosen.ok is False
    client.close()


# ---------------------------------------------------------------------------------
# unreachable / 403 message
# ---------------------------------------------------------------------------------


def test_describe_unreachable_failure_for_403() -> None:
    result = ProbeResult(
        method="GET", path="/ping", url="https://x/ping", status_code=403, latency_ms=1.0, ok=False
    )
    message = describe_unreachable_failure(result)
    assert "403" in message
    assert "geo-block" in message or "allow-list" in message


def test_describe_unreachable_failure_for_timeout() -> None:
    result = ProbeResult(
        method="GET",
        path="/ping",
        url="https://x/ping",
        status_code=None,
        latency_ms=1.0,
        ok=False,
        error="timeout: ReadTimeout",
    )
    message = describe_unreachable_failure(result)
    assert "timed out" in message


# ---------------------------------------------------------------------------------
# end-to-end: missing credentials and the 403/unreachable exit path via main()
# ---------------------------------------------------------------------------------


def _args_for(tmp_path: Path, **overrides: Any) -> list[str]:
    out = overrides.pop("out", tmp_path / "report.json")
    argv = [
        "--config", "default",
        "--config-dir", str(REPO_ROOT / "config"),
        "--out", str(out),
        "--samples", "1",
        "--interval", "0",
        "--symbols", "BTCUSDT",
        "--trades-limit-candidates", "100",
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return argv


@respx.mock
def test_main_exits_nonzero_and_prints_one_line_when_unreachable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MINOR-16: Secrets() reads env_file=".env" relative to the process cwd. On a provisioned
    # server (or a dev machine) a real .env with real credentials could sit in the repo root --
    # chdir into an empty tmp_path so this test's behaviour never depends on what, if anything,
    # is sitting in the real cwd's .env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
    monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(403))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(403))

    exit_code = main(_args_for(tmp_path))

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Tabdeal unreachable" in captured.err
    assert "403" in captured.err
    # no traceback noise
    assert "Traceback" not in captured.err
    assert not (tmp_path / "report.json").exists()


@respx.mock
def test_main_skips_account_when_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # MINOR-16
    monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
    monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
    _mock_full_happy_path()

    exit_code = main(_args_for(tmp_path))

    assert exit_code == 0
    import json

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "skipped" in report["account"]
    assert report["key_permissions_unsafe"] is False


@respx.mock
def test_main_warns_on_unsafe_key_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # MINOR-16
    monkeypatch.setenv("TBOT_TABDEAL_API_KEY", "fake-key")
    monkeypatch.setenv("TBOT_TABDEAL_API_SECRET", "fake-secret")
    _mock_full_happy_path()
    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(
        return_value=httpx.Response(200, json={"balances": [], "canTrade": True, "canWithdraw": False})
    )

    exit_code = main(_args_for(tmp_path))

    # M5: a key with trade/withdraw permission must exit 2, not 0 -- exit 0 here used to let a
    # CI check or an operator's shell script silently treat an unsafe key as a pass.
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    import json

    report_text = (tmp_path / "report.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["key_permissions_unsafe"] is True
    # MINOR-18: the API key itself must never reach the report, whatever else it contains.
    assert "fake-key" not in report_text


def _mock_full_happy_path() -> None:
    """Mock every endpoint the happy path touches, on the read prefix."""
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(404))
    server_time_body = {"serverTime": 1_700_000_000_000}
    respx.get(f"{BASE_URL}{READ_PREFIX}/time").mock(return_value=httpx.Response(200, json=server_time_body))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/time").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{READ_PREFIX}/exchangeInfo").mock(
        return_value=httpx.Response(200, json=EXCHANGE_INFO_BODY)
    )
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/exchangeInfo").mock(return_value=httpx.Response(404))
    depth_body = {
        "bids": [["100.00", "1.0"], ["99.90", "2.0"]],
        "asks": [["100.10", "1.5"], ["100.20", "1.0"]],
    }
    respx.get(f"{BASE_URL}{READ_PREFIX}/depth").mock(return_value=httpx.Response(200, json=depth_body))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/depth").mock(return_value=httpx.Response(404))
    trades_body = [
        {"id": i, "price": "100.0", "qty": "0.1", "time": 1_700_000_000_000 + i * 1000} for i in range(50)
    ]
    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(return_value=httpx.Response(200, json=trades_body))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/trades").mock(return_value=httpx.Response(404))


@respx.mock
def test_g0_spread_plan_ok_is_false_when_most_depth_samples_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for MINOR-2 (second fix round): g0_spread_plan_ok must be derived from
    *successful* depth samples, not attempted ones -- and the printed human summary must never
    claim a sampling plan is satisfied while also printing a much smaller sample count.

    30 attempts are requested; only the first 5 depth calls succeed (the rest return HTTP 500),
    so there must be exactly 5 successful samples, g0_spread_plan_ok must be False, and the human
    summary line must show samples=5, not samples=30.
    """
    monkeypatch.chdir(tmp_path)  # MINOR-16
    monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
    monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
    # Avoid real sleeping: the probe's own inter-sample sleep (--interval 0 makes this a no-op
    # anyway) and the client's exponential-backoff retry sleep for the ~25 failing 500 responses.
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.tabdeal_probe.time.sleep", lambda _seconds: None)

    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{READ_PREFIX}/time").mock(
        return_value=httpx.Response(200, json={"serverTime": 1_700_000_000_000})
    )
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/time").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{READ_PREFIX}/exchangeInfo").mock(
        return_value=httpx.Response(200, json=EXCHANGE_INFO_BODY)
    )
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/exchangeInfo").mock(return_value=httpx.Response(404))
    trades_body = [
        {"id": i, "price": "100.0", "qty": "0.1", "time": 1_700_000_000_000 + i * 1000} for i in range(50)
    ]
    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(return_value=httpx.Response(200, json=trades_body))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/trades").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/depth").mock(return_value=httpx.Response(404))

    depth_body = {
        "bids": [["100.00", "1.0"], ["99.90", "2.0"]],
        "asks": [["100.10", "1.5"], ["100.20", "1.0"]],
    }
    call_count = {"n": 0}

    def depth_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] <= 5:
            return httpx.Response(200, json=depth_body)
        return httpx.Response(500)

    respx.get(f"{BASE_URL}{READ_PREFIX}/depth").mock(side_effect=depth_handler)

    argv = _args_for(tmp_path, samples=30, interval=0)
    exit_code = main(argv)

    assert exit_code == 0
    import json

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    depth_report = report["depth"]["BTCUSDT"]

    # Exactly 5 successful samples, not 30 attempted ones.
    assert depth_report["spread_summary"]["samples"] == 5.0
    assert len(depth_report["samples"]) == 5
    assert depth_report["g0_spread_plan_ok"] is False

    captured = capsys.readouterr()
    # The human summary must report the same (small) sample count it bases ok=False on -- never
    # "ok=True" next to a samples= number that is obviously short of 30.
    assert "ok=False" in captured.out
    assert "samples=5" in captured.out
    assert "samples=30" not in captured.out


@respx.mock
def test_main_end_to_end_happy_path_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # MINOR-16
    monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
    monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
    _mock_full_happy_path()

    exit_code = main(_args_for(tmp_path))

    assert exit_code == 0
    import json

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["ping"]["answering_prefix"] == "read"
    assert report["symbols"]["BTCUSDT"]["found"] is True
    assert report["symbols"]["BTCUSDT"]["filters"]["tick_size"] == "0.01"
    assert report["trades"]["window_stats"]["count"] == 50
    assert "clock_skew_ms" in report["time"]

    # M3: G0 needs the sampling span to be explicit and distinguishable from a short run --
    # with --samples 1 the span is necessarily 0 and the plan is necessarily not satisfied, but
    # the fields themselves must always be present.
    depth_report = report["depth"]["BTCUSDT"]
    assert depth_report["first_sample_ts"] is not None
    assert depth_report["last_sample_ts"] is not None
    assert depth_report["sampling_span_seconds"] == pytest.approx(0.0)
    assert depth_report["g0_spread_plan_ok"] is False

    # M4: bid and ask cumulative depth must be reported separately, never summed together.
    sample = depth_report["samples"][0]
    one_threshold_depth = next(iter(sample["depth"].values()))
    assert set(one_threshold_depth) == {"bid", "ask"}
    assert set(one_threshold_depth["bid"]) == {"base_qty", "quote_notional"}
    assert set(one_threshold_depth["ask"]) == {"base_qty", "quote_notional"}


# ---------------------------------------------------------------------------------
# MAJOR M-C (third fix round): this is the one process that actually holds Tabdeal
# credentials and signs real requests with them. Before this fix it never called
# `configure_logging` at all (no redacting pipeline, no scrubbing `sys.excepthook`) and
# never registered its credentials with the logging module's value registry, so a bare
# API key/secret leaking into free text (no recognizable prefix, no sensitive key name)
# would have been emitted completely unredacted.
# ---------------------------------------------------------------------------------


@pytest.fixture
def _logging_state_guard() -> Iterator[None]:
    """Save/restore global logging state around a test that calls `main()` (which, after
    this fix, calls `configure_logging`/`register_secrets_for_logging` for real).

    Deliberately NOT autouse: every other test in this module calls `main()` too (most with
    no credentials at all), and letting configure_logging's root-handler/excepthook/registry
    side effects leak across the *whole* module unconditionally is a bigger blast radius than
    this specific test needs. Opting in only here keeps that blast radius to the one test
    that actually asserts something about the logging pipeline.
    """
    original_excepthook = sys.excepthook
    secret_values_before = tbot_logging._SECRET_VALUES
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    sys.excepthook = original_excepthook
    tbot_logging._SECRET_VALUES = secret_values_before


@respx.mock
def test_main_registers_tabdeal_secrets_and_configures_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _logging_state_guard: None,
) -> None:
    """Reviewer's exact M-C probe: load `Secrets()` from a monkeypatched environment, run the
    probe's real startup wiring via `main()`, and prove two things production needed and
    didn't have:

    1. the probe installs `configure_logging` at all (nothing did, before this fix) -- checked
       via `structlog.is_configured()`;
    2. the raw Tabdeal credentials are registered with the logging value registry as a side
       effect of running `main()` -- checked by logging the raw secret value through the now-
       configured pipeline afterwards and asserting it comes out redacted, which is only
       possible if `register_secrets_for_logging(secrets)` actually ran during `main()`.
    """
    monkeypatch.chdir(tmp_path)  # MINOR-16: never depend on a real .env in the repo root
    api_key = "tabdeal-live-api-key-0123456789"
    api_secret = "tabdeal-live-api-secret-abcdefghij"
    monkeypatch.setenv("TBOT_TABDEAL_API_KEY", api_key)
    monkeypatch.setenv("TBOT_TABDEAL_API_SECRET", api_secret)
    _mock_full_happy_path()
    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(
        return_value=httpx.Response(200, json={"balances": [], "canTrade": False, "canWithdraw": False})
    )

    exit_code = main(_args_for(tmp_path))
    assert exit_code == 0

    # (1) configure_logging actually ran.
    assert structlog.is_configured()

    # (2) the raw credentials are in the registry: log them through the real pipeline exactly
    # as a careless call site elsewhere in the process might, and confirm they come out
    # redacted -- this can only pass if `main()` itself called `register_secrets_for_logging`,
    # since nothing in this test calls it directly.
    capsys.readouterr()  # discard the report/summary output `main()` already produced
    log = structlog.get_logger("test")
    log.info("probe.credential_leak_check", note=f"leaked key={api_key} secret={api_secret}")

    rendered = capsys.readouterr().out
    assert api_key not in rendered
    assert api_secret not in rendered
    assert REDACTED in rendered
