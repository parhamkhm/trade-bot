"""Public-only LBank G0 probe (docs/SPEC.md section 7, "G0 -- Foundations (LBank, D-073)").

Strictly public market-data reads. No API key, no signature, no private endpoint, no account or
trading endpoint of any kind: every request is an HTTP GET to one of the ten URLs in ``ENDPOINTS``
below, and ``tests/scripts/test_lbank_probe.py`` asserts that nothing else is ever requested.

Three modes (mutually exclusive), all driven from the command line::

    # 1. Sampling (default): 32 samples, 12 min apart => 31 x 12 min = 6.2 h >= the G0 6 h minimum.
    uv run python scripts/lbank_probe.py [--samples 32] [--interval 720] [--out-dir data/lbank_probe]
        [--request-gap 0.6] [--no-kline-history]

    # 2. Rate-limit discovery: run ONCE, never together with a sampling run.
    uv run python scripts/lbank_probe.py --ratelimit [--ratelimit-endpoint depth]
        [--ratelimit-steps 1 2 3 5] [--step-seconds 30] [--pause-seconds 30]

    # 3. Offline: fixture run through a fake transport (no network), or rebuild a summary from JSONL.
    uv run python scripts/lbank_probe.py --selftest [--out-dir DIR]
    uv run python scripts/lbank_probe.py --summarize data/lbank_probe/lbank_probe_<stamp>.jsonl

Per sample it records, for the BTC/USDT spot pair (``btc_usdt``): HTTP status, latency (monotonic
clock) and payload health of ``timestamp``, ``accuracy``, ``depth``, ``supplement/trades``, ``kline``
(1d, 4h, 1h, 1m), ``ticker/24hr`` and the perpetual ``marketData``; the server-minus-local clock
delta; the best bid/ask spread; depth within 0.1 / 0.5 / 1 % of mid per side; and the largest market
order that fills inside 0.1 % and 0.5 % slippage when walking the book (both sides). The first sample
is followed once by a kline-history probe (how many bars one request returns, how far back the
history goes, paging rules) and the first sample is preceded by a depth-size discovery.

Outputs (all embed provenance: hostname, UTC start/end, python version, git commit, "measured from
<hostname> at <time>", and whether the data are live or an offline fixture):

* ``<stem>.jsonl``        raw, append-only, fsynced after every sample (first line = provenance);
* ``<stem>.summary.json`` machine-readable summary incl. the G0 pass/fail evaluation;
* ``<stem>.md``           English markdown tables (the Persian report is written from this).

Honest limits of what this script can know:

* Local clock accuracy: the script cannot tell whether the host clock is NTP-synced. The operator
  must record ``chronyc tracking`` / ``timedatectl`` next to the run. The reported server-minus-local
  delta carries an uncertainty of about half the round-trip time (LBank answers in 1-8 s from the
  Turkey server, so this matters), reported alongside as ``median_half_rtt_ms``.
* Latencies measured here are from the machine that runs the script, at that moment.
* "Largest fill" is computed from the visible levels only; where the visible book ends inside the
  band the figure is a lower bound and is flagged ``truncated``.

Pre-registered G0 interpretation (fixed before any data is seen; see docs/SPEC.md section 7 / D-073):

* Per-request timeout: 20 s for ``supplement/trades`` (LBank's own notes say it can be the slowest
  public call), 10 s for every other endpoint. A timeout counts as a non-200 response.
* "Every endpoint answers 200" passes when EVERY whitelisted endpoint individually sees HTTP 200 with
  a valid payload on at least 95 % of its sampled requests, AND no ban-type response (429/403/418)
  ever occurs at any point in the run. The per-endpoint rate alone is not enough -- a single ban-type
  response fails this condition even if the overall rate would still clear 95 % -- because sampling
  mode stops the entire run the instant one is seen (see below), so in practice this half of the
  condition is really "did the run survive to the end without being banned".
* The clock-offset condition passes when BOTH ``|delta_at_min_rtt_ms| < 1000`` AND
  ``median |delta| < 1000``. When the RTT-uncertainty band (``median |delta| +/- median half-RTT``)
  straddles the 1000 ms line, the result is reported as "inconclusive" (counted as not-passed, with
  that reason) rather than guessed at either way.

Sampling-mode ban handling: every response is routed through one shared :class:`StopTracker`
(``ban_only=True``: it reacts only to 429/403/418, a burst of 3 consecutive 5xx, or a burst of 3
consecutive transport errors -- an isolated 4xx or payload error is left to the 95 % per-endpoint
tolerance above, since stopping the whole run on the very first one of those would make that
tolerance meaningless). The instant it trips, the triggering response is recorded, NO further
request of any kind is made (no more of the current sample, no depth-size/kline-history discovery,
no further samples), and the run finishes with verdict FAIL and the reason.

Exit codes: 0 = G0 PASS (or the rate-limit run completed without incident), 2 = G0 FAIL,
3 = G0 INCOMPLETE (sampling plan not met; this also covers zero samples -- an empty run is
inconclusive, not a failure), 4 = rate-limit run stopped early on a throttling/ban-type response,
5 = rate-limit run stopped early because a step's achieved rate fell short of 90 % of the requested
rate (latency-limited -- the client is strictly serial and never adds concurrency to try to reach
it), 1 = exchange unreachable, 130 / 143 = interrupted by SIGINT / SIGTERM (partial outputs are
still written).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from types import FrameType
from typing import Any, Literal, Protocol

import httpx

from tbot.data.depth import (
    best_bid_ask,
    cumulative_depth,
    median_decimal,
    parse_depth_levels,
    percentile_decimal,
    spread_bps_and_pct,
)
from tbot.data.lbank_recorder import PERP_BASE_URL, SPOT_BASE_URL, parse_funding, parse_trades

__all__ = [
    "ENDPOINTS",
    "BanDetectedError",
    "CheckedFetcher",
    "Env",
    "Fetcher",
    "FillResult",
    "HttpResult",
    "KlineRowError",
    "StopTracker",
    "VirtualEnv",
    "analyze_book",
    "clock_delta_ms",
    "collect_provenance",
    "evaluate_accuracy_row",
    "evaluate_g0",
    "fixture_transport",
    "main",
    "max_fill_within_slippage",
    "percentile",
    "run_ratelimit",
    "run_sampling",
    "spread_pct_of",
]

USER_AGENT = "tbot-lbank-g0-probe/1 (public read-only market-data probe; contact: project owner)"
SYMBOL = "btc_usdt"
PERP_SYMBOL = "BTCUSDT"
REQUEST_TIMEOUT_S = 10.0  # default per-request timeout; supplement/trades overrides to TRADES_TIMEOUT_S
TRADES_TIMEOUT_S = 20.0  # pre-registered (item 3): supplement/trades is the one endpoint given 20 s
MAX_RATE_RPS = 5.0  # hard ceiling, never exceeded in any mode
MIN_REQUEST_GAP_S = 1.0 / MAX_RATE_RPS
DEFAULT_REQUEST_GAP_S = 0.6  # sampling: ~1.7 req/s at most inside one sample
DEPTH_SIZE_CANDIDATES: tuple[int, ...] = (200, 100, 60, 20)
KLINE_HISTORY_SIZE = 2000
KLINE_SAMPLE_BARS = 10
EARLIEST_PROBE_TS_S = 1_483_228_800  # 2017-01-01 UTC, far before any LBank BTC/USDT history
TRADES_SIZE = 600
BODY_SNIPPET_CHARS = 2000

# Pre-registered G0 thresholds (item 3): a per-endpoint success rate below this, or a rate-limit
# step whose achieved rate falls short of this fraction of its target, changes the verdict.
ENDPOINT_SUCCESS_MIN_RATE = 0.95
RATE_ACHIEVED_MIN_FRACTION = 0.9

# docs/SPEC.md D-068: the LBank recorder (src/tbot/data/lbank_recorder.py) runs continuously on the
# same host/IP as this probe. Its own steady-state request rate, from its default polling schedule
# (depth every 10 s, trades every 30 s, funding every 60 s, klines every 60/600/3600 s), sums to
# approximately 0.15 req/s -- traffic this probe's rate-limit ceiling does not include or control.
RECORDER_SHARED_IP_RPS = 0.15

# timeframe label -> (LBank kline ``type``, bar seconds)
KLINES: dict[str, tuple[str, int]] = {
    "1d": ("day1", 86_400),
    "4h": ("hour4", 14_400),
    "1h": ("hour1", 3_600),
    "1m": ("minute1", 60),
}

BANDS: tuple[Decimal, ...] = (Decimal("0.001"), Decimal("0.005"), Decimal("0.01"))
SLIPPAGES: tuple[Decimal, ...] = (Decimal("0.001"), Decimal("0.005"))

# G0 thresholds, docs/SPEC.md section 7 (pre-registered; never relax after seeing data).
G0_MIN_SAMPLES = 30
G0_MIN_SPAN_S = 6 * 3600
G0_MAX_MEDIAN_SKEW_MS = 1000.0
G0_MAX_MEDIAN_SPREAD_PCT = 0.20

# Throttling / ban-type reactions.
BAN_STATUSES = (403, 418)
BAN_LIKE_STATUSES = (429, *BAN_STATUSES)
MAX_CONSECUTIVE_5XX = 3
MAX_CONSECUTIVE_TRANSPORT_ERRORS = 3

# item 7: files whose working-tree state marks the reported commit "-dirty". Anything these files
# read or re-export (tbot.data.depth's Decimal maths, tbot.data.lbank_recorder's parsers/base URLs)
# can change this script's measured output without scripts/lbank_probe.py itself changing.
DIRTY_CHECK_PATHS: tuple[str, ...] = (
    "scripts/lbank_probe.py",
    "src/tbot/data/depth.py",
    "src/tbot/data/lbank_recorder.py",
)

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class EndpointSpec:
    """One whitelisted public GET endpoint."""

    name: str
    base: str
    path: str
    timeout_s: float = REQUEST_TIMEOUT_S


# The complete request whitelist. Nothing else is ever requested by this script.
ENDPOINTS: dict[str, EndpointSpec] = {
    "timestamp": EndpointSpec("timestamp", SPOT_BASE_URL, "/v2/timestamp.do"),
    "accuracy": EndpointSpec("accuracy", SPOT_BASE_URL, "/v2/accuracy.do"),
    "depth": EndpointSpec("depth", SPOT_BASE_URL, "/v2/depth.do"),
    "trades": EndpointSpec("trades", SPOT_BASE_URL, "/v2/supplement/trades.do", TRADES_TIMEOUT_S),
    "kline": EndpointSpec("kline", SPOT_BASE_URL, "/v2/kline.do"),
    "ticker24hr": EndpointSpec("ticker24hr", SPOT_BASE_URL, "/v2/ticker/24hr.do"),
    "perp_marketdata": EndpointSpec("perp_marketdata", PERP_BASE_URL, "/cfd/openApi/v1/pub/marketData"),
}

RATELIMIT_ENDPOINTS: dict[str, tuple[str, dict[str, Any]]] = {
    "depth": ("depth", {"symbol": SYMBOL, "size": 20}),
    "timestamp": ("timestamp", {}),
    "ticker": ("ticker24hr", {"symbol": SYMBOL}),
    "trades": ("trades", {"symbol": SYMBOL, "size": 100}),
}


# ---------------------------------------------------------------------------------
# environment (injectable clocks, so tests and --selftest never really sleep)
# ---------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Env:
    """Wall clock, monotonic clock and sleep. Production uses the real ones."""

    now: Callable[[], datetime] = _utcnow
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


class VirtualEnv(Env):
    """Deterministic virtual time: ``sleep`` advances the clock instead of waiting."""

    def __init__(self, epoch0: datetime | None = None) -> None:
        self.t = 0.0
        self.epoch0 = epoch0 or datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
        super().__init__(now=self._now, monotonic=self._monotonic, sleep=self._sleep)

    def _now(self) -> datetime:
        return self.epoch0 + timedelta(seconds=self.t)

    def _monotonic(self) -> float:
        return self.t

    def _sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


# ---------------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------------


def dstr(value: Decimal) -> str:
    """Plain (non-exponent) decimal string."""
    return format(value, "f")


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in [0, 1]; ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    pos = min(max(q, 0.0), 1.0) * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _safe_decimal(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return dstr(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, default=_json_default, indent=indent, separators=None if indent else (",", ":"))


def _loads(content: bytes) -> Any:
    """JSON with floats as Decimal so prices never pass through binary floating point."""
    return json.loads(content, parse_float=Decimal)


def _iso(ts_s: int | float | None) -> str | None:
    return datetime.fromtimestamp(ts_s, tz=UTC).isoformat() if ts_s is not None else None


# ---------------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------------


def resolve_git_commit() -> str:
    """Script git commit: env ``TBOT_GIT_COMMIT``, else ``git rev-parse HEAD``; ``unknown`` if absent.

    The dirty check covers :data:`DIRTY_CHECK_PATHS` -- not just this script -- since a change to
    the parsing/maths helpers it imports from ``tbot.data.depth`` or ``tbot.data.lbank_recorder``
    can change this probe's measured output just as much as editing the script itself would.
    """
    from_env = os.environ.get("TBOT_GIT_COMMIT", "").strip()
    if from_env:
        return from_env
    repo = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *DIRTY_CHECK_PATHS],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not head:
        return "unknown"
    return f"{head}-dirty" if dirty else head


def collect_provenance(
    env: Env,
    mode: str,
    argv: Sequence[str],
    *,
    data_source: Literal["live", "offline_fixture"] = "live",
    host_label: str | None = None,
) -> dict[str, Any]:
    """``host_label`` overrides ``socket.gethostname()`` -- item 7: inside a Docker container the
    kernel hostname is the container id, not anything a human running the probe would recognise;
    the caller (e.g. the entrypoint script) can pass the real host name instead."""
    started = env.now()
    host = host_label if host_label else socket.gethostname()
    git_commit = resolve_git_commit()
    return {
        "hostname": host,
        "utc_start": started.isoformat(),
        "utc_end": None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "script": "scripts/lbank_probe.py",
        "git_commit": git_commit,
        "git_commit_unknown": git_commit == "unknown",
        "mode": mode,
        "argv": list(argv),
        "user_agent": USER_AGENT,
        "data_source": data_source,
        "measured_from": f"measured from {host} at {started.isoformat()}",
        "local_clock_sync": (
            "NOT checked by this script - the operator records chrony/timedatectl status separately"
        ),
    }


# ---------------------------------------------------------------------------------
# HTTP layer: one request in flight, hard rate ceiling, never raises on network errors
# ---------------------------------------------------------------------------------

_SENSITIVE_HEADERS = frozenset({"set-cookie", "authorization", "cookie", "proxy-authorization"})


def payload_status(body: Any) -> tuple[bool, str | None]:
    """Whether an LBank body is a success payload (LBank answers HTTP 200 even for errors)."""
    if isinstance(body, list):
        return True, None
    if not isinstance(body, dict):
        return False, "body is not a JSON object or list"
    result = body.get("result")
    if result is not None and str(result).lower() != "true":
        return False, f"result={result} error_code={body.get('error_code')} msg={body.get('msg')}"
    success = body.get("success")
    if success is not None and success is not True and str(success).lower() != "true":
        return False, f"success={success} msg={body.get('msg')}"
    code = body.get("error_code")
    if code not in (None, 0, Decimal(0)):
        return False, f"error_code={code} msg={body.get('msg')}"
    return True, None


@dataclass
class HttpResult:
    endpoint: str
    url: str
    params: dict[str, Any]
    sent_at: datetime
    received_at: datetime
    status: int | None
    latency_ms: float
    nbytes: int
    error: str | None
    body: Any
    payload_ok: bool
    payload_error: str | None
    timeout_s: float = REQUEST_TIMEOUT_S
    headers: dict[str, str] = field(default_factory=dict)
    body_snippet: str | None = None

    @property
    def http_ok(self) -> bool:
        return self.status == 200

    @property
    def ok(self) -> bool:
        return self.http_ok and self.payload_ok and self.error is None

    @property
    def is_timeout(self) -> bool:
        return self.error == "timeout"

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "endpoint": self.endpoint,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "timeout_s": self.timeout_s,
            "nbytes": self.nbytes,
            "ok": self.ok,
            "error": self.error,
            "payload_error": self.payload_error,
        }
        return out


class Fetcher:
    """Single-threaded GET client: at most one request in flight, request starts at least
    ``min_gap_s`` apart (never above ``MAX_RATE_RPS``), 10 s timeouts, no retries, no raising."""

    def __init__(
        self,
        env: Env,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
        min_gap_s: float = DEFAULT_REQUEST_GAP_S,
    ) -> None:
        self._env = env
        self._min_gap_s = max(min_gap_s, MIN_REQUEST_GAP_S)
        self._last_start: float | None = None
        self._client = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": USER_AGENT}, transport=transport, follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_start is not None:
            wait = self._min_gap_s - (self._env.monotonic() - self._last_start)
            if wait > 0:
                self._env.sleep(wait)
        self._last_start = self._env.monotonic()

    def get(self, endpoint: str, spec: EndpointSpec, params: Mapping[str, Any] | None = None) -> HttpResult:
        self._pace()
        url = spec.base + spec.path
        sent_at = self._env.now()
        started = self._env.monotonic()
        status: int | None = None
        error: str | None = None
        content = b""
        headers: dict[str, str] = {}
        try:
            response = self._client.get(url, params=dict(params or {}), timeout=spec.timeout_s)
            status, content = response.status_code, response.content
            headers = {k: v for k, v in response.headers.items() if k.lower() not in _SENSITIVE_HEADERS}
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        latency_ms = (self._env.monotonic() - started) * 1000
        received_at = self._env.now()
        body: Any = None
        payload_ok, payload_error = False, error
        if status is not None:
            try:
                body = _loads(content)
                payload_ok, payload_error = payload_status(body)
            except ValueError:
                error, payload_error = "invalid_json", "response is not valid JSON"
        snippet = None
        if status is not None and status != 200:
            snippet = content.decode("utf-8", errors="replace")[:BODY_SNIPPET_CHARS]
        return HttpResult(
            endpoint=endpoint,
            url=url,
            params=dict(params or {}),
            sent_at=sent_at,
            received_at=received_at,
            status=status,
            latency_ms=latency_ms,
            nbytes=len(content),
            error=error,
            body=body,
            payload_ok=payload_ok,
            payload_error=payload_error,
            timeout_s=spec.timeout_s,
            headers=headers,
            body_snippet=snippet,
        )


class FetchLike(Protocol):
    """What :class:`SampleRunner` and the discovery helpers need from a fetcher -- satisfied by
    both :class:`Fetcher` itself and :class:`CheckedFetcher` (sampling mode's ban-aware wrapper)."""

    def get(
        self, endpoint: str, spec: EndpointSpec, params: Mapping[str, Any] | None = None
    ) -> HttpResult: ...


class BanDetectedError(Exception):
    """Raised by :class:`CheckedFetcher`.get() the instant its :class:`StopTracker` trips.

    Carries the triggering :class:`HttpResult` so the caller can record exactly what was seen.
    """

    def __init__(self, reason: str, result: HttpResult) -> None:
        super().__init__(reason)
        self.reason = reason
        self.result = result


class CheckedFetcher:
    """Wraps a :class:`Fetcher` so every response passes through one shared :class:`StopTracker`
    before anything else sees it -- the single choke point item 1 requires. The instant the
    tracker trips, ``get()`` raises :class:`BanDetectedError` instead of returning, so a caller that
    does nothing special (``SampleRunner``, ``discover_depth_size``, ``probe_kline_history`` all
    just call ``fetcher.get(...)`` exactly as they would on a plain :class:`Fetcher`) makes no
    further request once tripped: the exception unwinds the call stack instead of a result
    coming back to act on.
    """

    def __init__(self, fetcher: Fetcher, tracker: StopTracker) -> None:
        self._fetcher = fetcher
        self.tracker = tracker

    def get(self, endpoint: str, spec: EndpointSpec, params: Mapping[str, Any] | None = None) -> HttpResult:
        res = self._fetcher.get(endpoint, spec, params)
        reason = self.tracker.observe(res)
        if reason is not None:
            raise BanDetectedError(reason, res)
        return res


# ---------------------------------------------------------------------------------
# order-book maths (pure, Decimal)
# ---------------------------------------------------------------------------------

Levels = list[tuple[Decimal, Decimal]]


def spread_pct_of(bids: Levels, asks: Levels) -> float | None:
    """Best bid/ask spread as a percentage of mid; ``None`` for an empty side or a crossed book."""
    best = best_bid_ask(bids, asks)
    if best is None:
        return None
    try:
        return spread_bps_and_pct(*best)[1]
    except ValueError:
        return None


def depth_within(
    levels: Levels, mid: Decimal, threshold: Decimal, side: Literal["bid", "ask"]
) -> dict[str, Any]:
    """Base qty and quote notional within ``threshold`` (fraction) of mid on one side.

    ``truncated`` is true when the furthest visible level is still inside the band, i.e. the
    figure is a lower bound because the response ended before the band did.
    """
    base, quote = cumulative_depth(levels, mid, threshold, side=side)
    truncated = False
    if levels:
        bound = mid * threshold
        far = min(p for p, _ in levels) if side == "bid" else max(p for p, _ in levels)
        truncated = (mid - far if side == "bid" else far - mid) <= bound
    return {"base_qty": dstr(base), "quote": dstr(quote), "truncated": truncated}


@dataclass(frozen=True)
class FillResult:
    base_qty: Decimal
    quote: Decimal
    vwap: Decimal | None
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_qty": dstr(self.base_qty),
            "quote": dstr(self.quote),
            "vwap": dstr(self.vwap) if self.vwap is not None else None,
            "truncated": self.truncated,
        }


