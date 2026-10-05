"""Thin, strictly read-only HTTP client for the Tabdeal REST API.

Scope (see the phase-0 "exchange-integrator" task brief this module was written under): only
public market-data reads (``ping``, ``time``, ``exchangeInfo``, ``depth``, ``trades``) and the
signed account-balances read (``account``) exist here. There is deliberately no order, cancel,
OCO, ``userDataStream`` or withdrawal surface -- not even an unused helper, constant or URL for
one. CLAUDE.md section 3.6 keeps the real order path unreachable until ``LIVE_TRADING=true`` and
config phase >= 6; this module simply does not grow the vocabulary needed to place an order.

Security invariants enforced in this file:

* The API secret is only ever used inside :func:`_sign` (``hmac.new`` over the url-encoded query
  string). It is never logged, returned or stored anywhere except the ``SecretStr`` handed in by
  the caller.
* The API key only ever becomes the ``X-MBX-APIKEY`` header value passed to ``httpx``. It is never
  part of a :class:`ProbeResult`.
* Every URL reported back to a caller has its ``signature`` query parameter stripped
  (:func:`_redact_url`) -- callers may safely print or persist ``ProbeResult.url``.
* :class:`ProbeResult` never carries request headers, only a filtered allow-list of *response*
  rate-limit-related headers (:data:`_RATE_LIMIT_HEADER_NAMES`).
* Every signed/unsigned request builds a **fresh** parameter dict (never a mutable default
  argument) -- this is the exact bug CLAUDE.md section 6 warns about in the official SDK.

Client-side protection: a :class:`TokenBucket` throttles outgoing requests to a configured
``requests_per_second``, and exponential backoff with jitter is applied on HTTP 429, 5xx and
timeouts (CLAUDE.md section 6: Tabdeal's rate limits are undocumented).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
import structlog
from pydantic import SecretStr

from tbot.core.types import Clock

logger = structlog.get_logger(__name__)

__all__ = [
    "ProbeResult",
    "RateLimitObservation",
    "RetryAttempt",
    "TabdealClient",
    "TabdealClientError",
    "TokenBucket",
]

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Allow-list of *response* headers that may be reported: none of these can ever contain a secret.
# MINOR-12: the X-MBX-ORDER-COUNT-* headers are order-placement counters -- they can never be
# populated at phase 0 (this client places no orders) and listing them here would be misleading.
_RATE_LIMIT_HEADER_NAMES: tuple[str, ...] = (
    "Retry-After",
    "X-MBX-USED-WEIGHT",
    "X-MBX-USED-WEIGHT-1M",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0
_BACKOFF_JITTER_SECONDS = 0.25

# m4 (reviewer finding): a server-named Retry-After is a floor on the wait (MINOR-9), but an
# uncapped one is a self-inflicted denial of service -- a single malicious or misconfigured
# response (e.g. "Retry-After: 86400") would otherwise make this client sleep for a full day on
# one HTTP call, with no way for a caller to notice or interrupt it. Cap it at a conservative
# ceiling; CLAUDE.md section 6 already caps recvWindow at 60000ms, so 60s keeps the same order of
# magnitude in mind for "how long is too long for one retry".
_RETRY_AFTER_CAP_SECONDS = 60.0

# MINOR-11 / M1: scrub a leaked ``signature=...`` query param out of any error string before it
# reaches ProbeResult.error -- e.g. an httpx exception's ``str()`` can embed the full request URL.
_SIGNATURE_IN_TEXT_RE = re.compile(r"signature=[^&\s]*")


def _scrub_signature(text: str) -> str:
    return _SIGNATURE_IN_TEXT_RE.sub("signature=[REDACTED]", text)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value (seconds, as Tabdeal/Binance-style APIs send it).

    Returns ``None`` if the header is absent or not a plain number (e.g. an HTTP-date, which
    this client does not need to support yet).
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class TabdealClientError(RuntimeError):
    """Raised for programmer errors, e.g. a signed call without credentials."""


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """One retry the client took before succeeding or giving up. ``attempt`` is 1-based."""

    attempt: int
    reason: str
    waited_seconds: float


@dataclass(frozen=True, slots=True)
class RateLimitObservation:
    """A single rate-limit-related response header, exactly as the server sent it."""

    header: str
    value: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one HTTP call. Never carries secrets, signatures or request headers."""

    method: str
    path: str
    url: str
    status_code: int | None
    latency_ms: float
    ok: bool
    error: str | None = None
    body: Any = None
    retries: tuple[RetryAttempt, ...] = ()
    rate_limit_headers: tuple[RateLimitObservation, ...] = ()


