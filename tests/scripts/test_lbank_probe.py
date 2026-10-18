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
    fail_status: int = 429,
) -> dict[str, Any]:
    return {
        "kind": "sample",
        "index": index,
        "utc_start": (T0 + timedelta(minutes=minutes)).isoformat(),
        "requests": [
            {
                "endpoint": "depth",
                "status": 200 if request_ok else fail_status,
                "ok": request_ok,
                "error": None,
                "payload_error": None,
                "latency_ms": 10.0,
                "nbytes": 1,
                "timeout_s": lp.REQUEST_TIMEOUT_S,
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


def test_parse_kline_rows_accepts_dict_shaped_rows() -> None:
    """item 5: a dict-shaped row (e.g. a mirror or future API version) is parsed via its first
    recognised timestamp field, not rejected outright."""
    body = {"data": [{"openTime": 1_790_000_000_000, "open": "1"}, {"t": 1_790_000_060, "open": "1"}]}
    assert lp.parse_kline_rows(body) == [(1_790_000_000,), (1_790_000_060,)]


def test_parse_kline_rows_raises_typed_error_for_unrecognised_shapes() -> None:
    """item 5: the failure mode is always KlineRowError (a ValueError subclass), never a bare
    KeyError/IndexError/TypeError that a generic ``except ValueError`` would miss."""
    with pytest.raises(lp.KlineRowError):
        lp.parse_kline_rows({"data": [{"unknown_field": 1}]})
    with pytest.raises(lp.KlineRowError):
        lp.parse_kline_rows({"data": [[]]})
    with pytest.raises(lp.KlineRowError):
        lp.parse_kline_rows({"data": [42]})
    with pytest.raises(lp.KlineRowError):
        lp.parse_kline_rows({"data": [{"t": "not-a-number"}]})
    with pytest.raises(lp.KlineRowError):
        lp.parse_kline_rows({"data": "not-a-list"})


def test_kline_probe_catches_a_bad_row_records_it_and_does_not_raise() -> None:
    """item 5: ``_kline_probe`` catches ``KlineRowError``, records ``parse_error``, and returns
    empty rows instead of propagating -- so one malformed page never aborts the whole G0 run."""

    class BadRowFetcher:
        def get(self, endpoint: str, _spec: lp.EndpointSpec, params: Any = None) -> lp.HttpResult:
            return lp.HttpResult(
                endpoint=endpoint,
                url="https://example.invalid/v2/kline.do",
                params=dict(params or {}),
                sent_at=T0,
                received_at=T0,
                status=200,
                latency_ms=1.0,
                nbytes=1,
                error=None,
                body={"result": "true", "error_code": 0, "data": [{"nonsense": 1}]},
                payload_ok=True,
                payload_error=None,
            )

    info, rows = lp._kline_probe(BadRowFetcher(), "1d", "recent", 0)
    assert rows == []
    assert "parse_error" in info
    assert "KlineRowError" in info["parse_error"]


def test_probe_kline_history_continues_past_a_bad_timeframe(tmp_path: Path) -> None:
    """item 5: a malformed kline response for ONE timeframe does not stop the history probe from
    covering the others."""
    env = lp.VirtualEnv()
    seen_params: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen_params.append(params)
        if params.get("type") == "day1":
            return httpx.Response(200, json={"result": "true", "error_code": 0, "data": [{"bad": 1}]})
        secs = {"hour4": 14_400, "hour1": 3_600, "minute1": 60}[params["type"]]
        start = int(params["time"])
        return httpx.Response(
            200,
            json={
                "result": "true",
                "error_code": 0,
                "data": [[start + i * secs, "1", "1", "1", "1", "1"] for i in range(3)],
            },
        )

    with lp.Fetcher(env, transport=httpx.MockTransport(handler)) as fetcher:
        history = lp.probe_kline_history(fetcher, env)
    tfs = history["timeframes"]
    assert "parse_error" in tfs["1d"]["recent"]
    assert tfs["1d"]["max_bars_per_request_observed"] == 0
    # the other three timeframes were not affected by 1d's bad response
    for tf in ("4h", "1h", "1m"):
        assert "parse_error" not in tfs[tf]["recent"]
        assert tfs[tf]["max_bars_per_request_observed"] == 3


def test_summarize_latency_counts_timeouts_at_the_timeout_value_and_separately() -> None:
    """item 4: a timeout's latency is counted AT its configured timeout (not excluded, and not at
    whatever partial latency_ms happened to be measured), and timeout_count is reported
    separately from other kinds of failure."""
    samples = [
        {
            "requests": [
                {"endpoint": "trades", "status": 200, "ok": True, "error": None, "latency_ms": 50.0},
                {
                    "endpoint": "trades",
                    "status": None,
                    "ok": False,
                    "error": "timeout",
                    "latency_ms": 3.0,  # a short partial measurement -- must NOT be used as-is
                    "timeout_s": 20.0,
                },
                {
                    "endpoint": "trades",
                    "status": None,
                    "ok": False,
                    "error": "ConnectError",  # a non-timeout transport error
                    "latency_ms": 1.0,
                },
            ]
        }
    ]
    out = lp.summarize_latency(samples)
    assert out["trades"]["timeout_count"] == 1
    assert out["trades"]["max_ms"] == 20_000.0  # the timeout, in ms, not the 3.0 ms partial read
    assert out["trades"]["n"] == 3
    assert out["trades"]["http_not_200"] == 2  # both the timeout and the connect error


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


def test_g0_tolerates_isolated_non_ban_failures_within_95_percent() -> None:
    """item 3: a single isolated non-ban-type failure (e.g. a 400) out of 32 samples is 31/32 =
    96.9% success, above the 95% per-endpoint threshold, and there is no ban-type status --
    so the condition passes."""
    samples = full_plan()
    samples[5] = make_sample(5, minutes=60, request_ok=False, fail_status=400)
    out = lp.evaluate_g0(samples)
    assert out["checks"]["all_endpoints_200"]["pass"] is True
    assert out["verdict"] == "PASS"


def test_g0_fails_below_95_percent_even_without_any_ban_status() -> None:
    """item 3: the per-endpoint rate matters on its own too, not only ban-type statuses -- enough
    isolated 400s to drop under 95% fails even though none of them is a ban-type response."""
    samples = full_plan()
    for i in (1, 3, 5, 7):  # 4/32 = 12.5% failing -> 28/32 = 87.5% success, under 95%
        samples[i] = make_sample(i, minutes=i * 12, request_ok=False, fail_status=400)
    out = lp.evaluate_g0(samples)
    assert out["checks"]["all_endpoints_200"]["pass"] is False
    assert out["checks"]["all_endpoints_200"]["ban_type_response_seen"] is False
    assert out["verdict"] == "FAIL"


def test_g0_fails_on_a_single_ban_status_even_above_95_percent() -> None:
    """item 3: even one ban-type response (429/403/418) fails the condition outright, regardless
    of how high the overall per-endpoint success rate still is."""
    samples = full_plan()
    samples[5] = make_sample(5, minutes=60, request_ok=False, fail_status=403)
    out = lp.evaluate_g0(samples)
    assert out["checks"]["all_endpoints_200"]["pass"] is False
    assert out["checks"]["all_endpoints_200"]["ban_type_response_seen"] is True
    assert out["verdict"] == "FAIL"


def test_g0_fails_on_clock_skew_but_uses_the_median_not_the_worst_case() -> None:
    assert lp.evaluate_g0(full_plan(delta_ms=1500.0))["failed_checks"] == ["clock_offset"]
    samples = full_plan(delta_ms=200.0)
    samples[3]["server_time"]["delta_ms"] = 9000.0  # one outlier must not fail the median test
    assert lp.evaluate_g0(samples)["verdict"] == "PASS"
    samples = full_plan(delta_ms=-1200.0)  # negative skew counts as well
    assert lp.evaluate_g0(samples)["failed_checks"] == ["clock_offset"]


def _clock(med_abs: float, half_rtt: float, delta_at_min_rtt: float) -> dict[str, Any]:
    return {
        "median_abs_delta_ms": med_abs,
        "median_half_rtt_ms": half_rtt,
        "delta_at_min_rtt_ms": delta_at_min_rtt,
    }


def test_clock_check_passes_when_clearly_under_threshold() -> None:
    out = lp._clock_check(_clock(200.0, 50.0, 180.0))
    assert out["pass"] is True
    assert out["status"] == "pass"


def test_clock_check_fails_when_clearly_over_threshold() -> None:
    out = lp._clock_check(_clock(2000.0, 50.0, 1900.0))
    assert out["pass"] is False
    assert out["status"] == "fail"


def test_clock_check_fails_when_delta_at_min_rtt_alone_is_over_even_if_median_is_under() -> None:
    """item 6: BOTH conditions must hold -- a clean median with one bad low-RTT sample still
    fails (and is not inconclusive, since the uncertainty band here does not straddle 1000)."""
    out = lp._clock_check(_clock(200.0, 10.0, 1500.0))
    assert out["pass"] is False
    assert out["status"] == "fail"


def test_clock_check_is_inconclusive_when_uncertainty_band_straddles_threshold() -> None:
    """item 6: median 900 +/- half-RTT 150 spans [750, 1050], straddling 1000 -- cannot confirm
    pass or fail, so this is reported as inconclusive (which counts as not-passed)."""
    out = lp._clock_check(_clock(900.0, 150.0, 900.0))
    assert out["pass"] is False
    assert out["status"] == "inconclusive"
    assert "reason" in out


def test_clock_check_no_data() -> None:
    out = lp._clock_check({"n": 0})
    assert out["pass"] is False
    assert out["status"] == "no_data"


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


def test_g0_with_no_samples_is_incomplete_not_fail() -> None:
    """item 8: zero samples is INCOMPLETE (nothing was measured yet), never FAIL."""
    out = lp.evaluate_g0([])
    assert out["verdict"] == "INCOMPLETE"
    assert out["plan"]["plan_ok"] is False


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


def test_per_endpoint_timeouts_match_item_3() -> None:
    """item 3: 20 s for supplement/trades, 10 s (the default) for every other endpoint."""
    assert lp.ENDPOINTS["trades"].timeout_s == 20.0
    for key, spec in lp.ENDPOINTS.items():
        if key != "trades":
            assert spec.timeout_s == 10.0, key


def test_fetcher_result_carries_the_endpoints_configured_timeout() -> None:
    env = lp.VirtualEnv()
    with lp.Fetcher(env, transport=lp.fixture_transport(env)) as fetcher:
        trades_res = fetcher.get("trades", lp.ENDPOINTS["trades"], {"symbol": "btc_usdt", "size": 10})
        depth_res = fetcher.get("depth", lp.ENDPOINTS["depth"], {"symbol": "btc_usdt", "size": 10})
    assert trades_res.timeout_s == 20.0
    assert depth_res.timeout_s == 10.0


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


def test_stop_tracker_ban_only_ignores_isolated_non_ban_statuses_and_payload_errors() -> None:
    """item 1: sampling mode's ban_only tracker does NOT stop on an isolated 4xx or a payload
    error on HTTP 200 -- those are left to the per-endpoint 95% tolerance (item 3)."""
    tracker = lp.StopTracker(ban_only=True)
    assert tracker.observe(result(400)) is None
    assert tracker.observe(result(404)) is None
    assert tracker.observe(result(200, payload_ok=False)) is None
    assert tracker.reason is None


@pytest.mark.parametrize("status", [429, 403, 418])
def test_stop_tracker_ban_only_still_stops_on_ban_statuses(status: int) -> None:
    assert lp.StopTracker(ban_only=True).observe(result(status)) is not None


def test_stop_tracker_ban_only_still_stops_on_5xx_and_transport_error_bursts() -> None:
    t1 = lp.StopTracker(ban_only=True)
    assert t1.observe(result(502)) is None
    assert t1.observe(result(503)) is None
    assert t1.observe(result(504)) is not None

    t2 = lp.StopTracker(ban_only=True)
    assert t2.observe(result(None, error="timeout", payload_ok=False)) is None
    assert t2.observe(result(None, error="timeout", payload_ok=False)) is None
    assert t2.observe(result(None, error="timeout", payload_ok=False)) is not None


def run_fixture_ratelimit(
    tmp_path: Path,
    *,
    throttle_after: int | None,
    steps: list[float],
    step_seconds: float = 5.0,
    latency_s: float = 0.05,
) -> tuple[int, dict[str, Any], list[httpx.Request], lp.OutputPaths]:
    env = lp.VirtualEnv()
    seen: list[httpx.Request] = []
    prov = lp.collect_provenance(env, "ratelimit", ["--test"], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "rl", "stamp", summary_suffix=".json")
    transport = lp.fixture_transport(env, throttle_after=throttle_after, seen=seen, latency_s=latency_s)
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
    assert summary["stopped_reason"] == "ban"
    # item 2: last_safe_rate_rps is the measured achieved_rps of step 0 (rate=1.0), not the
    # nominal 1.0 itself.
    assert summary["last_safe_rate_rps"] == pytest.approx(summary["steps"][0]["achieved_rps"])
    assert summary["last_safe_rate_rps"] == pytest.approx(1.0, abs=0.05)
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
    # item 2: last_safe_rate_rps is the MEASURED achieved_rps of the last clean step, not the
    # nominal target -- here they coincide to within float/virtual-clock noise.
    assert summary["last_safe_rate_rps"] == pytest.approx(summary["steps"][-1]["achieved_rps"])
    assert summary["last_safe_rate_rps"] == pytest.approx(5.0, abs=0.05)
    assert summary["steps"][1]["achieved_rps"] <= 5.0 + 1e-9
    assert all(s["reached"] for s in summary["steps"])
    assert len(seen) == sum(s["requests_sent"] for s in summary["steps"])


def test_ratelimit_outputs_record_failing_response_and_provenance(tmp_path: Path) -> None:
    _, _, _, paths = run_fixture_ratelimit(tmp_path, throttle_after=4, steps=[1.0])
    md = paths.markdown.read_text(encoding="utf-8")
    assert "Failing response" in md and "retry-after" in md
    assert "measured from" in md


def test_ratelimit_stops_climbing_when_latency_prevents_reaching_the_target_rate(
    tmp_path: Path,
) -> None:
    """item 2: with a realistic 1.1 s per-request latency, this strictly serial client cannot
    exercise any rate above ~1/1.1 ~= 0.91 req/s no matter what is requested -- not because the
    exchange complained (no response here is ever anything but clean 200), but because there is
    no concurrency to fall back on. Step 0 (rate=1.0) just barely reaches 90% of target; step 1
    (rate=2.0) falls far short, so the run reports that honestly and stops instead of continuing
    to escalate to a rate it structurally cannot test.
    """
    code, summary, seen, _ = run_fixture_ratelimit(
        tmp_path, throttle_after=None, steps=[1.0, 2.0, 5.0], step_seconds=10.0, latency_s=1.1
    )
    assert code == 5
    assert summary["stopped_early"] is True
    assert summary["stopped_reason"] == "latency"
    assert summary["failure"] is None  # never a bad response -- purely latency-limited
    assert len(summary["steps"]) == 2  # step rate=5.0 never started
    step0, step1 = summary["steps"]
    assert step0["rate_rps"] == 1.0
    assert step0["reached"] is True
    assert step0["achieved_rps"] == pytest.approx(1 / 1.1, abs=0.01)
    assert step1["rate_rps"] == 2.0
    assert step1["reached"] is False
    assert step1["achieved_rps"] == pytest.approx(1 / 1.1, abs=0.01)
    # last_safe_rate_rps is step 0's measured rate (nothing ever complained up to there), not its
    # nominal 1.0, and not step 1's shortfall (which was never confirmed as a safe TARGET rate).
    assert summary["last_safe_rate_rps"] == pytest.approx(step0["achieved_rps"])
    not_reached = summary["rate_not_reached"]
    assert not_reached["rate_rps"] == 2.0
    assert "latency-limited" in not_reached["reason"]
    assert "concurrency" in not_reached["reason"]
    assert len(seen) == step0["requests_sent"] + step1["requests_sent"]


def test_ratelimit_markdown_shows_rate_not_reached_block(tmp_path: Path) -> None:
    _, _, _, paths = run_fixture_ratelimit(
        tmp_path, throttle_after=None, steps=[1.0, 2.0], step_seconds=10.0, latency_s=1.1
    )
    md = paths.markdown.read_text(encoding="utf-8")
    assert "Rate not reached" in md
    assert "latency-limited" in md


def test_ratelimit_caveat_names_the_shared_ip_and_this_process_only_scope(tmp_path: Path) -> None:
    """item 9: the rate-limit summary must say the ceiling covers this probe process only, and
    that the LBank recorder shares the IP at roughly RECORDER_SHARED_IP_RPS."""
    _, summary, _, paths = run_fixture_ratelimit(tmp_path, throttle_after=None, steps=[1.0])
    assert "THIS PROBE PROCESS only" in summary["caveat"]
    assert "lbank_recorder" in summary["caveat"]
    assert f"{lp.RECORDER_SHARED_IP_RPS:g}" in summary["caveat"]
    md = paths.markdown.read_text(encoding="utf-8")
    assert "THIS PROBE PROCESS only" in md


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
    """A transport-level failure (no HTTP response at all) on every request trips the ban_only
    StopTracker's 3-consecutive-transport-error burst after the 3rd call of sample 0 (timestamp,
    accuracy, depth) -- before that sample ever completes. This is reported as "unreachable"
    (exit 1), distinct from a FAIL via stopped_on_ban, because no HTTP response was ever received
    at all (see run_sampling's BanDetectedError handler)."""

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
    assert summary["plan"]["completed_samples"] == 0  # sample 0 never completed -- never retried
    assert paths.summary.exists() and paths.markdown.exists()


def test_sampling_run_stops_immediately_on_429_and_makes_no_further_requests(tmp_path: Path) -> None:
    """item 1: a 429 on depth.do must be the LAST request this run ever makes -- no more of the
    sample it occurred in, no further samples, no depth-size/kline-history discovery. The 429
    arrives on the 5th depth.do call overall: 3 from discover_depth_size (200/100 rejected, 60
    accepted), 1 from sample 0's own depth read, then the 5th on sample 1's depth read -- so
    sample 0 completes in full and sample 1 stops partway through.
    """
    code, summary, paths, seen = run_fixture_sampling(
        tmp_path, samples=10, interval_s=60.0, throttle_after=5, throttle_path="/v2/depth.do"
    )
    assert code == 2
    assert summary["g0"]["verdict"] == "FAIL"
    assert summary["stopped_on_ban"] is not None
    assert summary["stopped_on_ban"]["reason"] == "HTTP 429"
    assert summary["plan"]["completed_samples"] == 1  # only sample 0 finished

    depth_calls = [r for r in seen if r.url.path == "/v2/depth.do"]
    assert len(depth_calls) == 5
    # the single most important assertion: nothing was ever requested after the 429
    assert seen[-1] is depth_calls[-1]

    records = lp.read_jsonl(paths.jsonl)
    ban_records = [r for r in records if r.get("kind") == "stopped_on_ban"]
    assert len(ban_records) == 1
    assert ban_records[0]["reason"] == "HTTP 429"
    # the run ends right there -- no further "sample" records after the one completed sample
    sample_records = [r for r in records if r.get("kind") == "sample"]
    assert len(sample_records) == 1

    md = paths.markdown.read_text(encoding="utf-8")
    assert "FAIL" in md
    assert "STOPPED ON BAN" in md


def test_sampling_run_stops_on_a_transport_error_burst_mid_run(tmp_path: Path) -> None:
    """item 1: the ban_only tracker also stops on a burst of 3 consecutive transport errors, not
    only on an explicit ban-type HTTP status -- and, because at least one sample had already
    completed cleanly, this is reported as a FAIL via stopped_on_ban, not as "unreachable" (which
    is reserved for a transport failure before the very first sample ever completes).
    """
    env = lp.VirtualEnv()
    base_transport = lp.fixture_transport(env)
    calls = {"n": 0}

    def flaky_after_one_sample(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # depth-size discovery (3) + sample 0 (10) + the one-off kline-history probe (12) = 25
        # calls complete cleanly first; everything after that fails.
        if calls["n"] > 25:
            raise httpx.ConnectError("flaky")
        return base_transport.handle_request(request)

    prov = lp.collect_provenance(env, "sampling", [], data_source="offline_fixture")
    paths = lp.OutputPaths.for_run(tmp_path, "flaky", "stamp")
    with lp.Fetcher(env, transport=httpx.MockTransport(flaky_after_one_sample)) as fetcher:
        code, summary = lp.run_sampling(
            fetcher, env, samples=10, interval_s=60.0, paths=paths, provenance=prov
        )
    assert code == 2  # FAIL, not 1 ("unreachable") -- sample 0 did complete
    assert summary["g0"]["verdict"] == "FAIL"
    assert summary["stopped_on_ban"] is not None
    assert "transport errors" in summary["stopped_on_ban"]["reason"]
    assert summary["plan"]["completed_samples"] >= 1


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


def test_dirty_check_covers_the_probe_and_its_two_data_dependencies() -> None:
    """item 7: the dirty check must cover scripts/lbank_probe.py AND the two data-layer modules
    it imports from -- a change to either can change this probe's measured output just as much as
    editing the script itself."""
    assert set(lp.DIRTY_CHECK_PATHS) == {
        "scripts/lbank_probe.py",
        "src/tbot/data/depth.py",
        "src/tbot/data/lbank_recorder.py",
    }


def test_resolve_git_commit_uses_the_full_dirty_check_path_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """item 7: the actual git status call passed to subprocess.run must include every path in
    DIRTY_CHECK_PATHS, not just the script itself."""
    monkeypatch.delenv("TBOT_GIT_COMMIT", raising=False)
    seen_argvs: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen_argvs.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    lp.resolve_git_commit()
    status_calls = [a for a in seen_argvs if a[:2] == ["git", "status"]]
    assert len(status_calls) == 1
    for path in lp.DIRTY_CHECK_PATHS:
        assert path in status_calls[0]


def test_git_commit_unknown_flag_is_set_when_commit_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TBOT_GIT_COMMIT", raising=False)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    env = lp.VirtualEnv()
    prov = lp.collect_provenance(env, "sampling", [])
    assert prov["git_commit"] == "unknown"
    assert prov["git_commit_unknown"] is True


def test_unknown_git_commit_is_a_warning_in_notes_and_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """item 7: an unresolved commit must show up as a WARNING in both the summary (notes) and the
    markdown, not just silently as the string "unknown" buried in the provenance block."""
    monkeypatch.delenv("TBOT_GIT_COMMIT", raising=False)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    _, summary, paths, _ = run_fixture_sampling(tmp_path, samples=2)
    assert any("WARNING" in n and "unknown" in n for n in summary["notes"])
    md = paths.markdown.read_text(encoding="utf-8")
    assert "WARNING" in md


def test_host_label_overrides_the_kernel_hostname() -> None:
    """item 7: --host-label lets the caller (e.g. a Docker entrypoint) report a human-meaningful
    name instead of the container id that socket.gethostname() would otherwise return."""
    env = lp.VirtualEnv()
    prov = lp.collect_provenance(env, "sampling", [], host_label="turkey-vps-1")
    assert prov["hostname"] == "turkey-vps-1"
    assert prov["measured_from"].startswith("measured from turkey-vps-1 at ")


def test_host_label_defaults_to_kernel_hostname_when_not_given() -> None:
    env = lp.VirtualEnv()
    prov = lp.collect_provenance(env, "sampling", [])
    assert prov["hostname"]  # whatever socket.gethostname() returns -- just confirm it is used
    assert prov["hostname"] != "turkey-vps-1"


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
    env = lp.VirtualEnv(datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC))  # the clock that stamps the output name
    assert lp.main(["--summarize", str(paths.jsonl), "--out-dir", str(out)], env=env) == 0
    assert [p.name for p in out.glob("lbank_probe_resummary_*.summary.json")] == [
        f"lbank_probe_resummary_{lp._stamp(env.now())}.summary.json"
    ]
    rebuilt = json.loads(next(out.glob("*.summary.json")).read_text(encoding="utf-8"))
    assert rebuilt["g0"]["plan"]["samples"] == 2
    assert rebuilt["provenance"]["summarized_from"] == str(paths.jsonl)