def max_fill_within_slippage(levels: Levels, side: Side, reference: Decimal, slippage: Decimal) -> FillResult:
    """Largest market order (in base units) whose volume-weighted fill price stays within
    ``slippage`` (a fraction) of ``reference`` while walking ``levels`` (asks for ``buy``, bids
    for ``sell``).

    Exact, not iterative: levels up to the limit price ``L`` are taken whole. A level beyond ``L``
    can still be partly filled while the running VWAP stays at or below ``L``: the amount that
    brings it exactly to ``L`` is ``(L*B - C) / (p - L)`` (``B`` base so far, ``C`` quote so far,
    ``p`` the level price). If the level is smaller than that it is taken whole and the walk goes
    on; otherwise the walk stops there. ``truncated`` means the visible book ended before the
    limit was reached (the figure is a lower bound).
    """
    buy = side == "buy"
    ordered = sorted((lv for lv in levels if lv[1] > 0), key=lambda lv: lv[0], reverse=not buy)
    limit = reference * (1 + slippage) if buy else reference * (1 - slippage)
    base = quote = Decimal(0)
    stopped = False
    for price, qty in ordered:
        within = price <= limit if buy else price >= limit
        if within:
            take = qty
        else:
            headroom = (limit * base - quote) if buy else (quote - limit * base)
            gap = (price - limit) if buy else (limit - price)
            take = min(qty, headroom / gap) if headroom > 0 else Decimal(0)
            stopped = take < qty  # a level taken whole leaves headroom for the next, pricier one
        base += take
        quote += take * price
        if stopped:
            break
    vwap = quote / base if base > 0 else None
    return FillResult(base, quote, vwap, truncated=bool(ordered) and not stopped)


