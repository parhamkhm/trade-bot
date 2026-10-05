"""Tests for tbot.execution.tabdeal_client.

All HTTP is mocked with respx; no test may reach a real exchange and no test may send an order
(this module only exposes read-only endpoints in the first place).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import respx
from pydantic import SecretStr

from tbot.execution.tabdeal_client import (
    TabdealClient,
    TabdealClientError,
    TokenBucket,
    _redact_url,
    _sign,
)

BASE_URL = "https://api1.tabdeal.org"
READ_PREFIX = "/r/api/v1"
WRITE_PREFIX = "/api/v1"


class FakeClock:
    """Deterministic Clock for tests -- never reads the real wall clock."""

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
        "requests_per_second": 1000.0,  # fast by default; bucket behaviour tested separately
        "timeout_seconds": 1.0,
        "max_retries": 3,
    }
    kwargs.update(overrides)
    return TabdealClient(**kwargs)


# ---------------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------------


def test_sign_matches_known_vector() -> None:
    """HMAC-SHA256 over the exact url-encoded query string, hex-encoded.

    MINOR-13: provenance -- this is Binance's official "HMAC SHA256" signed-request example from
    their REST API documentation (the SIGNED endpoint example for a LIMIT order). It predates
    this codebase and was independently re-derived by the reviewer (not computed by the same
    `_sign` implementation under test), so it is not a circular check.
    """
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert _sign(secret, query) == expected


def test_sign_matches_second_vector_over_a_non_order_query_string() -> None:
    """MINOR-13: a second vector over a query string shaped like this client's own ``account()``
    call (just ``recvWindow``/``timestamp``, no order fields at all) -- independently computed
    with ``openssl dgst -sha256 -hmac`` against the same secret as the vector above, not with
    Python's ``hmac`` module, so this does not just re-test `_sign` against itself."""
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "recvWindow=5000&timestamp=1499827319559"
    expected = "82f4e72e95e63d666b6da651e82a701722ad8a785a169318d91f36f279c55821"
    assert _sign(secret, query) == expected


def test_redact_url_strips_signature_but_keeps_other_params() -> None:
    url = httpx.URL(f"{BASE_URL}{READ_PREFIX}/account?timestamp=1&recvWindow=5000&signature=deadbeef")
    redacted = _redact_url(url)
    assert "signature" not in redacted
    assert "deadbeef" not in redacted
    assert "timestamp=1" in redacted
    assert "recvWindow=5000" in redacted


# ---------------------------------------------------------------------------------
# basic public reads
# ---------------------------------------------------------------------------------


@respx.mock
def test_ping_success_reports_status_and_latency() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    result = client.ping()
    assert result.ok
    assert result.status_code == 200
    assert result.path == f"{READ_PREFIX}/ping"
    client.close()


@respx.mock
def test_depth_and_trades_send_symbol_and_limit_as_fresh_params_each_call() -> None:
    """Guards against the official SDK's mutable-default-dict bug (CLAUDE.md section 6):
    each call must carry only its own params, never a previous call's leftovers."""
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(side_effect=_capture)
    client = make_client()

    client.trades("BTCUSDT", limit=100)
    client.trades("ETHUSDT", limit=500)

    assert len(captured) == 2
    first_params = dict(httpx.QueryParams(captured[0].url.query))
    second_params = dict(httpx.QueryParams(captured[1].url.query))
    assert first_params == {"symbol": "BTCUSDT", "limit": "100"}
    assert second_params == {"symbol": "ETHUSDT", "limit": "500"}
    client.close()


# ---------------------------------------------------------------------------------
# bare JSON numbers must parse as exact Decimal -- no float rounding in between
# ---------------------------------------------------------------------------------


