"""Tests for scripts/lbank_probe.py -- offline only (httpx.MockTransport, virtual time).

No test here touches the network: tests/conftest.py blocks real sockets, and every HTTP exchange goes
through ``fixture_transport`` or a local fake transport. The fixture "LBank" is not a recording of
the real exchange; it only exercises the code paths.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import lbank_probe as lp

D = Decimal
T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
BTC_PRICE_STEP = D("0.1")


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


def result(
    status: int | None,
    *,
    payload_ok: bool = True,
    error: str | None = None,
    endpoint: str = "depth",
) -> lp.HttpResult:
    return lp.HttpResult(
        endpoint=endpoint,
        url="https://example.invalid/x",
        params={},
        sent_at=T0,
        received_at=T0,
        status=status,
        latency_ms=1.0,
        nbytes=0,
        error=error,
        body=None,
        payload_ok=payload_ok,
        payload_error=None if payload_ok else "boom",
    )


def book_for_spread(spread_pct: float) -> dict[str, Any]:
    bid = D(1000)
    ask = bid * (1 + D(str(spread_pct)) / 100)
    book = lp.analyze_book([(bid, D(1))], [(ask, D(1))])
    book["requested_size"] = 20
    return book


def make_sample(
    index: int,
    *,
    minutes: float,
    delta_ms: float = 100.0,
    spread_pct: float = 0.05,
    accuracy_ok: bool = True,
    request_ok: bool = True,
) -> dict[str, Any]:
    return {
        "kind": "sample",
        "index": index,
        "utc_start": (T0 + timedelta(minutes=minutes)).isoformat(),
        "requests": [
            {
                "endpoint": "depth",
                "status": 200 if request_ok else 429,
                "ok": request_ok,
                "error": None,
                "payload_error": None,
                "latency_ms": 10.0,
                "nbytes": 1,
            }
        ],
        "server_time": {"ok": True, "delta_ms": delta_ms, "rtt_ms": 100.0, "half_rtt_ms": 50.0},
        "accuracy": {"ok": accuracy_ok, "fields": {}, "row_keys": []},
        "book": book_for_spread(spread_pct),
    }


def full_plan(**kwargs: Any) -> list[dict[str, Any]]:
    """32 samples 12 min apart (6.2 h span): satisfies the G0 sampling plan."""
    return [make_sample(i, minutes=i * 12, **kwargs) for i in range(32)]


def run_fixture_sampling(
    tmp_path: Path, *, samples: int = 3, interval_s: float = 60.0, **transport_kw: Any
) -> tuple[int, dict[str, Any], lp.OutputPaths, list[httpx.Request]]:
    env = lp.VirtualEnv()
    seen: list[httpx.Request] = []
    prov = lp.collect_provenance(env, "sampling", ["--test"], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "t", "stamp")
    with lp.Fetcher(env, transport=lp.fixture_transport(env, seen=seen, **transport_kw)) as fetcher:
        code, summary = lp.run_sampling(
            fetcher, env, samples=samples, interval_s=interval_s, paths=paths, provenance=prov
        )
    return code, summary, paths, seen


# ---------------------------------------------------------------------------------
# spread, depth-within-x%, max-fill walking
# ---------------------------------------------------------------------------------


def test_spread_pct_of_known_book() -> None:
    bids = [(D("100.0"), D(1)), (D("99.0"), D(2))]
    asks = [(D("100.2"), D(1)), (D("101.0"), D(2))]
    # 0.2 / mid(100.1) * 100
    assert lp.spread_pct_of(bids, asks) == pytest.approx(0.2 / 100.1 * 100)


def test_spread_pct_of_degenerate_books() -> None:
    assert lp.spread_pct_of([], [(D(1), D(1))]) is None
    assert lp.spread_pct_of([(D(1), D(1))], []) is None
    assert lp.spread_pct_of([(D(101), D(1))], [(D(100), D(1))]) is None  # crossed


def test_depth_within_band_counts_only_levels_inside() -> None:
    mid = D(1000)
    bids = [(D("999.5"), D(2)), (D("995"), D(3)), (D("980"), D(10))]
    out = lp.depth_within(bids, mid, D("0.001"), "bid")  # 0.1 % -> down to 999.0
    assert D(out["base_qty"]) == D(2)
    assert D(out["quote"]) == D("1999.0")
    out = lp.depth_within(bids, mid, D("0.01"), "bid")  # 1 % -> down to 990
    assert D(out["base_qty"]) == D(5)


def test_depth_within_flags_truncated_when_book_ends_inside_band() -> None:
    mid = D(1000)
    shallow = [(D("999.9"), D(1)), (D("999.8"), D(1))]
    assert lp.depth_within(shallow, mid, D("0.01"), "bid")["truncated"] is True
    deep = [*shallow, (D("900"), D(1))]
    assert lp.depth_within(deep, mid, D("0.01"), "bid")["truncated"] is False


def test_max_fill_buy_walks_book_to_exact_vwap_limit() -> None:
    asks = [(D(100), D(1)), (D(101), D(1)), (D(102), D(10))]
    fill = lp.max_fill_within_slippage(asks, "buy", D(100), D("0.01"))  # limit 101
    # level 100 and 101 whole (quote 201), then 1 BTC at 102 brings the VWAP to exactly 101
    assert fill.base_qty == D(3)
    assert fill.quote == D(303)
    assert fill.vwap == D(101)
    assert fill.truncated is False


def test_max_fill_sell_mirrors_buy() -> None:
    bids = [(D(100), D(1)), (D(99), D(1)), (D(98), D(10))]
    fill = lp.max_fill_within_slippage(bids, "sell", D(100), D("0.01"))  # limit 99
    assert fill.base_qty == D(3)
    assert fill.quote == D(297)
    assert fill.vwap == D(99)
    assert fill.truncated is False


def test_max_fill_is_truncated_when_book_ends_before_limit() -> None:
    asks = [(D(100), D(1)), (D("100.5"), D(2))]
    fill = lp.max_fill_within_slippage(asks, "buy", D(100), D("0.05"))
    assert fill.base_qty == D(3)
    assert fill.truncated is True


def test_max_fill_zero_when_first_level_is_beyond_limit() -> None:
    fill = lp.max_fill_within_slippage([(D(105), D(5))], "buy", D(100), D("0.01"))
    assert fill.base_qty == D(0)
    assert fill.vwap is None
    assert fill.truncated is False


def test_max_fill_continues_past_a_small_expensive_level() -> None:
    # A beyond-limit level that is smaller than the headroom is taken whole and the walk goes on.
    asks = [(D(100), D(10)), (D("101.5"), D("0.1")), (D(102), D(100))]
    fill = lp.max_fill_within_slippage(asks, "buy", D(100), D("0.01"))  # limit 101
    assert fill.vwap is not None
    assert fill.vwap <= D(101)
    assert fill.base_qty > D("10.1")  # more than the first two levels together
    assert abs(fill.vwap - D(101)) < D("1e-20")  # ended on the third level, exactly at the limit


def test_max_fill_ignores_unordered_input_and_zero_qty() -> None:
    asks = [(D(102), D(10)), (D(100), D(1)), (D(101), D(1)), (D("100.1"), D(0))]
    fill = lp.max_fill_within_slippage(asks, "buy", D(100), D("0.01"))
    assert fill.base_qty == D(3)


level_strategy = st.lists(
    st.tuples(st.integers(min_value=0, max_value=600), st.integers(min_value=1, max_value=50)),
    min_size=1,
    max_size=25,
)


@given(raw=level_strategy, slip_bps=st.integers(min_value=1, max_value=300), buy=st.booleans())
def test_max_fill_property_vwap_never_exceeds_limit(
    raw: list[tuple[int, int]], slip_bps: int, buy: bool
) -> None:
    reference = D(10_000)
    sign = 1 if buy else -1
    levels = [(reference + sign * D(off) / 10, D(q)) for off, q in raw]
    slip = D(slip_bps) / 10_000
    side: lp.Side = "buy" if buy else "sell"
    fill = lp.max_fill_within_slippage(levels, side, reference, slip)
    limit = reference * (1 + sign * slip)
    assert fill.base_qty <= sum(q for _, q in levels)
    if fill.vwap is not None:
        assert (fill.vwap - limit) * sign <= D("1e-18")
        if not fill.truncated:
            # stopped inside the book: the walk consumed exactly the allowed slippage
            assert abs(fill.vwap - limit) <= D("1e-18")


# ---------------------------------------------------------------------------------
# clock helpers and payload parsing
# ---------------------------------------------------------------------------------


def test_clock_delta_uses_midpoint_of_request() -> None:
    sent, received = T0, T0 + timedelta(seconds=2)
    midpoint_ms = (T0 + timedelta(seconds=1)).timestamp() * 1000
    assert lp.clock_delta_ms(int(midpoint_ms) + 150, sent, received) == pytest.approx(150.0)
    assert lp.clock_delta_ms(int(midpoint_ms) - 400, sent, received) == pytest.approx(-400.0)


def test_parse_server_time_accepts_seconds_and_milliseconds() -> None:
    assert lp.parse_server_time_ms({"data": 1_790_000_000}) == 1_790_000_000_000
    assert lp.parse_server_time_ms({"data": 1_790_000_000_123}) == 1_790_000_000_123
    assert lp.parse_server_time_ms({"data": "x"}) is None
    assert lp.parse_server_time_ms({}) is None


def test_payload_status_detects_errors_behind_http_200() -> None:
    assert lp.payload_status({"result": "true", "data": []}) == (True, None)
    ok, why = lp.payload_status({"result": "false", "error_code": 10008, "msg": "bad"})
    assert ok is False and why is not None and "10008" in why
    assert lp.payload_status({"result": True, "error_code": 0})[0] is True
    assert lp.payload_status({"success": False, "msg": "x"})[0] is False
    assert lp.payload_status("nope")[0] is False


def test_percentile_interpolates_and_handles_empty() -> None:
    assert lp.percentile([], 0.5) is None
    assert lp.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert lp.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)
    assert lp.percentile([7.0], 0.95) == 7.0


def test_kline_extent_counts_non_contiguous_steps() -> None:
    rows = [(1000,), (1060,), (1120,), (1300,)]
    ext = lp.kline_extent(rows, 60)
    assert ext["returned"] == 4
    assert ext["non_contiguous_steps"] == 1
    assert ext["ascending"] is True


def test_parse_kline_rows_normalises_milliseconds() -> None:
    body = {"data": [[1_790_000_000_000, "1"], [1_790_000_060, "1"]]}
    assert lp.parse_kline_rows(body) == [(1_790_000_000,), (1_790_000_060,)]


# ---------------------------------------------------------------------------------
# accuracy row (D-073 condition 3)
# ---------------------------------------------------------------------------------

GOOD_ROW = {
    "symbol": "btc_usdt",
    "priceAccuracy": "2",
    "quantityAccuracy": "6",
    "minTranQua": "0.0001",
    "minOrderAmount": "5",
}


def test_accuracy_row_ok_when_all_fields_readable() -> None:
    out = lp.evaluate_accuracy_row(GOOD_ROW)
    assert out["ok"] is True
    assert out["fields"]["min_order_value"]["key"] == "minOrderAmount"


@pytest.mark.parametrize("missing", ["priceAccuracy", "quantityAccuracy", "minTranQua", "minOrderAmount"])
def test_accuracy_row_fails_when_a_field_is_missing(missing: str) -> None:
    row = {k: v for k, v in GOOD_ROW.items() if k != missing}
    out = lp.evaluate_accuracy_row(row)
    assert out["ok"] is False


def test_accuracy_row_rejects_unreadable_values() -> None:
    assert lp.evaluate_accuracy_row({**GOOD_ROW, "priceAccuracy": "2.5"})["ok"] is False
    assert lp.evaluate_accuracy_row({**GOOD_ROW, "minTranQua": "abc"})["ok"] is False
    assert lp.evaluate_accuracy_row({**GOOD_ROW, "minOrderAmount": "0"})["ok"] is False


def test_find_accuracy_row_picks_btc_usdt_case_insensitively() -> None:
    body = {"data": [{"symbol": "eth_usdt"}, {"symbol": "BTC_USDT", "priceAccuracy": "2"}]}
    row = lp.find_accuracy_row(body, "btc_usdt")
    assert row is not None and row["priceAccuracy"] == "2"
    assert lp.find_accuracy_row({"data": [{"symbol": "eth_usdt"}]}, "btc_usdt") is None


# ---------------------------------------------------------------------------------
# G0 pass/fail evaluator
# ---------------------------------------------------------------------------------


def test_g0_passes_on_a_clean_full_plan() -> None:
    out = lp.evaluate_g0(full_plan())
    assert out["verdict"] == "PASS"
    assert out["failed_checks"] == []
    assert out["plan"]["plan_ok"] is True


def test_g0_fails_when_any_request_is_not_200() -> None:
    samples = full_plan()
    samples[5] = make_sample(5, minutes=60, request_ok=False)
    out = lp.evaluate_g0(samples)
    assert out["verdict"] == "FAIL"
    assert out["failed_checks"] == ["all_endpoints_200"]
    assert out["checks"]["all_endpoints_200"]["failures"][0]["status"] == 429


def test_g0_fails_on_clock_skew_but_uses_the_median_not_the_worst_case() -> None:
    assert lp.evaluate_g0(full_plan(delta_ms=1500.0))["failed_checks"] == ["clock_offset"]
    samples = full_plan(delta_ms=200.0)
    samples[3]["server_time"]["delta_ms"] = 9000.0  # one outlier must not fail the median test
    assert lp.evaluate_g0(samples)["verdict"] == "PASS"
    samples = full_plan(delta_ms=-1200.0)  # negative skew counts as well
    assert lp.evaluate_g0(samples)["failed_checks"] == ["clock_offset"]


def test_g0_fails_when_accuracy_is_missing_in_any_sample() -> None:
    samples = full_plan()
    samples[10]["accuracy"] = {"ok": False, "reason": "btc_usdt not listed"}
    assert lp.evaluate_g0(samples)["failed_checks"] == ["accuracy_btc_usdt"]


def test_g0_fails_on_wide_median_spread() -> None:
    assert lp.evaluate_g0(full_plan(spread_pct=0.25))["failed_checks"] == ["median_spread"]
    assert lp.evaluate_g0(full_plan(spread_pct=0.19))["verdict"] == "PASS"


def test_g0_is_incomplete_when_the_sampling_plan_is_not_met() -> None:
    few = [make_sample(i, minutes=i * 12) for i in range(10)]
    out = lp.evaluate_g0(few)
    assert out["verdict"] == "INCOMPLETE"
    assert out["plan"]["plan_ok"] is False
    short_span = [make_sample(i, minutes=i * 2) for i in range(32)]
    assert lp.evaluate_g0(short_span)["verdict"] == "INCOMPLETE"


def test_g0_with_no_samples_is_not_a_pass() -> None:
    out = lp.evaluate_g0([])
    assert out["verdict"] == "FAIL"


def test_g0_fail_beats_incomplete() -> None:
    few = [make_sample(i, minutes=i * 12, spread_pct=0.5) for i in range(5)]
    assert lp.evaluate_g0(few)["verdict"] == "FAIL"


# ---------------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------------


def test_fetcher_maps_timeout_to_error_result_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    env = lp.VirtualEnv()
    with lp.Fetcher(env, transport=httpx.MockTransport(handler)) as fetcher:
        res = fetcher.get("timestamp", lp.ENDPOINTS["timestamp"])
    assert res.status is None
    assert res.error == "timeout"
    assert res.ok is False


def test_fetcher_marks_error_payload_on_http_200_as_not_ok() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "false", "error_code": 10008, "msg": "invalid"})

    with lp.Fetcher(lp.VirtualEnv(), transport=httpx.MockTransport(handler)) as fetcher:
        res = fetcher.get("depth", lp.ENDPOINTS["depth"], {"symbol": "btc_usdt", "size": 5})
    assert res.http_ok is True
    assert res.payload_ok is False
    assert res.ok is False


def test_fetcher_handles_non_json_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway</html>")

    with lp.Fetcher(lp.VirtualEnv(), transport=httpx.MockTransport(handler)) as fetcher:
        res = fetcher.get("depth", lp.ENDPOINTS["depth"])
    assert res.ok is False
    assert res.error == "invalid_json"


def test_fetcher_never_exceeds_five_requests_per_second() -> None:
    """Pacing is measured via ``env.monotonic()`` (what ``Fetcher._pace`` itself paces against),
    not via ``HttpResult.sent_at.timestamp()``. The latter is a wall-clock float64 anchored at the
    VirtualEnv epoch (~1.79e9 s for 2026 dates): a float64 has 52 mantissa bits, so at that
    magnitude (2**30 <= 1.79e9 < 2**31) one ULP is 2**(30-52) = 2**-22 ~= 2.384e-7 s, and two
    timestamps differenced can disagree from the true 0.2 s gap by up to ~2 ULP ~= 4.8e-7 s -- which
    is exactly the ~1.9e-7 s shortfall that was previously (wrongly) papered over with a 1e-4 s
    tolerance. ``env.monotonic()`` has no such issue: VirtualEnv starts it at 0.0 and only ever adds
    exact sleep durations, so across 10 steps its magnitude never exceeds ~2.0, where one ULP is
    2**(1-52) ~= 4.4e-16 s -- nine orders of magnitude tighter than the 0.2 s gap being asserted.
    """
    env = lp.VirtualEnv()
    seen: list[httpx.Request] = []
    transport = lp.fixture_transport(env, latency_s=0.0, seen=seen)
    marks: list[float] = []
    with lp.Fetcher(env, transport=transport, min_gap_s=0.0) as fetcher:  # asks for "no pacing"
        for _ in range(10):
            fetcher.get("timestamp", lp.ENDPOINTS["timestamp"])
            marks.append(env.monotonic())
    gaps = [b - a for a, b in pairwise(marks)]
    assert min(gaps) >= 0.2 - 1e-9


def test_fetcher_sends_identifying_user_agent_and_no_credentials() -> None:
    env = lp.VirtualEnv()
    seen: list[httpx.Request] = []
    with lp.Fetcher(env, transport=lp.fixture_transport(env, seen=seen)) as fetcher:
        fetcher.get("timestamp", lp.ENDPOINTS["timestamp"])
    request = seen[0]
    assert request.headers["user-agent"] == lp.USER_AGENT
    assert "lbank-g0-probe" in lp.USER_AGENT
    lowered = {k.lower() for k in request.headers}
    assert not lowered & {"authorization", "x-mbx-apikey", "api-key", "signature", "cookie"}
    assert not {"sign", "signature", "api_key", "apikey"} & {k.lower() for k in request.url.params}


def test_request_whitelist_is_get_only_and_public() -> None:
    allowed_paths = {spec.path for spec in lp.ENDPOINTS.values()}
    assert allowed_paths == {
        "/v2/timestamp.do",
        "/v2/accuracy.do",
        "/v2/depth.do",
        "/v2/supplement/trades.do",
        "/v2/kline.do",
        "/v2/ticker/24hr.do",
        "/cfd/openApi/v1/pub/marketData",
    }


# ---------------------------------------------------------------------------------
# rate-limit stop logic
# ---------------------------------------------------------------------------------


def test_stop_tracker_stops_on_429_immediately() -> None:
    tracker = lp.StopTracker()
    assert tracker.observe(result(200)) is None
    assert tracker.observe(result(429)) == "HTTP 429"


@pytest.mark.parametrize("status", [403, 418])
def test_stop_tracker_stops_on_ban_statuses(status: int) -> None:
    assert lp.StopTracker().observe(result(status)) is not None


def test_stop_tracker_needs_three_consecutive_5xx() -> None:
    tracker = lp.StopTracker()
    assert tracker.observe(result(502)) is None
    assert tracker.observe(result(503)) is None
    assert tracker.observe(result(200)) is None  # resets the burst
    assert tracker.observe(result(500)) is None
    assert tracker.observe(result(500)) is None
    assert tracker.observe(result(504)) is not None


def test_stop_tracker_stops_on_payload_error_behind_200() -> None:
    reason = lp.StopTracker().observe(result(200, payload_ok=False))
    assert reason is not None and "payload" in reason


def test_stop_tracker_stops_on_repeated_transport_errors() -> None:
    tracker = lp.StopTracker()
    assert tracker.observe(result(None, error="timeout", payload_ok=False)) is None
    assert tracker.observe(result(None, error="timeout", payload_ok=False)) is None
    assert tracker.observe(result(None, error="timeout", payload_ok=False)) is not None


def test_stop_tracker_stays_stopped() -> None:
    tracker = lp.StopTracker()
    tracker.observe(result(429))
    assert tracker.observe(result(200)) == "HTTP 429"


def run_fixture_ratelimit(
    tmp_path: Path, *, throttle_after: int | None, steps: list[float], step_seconds: float = 5.0
) -> tuple[int, dict[str, Any], list[httpx.Request], lp.OutputPaths]:
    env = lp.VirtualEnv()
    seen: list[httpx.Request] = []
    prov = lp.collect_provenance(env, "ratelimit", ["--test"], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "rl", "stamp", summary_suffix=".json")
    transport = lp.fixture_transport(env, throttle_after=throttle_after, seen=seen, latency_s=0.05)
    with lp.Fetcher(env, transport=transport, min_gap_s=lp.MIN_REQUEST_GAP_S) as fetcher:
        code, summary = lp.run_ratelimit(
            fetcher,
            env,
            endpoint="depth",
            steps=steps,
            step_seconds=step_seconds,
            pause_seconds=2.0,
            paths=paths,
            provenance=prov,
        )
    return code, summary, seen, paths


def test_ratelimit_run_stops_at_first_429_and_never_retries(tmp_path: Path) -> None:
    code, summary, seen, _ = run_fixture_ratelimit(tmp_path, throttle_after=9, steps=[1.0, 2.0, 3.0])
    assert code == 4
    assert summary["stopped_early"] is True
    assert summary["last_safe_rate_rps"] == 1.0
    assert summary["max_rate_tested_rps"] == 2.0  # step 3 never started
    assert len([r for r in seen if r.url.path == "/v2/depth.do"]) == 9  # the 429 was the last request
    failure = summary["failure"]
    assert failure["status"] == 429
    assert failure["headers"]["retry-after"] == "10"
    assert "too many requests" in failure["body"]
    assert failure["retry_after"] == "10"


def test_ratelimit_run_without_incident_returns_zero_and_never_exceeds_five_rps(tmp_path: Path) -> None:
    code, summary, seen, _ = run_fixture_ratelimit(tmp_path, throttle_after=None, steps=[1.0, 5.0])
    assert code == 0
    assert summary["stopped_early"] is False
    assert summary["last_safe_rate_rps"] == 5.0
    assert summary["steps"][1]["achieved_rps"] <= 5.0 + 1e-9
    assert len(seen) == sum(s["requests_sent"] for s in summary["steps"])


def test_ratelimit_outputs_record_failing_response_and_provenance(tmp_path: Path) -> None:
    _, _, _, paths = run_fixture_ratelimit(tmp_path, throttle_after=4, steps=[1.0])
    md = paths.markdown.read_text(encoding="utf-8")
    assert "Failing response" in md and "retry-after" in md
    assert "measured from" in md


# ---------------------------------------------------------------------------------
# sampling run end to end on the fixture
# ---------------------------------------------------------------------------------


def test_sampling_run_records_every_endpoint_and_metric(tmp_path: Path) -> None:
    code, summary, paths, seen = run_fixture_sampling(tmp_path, samples=2)
    assert code == 3  # two samples cannot satisfy the 30-sample / 6 h plan
    assert summary["g0"]["verdict"] == "INCOMPLETE"
    records = lp.read_jsonl(paths.jsonl)
    samples = [r for r in records if r["kind"] == "sample"]
    assert len(samples) == 2
    s0 = samples[0]
    assert {r["endpoint"] for r in s0["requests"]} == {
        "timestamp",
        "accuracy",
        "depth",
        "trades",
        "kline_1d",
        "kline_4h",
        "kline_1h",
        "kline_1m",
        "ticker24hr",
        "perp_marketdata",
    }
    assert all(r["status"] == 200 and "latency_ms" in r for r in s0["requests"])
    assert s0["server_time"]["delta_ms"] == pytest.approx(120.0 + 75.0, abs=1.0)  # +120 skew, half latency
    assert set(s0["book"]["depth"]) == {"0.001", "0.005", "0.01"}
    assert set(s0["book"]["max_fill"]) == {"0.001", "0.005"}
    assert s0["accuracy"]["ok"] is True
    assert all(isinstance(s0["klines"][tf]["returned"], int) for tf in ("1d", "4h", "1h", "1m"))
    # request logging: exactly one request in flight is guaranteed by the serial design; sanity-check volume
    assert len(seen) > 2 * 10


def test_sampling_run_uses_discovered_depth_size_and_probes_kline_history(tmp_path: Path) -> None:
    _, summary, paths, _ = run_fixture_sampling(tmp_path, samples=1, max_depth_size=60)
    assert summary["depth_size_discovery"]["chosen_size"] == 60
    sizes = [(a["requested"], a["accepted"]) for a in summary["depth_size_discovery"]["attempts"]]
    assert sizes == [(200, False), (100, False), (60, True)]
    history = summary["kline_history"]["timeframes"]
    assert set(history) == {"1d", "4h", "1h", "1m"}
    assert history["1d"]["max_bars_per_request_observed"] == 2000
    assert history["1d"]["history_start_utc"].startswith("2018-06-01")
    assert history["1d"]["next_page"]["contiguous_with_previous_page"] is True
    md = paths.markdown.read_text(encoding="utf-8")
    assert "chosen = 60" in md


def test_full_plan_on_fixture_passes_g0(tmp_path: Path) -> None:
    code, summary, _, _ = run_fixture_sampling(tmp_path, samples=32, interval_s=720.0)
    assert code == 0
    assert summary["g0"]["verdict"] == "PASS"
    assert summary["g0"]["plan"]["plan_ok"] is True


def test_sampling_run_reports_fail_with_exit_code_2_on_wide_clock_skew(tmp_path: Path) -> None:
    code, summary, _, _ = run_fixture_sampling(tmp_path, samples=32, interval_s=720.0, clock_offset_ms=2500)
    assert code == 2
    assert summary["g0"]["failed_checks"] == ["clock_offset"]


def test_sampling_run_never_requests_anything_but_whitelisted_gets(tmp_path: Path) -> None:
    _, _, _, seen = run_fixture_sampling(tmp_path, samples=2)
    allowed = {(httpx.URL(s.base).host, s.path) for s in lp.ENDPOINTS.values()}
    assert {r.method for r in seen} == {"GET"}
    assert {(r.url.host, r.url.path) for r in seen} <= allowed
    assert {r.url.host for r in seen} <= {"api.lbkex.com", "lbkperp.lbank.com"}
    for request in seen:
        assert not any("key" in k.lower() or "sign" in k.lower() for k in request.url.params)


def test_sampling_run_when_exchange_unreachable_exits_one(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    env = lp.VirtualEnv()
    prov = lp.collect_provenance(env, "sampling", [], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "u", "stamp")
    with lp.Fetcher(env, transport=httpx.MockTransport(handler)) as fetcher:
        code, summary = lp.run_sampling(
            fetcher, env, samples=3, interval_s=60.0, paths=paths, provenance=prov
        )
    assert code == 1
    assert summary["plan"]["completed_samples"] == 1  # stopped after the first, fully failed, sample
    assert paths.summary.exists() and paths.markdown.exists()


class InterruptingEnv(lp.VirtualEnv):
    """Raises KeyboardInterrupt from ``sleep`` after a number of calls (like Ctrl-C between samples)."""

    def __init__(self, after_sleeps: int) -> None:
        super().__init__()
        self.left = after_sleeps
        self.sleep = self._maybe_interrupt

    def _maybe_interrupt(self, seconds: float) -> None:
        self.left -= 1
        if self.left < 0:
            raise KeyboardInterrupt
        self.t += max(0.0, seconds)


def test_interrupted_run_still_writes_partial_outputs(tmp_path: Path) -> None:
    env = InterruptingEnv(after_sleeps=40)
    prov = lp.collect_provenance(env, "sampling", [], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "i", "stamp")
    with lp.Fetcher(env, transport=lp.fixture_transport(env)) as fetcher:
        code, summary = lp.run_sampling(
            fetcher, env, samples=32, interval_s=720.0, paths=paths, provenance=prov
        )
    assert code in (130, 143)
    assert summary["status"]["interrupted"] is True
    assert 0 < summary["plan"]["completed_samples"] < 32
    records = lp.read_jsonl(paths.jsonl)
    assert records[0]["kind"] == "provenance"
    assert records[-1]["kind"] == "end" and records[-1]["interrupted"] is True
    assert "Interrupted: True" in paths.markdown.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------
# provenance in every output
# ---------------------------------------------------------------------------------

PROVENANCE_KEYS = {
    "hostname",
    "utc_start",
    "utc_end",
    "python",
    "git_commit",
    "measured_from",
    "mode",
    "data_source",
}


def test_collect_provenance_has_required_fields() -> None:
    env = lp.VirtualEnv()
    prov = lp.collect_provenance(env, "sampling", ["--samples", "3"])
    assert set(prov) >= PROVENANCE_KEYS
    assert prov["hostname"] and prov["python"].startswith("3.")
    assert prov["measured_from"] == f"measured from {prov['hostname']} at {prov['utc_start']}"
    assert prov["argv"] == ["--samples", "3"]
    assert prov["data_source"] == "live"


def test_git_commit_comes_from_env_var_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TBOT_GIT_COMMIT", "deadbeef")
    assert lp.resolve_git_commit() == "deadbeef"


def test_git_commit_tolerates_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TBOT_GIT_COMMIT", raising=False)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert lp.resolve_git_commit() == "unknown"


def test_every_sampling_output_embeds_provenance(tmp_path: Path) -> None:
    _, summary, paths, _ = run_fixture_sampling(tmp_path, samples=2)
    jsonl_first = json.loads(paths.jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert jsonl_first["kind"] == "provenance"
    assert set(jsonl_first) >= PROVENANCE_KEYS
    summary_file = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert set(summary_file["provenance"]) >= PROVENANCE_KEYS
    assert summary_file["provenance"]["utc_end"] is not None
    assert summary["provenance"]["hostname"] == jsonl_first["hostname"]
    md = paths.markdown.read_text(encoding="utf-8")
    host = jsonl_first["hostname"]
    assert f"measured from {host} at " in md
    assert "OFFLINE FIXTURE DATA" in md
    assert jsonl_first["git_commit"] in md


def test_every_ratelimit_output_embeds_provenance(tmp_path: Path) -> None:
    _, summary, _, paths = run_fixture_ratelimit(tmp_path, throttle_after=None, steps=[1.0])
    first = json.loads(paths.jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert set(first) >= PROVENANCE_KEYS
    assert set(json.loads(paths.summary.read_text(encoding="utf-8"))["provenance"]) >= PROVENANCE_KEYS
    assert "measured from" in paths.markdown.read_text(encoding="utf-8")
    assert summary["provenance"]["utc_end"] is not None


def test_prices_stay_exact_decimals_in_the_raw_depth_record(tmp_path: Path) -> None:
    _, _, paths, _ = run_fixture_sampling(tmp_path, samples=1)
    sample = next(r for r in lp.read_jsonl(paths.jsonl) if r["kind"] == "sample")
    first_bid = sample["depth_raw"]["bids"][0]
    assert all(isinstance(x, str) for x in first_bid)  # strings, never floats
    assert D(first_bid[0]) > 0


# ---------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------


def test_cli_rejects_ratelimit_steps_above_five(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        lp.main(["--ratelimit", "--ratelimit-steps", "1", "6"])
    assert exc.value.code == 2
    assert "5" in capsys.readouterr().err


def test_cli_rejects_request_gap_that_would_exceed_five_rps() -> None:
    with pytest.raises(SystemExit):
        lp.main(["--request-gap", "0.1"])


def test_cli_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        lp.main(["--ratelimit", "--selftest"])


def test_cli_selftest_runs_offline_and_labels_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = lp.main(["--selftest", "--out-dir", str(tmp_path)])
    assert code == 0
    assert "not a measurement" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.md"))) == 2
    for md in tmp_path.glob("*.md"):
        assert "OFFLINE FIXTURE DATA" in md.read_text(encoding="utf-8")


def test_cli_sampling_with_injected_transport(tmp_path: Path) -> None:
    env = lp.VirtualEnv()
    code = lp.main(
        ["--samples", "2", "--interval", "30", "--out-dir", str(tmp_path), "--no-kline-history"],
        env=env,
        transport=lp.fixture_transport(env),
    )
    assert code == 3  # INCOMPLETE: only 2 samples
    assert len(list(tmp_path.glob("lbank_probe_*.summary.json"))) == 1


def test_cli_ratelimit_with_injected_transport(tmp_path: Path) -> None:
    env = lp.VirtualEnv()
    code = lp.main(
        [
            "--ratelimit",
            "--ratelimit-steps",
            "1",
            "2",
            "--step-seconds",
            "4",
            "--pause-seconds",
            "1",
            "--out-dir",
            str(tmp_path),
        ],
        env=env,
        transport=lp.fixture_transport(env, throttle_after=6),
    )
    assert code == 4
    summary = json.loads(next(tmp_path.glob("lbank_ratelimit_*.json")).read_text(encoding="utf-8"))
    assert summary["stopped_early"] is True


def test_cli_summarize_rebuilds_summary_from_jsonl(tmp_path: Path) -> None:
    _, _, paths, _ = run_fixture_sampling(tmp_path, samples=2)
    out = tmp_path / "re"
    assert lp.main(["--summarize", str(paths.jsonl), "--out-dir", str(out)]) == 0
    rebuilt = json.loads(next(out.glob("*.summary.json")).read_text(encoding="utf-8"))
    assert rebuilt["g0"]["plan"]["samples"] == 2
    assert rebuilt["provenance"]["summarized_from"] == str(paths.jsonl)