def analyze_book(bids: Levels, asks: Levels) -> dict[str, Any]:
    """Spread, depth bands and max-fill sizes for one order-book snapshot."""
    best = best_bid_ask(bids, asks)
    if best is None:
        return {"ok": False, "reason": "empty_side", "bid_levels": len(bids), "ask_levels": len(asks)}
    best_bid, best_ask = best
    try:
        bps, pct = spread_bps_and_pct(best_bid, best_ask)
    except ValueError:
        return {"ok": False, "reason": "crossed_book", "best_bid": dstr(best_bid), "best_ask": dstr(best_ask)}
    mid = (best_bid + best_ask) / 2
    depth = {
        str(band): {
            "bid": depth_within(bids, mid, band, "bid"),
            "ask": depth_within(asks, mid, band, "ask"),
        }
        for band in BANDS
    }
    max_fill = {
        str(slip): {
            "vs_mid": {
                "buy": max_fill_within_slippage(asks, "buy", mid, slip).as_dict(),
                "sell": max_fill_within_slippage(bids, "sell", mid, slip).as_dict(),
            },
            "vs_touch": {
                "buy": max_fill_within_slippage(asks, "buy", best_ask, slip).as_dict(),
                "sell": max_fill_within_slippage(bids, "sell", best_bid, slip).as_dict(),
            },
        }
        for slip in SLIPPAGES
    }
    return {
        "ok": True,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "best_bid": dstr(best_bid),
        "best_ask": dstr(best_ask),
        "mid": dstr(mid),
        "spread_bps": bps,
        "spread_pct": pct,
        "depth": depth,
        "max_fill": max_fill,
    }


def parse_book(body: Any) -> tuple[Levels, Levels]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ValueError("depth data is not an object")
    return parse_depth_levels(data.get("bids")), parse_depth_levels(data.get("asks"))


# ---------------------------------------------------------------------------------
# endpoint-specific parsing (pure)
# ---------------------------------------------------------------------------------


def parse_server_time_ms(body: Any) -> int | None:
    """Server time in ms from a ``timestamp`` body (seconds are scaled up, by magnitude)."""
    data = body.get("data") if isinstance(body, dict) else None
    value = _safe_decimal(data)
    if value is None or value <= 0:
        return None
    return int(value * 1000) if value < Decimal(10**11) else int(value)


def clock_delta_ms(server_ms: int, sent_at: datetime, received_at: datetime) -> float:
    """Server time minus the local wall-clock midpoint of the request, in ms (positive: server ahead)."""
    midpoint_ms = (sent_at.timestamp() + received_at.timestamp()) / 2 * 1000
    return float(server_ms) - midpoint_ms


# Field names: the first entry of each tuple was verified against a live accuracy.do response taken
# from the Turkey server on 2026-10-10 22:20 UTC (btc_usdt: priceAccuracy 2, quantityAccuracy 5,
# minTranQua 0.00001, minOrderAmount 1). The later entries are fallbacks only; the raw row is always
# stored so the evaluation can be redone offline with --summarize.
ACCURACY_FIELDS: dict[str, tuple[str, ...]] = {
    "price_precision": ("priceAccuracy", "pricePrecision", "price_accuracy"),
    "quantity_precision": ("quantityAccuracy", "quantityPrecision", "quantity_accuracy"),
    "min_qty": ("minTranQua", "minQty", "minQuantity"),
    "min_order_value": (
        "minOrderAmount",
        "minTranAmt",
        "minTranAmount",
        "minNotional",
        "minOrderValue",
        "minTradeAmount",
    ),
}


def find_accuracy_row(body: Any, symbol: str) -> dict[str, Any] | None:
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and symbol in data and isinstance(data[symbol], dict):
        return data[symbol]  # type: ignore[no-any-return]
    if not isinstance(data, list):
        return None
    for row in data:
        if isinstance(row, dict) and str(row.get("symbol", "")).lower() == symbol.lower():
            return row
    return None