@respx.mock
def test_trades_bare_json_number_price_parses_as_exact_decimal_no_float_rounding() -> None:
    """Regression: httpx's own ``Response.json()`` parses a bare JSON number through Python
    ``float`` first, which rounds a value like ``61234.56789012345`` to the nearest IEEE-754
    double before anything downstream (``Decimal(str(value))``) ever sees it -- the wrong number
    is then preserved exactly. The raw bytes below are the literal response text (not built via
    httpx's ``json=`` kwarg, which would itself construct a Python float before serializing),
    so this exercises the real wire format end to end."""
    body = (
        b'[{"id": 1, "price": 61234.56789012345, "qty": 0.1, "time": 1700000000000},'
        b'{"id": 2, "price": "100.5", "qty": "2", "time": 1700000001000}]'
    )
    respx.get(f"{BASE_URL}{READ_PREFIX}/trades").mock(
        return_value=httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
    )
    client = make_client()

    result = client.trades("BTCUSDT")

    assert result.ok
    assert isinstance(result.body, list)
    first, second = result.body
    assert isinstance(first["price"], Decimal)
    assert isinstance(first["qty"], Decimal)
    # Exact string equality, not pytest.approx -- a float-rounded value would differ here.
    assert str(first["price"]) == "61234.56789012345"
    assert str(first["qty"]) == "0.1"
    # A quoted string is NOT a JSON number, so parse_float never sees it: it stays a str and is
    # converted exactly downstream. That is the safe direction -- no float ever existed.
    assert isinstance(second["price"], str)
    assert Decimal(second["price"]) == Decimal("100.5")
    client.close()


@respx.mock
def test_depth_bare_json_number_in_nested_array_parses_as_exact_decimal() -> None:
    """The ``parse_float`` hook applies to every float literal in the document, however deeply
    nested -- including inside a depth response's ``bids``/``asks`` arrays, not just top-level
    fields."""
    body = b'{"bids": [[61234.56789012345, 0.1]], "asks": [["61235.0", "2"]]}'
    respx.get(f"{BASE_URL}{READ_PREFIX}/depth").mock(
        return_value=httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
    )
    client = make_client()

    result = client.depth("BTCUSDT")

    assert result.ok
    bid_price, bid_qty = result.body["bids"][0]
    assert isinstance(bid_price, Decimal)
    assert str(bid_price) == "61234.56789012345"
    assert isinstance(bid_qty, Decimal)
    assert str(bid_qty) == "0.1"
    client.close()


# ---------------------------------------------------------------------------------
# both path prefixes
# ---------------------------------------------------------------------------------


@respx.mock
def test_prefix_is_selectable_per_call() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}{WRITE_PREFIX}/ping").mock(return_value=httpx.Response(200, json={}))
    client = make_client()

    read_result = client.ping(prefix=client.read_prefix)
    write_result = client.ping(prefix=client.write_prefix)

    assert read_result.ok is False
    assert read_result.status_code == 404
    assert write_result.ok is True
    assert write_result.status_code == 200
    client.close()


# ---------------------------------------------------------------------------------
# missing credentials
# ---------------------------------------------------------------------------------


def test_account_without_credentials_raises() -> None:
    client = make_client()
    assert client.has_credentials is False
    with pytest.raises(TabdealClientError):
        client.account()
    client.close()


@respx.mock
def test_account_signs_request_and_never_leaks_secret_in_reported_url() -> None:
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"balances": []})

    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(side_effect=_capture)
    secret = "s3cr3t-api-secret"
    client = make_client(
        api_key=SecretStr("my-api-key"),
        api_secret=SecretStr(secret),
        recv_window_ms=5000,
    )

    result = client.account()

    assert result.ok
    assert "signature" not in result.url
    assert len(captured) == 1
    request = captured[0]
    assert request.headers["X-MBX-APIKEY"] == "my-api-key"
    params = dict(httpx.QueryParams(request.url.query))
    signature = params.pop("signature")
    expected_signature = _sign(secret, urlencode(params))
    assert signature == expected_signature
    # MINOR-18: timestamp must be an integer number of ms -- CLAUDE.md section 6 -- never a float
    # string like "1700000000000.0" (the "." is the tell).
    assert "." not in params["timestamp"]
    int(params["timestamp"])  # must parse as an int
    # MINOR-18: the API key must never leak into anything the caller could persist or print.
    assert "my-api-key" not in result.url
    assert "my-api-key" not in repr(result)
    client.close()