def _sign(secret: str, query_string: str) -> str:
    """HMAC-SHA256 signature over the url-encoded query string, hex-encoded."""
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _redact_url(url: httpx.URL) -> str:
    """Render ``url`` as a string with the ``signature`` query parameter removed."""
    pairs = parse_qsl(url.query.decode("ascii"), keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k != "signature"]
    base = f"{url.scheme}://{url.host}{url.path}"
    if not kept:
        return base
    return f"{base}?{urlencode(kept)}"


def _safe_json(response: httpx.Response) -> Any:
    """Parse the response body as JSON, routing every bare JSON number through ``Decimal``.

    httpx's own ``Response.json()`` parses numbers via the stdlib ``float`` constructor, which
    rounds a value like ``61234.56789012345`` to the nearest IEEE-754 double *before* anything
    downstream ever sees it -- ``Decimal(str(already_rounded_float))`` then preserves the wrong
    number forever. ``json.loads(..., parse_float=Decimal)`` instead hands ``Decimal`` the exact
    literal substring matched in the response text (not a pre-rounded float), so a price/qty
    expressed as a bare JSON number -- not a quoted string -- still arrives exact. Applies to
    every float literal in the document, however deeply nested (e.g. inside a depth response's
    ``bids``/``asks`` arrays).
    """
    try:
        return json.loads(response.text, parse_float=Decimal)
    except ValueError:
        return None