def evaluate_accuracy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Is every G0-required field readable (precisions are non-negative ints; minimums are > 0)?"""
    fields: dict[str, Any] = {}
    for logical, keys in ACCURACY_FIELDS.items():
        key = next((k for k in keys if k in row), None)
        raw = row.get(key) if key is not None else None
        value = _safe_decimal(raw) if raw is not None else None
        if logical.endswith("precision"):
            ok = value is not None and value >= 0 and value == value.to_integral_value() and value <= 18
        else:
            ok = value is not None and value > 0
        fields[logical] = {"key": key, "raw": str(raw) if raw is not None else None, "ok": ok}
    return {"ok": all(f["ok"] for f in fields.values()), "fields": fields, "row_keys": sorted(row)}


class KlineRowError(ValueError):
    """Raised when a kline row's shape is not understood -- neither LBank's documented array
    (``[open_ts, open, high, low, close, volume]``) nor a dict with a recognised timestamp field.

    A :class:`ValueError` subclass so existing ``except ValueError`` handlers still catch it, but a
    distinct type so ``_kline_probe`` (item 5) can catch it specifically rather than any stray
    ``ValueError`` from unrelated code it might one day also call.
    """


# Dict-row timestamp field candidates, in priority order. LBank's documented kline response is
# array-style; this exists for the (currently hypothetical) case of a dict-shaped row from some
# mirror or future API version, so that case is handled rather than crashing the whole G0 run.
_KLINE_TS_DICT_KEYS: tuple[str, ...] = ("openTime", "open_time", "t", "time", "startTime")


def _row_open_ts(item: Any) -> int:
    """Open-time (seconds or ms) from one kline row -- array-style (``item[0]``) or dict-style
    (first matching key in :data:`_KLINE_TS_DICT_KEYS`). Raises :class:`KlineRowError` for
    anything else, or if the extracted value is not an integer."""
    if isinstance(item, Mapping):
        key = next((k for k in _KLINE_TS_DICT_KEYS if k in item), None)
        if key is None:
            raise KlineRowError(f"dict kline row has none of {_KLINE_TS_DICT_KEYS}: keys={sorted(item)}")
        raw = item[key]
    elif isinstance(item, (list, tuple)) and item:
        raw = item[0]
    else:
        raise KlineRowError(f"kline row is neither a dict nor a non-empty array: {type(item).__name__}")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise KlineRowError(f"kline row timestamp {raw!r} is not an integer: {exc}") from exc


def parse_kline_rows(body: Any) -> list[tuple[int, ...]]:
    """``(open_ts_s, ...)`` rows; millisecond timestamps are normalised to seconds.

    Raises :class:`KlineRowError` (never a bare ``KeyError``/``IndexError``) if the payload shape
    or any row's shape is not understood; see ``_kline_probe``, which catches it per timeframe so
    one odd row never aborts the whole G0 run.
    """
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise KlineRowError("kline data is not a list")
    rows: list[tuple[int, ...]] = []
    for item in data:
        ts = _row_open_ts(item)
        rows.append((ts // 1000 if ts >= 10**11 else ts,))
    return rows


def kline_extent(rows: Sequence[tuple[int, ...]], bar_seconds: int) -> dict[str, Any]:
    """Count, first/last open time and the number of non-contiguous steps inside the page."""
    stamps = [r[0] for r in rows]
    breaks = sum(1 for a, b in pairwise(stamps) if b - a != bar_seconds)
    return {
        "returned": len(stamps),
        "first_ts_s": stamps[0] if stamps else None,
        "last_ts_s": stamps[-1] if stamps else None,
        "first_utc": _iso(stamps[0]) if stamps else None,
        "last_utc": _iso(stamps[-1]) if stamps else None,
        "ascending": stamps == sorted(stamps),
        "non_contiguous_steps": breaks,
    }


def ticker_row(body: Any, symbol: str) -> dict[str, Any] | None:
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    for row in data:
        if isinstance(row, dict) and str(row.get("symbol", symbol)).lower() == symbol.lower():
            inner = row.get("ticker")
            return inner if isinstance(inner, dict) else row
    return None


# ---------------------------------------------------------------------------------
# JSONL output
# ---------------------------------------------------------------------------------


class JsonlWriter:
    """Append-only JSONL; flushed and fsynced after every record so a kill loses nothing."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, record: Mapping[str, Any]) -> None:
        self._fh.write(_dumps(record) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------------


def _section(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A parser bug in one section must never end a six-hour run."""
    try:
        return fn()
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}


def _failed(res: HttpResult) -> dict[str, Any]:
    return {"ok": False, "reason": res.error or res.payload_error or f"http {res.status}"}


class SampleRunner:
    """Runs one sample: every endpoint once, strictly sequentially."""

    def __init__(self, fetcher: FetchLike, env: Env, *, depth_size: int = DEPTH_SIZE_CANDIDATES[-1]) -> None:
        self._fetcher = fetcher
        self._env = env
        self.depth_size = depth_size
        self._requests: list[HttpResult] = []

    def _call(self, name: str, spec_key: str, params: Mapping[str, Any]) -> HttpResult:
        res = self._fetcher.get(name, ENDPOINTS[spec_key], params)
        self._requests.append(res)
        return res

    def run(self, index: int) -> dict[str, Any]:
        self._requests = []
        started = self._env.now()
        sample: dict[str, Any] = {"kind": "sample", "index": index, "utc_start": started.isoformat()}
        sample["server_time"] = self._timestamp()
        sample["accuracy"] = self._accuracy()
        book, raw = self._depth()
        sample["book"], sample["depth_raw"] = book, raw
        sample["trades"] = self._trades()
        sample["klines"] = {tf: self._kline(tf) for tf in KLINES}
        sample["ticker"] = self._ticker(book)
        sample["perp"] = self._perp()
        sample["requests"] = [r.summary() for r in self._requests]
        return sample

    def _timestamp(self) -> dict[str, Any]:
        res = self._call("timestamp", "timestamp", {})
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            server_ms = parse_server_time_ms(res.body)
            if server_ms is None:
                return {"ok": False, "reason": "unparseable server time"}
            return {
                "ok": True,
                "server_ms": server_ms,
                "delta_ms": clock_delta_ms(server_ms, res.sent_at, res.received_at),
                "rtt_ms": res.latency_ms,
                "half_rtt_ms": res.latency_ms / 2,
            }

        return _section(build)

    def _accuracy(self) -> dict[str, Any]:
        res = self._call("accuracy", "accuracy", {})
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            row = find_accuracy_row(res.body, SYMBOL)
            if row is None:
                return {"ok": False, "reason": f"{SYMBOL} not listed", "row": None}
            return {**evaluate_accuracy_row(row), "row": {k: str(v) for k, v in row.items()}}

        return _section(build)

    def _depth(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        res = self._call("depth", "depth", {"symbol": SYMBOL, "size": self.depth_size})
        if not res.ok:
            return _failed(res), None
        try:
            bids, asks = parse_book(res.body)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}, None
        book = analyze_book(bids, asks)
        book["requested_size"] = self.depth_size
        raw = {"bids": [[dstr(p), dstr(q)] for p, q in bids], "asks": [[dstr(p), dstr(q)] for p, q in asks]}
        return book, raw

    def _trades(self) -> dict[str, Any]:
        res = self._call("trades", "trades", {"symbol": SYMBOL, "size": TRADES_SIZE})
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            rows = parse_trades(res.body)
            times = [r[1] for r in rows]
            newest = max(times) if times else None
            return {
                "ok": True,
                "n": len(rows),
                "requested": TRADES_SIZE,
                "oldest_ms": min(times) if times else None,
                "newest_ms": newest,
                "span_s": (max(times) - min(times)) / 1000 if times else None,
                "newest_age_s": (self._env.now().timestamp() * 1000 - newest) / 1000 if newest else None,
            }

        return _section(build)

    def _kline(self, tf: str) -> dict[str, Any]:
        ltype, secs = KLINES[tf]
        start = int(self._env.now().timestamp()) - KLINE_SAMPLE_BARS * secs
        res = self._call(
            f"kline_{tf}",
            "kline",
            {"symbol": SYMBOL, "size": KLINE_SAMPLE_BARS, "type": ltype, "time": start},
        )
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            extent = kline_extent(parse_kline_rows(res.body), secs)
            last = extent["last_ts_s"]
            age = self._env.now().timestamp() - last if last is not None else None
            return {"ok": True, **extent, "last_open_age_s": age}

        return _section(build)

    def _ticker(self, book: Mapping[str, Any]) -> dict[str, Any]:
        res = self._call("ticker24hr", "ticker24hr", {"symbol": SYMBOL})
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            row = ticker_row(res.body, SYMBOL)
            latest = _safe_decimal(row.get("latest")) if row else None
            if row is None or latest is None:
                return {"ok": False, "reason": "no ticker row / latest"}
            out: dict[str, Any] = {"ok": True, "latest": dstr(latest), "vol": str(row.get("vol"))}
            mid = _safe_decimal(book.get("mid"))
            if mid:
                out["latest_vs_mid_bps"] = float((latest - mid) / mid) * 10_000
            return out

        return _section(build)

    def _perp(self) -> dict[str, Any]:
        res = self._call("perp_marketdata", "perp_marketdata", {"productGroup": "SwapU"})
        if not res.ok:
            return _failed(res)

        def build() -> dict[str, Any]:
            row = parse_funding(res.body, PERP_SYMBOL)
            if row is None:
                return {"ok": True, "row_found": False}
            return {
                "ok": True,
                "row_found": True,
                "funding_rate": str(row.get("fundingRate")),
                "last_price": str(row.get("lastPrice")),
                "next_fee_time_ms": str(row.get("nextFeeTime")),
            }

        return _section(build)


# --- one-off discovery blocks ------------------------------------------------------


def discover_depth_size(
    fetcher: FetchLike, candidates: Sequence[int] = DEPTH_SIZE_CANDIDATES
) -> dict[str, Any]:
    """Find the largest accepted ``size`` for ``depth`` by trying candidates from large to small."""
    attempts: list[dict[str, Any]] = []
    chosen: int | None = None
    for size in candidates:
        res = fetcher.get("depth_size_discovery", ENDPOINTS["depth"], {"symbol": SYMBOL, "size": size})
        n_bids = n_asks = None
        if res.ok:
            try:
                bids, asks = parse_book(res.body)
                n_bids, n_asks = len(bids), len(asks)
            except ValueError:
                pass
        accepted = res.ok and bool(n_bids) and bool(n_asks)
        attempts.append(
            {"requested": size, "bids": n_bids, "asks": n_asks, **res.summary(), "accepted": accepted}
        )
        if accepted:
            chosen = size
            break
    return {"kind": "depth_size_discovery", "attempts": attempts, "chosen_size": chosen}


def _kline_probe(
    fetcher: FetchLike, tf: str, label: str, start_s: int
) -> tuple[dict[str, Any], list[tuple[int, ...]]]:
    ltype, secs = KLINES[tf]
    params = {"symbol": SYMBOL, "size": KLINE_HISTORY_SIZE, "type": ltype, "time": start_s}
    res = fetcher.get(f"kline_{tf}_history_{label}", ENDPOINTS["kline"], params)
    info: dict[str, Any] = {
        "requested_size": KLINE_HISTORY_SIZE,
        "requested_start_s": start_s,
        "requested_start_utc": _iso(start_s),
        **res.summary(),
    }
    rows: list[tuple[int, ...]] = []
    if res.ok:
        try:
            rows = parse_kline_rows(res.body)
            info.update(kline_extent(rows, secs))
        except KlineRowError as exc:
            # item 5: a malformed/unrecognised row shape is recorded and this timeframe's history
            # probe stops here (whatever rows were parsed before the bad one are discarded, since
            # kline_extent needs a trustworthy sequence) -- it never aborts the other timeframes or
            # the surrounding sampling run.
            info["parse_error"] = f"{type(exc).__name__}: {exc}"[:200]
            rows = []
    return info, rows


def probe_kline_history(fetcher: FetchLike, env: Env) -> dict[str, Any]:
    """Per timeframe: a recent full-size page, the earliest page, and the page after it.

    Answers: how many bars does one request return, how far back does the history go, is the
    ``time`` start inclusive, and is paging by ``time = last_ts + bar`` contiguous.
    """
    now_s = int(env.now().timestamp())
    out: dict[str, Any] = {}
    for tf, (ltype, secs) in KLINES.items():
        recent, rows_a = _kline_probe(fetcher, tf, "recent", now_s - KLINE_HISTORY_SIZE * secs)
        earliest, rows_b = _kline_probe(fetcher, tf, "earliest", EARLIEST_PROBE_TS_S)
        entry: dict[str, Any] = {"type": ltype, "bar_seconds": secs, "recent": recent, "earliest": earliest}
        first_b = rows_b[0][0] if rows_b else None
        if rows_b:
            nxt, rows_c = _kline_probe(fetcher, tf, "next_page", rows_b[-1][0] + secs)
            nxt["contiguous_with_previous_page"] = bool(rows_c) and rows_c[0][0] == rows_b[-1][0] + secs
            entry["next_page"] = nxt
        max_bars = max(len(rows_a), len(rows_b))
        entry["max_bars_per_request_observed"] = max_bars
        entry["cap_may_exceed_requested"] = max_bars >= KLINE_HISTORY_SIZE
        entry["history_start_utc"] = _iso(first_b)
        entry["time_param_ignored_suspected"] = bool(rows_a and rows_b and rows_b[0][0] >= rows_a[0][0])
        if first_b is not None:
            bars = (now_s - first_b) // secs + 1
            entry["history_bars_estimate"] = bars
            entry["history_days_estimate"] = round((now_s - first_b) / 86_400, 1)
            entry["pages_to_backfill_estimate"] = math.ceil(bars / max_bars) if max_bars else None
        out[tf] = entry
    return {"kind": "kline_history", "timeframes": out}


# ---------------------------------------------------------------------------------
# summary / G0 evaluation
# ---------------------------------------------------------------------------------


def _valid(samples: Sequence[Mapping[str, Any]], key: str) -> list[Mapping[str, Any]]:
    return [s[key] for s in samples if isinstance(s.get(key), dict) and s[key].get("ok")]


def summarize_latency(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-endpoint latency stats (item 4). A timeout is counted AT its configured timeout value
    (not excluded, and not at whatever partial ``latency_ms`` happened to be measured) so p50/p95
    reflect the true tail risk instead of looking artificially good by dropping the worst cases;
    ``timeout_count`` reports how many of those there were, separately from other failures.
    """
    by_ep: dict[str, list[Mapping[str, Any]]] = {}
    for sample in samples:
        for req in sample.get("requests", []):
            by_ep.setdefault(req["endpoint"], []).append(req)
    out: dict[str, Any] = {}
    for name, reqs in sorted(by_ep.items()):
        lat: list[float] = []
        timeout_count = 0
        for r in reqs:
            if r.get("error") == "timeout":
                timeout_count += 1
                lat.append(float(r.get("timeout_s", REQUEST_TIMEOUT_S)) * 1000.0)
            elif r.get("status") is not None:
                lat.append(float(r["latency_ms"]))
        out[name] = {
            "n": len(reqs),
            "ok": sum(1 for r in reqs if r.get("ok")),
            "http_not_200": sum(1 for r in reqs if r.get("status") != 200),
            "timeout_count": timeout_count,
            "p50_ms": percentile(lat, 0.5),
            "p95_ms": percentile(lat, 0.95),
            "max_ms": max(lat) if lat else None,
        }
    return out


def summarize_clock(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    clocks = _valid(samples, "server_time")
    if not clocks:
        return {"n": 0}
    deltas = [float(c["delta_ms"]) for c in clocks]
    best = min(clocks, key=lambda c: float(c["rtt_ms"]))
    return {
        "n": len(clocks),
        "median_delta_ms": _median(deltas),
        "median_abs_delta_ms": _median([abs(d) for d in deltas]),
        "p95_abs_delta_ms": percentile([abs(d) for d in deltas], 0.95),
        "median_rtt_ms": _median([float(c["rtt_ms"]) for c in clocks]),
        "median_half_rtt_ms": _median([float(c["half_rtt_ms"]) for c in clocks]),
        "delta_at_min_rtt_ms": float(best["delta_ms"]),
        "min_rtt_ms": float(best["rtt_ms"]),
        "note": "positive = LBank ahead of local; uncertainty about +/- half the round trip; "
        "local clock sync not checked by this script",
    }


def _clock_check(clock: Mapping[str, Any]) -> dict[str, Any]:
    """Item 6's pre-registered clock-offset interpretation.

    Pass requires BOTH ``|delta_at_min_rtt_ms| < 1000`` and ``median |delta| < 1000``: the first
    is the single lowest-uncertainty sample (least RTT, so least +/- half-RTT ambiguity); the
    second guards against that one sample being a fluke. When the RTT-uncertainty band around the
    median (``median_abs_delta_ms +/- median_half_rtt_ms``) straddles the 1000 ms line, this script
    cannot tell which side of the threshold the truth is on, so it reports "inconclusive" (counted
    as not-passed, with that reason) instead of picking a side.
    """
    threshold = G0_MAX_MEDIAN_SKEW_MS
    med_abs = clock.get("median_abs_delta_ms")
    half_rtt = clock.get("median_half_rtt_ms")
    delta_min_rtt = clock.get("delta_at_min_rtt_ms")
    base: dict[str, Any] = {"threshold": f"both < {threshold:.0f} ms", "uncertainty_ms": half_rtt}
    if med_abs is None or delta_min_rtt is None:
        return {**base, "pass": False, "status": "no_data", "observed": "no data"}
    abs_delta_min_rtt = abs(delta_min_rtt)
    if half_rtt is not None and (med_abs - half_rtt) < threshold < (med_abs + half_rtt):
        return {
            **base,
            "pass": False,
            "status": "inconclusive",
            "observed": (
                f"median abs(delta) = {med_abs:.0f} ms +/- half-RTT {half_rtt:.0f} ms "
                f"straddles the {threshold:.0f} ms threshold"
            ),
            "reason": "RTT-uncertainty band crosses the threshold; cannot confirm pass or fail",
        }
    ok = abs_delta_min_rtt < threshold and med_abs < threshold
    return {
        **base,
        "pass": ok,
        "status": "pass" if ok else "fail",
        "observed": f"median abs(delta) = {med_abs:.0f} ms; |delta at min-RTT| = {abs_delta_min_rtt:.0f} ms",
    }


def summarize_book(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    books = _valid(samples, "book")
    spreads = [float(b["spread_pct"]) for b in books]
    out: dict[str, Any] = {
        "n_books": len(books),
        "spread_pct": {
            "median": _median(spreads),
            "p95": percentile(spreads, 0.95),
            "max": max(spreads) if spreads else None,
        },
        "levels_returned": {
            "requested_size": books[0].get("requested_size") if books else None,
            "max_bid_levels": max((b["bid_levels"] for b in books), default=None),
            "max_ask_levels": max((b["ask_levels"] for b in books), default=None),
        },
        "depth": {},
        "max_fill": {},
    }
    p10 = Decimal("0.10")
    for band in BANDS:
        for side in ("bid", "ask"):
            cells = [b["depth"][str(band)][side] for b in books]
            out["depth"].setdefault(str(band), {})[side] = _decimal_stats(cells, "base_qty", "quote", p10)
    for slip in SLIPPAGES:
        for ref in ("vs_mid", "vs_touch"):
            for side in ("buy", "sell"):
                cells = [b["max_fill"][str(slip)][ref][side] for b in books]
                out["max_fill"].setdefault(str(slip), {}).setdefault(ref, {})[side] = _decimal_stats(
                    cells, "base_qty", "quote", p10
                )
    return out


def _decimal_stats(
    cells: Sequence[Mapping[str, Any]], base_key: str, quote_key: str, p: Decimal
) -> dict[str, Any]:
    bases = [Decimal(c[base_key]) for c in cells]
    quotes = [Decimal(c[quote_key]) for c in cells]

    def fmt(v: Decimal | None) -> str | None:
        return dstr(v) if v is not None else None

    return {
        "n": len(cells),
        "base_median": fmt(median_decimal(bases)),
        "base_p10": fmt(percentile_decimal(bases, p)),
        "quote_median": fmt(median_decimal(quotes)),
        "quote_p10": fmt(percentile_decimal(quotes, p)),
        "truncated_samples": sum(1 for c in cells if c.get("truncated")),
    }


def _span_s(samples: Sequence[Mapping[str, Any]]) -> float:
    if len(samples) < 2:
        return 0.0
    first = datetime.fromisoformat(samples[0]["utc_start"])
    last = datetime.fromisoformat(samples[-1]["utc_start"])
    return (last - first).total_seconds()


def _per_endpoint_rates(reqs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_ep: dict[str, list[Mapping[str, Any]]] = {}
    for r in reqs:
        by_ep.setdefault(r["endpoint"], []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for name, rs in sorted(by_ep.items()):
        ok = sum(1 for r in rs if r.get("ok"))
        out[name] = {"n": len(rs), "ok": ok, "rate": ok / len(rs) if rs else None}
    return out


def evaluate_g0(
    samples: Sequence[Mapping[str, Any]], *, stopped_on_ban: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Pass/fail of the four pre-registered D-073 conditions plus the sampling-plan check.

    ``stopped_on_ban`` (item 1): when sampling mode's shared :class:`StopTracker` trips, the run
    is stopped immediately and the verdict is forced to FAIL with that reason -- regardless of what
    the (necessarily incomplete) checks below would otherwise say, since an aborted run proves
    nothing about the conditions it never got to finish measuring.
    """
    reqs = [(s["index"], r) for s in samples for r in s.get("requests", [])]
    failures = [
        {
            "sample": i,
            "endpoint": r["endpoint"],
            "status": r.get("status"),
            "why": r.get("error") or r.get("payload_error"),
        }
        for i, r in reqs
        if not r.get("ok")
    ]
    rates = _per_endpoint_rates([r for _, r in reqs])
    worst_rate = min((v["rate"] for v in rates.values() if v["rate"] is not None), default=None)
    ban_seen = any(r.get("status") in BAN_LIKE_STATUSES for _, r in reqs)
    clock = summarize_clock(samples)
    book = summarize_book(samples)
    acc = [s["accuracy"] for s in samples if isinstance(s.get("accuracy"), dict)]
    acc_bad = [a for a in acc if not a.get("ok")]
    med_spread = book["spread_pct"]["median"]
    endpoints_ok = (
        bool(reqs) and worst_rate is not None and worst_rate >= ENDPOINT_SUCCESS_MIN_RATE and not ban_seen
    )
    checks: dict[str, dict[str, Any]] = {
        "all_endpoints_200": {
            "pass": endpoints_ok,
            "observed": (
                f"worst per-endpoint success rate = {worst_rate:.1%}; ban-type response seen = {ban_seen}"
                if worst_rate is not None
                else "no data"
            ),
            "threshold": f">= {ENDPOINT_SUCCESS_MIN_RATE:.0%} success per endpoint, and no 429/403/418 ever",
            "per_endpoint_rates": rates,
            "ban_type_response_seen": ban_seen,
            "failures": failures[:50],
        },
        "clock_offset": _clock_check(clock),
        "accuracy_btc_usdt": {
            "pass": bool(acc) and not acc_bad,
            "observed": f"{len(acc) - len(acc_bad)}/{len(acc)} samples list {SYMBOL} with readable "
            "price/quantity precision, min quantity and min order value",
            "threshold": "all samples",
            "fields_last_sample": acc[-1].get("fields") if acc else None,
            "row_keys_last_sample": acc[-1].get("row_keys") if acc else None,
        },
        "median_spread": {
            "pass": med_spread is not None and med_spread < G0_MAX_MEDIAN_SPREAD_PCT,
            "observed": f"median spread = {med_spread:.4f} %" if med_spread is not None else "no data",
            "threshold": f"< {G0_MAX_MEDIAN_SPREAD_PCT:.2f} %",
        },
    }
    if stopped_on_ban is not None:
        checks["no_ban_response"] = {
            "pass": False,
            "observed": f"sampling stopped: {stopped_on_ban.get('reason')}",
            "threshold": "no 429/403/418, no 5xx burst, no transport-error burst, at any point",
        }
    span = _span_s(samples)
    plan_ok = len(samples) >= G0_MIN_SAMPLES and span >= G0_MIN_SPAN_S
    failed = [name for name, c in checks.items() if not c["pass"]]
    if stopped_on_ban is not None:
        verdict = "FAIL"
    elif not samples:
        # item 8: zero samples means nothing has been measured yet, which is INCOMPLETE (the
        # sampling plan was not met), never FAIL -- FAIL is reserved for a check that was actually
        # evaluated and came back bad. Checked BEFORE `failed`, because every check above reports
        # "not passed" on an empty sample set purely for lack of data, which must not read as FAIL.
        verdict = "INCOMPLETE"
    elif failed:
        verdict = "FAIL"
    else:
        verdict = "PASS" if plan_ok else "INCOMPLETE"
    return {
        "verdict": verdict,
        "failed_checks": failed,
        "checks": checks,
        "stopped_on_ban": dict(stopped_on_ban) if stopped_on_ban else None,
        "plan": {
            "samples": len(samples),
            "min_samples": G0_MIN_SAMPLES,
            "span_hours": round(span / 3600, 3),
            "min_span_hours": G0_MIN_SPAN_S / 3600,
            "plan_ok": plan_ok,
        },
    }


def _aux_summaries(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    trades = _valid(samples, "trades")
    tickers = _valid(samples, "ticker")
    perp = _valid(samples, "perp")
    klines = {
        tf: [s["klines"][tf] for s in samples if s.get("klines", {}).get(tf, {}).get("ok")] for tf in KLINES
    }
    return {
        "trades": {
            "median_n": _median([t["n"] for t in trades]),
            "median_span_s": _median([t["span_s"] for t in trades if t.get("span_s") is not None]),
            "saturated_samples": sum(1 for t in trades if t["n"] >= t["requested"]),
        },
        "ticker_latest_vs_mid_bps_median": _median(
            [t["latest_vs_mid_bps"] for t in tickers if "latest_vs_mid_bps" in t]
        ),
        "perp": {
            "rows_found": sum(1 for p in perp if p.get("row_found")),
            "samples": len(perp),
            "last_funding_rate": perp[-1].get("funding_rate") if perp else None,
        },
        "kline_sample_bars_returned_median": {
            tf: _median([k["returned"] for k in ks]) for tf, ks in klines.items()
        },
    }


def build_summary(
    provenance: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    *,
    discovery: Mapping[str, Any] | None,
    history: Mapping[str, Any] | None,
    planned_samples: int,
    interval_s: float,
    interrupted: bool,
    finished: bool,
    stopped_on_ban: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provenance": dict(provenance),
        "mode": "sampling",
        "status": {"finished": finished, "interrupted": interrupted},
        "stopped_on_ban": dict(stopped_on_ban) if stopped_on_ban else None,
        "plan": {
            "planned_samples": planned_samples,
            "interval_s": interval_s,
            "completed_samples": len(samples),
        },
        "g0": evaluate_g0(samples, stopped_on_ban=stopped_on_ban),
        "latency": summarize_latency(samples),
        "clock": summarize_clock(samples),
        "book": summarize_book(samples),
        "accuracy_row_last": next(
            (s["accuracy"].get("row") for s in reversed(samples) if s["accuracy"].get("row")), None
        ),
        "depth_size_discovery": dict(discovery) if discovery else None,
        "kline_history": dict(history) if history else None,
        **_aux_summaries(samples),
        "notes": _notes(provenance),
    }


def _notes(provenance: Mapping[str, Any]) -> list[str]:
    notes = [
        "Max-fill figures are computed from the visible levels only; 'truncated' marks a lower bound.",
        "vs_mid slippage includes the half-spread; vs_touch is measured from the best price consumed.",
        "Latencies and clock deltas are valid only for the host and time in the provenance block.",
        "The local clock's NTP sync is not checked by the script; the operator must record it separately.",
    ]
    if provenance.get("git_commit") == "unknown" or provenance.get("git_commit_unknown"):
        notes.insert(0, 'WARNING: git commit could not be resolved (provenance.git_commit = "unknown").')
    if provenance.get("data_source") == "offline_fixture":
        notes.insert(0, "OFFLINE FIXTURE DATA - these numbers are NOT measurements of LBank.")
    return notes


# ---------------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------------


def _fmt4(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(v: Any) -> str:
        if v is None:
            return "-"
        return f"{v:.3f}" if isinstance(v, float) else str(v)

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _provenance_md(prov: Mapping[str, Any]) -> str:
    lines = [
        f"> {prov['measured_from']}",
        "",
        f"- hostname: `{prov['hostname']}`",
        f"- UTC start / end: `{prov['utc_start']}` / `{prov.get('utc_end')}`",
        f"- python: `{prov['python']}` ({prov['platform']})",
        f"- script git commit: `{prov['git_commit']}`",
        f"- mode: `{prov['mode']}`, data source: **{prov['data_source']}**",
        f"- argv: `{' '.join(prov['argv'])}`",
        f"- local clock sync: {prov['local_clock_sync']}",
    ]
    if prov.get("git_commit") == "unknown":
        lines.append('- **WARNING: git commit could not be resolved (reported as "unknown")**')
    if prov["data_source"] == "offline_fixture":
        lines.insert(1, "> **OFFLINE FIXTURE DATA - NOT A MEASUREMENT OF LBANK.**")
    return "\n".join(lines)


def render_markdown(summary: Mapping[str, Any]) -> str:
    g0 = summary["g0"]
    parts = [
        "# LBank G0 probe (public endpoints only)",
        _provenance_md(summary["provenance"]),
        f"## Verdict: {g0['verdict']}",
        _table(
            ["Condition", "Observed", "Threshold", "Result"],
            [
                [n, c["observed"], c["threshold"], c.get("status", "pass" if c["pass"] else "fail").upper()]
                for n, c in g0["checks"].items()
            ],
        ),
        f"\nSampling plan: {g0['plan']['samples']} samples over {g0['plan']['span_hours']} h "
        f"(required: >= {g0['plan']['min_samples']} samples, >= {g0['plan']['min_span_hours']:.0f} h) "
        f"-> plan_ok = {g0['plan']['plan_ok']}. Interrupted: {summary['status']['interrupted']}."
        + (f" STOPPED ON BAN: {g0['stopped_on_ban']['reason']}." if g0.get("stopped_on_ban") else ""),
        "## Latency per endpoint (ms)",
        _table(
            ["endpoint", "n", "ok", "not-200", "timeouts", "p50", "p95", "max"],
            [
                [
                    k,
                    v["n"],
                    v["ok"],
                    v["http_not_200"],
                    v["timeout_count"],
                    v["p50_ms"],
                    v["p95_ms"],
                    v["max_ms"],
                ]
                for k, v in summary["latency"].items()
            ],
        ),
        _clock_md(summary["clock"]),
        _book_md(summary["book"]),
        _kline_md(summary.get("kline_history"), summary.get("depth_size_discovery")),
        "## Notes",
        "\n".join(f"- {n}" for n in summary["notes"]),
    ]
    return "\n\n".join(parts) + "\n"


def _clock_md(clock: Mapping[str, Any]) -> str:
    if not clock.get("n"):
        return "## Clock offset\n\nno data"
    return "## Clock offset (LBank server time minus local clock)\n\n" + _table(
        [
            "n",
            "median delta ms",
            "median abs ms",
            "p95 abs ms",
            "median RTT ms",
            "half-RTT uncertainty ms",
            "delta at min-RTT ms",
        ],
        [
            [
                clock["n"],
                clock["median_delta_ms"],
                clock["median_abs_delta_ms"],
                clock["p95_abs_delta_ms"],
                clock["median_rtt_ms"],
                clock["median_half_rtt_ms"],
                clock["delta_at_min_rtt_ms"],
            ]
        ],
    )


def _book_md(book: Mapping[str, Any]) -> str:
    sp = book["spread_pct"]
    lv = book["levels_returned"]
    out = [
        "## Spread, depth and max fill",
        f"Books analysed: {book['n_books']}. Requested depth size: {lv['requested_size']}; "
        f"levels returned (max): {lv['max_bid_levels']} bids / {lv['max_ask_levels']} asks.",
        f"Spread %: median {_fmt4(sp['median'])}, p95 {_fmt4(sp['p95'])}, max {_fmt4(sp['max'])}.",
    ]
    rows = []
    for band, sides in book["depth"].items():
        for side, st in sides.items():
            rows.append(
                [
                    f"{float(band) * 100:g} %",
                    side,
                    st["base_median"],
                    st["base_p10"],
                    st["quote_median"],
                    st["quote_p10"],
                    st["truncated_samples"],
                ]
            )
    out.append("### Depth within band of mid (base BTC / quote USDT)")
    out.append(
        _table(
            ["band", "side", "BTC median", "BTC p10", "USDT median", "USDT p10", "truncated samples"], rows
        )
    )
    rows = []
    for slip, refs in book["max_fill"].items():
        for ref, sides in refs.items():
            for side, st in sides.items():
                rows.append(
                    [
                        f"{float(slip) * 100:g} %",
                        ref,
                        side,
                        st["base_median"],
                        st["base_p10"],
                        st["quote_median"],
                        st["quote_p10"],
                        st["truncated_samples"],
                    ]
                )
    out.append("### Largest market order that fills within slippage")
    out.append(
        _table(
            [
                "slippage",
                "reference",
                "side",
                "BTC median",
                "BTC p10",
                "USDT median",
                "USDT p10",
                "truncated samples",
            ],
            rows,
        )
    )
    return "\n\n".join(out)


def _kline_md(history: Mapping[str, Any] | None, discovery: Mapping[str, Any] | None) -> str:
    out = ["## Kline history and depth size"]
    if discovery:
        tried = ", ".join(
            f"{a['requested']}->{'ok' if a['accepted'] else 'rejected'}" for a in discovery["attempts"]
        )
        out.append(f"Depth size discovery: {tried}; chosen = {discovery['chosen_size']}.")
    if not history:
        out.append("Kline history probe: not run.")
        return "\n\n".join(out)
    rows = []
    for tf, e in history["timeframes"].items():
        nxt = e.get("next_page", {})
        rows.append(
            [
                tf,
                e["max_bars_per_request_observed"],
                "yes" if e["cap_may_exceed_requested"] else "no",
                e["history_start_utc"],
                e.get("history_days_estimate"),
                e.get("pages_to_backfill_estimate"),
                nxt.get("contiguous_with_previous_page"),
                e["time_param_ignored_suspected"],
            ]
        )
    out.append(
        _table(
            [
                "tf",
                "max bars/request",
                "cap may be higher",
                "history starts (UTC)",
                "days",
                "pages to backfill",
                "next page contiguous",
                "time ignored?",
            ],
            rows,
        )
    )
    return "\n\n".join(out)


# ---------------------------------------------------------------------------------
# sampling driver
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputPaths:
    jsonl: Path
    summary: Path
    markdown: Path

    @classmethod
    def for_run(
        cls, out_dir: Path, prefix: str, stamp: str, *, summary_suffix: str = ".summary.json"
    ) -> OutputPaths:
        stem = out_dir / f"{prefix}_{stamp}"
        return cls(stem.with_suffix(".jsonl"), stem.with_suffix(summary_suffix), stem.with_suffix(".md"))


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _publish(paths: OutputPaths, summary: Mapping[str, Any], markdown: str) -> None:
    _write_atomic(paths.summary, _dumps(summary, indent=2) + "\n")
    _write_atomic(paths.markdown, markdown)


def run_sampling(
    fetcher: Fetcher,
    env: Env,
    *,
    samples: int,
    interval_s: float,
    paths: OutputPaths,
    provenance: dict[str, Any],
    kline_history: bool = True,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[int, dict[str, Any]]:
    """Run the whole sampling plan; returns ``(exit_code, summary)``. Always writes all outputs.

    Item 1: every request -- every ``SampleRunner`` call, ``discover_depth_size``, and
    ``probe_kline_history`` alike -- goes through one shared, ``ban_only`` :class:`StopTracker` via
    :class:`CheckedFetcher`, the single choke point. The instant it trips (429/403/418, or a burst
    of 3 consecutive 5xx or transport errors), :class:`BanDetectedError` unwinds whatever was in
    progress -- mid-sample, mid-discovery, or between samples -- and NO further request of any
    kind is made: the triggering response is recorded, and the run finishes with verdict FAIL and
    the reason.
    """
    writer = JsonlWriter(paths.jsonl)
    writer.write({"kind": "provenance", **provenance})
    tracker = StopTracker(ban_only=True)
    checked = CheckedFetcher(fetcher, tracker)
    runner = SampleRunner(checked, env)
    done: list[dict[str, Any]] = []
    discovery: dict[str, Any] | None = None
    history: dict[str, Any] | None = None
    interrupted = unreachable = False
    stopped_on_ban: dict[str, Any] | None = None
    start = env.monotonic()

    def snapshot(finished: bool) -> dict[str, Any]:
        summary = build_summary(
            provenance,
            done,
            discovery=discovery,
            history=history,
            planned_samples=samples,
            interval_s=interval_s,
            interrupted=interrupted,
            finished=finished,
            stopped_on_ban=stopped_on_ban,
        )
        _publish(paths, summary, render_markdown(summary))
        return summary

    try:
        for i in range(samples):
            wait = start + i * interval_s - env.monotonic()
            if wait > 0:
                env.sleep(wait)
            if i == 0:
                discovery = discover_depth_size(checked)
                writer.write(discovery)
                runner.depth_size = discovery["chosen_size"] or DEPTH_SIZE_CANDIDATES[-1]
            sample = runner.run(i)
            done.append(sample)
            writer.write(sample)
            log(f"sample {i + 1}/{samples} at {sample['utc_start']}: " + _one_line(sample))
            if i == 0 and kline_history:
                history = probe_kline_history(checked, env)
                writer.write(history)
            snapshot(finished=False)
    except BanDetectedError as ban:
        stopped_on_ban = {"reason": ban.reason, "response": ban.result.summary()}
        writer.write({"kind": "stopped_on_ban", **stopped_on_ban})
        # "Unreachable" (exit 1) is reserved for the stricter case of a transport-level failure
        # (no HTTP response at all) before even the first sample ever completed -- a real,
        # bad-but-meaningful HTTP response (429/403/418/5xx) means the exchange IS reachable, so
        # that stays a FAIL via stopped_on_ban, not "unreachable".
        if not done and ban.result.status is None:
            unreachable = True
    except KeyboardInterrupt:
        interrupted = True
    provenance["utc_end"] = env.now().isoformat()
    summary = snapshot(finished=not interrupted and not unreachable and stopped_on_ban is None)
    writer.write(
        {
            "kind": "end",
            "utc_end": provenance["utc_end"],
            "interrupted": interrupted,
            "unreachable": unreachable,
            "stopped_on_ban": stopped_on_ban is not None,
        }
    )
    writer.close()
    if unreachable:
        return 1, summary
    return _exit_code(summary, interrupted), summary


def _one_line(sample: Mapping[str, Any]) -> str:
    bad = [r["endpoint"] for r in sample["requests"] if not r["ok"]]
    spread = sample["book"].get("spread_pct")
    shown = spread if spread is None else round(spread, 4)
    return f"{len(sample['requests'])} requests, failed={bad or 'none'}, spread={shown}%"


def _exit_code(summary: Mapping[str, Any], interrupted: bool) -> int:
    if interrupted:
        return 143 if _SIGTERM_RECEIVED else 130
    return {"PASS": 0, "FAIL": 2, "INCOMPLETE": 3}[summary["g0"]["verdict"]]


_SIGTERM_RECEIVED = False


def _install_sigterm_handler() -> Callable[[], None]:
    """Turn SIGTERM into KeyboardInterrupt so partial outputs are written; returns a restorer."""

    def handler(_signum: int, _frame: FrameType | None) -> None:
        global _SIGTERM_RECEIVED
        _SIGTERM_RECEIVED = True
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not in the main thread
        return lambda: None

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return restore


# ---------------------------------------------------------------------------------
# rate-limit discovery
# ---------------------------------------------------------------------------------


@dataclass
class StopTracker:
    """Decides, response by response, whether a run must stop immediately.

    ``ban_only=False`` (rate-limit mode, the original behaviour): stops on a ban-type status
    (429/403/418), a burst of :data:`MAX_CONSECUTIVE_5XX` 5xx responses, a burst of
    :data:`MAX_CONSECUTIVE_TRANSPORT_ERRORS` transport errors, ANY other non-200 status, or a
    payload-level error on an HTTP 200 -- appropriate for a mode whose whole purpose is to probe
    for the first sign of trouble at ever-increasing request rates.

    ``ban_only=True`` (sampling mode, item 1): stops ONLY on a ban-type status or one of the two
    bursts above. An isolated 4xx or a single payload-level error does NOT stop a 6-hour sampling
    run on its own -- that is exactly what G0's per-endpoint 95 % success-rate tolerance (item 3)
    is meant to absorb; stopping the whole run on the very first one of those would make that
    tolerance meaningless.
    """

    ban_only: bool = False
    consecutive_5xx: int = 0
    consecutive_errors: int = 0
    reason: str | None = None

    def observe(self, res: HttpResult) -> str | None:
        if self.reason is not None:
            return self.reason
        status = res.status
        if status is None:
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_TRANSPORT_ERRORS:
                self.reason = f"{self.consecutive_errors} consecutive transport errors ({res.error})"
            return self.reason
        self.consecutive_errors = 0
        if status == 429:
            self.reason = "HTTP 429"
        elif status in BAN_STATUSES:
            self.reason = f"HTTP {status} (ban-type)"
        elif status >= 500:
            self.consecutive_5xx += 1
            if self.consecutive_5xx >= MAX_CONSECUTIVE_5XX:
                self.reason = f"{self.consecutive_5xx} consecutive HTTP 5xx (last {status})"
        else:
            self.consecutive_5xx = 0
            if self.ban_only:
                pass  # an isolated 4xx or payload error is left to the 95% success-rate check
            elif status != 200:
                self.reason = f"unexpected HTTP {status}"
            elif not res.payload_ok:
                self.reason = f"payload-level error on HTTP 200: {res.payload_error}"
        return self.reason


_INTERESTING_HEADER_WORDS = ("limit", "rate", "retry", "remaining", "reset")


def interesting_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if any(w in k.lower() for w in _INTERESTING_HEADER_WORDS)}


def _failure_record(res: HttpResult, rate: float, index: int, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "step_rate_rps": rate,
        "request_index_in_step": index,
        "status": res.status,
        "error": res.error,
        "headers": res.headers,
        "body": res.body_snippet,
        "payload_error": res.payload_error,
        "retry_after": res.headers.get("retry-after") or res.headers.get("Retry-After"),
    }


def _run_rate_step(
    fetcher: Fetcher,
    env: Env,
    spec_key: str,
    params: Mapping[str, Any],
    rate: float,
    step_seconds: float,
    tracker: StopTracker,
    writer: JsonlWriter,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One fixed-rate step. Strictly serial -- one request in flight, never concurrent, no matter
    how far behind ``rate`` a slow response puts the schedule; a slow response delays the next
    request, it never causes a burst or spins up a second one to catch up (item 2).
    """
    period = 1.0 / rate
    t0 = env.monotonic()
    statuses: Counter[str] = Counter()
    latencies: list[float] = []
    first_headers: dict[str, str] = {}
    sent = 0
    failure: dict[str, Any] | None = None
    while env.monotonic() - t0 < step_seconds:
        started = env.monotonic()
        res = fetcher.get(f"ratelimit_{spec_key}", ENDPOINTS[spec_key], params)
        sent += 1
        statuses[str(res.status)] += 1
        if res.status is not None:
            latencies.append(res.latency_ms)
        if sent == 1:
            first_headers = interesting_headers(res.headers)
        writer.write(
            {
                "kind": "ratelimit_request",
                "rate_rps": rate,
                "index": sent,
                **res.summary(),
                "sent_at": res.sent_at,
            }
        )
        reason = tracker.observe(res)
        if reason is not None:
            failure = _failure_record(res, rate, sent, reason)
            break
        wait = started + period - env.monotonic()
        if wait > 0:
            env.sleep(wait)
    elapsed = max(env.monotonic() - t0, 1e-9)
    achieved_rps = sent / elapsed
    step = {
        "rate_rps": rate,
        "step_seconds": step_seconds,
        "requests_sent": sent,
        "achieved_rps": achieved_rps,
        # item 2: a step only counts as having actually tested its target rate if the client could
        # keep up with it; falling short here means the client itself (its own per-request
        # latency, never concurrency -- this client never adds any) was the bottleneck, not
        # anything the exchange said.
        "reached": achieved_rps >= RATE_ACHIEVED_MIN_FRACTION * rate,
        "statuses": dict(statuses),
        "latency_p50_ms": percentile(latencies, 0.5),
        "latency_p95_ms": percentile(latencies, 0.95),
        "rate_limit_headers_seen": first_headers,
        "completed_without_incident": failure is None,
    }
    return step, failure


def run_ratelimit(
    fetcher: Fetcher,
    env: Env,
    *,
    endpoint: str,
    steps: Sequence[float],
    step_seconds: float,
    pause_seconds: float,
    paths: OutputPaths,
    provenance: dict[str, Any],
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[int, dict[str, Any]]:
    """Stepped, stop-at-first-throttle rate discovery. No retry is ever made after a stop.

    Item 2: ``last_safe_rate_rps`` is always the MEASURED ``achieved_rps`` of the last clean step,
    never the nominal target -- the two can differ once client-side latency, not the exchange,
    becomes the limiting factor. When a step's achieved rate falls short of
    :data:`RATE_ACHIEVED_MIN_FRACTION` of its target despite no bad response, that step's own
    achieved rate still counts as safe (nothing complained), but stepping to a higher nominal rate
    is pointless -- latency, not the exchange, is already the bottleneck -- so the run stops there
    instead of continuing to escalate.
    """
    spec_key, params = RATELIMIT_ENDPOINTS[endpoint]
    writer = JsonlWriter(paths.jsonl)
    writer.write({"kind": "provenance", **provenance})
    tracker = StopTracker()
    results: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    rate_not_reached: dict[str, Any] | None = None
    last_safe: float | None = None
    interrupted = False
    try:
        for n, rate in enumerate(steps):
            if n:
                env.sleep(pause_seconds)
            step, failure = _run_rate_step(
                fetcher, env, spec_key, params, rate, step_seconds, tracker, writer
            )
            results.append(step)
            log(
                f"step {rate} rps: {step['requests_sent']} requests, achieved "
                f"{step['achieved_rps']:.3f} rps, reached={step['reached']}, statuses {step['statuses']}"
            )
            if failure is not None:
                break
            last_safe = step["achieved_rps"]
            if not step["reached"]:
                rate_not_reached = {
                    "rate_rps": rate,
                    "achieved_rps": step["achieved_rps"],
                    "reason": (
                        "latency-limited: achieved rate fell short of "
                        f"{RATE_ACHIEVED_MIN_FRACTION:.0%} of the requested rate. The client is "
                        "strictly serial by design and never adds concurrency to try to reach it, "
                        "so this says nothing about what the exchange itself would tolerate -- "
                        "only that THIS client, at THIS measured per-request latency, could not "
                        "exercise that rate to test it."
                    ),
                }
                log(f"step {rate} rps not reached ({rate_not_reached['reason']}); stopping")
                break
    except KeyboardInterrupt:
        interrupted = True
    provenance["utc_end"] = env.now().isoformat()
    summary = {
        "provenance": dict(provenance),
        "mode": "ratelimit",
        "endpoint": endpoint,
        "request": {"url": ENDPOINTS[spec_key].base + ENDPOINTS[spec_key].path, "params": dict(params)},
        "plan": {"steps_rps": list(steps), "step_seconds": step_seconds, "pause_seconds": pause_seconds},
        "steps": results,
        "stopped_early": failure is not None or rate_not_reached is not None,
        "stopped_reason": "ban" if failure is not None else ("latency" if rate_not_reached else None),
        "interrupted": interrupted,
        "last_safe_rate_rps": last_safe,
        "max_rate_tested_rps": max((s["rate_rps"] for s in results), default=None),
        "failure": failure,
        "rate_not_reached": rate_not_reached,
        "caveat": (
            "A run that completes proves only that these rates were tolerated for the step "
            "length; it does not prove there is no limit. Limits may be per endpoint or per "
            "window. This ceiling covers requests made by THIS PROBE PROCESS only: the LBank "
            "recorder (src/tbot/data/lbank_recorder.py) runs continuously on the same host/IP and "
            f"adds its own steady-state traffic (approximately {RECORDER_SHARED_IP_RPS:g} req/s "
            "from its own polling schedule) on top of whatever this probe measures, which this "
            "probe neither controls nor accounts for -- the exchange sees a higher combined rate "
            "from this IP than the figures above show in isolation."
        ),
    }
    _write_atomic(paths.summary, _dumps(summary, indent=2) + "\n")
    _write_atomic(paths.markdown, render_ratelimit_markdown(summary))
    writer.write({"kind": "end", "utc_end": provenance["utc_end"], "stopped_early": summary["stopped_early"]})
    writer.close()
    if interrupted:
        return (143 if _SIGTERM_RECEIVED else 130), summary
    if failure is not None:
        return 4, summary
    if rate_not_reached is not None:
        return 5, summary
    return 0, summary


def render_ratelimit_markdown(summary: Mapping[str, Any]) -> str:
    parts = [
        "# LBank rate-limit discovery (public endpoint, stepped, stop at first throttle)",
        _provenance_md(summary["provenance"]),
        f"Endpoint: `{summary['endpoint']}` -> `{summary['request']['url']}` {summary['request']['params']}",
        f"## Result: last safe rate = {summary['last_safe_rate_rps']} req/s; "
        f"stopped early = {summary['stopped_early']}; interrupted = {summary['interrupted']}",
        _table(
            ["rate rps", "sent", "achieved rps", "reached", "statuses", "p50 ms", "p95 ms", "clean"],
            [
                [
                    s["rate_rps"],
                    s["requests_sent"],
                    s["achieved_rps"],
                    s["reached"],
                    s["statuses"],
                    s["latency_p50_ms"],
                    s["latency_p95_ms"],
                    s["completed_without_incident"],
                ]
                for s in summary["steps"]
            ],
        ),
    ]
    fail = summary["failure"]
    if fail:
        parts += [
            "## Failing response",
            f"- reason: {fail['reason']} (step {fail['step_rate_rps']} rps, "
            f"request #{fail['request_index_in_step']})",
            f"- status: {fail['status']}, error: {fail['error']}, retry-after: {fail['retry_after']}",
            f"- headers: `{fail['headers']}`",
            f"- body: `{fail['body']}`",
        ]
    not_reached = summary.get("rate_not_reached")
    if not_reached:
        parts += [
            "## Rate not reached (latency-limited, not a ban)",
            f"- target {not_reached['rate_rps']} rps, achieved {not_reached['achieved_rps']:.3f} rps",
            f"- {not_reached['reason']}",
        ]
    parts.append(f"## Caveat\n\n{summary['caveat']}")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------------
# offline fixture (used by --selftest and by the tests): a fake LBank behind httpx.MockTransport
# ---------------------------------------------------------------------------------


def _ok(data: Any, now_ms: int) -> httpx.Response:
    return httpx.Response(200, json={"result": "true", "data": data, "error_code": 0, "ts": now_ms})


def _fixture_book(mid: Decimal, size: int) -> dict[str, Any]:
    tick, qty = Decimal("0.1"), Decimal("0.05")
    bids = [[str(mid - tick * (i + 1)), str(qty * (1 + i % 5))] for i in range(size)]
    asks = [[str(mid + tick * (i + 1)), str(qty * (1 + i % 5))] for i in range(size)]
    return {"bids": bids, "asks": asks}


def fixture_transport(
    env: VirtualEnv,
    *,
    clock_offset_ms: int = 120,
    max_depth_size: int = 60,
    throttle_after: int | None = None,
    throttle_path: str = "/v2/depth.do",
    latency_s: float = 0.15,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """A deterministic fake of the ten LBank endpoints. ``throttle_after`` makes the N-th request to
    ``throttle_path`` (and every later one) answer 429 with a Retry-After header."""
    counts: Counter[str] = Counter()
    history_start = int(datetime(2018, 6, 1, tzinfo=UTC).timestamp())

    def kline_page(params: Mapping[str, str]) -> list[list[Any]]:
        secs = {"day1": 86_400, "hour4": 14_400, "hour1": 3_600, "minute1": 60}[params["type"]]
        start = max(int(params["time"]), history_start)
        start += (-start) % secs
        now_s = int(env.now().timestamp())
        n = max(0, min(int(params["size"]), 2000, (now_s - start) // secs + 1))
        return [[start + i * secs, "60000", "60100", "59900", "60050", "12.5"] for i in range(n)]

    def handler(request: httpx.Request) -> httpx.Response:
        env.t += latency_s
        if seen is not None:
            seen.append(request)
        path, params = request.url.path, dict(request.url.params)
        counts[path] += 1
        now_ms = int(env.now().timestamp() * 1000)
        if throttle_after is not None and path == throttle_path and counts[path] >= throttle_after:
            return httpx.Response(
                429,
                headers={"Retry-After": "10"},
                json={"result": "false", "error_code": 10015, "msg": "too many requests"},
            )
        if path == "/v2/timestamp.do":
            return _ok(now_ms + clock_offset_ms, now_ms)
        if path == "/v2/accuracy.do":
            rows = [
                {
                    "symbol": "eth_usdt",
                    "quantityAccuracy": "4",
                    "minTranQua": "0.001",
                    "priceAccuracy": "2",
                    "minOrderAmount": "5",
                },
                {
                    "symbol": "btc_usdt",
                    "quantityAccuracy": "6",
                    "minTranQua": "0.0001",
                    "priceAccuracy": "2",
                    "minOrderAmount": "5",
                },
            ]
            return _ok(rows, now_ms)
        if path == "/v2/depth.do":
            size = int(params["size"])
            if size > max_depth_size:
                return httpx.Response(
                    200, json={"result": "false", "error_code": 10008, "msg": "invalid size"}
                )
            mid = Decimal("60000.05") + Decimal(counts[path] % 7)
            return _ok(_fixture_book(mid, size), now_ms)
        if path == "/v2/supplement/trades.do":
            n = min(int(params["size"]), 600)
            trades = [
                {
                    "id": f"fixture-{i}",
                    "time": now_ms - (n - i) * 400,
                    "price": "60000.1",
                    "qty": "0.01",
                    "quoteQty": "600.001",
                    "isBuyerMaker": i % 2 == 0,
                }
                for i in range(n)
            ]
            return _ok(trades, now_ms)
        if path == "/v2/kline.do":
            return _ok(kline_page(params), now_ms)
        if path == "/v2/ticker/24hr.do":
            return _ok(
                [
                    {
                        "symbol": "btc_usdt",
                        "timestamp": now_ms,
                        "ticker": {
                            "latest": "60000.2",
                            "vol": "1234.5",
                            "high": "61000",
                            "low": "59000",
                            "change": "1.2",
                            "turnover": "74000000",
                        },
                    }
                ],
                now_ms,
            )
        if path == "/cfd/openApi/v1/pub/marketData":
            row = {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "markedPrice": "60000.4",
                "lastPrice": "60000.3",
                "nextFeeTime": now_ms + 3_600_000,
            }
            return httpx.Response(
                200,
                json={"result": "true", "success": True, "error_code": 0, "msg": "Success", "data": [row]},
            )
        return httpx.Response(404, json={"result": "false", "msg": "unknown fixture path"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lbank_probe.py", description="Public-only LBank G0 probe. No keys, no private endpoints."
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--ratelimit", action="store_true", help="rate-limit discovery mode (run once)")
    mode.add_argument("--selftest", action="store_true", help="offline fixture run, no network")
    mode.add_argument(
        "--summarize", type=Path, metavar="JSONL", help="rebuild summary + markdown from a JSONL"
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/lbank_probe"),
        help="output directory (default: %(default)s)",
    )
    p.add_argument(
        "--samples", type=int, default=32, help="sampling mode: number of samples (default: %(default)s)"
    )
    p.add_argument(
        "--interval", type=float, default=720.0, help="seconds between sample starts (default: %(default)s)"
    )
    p.add_argument(
        "--request-gap",
        type=float,
        default=DEFAULT_REQUEST_GAP_S,
        help="min seconds between requests in a sample",
    )
    p.add_argument("--no-kline-history", action="store_true", help="skip the one-off kline history probe")
    p.add_argument("--ratelimit-endpoint", choices=sorted(RATELIMIT_ENDPOINTS), default="depth")
    p.add_argument(
        "--ratelimit-steps",
        type=float,
        nargs="+",
        default=[1.0, 2.0, 3.0, 5.0],
        help="req/s per step, each <= 5",
    )
    p.add_argument("--step-seconds", type=float, default=30.0)
    p.add_argument("--pause-seconds", type=float, default=30.0)
    p.add_argument(
        "--host-label",
        type=str,
        default=None,
        help=(
            "override the reported hostname (item 7) -- e.g. inside Docker the kernel hostname "
            "is the container id, not anything a human running the probe would recognise"
        ),
    )
    return p


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.samples < 1:
        parser.error("--samples must be >= 1")
    if args.interval < 0:
        parser.error("--interval must be >= 0")
    if args.request_gap < MIN_REQUEST_GAP_S:
        parser.error(f"--request-gap must be >= {MIN_REQUEST_GAP_S} s (never above {MAX_RATE_RPS:g} req/s)")
    if any(r <= 0 or r > MAX_RATE_RPS for r in args.ratelimit_steps):
        parser.error(f"every --ratelimit-steps value must be in (0, {MAX_RATE_RPS:g}] req/s")
    if args.step_seconds <= 0 or args.pause_seconds < 0:
        parser.error("--step-seconds must be > 0 and --pause-seconds >= 0")


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def summarize_file(path: Path, out_dir: Path, *, now: Callable[[], datetime] = _utcnow) -> int:
    records = read_jsonl(path)
    prov = next((r for r in records if r.get("kind") == "provenance"), None)
    if prov is None:
        _stderr(f"no provenance record in {path}")
        return 1
    samples = [r for r in records if r.get("kind") == "sample"]
    end = next((r for r in records if r.get("kind") == "end"), {})
    prov = {k: v for k, v in prov.items() if k != "kind"}
    prov["utc_end"] = end.get("utc_end", prov.get("utc_end"))
    prov["summarized_from"] = str(path)
    ban_record = next((r for r in records if r.get("kind") == "stopped_on_ban"), None)
    summary = build_summary(
        prov,
        samples,
        discovery=next((r for r in records if r.get("kind") == "depth_size_discovery"), None),
        history=next((r for r in records if r.get("kind") == "kline_history"), None),
        planned_samples=len(samples),
        interval_s=0.0,
        interrupted=bool(end.get("interrupted")),
        finished=bool(end),
        stopped_on_ban={k: v for k, v in ban_record.items() if k != "kind"} if ban_record else None,
    )
    paths = OutputPaths.for_run(out_dir, "lbank_probe_resummary", _stamp(now()))
    _publish(paths, summary, render_markdown(summary))
    print(f"verdict={summary['g0']['verdict']} summary={paths.summary} markdown={paths.markdown}")
    return 0


def run_selftest(out_dir: Path | None) -> int:
    """Both modes against the fake LBank in virtual time. Output is labelled as fixture data."""
    with tempfile.TemporaryDirectory() as tmp:
        target = out_dir or Path(tmp)
        env = VirtualEnv()
        prov = collect_provenance(env, "selftest-sampling", ["--selftest"], data_source="offline_fixture")
        paths = OutputPaths.for_run(target, "lbank_selftest", _stamp(env.now()))
        with Fetcher(env, transport=fixture_transport(env)) as fetcher:
            code, summary = run_sampling(
                fetcher,
                env,
                samples=G0_MIN_SAMPLES + 2,
                interval_s=720.0,
                paths=paths,
                provenance=prov,
                log=_stderr,
            )
        ok = code == 0 and summary["g0"]["verdict"] == "PASS"
        env2 = VirtualEnv()
        prov2 = collect_provenance(env2, "selftest-ratelimit", ["--selftest"], data_source="offline_fixture")
        paths2 = OutputPaths.for_run(
            target, "lbank_selftest_ratelimit", _stamp(env2.now()), summary_suffix=".json"
        )
        with Fetcher(
            env2, transport=fixture_transport(env2, throttle_after=100), min_gap_s=MIN_REQUEST_GAP_S
        ) as f2:
            code2, summary2 = run_ratelimit(
                f2,
                env2,
                endpoint="depth",
                steps=[1.0, 2.0, 3.0, 5.0],
                step_seconds=30.0,
                pause_seconds=30.0,
                paths=paths2,
                provenance=prov2,
                log=_stderr,
            )
        last_safe2 = summary2["last_safe_rate_rps"]
        ok = ok and code2 == 4 and last_safe2 is not None and abs(last_safe2 - 2.0) < 0.2
        print(f"selftest {'OK' if ok else 'FAILED'} (fixture data, not a measurement); outputs in {target}")
        return 0 if ok else 1


def main(
    argv: Sequence[str] | None = None, *, env: Env | None = None, transport: httpx.BaseTransport | None = None
) -> int:
    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    _validate(args, parser)
    if args.selftest:
        return run_selftest(args.out_dir if "--out-dir" in raw_argv else None)
    env = env or Env()
    if args.summarize:
        return summarize_file(args.summarize, args.out_dir, now=env.now)
    ratelimit = bool(args.ratelimit)
    prov = collect_provenance(
        env, "ratelimit" if ratelimit else "sampling", raw_argv, host_label=args.host_label
    )
    prefix = "lbank_ratelimit" if ratelimit else "lbank_probe"
    paths = OutputPaths.for_run(
        args.out_dir, prefix, _stamp(env.now()), summary_suffix=".json" if ratelimit else ".summary.json"
    )
    restore = _install_sigterm_handler()
    try:
        with Fetcher(
            env, transport=transport, min_gap_s=MIN_REQUEST_GAP_S if ratelimit else args.request_gap
        ) as fetcher:
            if ratelimit:
                code, _ = run_ratelimit(
                    fetcher,
                    env,
                    endpoint=args.ratelimit_endpoint,
                    steps=args.ratelimit_steps,
                    step_seconds=args.step_seconds,
                    pause_seconds=args.pause_seconds,
                    paths=paths,
                    provenance=prov,
                    log=_stderr,
                )
            else:
                code, _ = run_sampling(
                    fetcher,
                    env,
                    samples=args.samples,
                    interval_s=args.interval,
                    paths=paths,
                    provenance=prov,
                    kline_history=not args.no_kline_history,
                    log=_stderr,
                )
    finally:
        restore()
    if code == 1 and not ratelimit:
        _stderr(
            f"LBank unreachable from {prov['hostname']}: no HTTP response to any request in the first sample"
        )
    print(f"exit={code} outputs: {paths.summary} {paths.markdown} {paths.jsonl}")
    return code


if __name__ == "__main__":
    sys.exit(main())