@respx.mock
def test_account_request_query_string_matches_signed_bytes_exactly() -> None:
    """MINOR-7: the signature must cover the *exact* bytes sent on the wire -- not a dict that
    httpx is free to re-encode independently. Assert the raw query string byte-for-byte, not just
    that the same key/value pairs are present in some order."""
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"balances": []})

    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(side_effect=_capture)
    secret = "s3cr3t-api-secret"
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
    client = make_client(clock=clock, api_key=SecretStr("my-api-key"), api_secret=SecretStr(secret))

    client.account()

    assert len(captured) == 1
    expected_timestamp = str(int(clock.now().timestamp() * 1000))
    expected_base = f"timestamp={expected_timestamp}&recvWindow=5000"
    expected_signature = _sign(secret, expected_base)
    expected_query = f"{expected_base}&signature={expected_signature}"
    assert captured[0].url.query.decode("ascii") == expected_query
    client.close()


# ---------------------------------------------------------------------------------
# M1: a retried signed request must not resend a stale timestamp/signature
# ---------------------------------------------------------------------------------


@respx.mock
def test_account_retry_resigns_with_fresh_timestamp_and_signature_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for M1: params used to be built once before the retry loop, so a retried
    account() call resent the *original* timestamp and signature -- which the exchange would
    reject (recvWindow elapsed) even though nothing was wrong with the request itself."""
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)
    captured: list[httpx.Request] = []
    clock = FakeClock()

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        clock.advance(1.0)  # simulate time passing between attempts
        if len(captured) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"balances": []})

    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(side_effect=_capture)
    client = make_client(clock=clock, api_key=SecretStr("my-api-key"), api_secret=SecretStr("s3cr3t"))

    result = client.account()

    assert result.ok
    assert len(captured) == 2
    first_params = dict(httpx.QueryParams(captured[0].url.query))
    second_params = dict(httpx.QueryParams(captured[1].url.query))
    assert first_params["timestamp"] != second_params["timestamp"]
    assert first_params["signature"] != second_params["signature"]
    client.close()


# ---------------------------------------------------------------------------------
# retry / backoff behaviour
# ---------------------------------------------------------------------------------


@respx.mock
def test_429_then_success_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda s: sleeps.append(s))

    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={})]
    client = make_client()

    result = client.ping()

    assert result.ok
    assert len(result.retries) == 1
    assert result.retries[0].reason == "429"
    assert len(sleeps) == 1
    assert any(h.header == "Retry-After" and h.value == "1" for h in result.rate_limit_headers)
    client.close()


@respx.mock
def test_retry_after_header_overrides_a_short_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """MINOR-9: Retry-After is a floor on the wait, not merely one more data point -- the
    computed exponential-backoff-plus-jitter on attempt 1 (<= ~0.75s) must never win over a
    server-named 5s wait."""
    sleeps: list[float] = []
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda s: sleeps.append(s))
    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, json={})]
    client = make_client()

    result = client.ping()

    assert result.ok
    assert len(sleeps) == 1
    assert sleeps[0] >= 5.0
    client.close()


@respx.mock
def test_retry_after_above_cap_is_capped_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """m4 (reviewer finding): an uncapped Retry-After would let one hostile/misconfigured
    response make this client sleep for arbitrarily long (e.g. a full day) on a single call --
    cap it at ``_RETRY_AFTER_CAP_SECONDS`` and log when the cap actually bites."""
    sleeps: list[float] = []
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda s: sleeps.append(s))
    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200, json={})]
    client = make_client()

    events: list[Any] = []
    from collections.abc import MutableMapping

    import structlog

    def _capture(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        events.append(dict(event_dict))
        return event_dict

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        result = client.ping()
    finally:
        structlog.reset_defaults()

    assert result.ok
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(60.0)
    assert any(e.get("event") == "tabdeal_client.retry_after_capped" for e in events)
    client.close()


@respx.mock
def test_retry_after_below_cap_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap must not kick in (or log) for an ordinary, well-behaved Retry-After value."""
    sleeps: list[float] = []
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda s: sleeps.append(s))
    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, json={})]
    client = make_client()

    result = client.ping()

    assert result.ok
    assert sleeps == [pytest.approx(5.0)]
    client.close()