class TokenBucket:
    """Client-side token bucket.

    Elapsed time is measured exclusively through the injected :class:`Clock`, never a direct
    wall-clock read -- ``tbot.execution`` must stay deterministic-friendly per CLAUDE.md section 3.7
    / docs/SPEC.md section 2.1 ("no wall-clock reads in core/, risk/, execution/").

    MINOR-10: ``sleep`` is injectable so the only wall-clock-*adjacent* dependency left is the one
    the caller explicitly hands in (defaulting to the real ``time.sleep``) -- this module itself
    holds no bare module-level dependency on it, and tests can pass a deterministic fake.
    """

    def __init__(
        self,
        rate_per_second: float,
        clock: Clock,
        *,
        capacity: float | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._clock = clock
        self._tokens = self._capacity
        self._last = clock.now()
        self._sleep = sleep if sleep is not None else time.sleep

    def acquire(self) -> float:
        """Consume one token, sleeping first if none is available. Returns seconds slept."""
        now = self._clock.now()
        elapsed = max(0.0, (now - self._last).total_seconds())
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self._rate
        self._sleep(wait)
        self._tokens = 0.0
        self._last = self._clock.now()
        return wait


class TabdealClient:
    """Thin httpx wrapper over Tabdeal's Binance-style REST API -- reads only.

    ``prefix`` on every method defaults to the client's configured read prefix but can be
    overridden per call. CLAUDE.md section 6 documents writes under ``/api/v1`` and reads
    (including signed reads) under ``/r/api/v1``, but does not pin down which prefix *public*
    reads use -- callers (the probe script) can and should try both rather than assume.
    """

    def __init__(
        self,
        *,
        base_url: str,
        read_prefix: str,
        write_prefix: str,
        clock: Clock,
        api_key: SecretStr | None = None,
        api_secret: SecretStr | None = None,
        recv_window_ms: int = 5_000,
        requests_per_second: float = 5.0,
        timeout_seconds: float = 10.0,
        max_retries: int = 5,
        sleep: Callable[[float], None] | None = None,
        random_: random.Random | None = None,
    ) -> None:
        # MINOR-8: this client is directly constructible without going through ExchangeConfig
        # (whose own Field(..., le=60_000) would otherwise be the only guard) -- CLAUDE.md
        # section 6 caps recvWindow at 60000ms, so enforce it here too.
        if not 0 < recv_window_ms <= 60_000:
            raise ValueError(f"recv_window_ms must be in (0, 60000], got {recv_window_ms}")
        self._read_prefix = read_prefix
        self._write_prefix = write_prefix
        self._clock = clock
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        # MINOR-10: inject sleep + a private Random instance so backoff is deterministic in
        # tests and this module holds no wall-clock or global-random dependency of its own.
        self._sleep = sleep if sleep is not None else time.sleep
        self._rng = random_ if random_ is not None else random.Random()
        self._bucket = TokenBucket(requests_per_second, clock, sleep=self._sleep)
        self._http = httpx.Client(base_url=base_url, timeout=timeout_seconds)
        # M2: plain httpx/httpcore logging is stdlib `logging`, not structlog -- our
        # `redact_secrets` structlog processor (tbot.monitoring.logging) never sees these
        # records, and httpx logs the full request URL (including a signed call's `signature=`
        # query param) at INFO level. Silence both loggers here, unconditionally, regardless of
        # whatever level the process's root logger is configured at.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    @property
    def base_url(self) -> str:
        return str(self._http.base_url)

    @property
    def read_prefix(self) -> str:
        return self._read_prefix

    @property
    def write_prefix(self) -> str:
        return self._write_prefix

    @property
    def has_credentials(self) -> bool:
        return self._api_key is not None and self._api_secret is not None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TabdealClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- public reads -----------------------------------------------------------------

    def ping(self, *, prefix: str | None = None) -> ProbeResult:
        return self._get(f"{prefix or self._read_prefix}/ping", lambda: {})

    def server_time(self, *, prefix: str | None = None) -> ProbeResult:
        return self._get(f"{prefix or self._read_prefix}/time", lambda: {})

    def exchange_info(self, *, prefix: str | None = None) -> ProbeResult:
        return self._get(f"{prefix or self._read_prefix}/exchangeInfo", lambda: {})

    def depth(self, symbol: str, *, limit: int = 100, prefix: str | None = None) -> ProbeResult:
        return self._get(
            f"{prefix or self._read_prefix}/depth", lambda: {"symbol": symbol, "limit": str(limit)}
        )

    def trades(self, symbol: str, *, limit: int = 500, prefix: str | None = None) -> ProbeResult:
        return self._get(
            f"{prefix or self._read_prefix}/trades", lambda: {"symbol": symbol, "limit": str(limit)}
        )

    # -- signed read (balances only) --------------------------------------------------

    def account(self, *, prefix: str | None = None) -> ProbeResult:
        """Signed balances read. Raises if credentials are missing -- callers must check first.

        M1: the params (and therefore the signature) are built by a *factory*, called fresh on
        every attempt inside ``_get`` -- not once up front. A stale ``timestamp``/``signature``
        resent on retry would be rejected by the exchange (recvWindow expired) even though the
        original request was still perfectly valid; re-signing on every attempt is the fix.
        """
        if self._api_key is None or self._api_secret is None:
            raise TabdealClientError("account() requires api_key and api_secret")
        api_secret = self._api_secret

        def _build_signed_params() -> dict[str, str]:
            params: dict[str, str] = {
                "timestamp": str(int(self._clock.now().timestamp() * 1000)),
                "recvWindow": str(self._recv_window_ms),
            }
            query_string = urlencode(params)
            params["signature"] = _sign(api_secret.get_secret_value(), query_string)
            return params

        return self._get(f"{prefix or self._read_prefix}/account", _build_signed_params, signed=True)

    # -- internals ----------------------------------------------------------------------

    def _backoff_wait(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Exponential backoff with jitter. ``attempt`` is 1-based.

        MINOR-9: when the server names a ``Retry-After``, that is a floor, not a suggestion --
        never wait less than it, even if the computed exponential backoff would be shorter.
        m4: that floor is itself capped at :data:`_RETRY_AFTER_CAP_SECONDS` -- an uncapped
        server-named wait is a self-inflicted denial of service (see the constant's docstring).
        """
        base: float = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        jitter: float = self._rng.uniform(0.0, _BACKOFF_JITTER_SECONDS)
        wait = base + jitter
        if retry_after is not None:
            if retry_after > _RETRY_AFTER_CAP_SECONDS:
                logger.warning(
                    "tabdeal_client.retry_after_capped",
                    requested_seconds=retry_after,
                    cap_seconds=_RETRY_AFTER_CAP_SECONDS,
                )
            capped_retry_after = min(retry_after, _RETRY_AFTER_CAP_SECONDS)
            if capped_retry_after > wait:
                wait = capped_retry_after
        return wait

    @staticmethod
    def _finalize(
        path: str,
        url: str,
        *,
        status_code: int | None,
        latency_ms: float,
        ok: bool,
        error: str | None,
        body: Any = None,
        attempts: list[RetryAttempt],
        rate_limit_headers: list[RateLimitObservation],
    ) -> ProbeResult:
        """Build the terminal :class:`ProbeResult` for one call -- whichever of the three ways
        ``_get`` can finish (timeout retries exhausted, an unreachable/connection error, or an
        actual HTTP response). m12 (reviewer finding): these three outcomes used to each build
        their own near-identical ``ProbeResult(...)`` literal; folding them into one helper means
        the ``retries``/``rate_limit_headers`` tuple-conversion and the ``method="GET"`` constant
        exist in exactly one place.
        """
        return ProbeResult(
            method="GET",
            path=path,
            url=url,
            status_code=status_code,
            latency_ms=latency_ms,
            ok=ok,
            error=error,
            body=body,
            retries=tuple(attempts),
            rate_limit_headers=tuple(rate_limit_headers),
        )

    def _retry_after_waiting(
        self,
        attempt_no: int,
        reason: str,
        attempts: list[RetryAttempt],
        *,
        retry_after: float | None = None,
    ) -> None:
        """Compute the backoff wait, record it as a :class:`RetryAttempt`, and sleep.

        Shared by the timeout and the retryable-HTTP-status branches of :meth:`_attempt` so the
        "append then sleep" sequence exists in exactly one place.
        """
        wait = self._backoff_wait(attempt_no, retry_after=retry_after)
        attempts.append(RetryAttempt(attempt_no, reason, wait))
        self._sleep(wait)

    def _send_or_retry(
        self,
        path: str,
        headers: dict[str, str],
        params_factory: Callable[[], dict[str, str]],
        attempt_no: int,
        attempts: list[RetryAttempt],
        rate_limit_headers: list[RateLimitObservation],
    ) -> tuple[httpx.Response, float] | ProbeResult | None:
        """Build and send one request -- the part of :meth:`_attempt` that can fail before any
        HTTP response exists at all.

        Returns ``(response, latency_ms)`` on an actual HTTP response (whatever its status),
        a terminal :class:`ProbeResult` if nothing here can retry (timeout retries exhausted, or
        any other connection-level error), or ``None`` if a timeout was retried -- having already
        appended to ``attempts`` and slept.
        """
        # M1 + MINOR-7: a fresh params dict (and, for signed calls, a fresh signature) every
        # attempt -- never reused across retries. The query string is built here, once, by hand
        # (`urlencode`) and sent as literal request-target bytes rather than handed to httpx as a
        # dict for it to re-encode: those must be the exact bytes that were signed.
        params = params_factory()
        query_string = urlencode(params)
        request_target = f"{path}?{query_string}" if query_string else path
        request = self._http.build_request("GET", request_target, headers=dict(headers))
        redacted_url = _redact_url(request.url)
        start = self._clock.now()
        try:
            response = self._http.send(request)
        except httpx.TimeoutException as exc:
            latency_ms = (self._clock.now() - start).total_seconds() * 1000
            if attempt_no > self._max_retries:
                return self._finalize(
                    path,
                    redacted_url,
                    status_code=None,
                    latency_ms=latency_ms,
                    ok=False,
                    error=_scrub_signature(f"timeout: {exc.__class__.__name__}"),
                    attempts=attempts,
                    rate_limit_headers=rate_limit_headers,
                )
            self._retry_after_waiting(attempt_no, "timeout", attempts)
            return None
        except httpx.HTTPError as exc:
            latency_ms = (self._clock.now() - start).total_seconds() * 1000
            return self._finalize(
                path,
                redacted_url,
                status_code=None,
                latency_ms=latency_ms,
                ok=False,
                error=_scrub_signature(f"unreachable: {exc.__class__.__name__}: {exc}"),
                attempts=attempts,
                rate_limit_headers=rate_limit_headers,
            )
        latency_ms = (self._clock.now() - start).total_seconds() * 1000
        return response, latency_ms

    def _attempt(
        self,
        path: str,
        headers: dict[str, str],
        params_factory: Callable[[], dict[str, str]],
        attempt_no: int,
        attempts: list[RetryAttempt],
        rate_limit_headers: list[RateLimitObservation],
    ) -> ProbeResult | None:
        """One HTTP attempt for :meth:`_get`.

        Returns a terminal :class:`ProbeResult` (timeout retries exhausted, an
        unreachable/connection error, or an actual HTTP response that is not itself being
        retried), or ``None`` if this attempt decided to retry -- having already appended to
        ``attempts`` and slept, so the caller's loop can simply try again.
        """
        sent = self._send_or_retry(path, headers, params_factory, attempt_no, attempts, rate_limit_headers)
        if sent is None or isinstance(sent, ProbeResult):
            return sent
        response, latency_ms = sent

        for name in _RATE_LIMIT_HEADER_NAMES:
            if name in response.headers:
                rate_limit_headers.append(RateLimitObservation(name, response.headers[name]))

        if response.status_code in _RETRYABLE_STATUS and attempt_no <= self._max_retries:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            self._retry_after_waiting(
                attempt_no, str(response.status_code), attempts, retry_after=retry_after
            )
            return None

        return self._finalize(
            path,
            _redact_url(response.url),
            status_code=response.status_code,
            latency_ms=latency_ms,
            ok=response.is_success,
            error=None if response.is_success else _scrub_signature(f"HTTP {response.status_code}"),
            body=_safe_json(response),
            attempts=attempts,
            rate_limit_headers=rate_limit_headers,
        )

    def _get(
        self, path: str, params_factory: Callable[[], dict[str, str]], *, signed: bool = False
    ) -> ProbeResult:
        headers: dict[str, str] = {}
        if signed:
            if self._api_key is None:
                raise TabdealClientError("signed request requires api_key")
            headers["X-MBX-APIKEY"] = self._api_key.get_secret_value()

        attempts: list[RetryAttempt] = []
        rate_limit_headers: list[RateLimitObservation] = []
        attempt_no = 0
        while True:
            attempt_no += 1
            self._bucket.acquire()
            result = self._attempt(path, headers, params_factory, attempt_no, attempts, rate_limit_headers)
            if result is not None:
                return result