@respx.mock
def test_order_count_headers_are_never_captured() -> None:
    """MINOR-12: X-MBX-ORDER-COUNT-10S/-1D are order-placement counters that can never be
    populated at phase 0 (this client places no orders) -- listing them in the rate-limit
    header allow-list would be misleading even if a server echoed them back."""
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(
        return_value=httpx.Response(
            200, json={}, headers={"X-MBX-ORDER-COUNT-10S": "1", "X-MBX-ORDER-COUNT-1D": "2"}
        )
    )
    client = make_client()

    result = client.ping()

    captured_header_names = {h.header for h in result.rate_limit_headers}
    assert "X-MBX-ORDER-COUNT-10S" not in captured_header_names
    assert "X-MBX-ORDER-COUNT-1D" not in captured_header_names
    client.close()


@respx.mock
def test_5xx_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)
    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={})]
    client = make_client()

    result = client.ping()

    assert result.ok
    assert result.retries[0].reason == "503"
    client.close()


@respx.mock
def test_persistent_429_exhausts_retries_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(429))
    client = make_client(max_retries=3)

    result = client.ping()

    assert result.ok is False
    assert result.status_code == 429
    assert len(result.retries) == 3
    client.close()


@respx.mock
def test_timeout_is_retried_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda _seconds: None)
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(side_effect=httpx.ReadTimeout("boom"))
    client = make_client(max_retries=2)

    result = client.ping()

    assert result.ok is False
    assert result.status_code is None
    assert result.error is not None and result.error.startswith("timeout")
    assert len(result.retries) == 2
    client.close()


# ---------------------------------------------------------------------------------
# terminal failures that must NOT be retried
# ---------------------------------------------------------------------------------


@respx.mock
def test_403_is_not_retried() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(403))
    client = make_client()

    result = client.ping()

    assert result.ok is False
    assert result.status_code == 403
    assert result.retries == ()
    client.close()


@respx.mock
def test_451_is_not_retried() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(return_value=httpx.Response(451))
    client = make_client()

    result = client.ping()

    assert result.ok is False
    assert result.status_code == 451
    client.close()


@respx.mock
def test_connect_error_is_unreachable_not_retried() -> None:
    respx.get(f"{BASE_URL}{READ_PREFIX}/ping").mock(side_effect=httpx.ConnectError("refused"))
    client = make_client()

    result = client.ping()

    assert result.ok is False
    assert result.status_code is None
    assert result.error is not None and result.error.startswith("unreachable")
    assert result.retries == ()
    client.close()


@respx.mock
def test_account_unreachable_error_message_scrubs_signature() -> None:
    """MINOR-11 / MINOR-18: an httpx exception's ``str()`` can itself embed the full signed
    request URL (httpx has done this in some connection-error messages). Whatever reaches
    ``ProbeResult.error`` must never contain a live signature value, regardless of which code
    path -- ours or httpx's -- put it there."""
    fake_signature = "deadbeefcafefeed0011223344556677"
    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(
        side_effect=httpx.ConnectError(
            f"connection refused: https://api1.tabdeal.org{READ_PREFIX}/account?timestamp=1"
            f"&recvWindow=5000&signature={fake_signature}"
        )
    )
    client = make_client(api_key=SecretStr("my-api-key"), api_secret=SecretStr("s3cr3t"))

    result = client.account()

    assert result.ok is False
    assert result.error is not None
    assert fake_signature not in result.error
    assert "signature=[REDACTED]" in result.error
    client.close()


# ---------------------------------------------------------------------------------
# token bucket
# ---------------------------------------------------------------------------------


def test_token_bucket_allows_burst_up_to_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(5.0, clock, capacity=5.0)
    waited = [bucket.acquire() for _ in range(5)]
    assert all(w == 0.0 for w in waited)


def test_token_bucket_blocks_once_capacity_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", _fake_sleep)
    bucket = TokenBucket(5.0, clock, capacity=1.0)

    first = bucket.acquire()
    second = bucket.acquire()

    assert first == 0.0
    assert second > 0.0
    assert sleeps == [second]


def test_token_bucket_accepts_injected_sleep_without_monkeypatching_the_module() -> None:
    """MINOR-10: sleep is injectable directly -- a test should not have to reach into the module
    and monkeypatch `time.sleep` (which is process-global) just to get determinism."""
    clock = FakeClock()
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    bucket = TokenBucket(5.0, clock, capacity=1.0, sleep=_fake_sleep)

    bucket.acquire()
    second = bucket.acquire()

    assert sleeps == [second]


# ---------------------------------------------------------------------------------
# MINOR-8: recv_window_ms is validated even without going through ExchangeConfig
# ---------------------------------------------------------------------------------


def test_recv_window_ms_above_60000_is_rejected() -> None:
    with pytest.raises(ValueError, match="recv_window_ms"):
        make_client(recv_window_ms=60_001)


def test_recv_window_ms_zero_or_negative_is_rejected() -> None:
    with pytest.raises(ValueError, match="recv_window_ms"):
        make_client(recv_window_ms=0)
    with pytest.raises(ValueError, match="recv_window_ms"):
        make_client(recv_window_ms=-1)


def test_recv_window_ms_at_the_60000_boundary_is_accepted() -> None:
    client = make_client(recv_window_ms=60_000)
    assert client.has_credentials is False
    client.close()


# ---------------------------------------------------------------------------------
# MINOR-10: backoff jitter is deterministic when a random.Random instance is injected
# ---------------------------------------------------------------------------------


@respx.mock
def test_injected_random_makes_backoff_jitter_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    sleeps: list[float] = []
    monkeypatch.setattr("tbot.execution.tabdeal_client.time.sleep", lambda s: sleeps.append(s))
    route = respx.get(f"{BASE_URL}{READ_PREFIX}/ping")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={})]
    client = make_client(random_=random.Random(1234))

    result = client.ping()

    assert result.ok
    # base for attempt 1 = 0.5s; jitter is random.Random(1234).uniform(0.0, 0.25) deterministically.
    expected_jitter = random.Random(1234).uniform(0.0, 0.25)
    assert sleeps == [pytest.approx(0.5 + expected_jitter)]
    client.close()


# ---------------------------------------------------------------------------------
# M2: httpx/httpcore must never log a signed URL at INFO level
# ---------------------------------------------------------------------------------


@respx.mock
def test_configure_logging_info_never_leaks_signature_via_httpx_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression for M2: httpx logs "HTTP Request: GET <full url>" at INFO level through plain
    stdlib `logging`, which is a different pipeline from our structlog `redact_secrets`
    processor (tbot.monitoring.logging) -- that processor never sees these records. A signed
    call under an INFO-level root logger must not print its signature via httpx's own logger."""
    from tbot.monitoring.logging import configure_logging

    configure_logging("INFO")
    respx.get(f"{BASE_URL}{READ_PREFIX}/account").mock(
        return_value=httpx.Response(200, json={"balances": []})
    )
    client = make_client(api_key=SecretStr("my-api-key"), api_secret=SecretStr("s3cr3t-api-secret"))

    client.account()
    client.close()

    captured = capsys.readouterr()
    assert "signature=" not in captured.out
    assert "signature=" not in captured.err
    assert "s3cr3t-api-secret" not in captured.out
    assert "s3cr3t-api-secret" not in captured.err
