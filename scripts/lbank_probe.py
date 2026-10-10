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

Exit codes: 0 = G0 PASS (or the rate-limit run completed without incident), 2 = G0 FAIL,
3 = G0 INCOMPLETE (sampling plan not met, no failed check), 4 = rate-limit run stopped early on a
throttling/ban-type response, 1 = exchange unreachable, 130 / 143 = interrupted by SIGINT / SIGTERM
(partial outputs are still written).
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
from typing import Any, Literal

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
    "Env",
    "Fetcher",
    "FillResult",
    "HttpResult",
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
REQUEST_TIMEOUT_S = 10.0
MAX_RATE_RPS = 5.0  # hard ceiling, never exceeded in any mode
MIN_REQUEST_GAP_S = 1.0 / MAX_RATE_RPS
DEFAULT_REQUEST_GAP_S = 0.6  # sampling: ~1.7 req/s at most inside one sample
DEPTH_SIZE_CANDIDATES: tuple[int, ...] = (200, 100, 60, 20)
KLINE_HISTORY_SIZE = 2000
KLINE_SAMPLE_BARS = 10
EARLIEST_PROBE_TS_S = 1_483_228_800  # 2017-01-01 UTC, far before any LBank BTC/USDT history
TRADES_SIZE = 600
BODY_SNIPPET_CHARS = 2000

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

# Throttling / ban-type reactions in rate-limit mode.
BAN_STATUSES = (403, 418)
MAX_CONSECUTIVE_5XX = 3
MAX_CONSECUTIVE_TRANSPORT_ERRORS = 3

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class EndpointSpec:
    """One whitelisted public GET endpoint."""

    name: str
    base: str
    path: str


# The complete request whitelist. Nothing else is ever requested by this script.
ENDPOINTS: dict[str, EndpointSpec] = {
    "timestamp": EndpointSpec("timestamp", SPOT_BASE_URL, "/v2/timestamp.do"),
    "accuracy": EndpointSpec("accuracy", SPOT_BASE_URL, "/v2/accuracy.do"),
    "depth": EndpointSpec("depth", SPOT_BASE_URL, "/v2/depth.do"),
    "trades": EndpointSpec("trades", SPOT_BASE_URL, "/v2/supplement/trades.do"),
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
    """Script git commit: env ``TBOT_GIT_COMMIT``, else ``git rev-parse HEAD``; ``unknown`` if absent."""
    from_env = os.environ.get("TBOT_GIT_COMMIT", "").strip()
    if from_env:
        return from_env
    repo = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "scripts/lbank_probe.py"],
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
    env: Env, mode: str, argv: Sequence[str], *, data_source: Literal["live", "offline_fixture"] = "live"
) -> dict[str, Any]:
    started = env.now()
    host = socket.gethostname()
    return {
        "hostname": host,
        "utc_start": started.isoformat(),
        "utc_end": None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "script": "scripts/lbank_probe.py",
        "git_commit": resolve_git_commit(),
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
    headers: dict[str, str] = field(default_factory=dict)
    body_snippet: str | None = None

    @property
    def http_ok(self) -> bool:
        return self.status == 200

    @property
    def ok(self) -> bool:
        return self.http_ok and self.payload_ok and self.error is None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "endpoint": self.endpoint,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
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
            response = self._client.get(url, params=dict(params or {}))
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
            headers=headers,
            body_snippet=snippet,
        )


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


def parse_kline_rows(body: Any) -> list[tuple[int, ...]]:
    """``(open_ts_s, ...)`` rows; millisecond timestamps are normalised to seconds."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise ValueError("kline data is not a list")
    rows: list[tuple[int, ...]] = []
    for item in data:
        ts = int(item[0])
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

    def __init__(self, fetcher: Fetcher, env: Env, *, depth_size: int = DEPTH_SIZE_CANDIDATES[-1]) -> None:
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
    fetcher: Fetcher, candidates: Sequence[int] = DEPTH_SIZE_CANDIDATES
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
    fetcher: Fetcher, tf: str, label: str, start_s: int
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
        except (ValueError, TypeError, IndexError) as exc:
            info["parse_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return info, rows


def probe_kline_history(fetcher: Fetcher, env: Env) -> dict[str, Any]:
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
    by_ep: dict[str, list[Mapping[str, Any]]] = {}
    for sample in samples:
        for req in sample.get("requests", []):
            by_ep.setdefault(req["endpoint"], []).append(req)
    out: dict[str, Any] = {}
    for name, reqs in sorted(by_ep.items()):
        lat = [float(r["latency_ms"]) for r in reqs if r.get("status") is not None]
        out[name] = {
            "n": len(reqs),
            "ok": sum(1 for r in reqs if r.get("ok")),
            "http_not_200": sum(1 for r in reqs if r.get("status") != 200),
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


def evaluate_g0(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pass/fail of the four D-073 pass conditions plus the sampling-plan check."""
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
    clock = summarize_clock(samples)
    book = summarize_book(samples)
    acc = [s["accuracy"] for s in samples if isinstance(s.get("accuracy"), dict)]
    acc_bad = [a for a in acc if not a.get("ok")]
    med_skew = clock.get("median_abs_delta_ms")
    med_spread = book["spread_pct"]["median"]
    checks = {
        "all_endpoints_200": {
            "pass": bool(reqs) and not failures,
            "observed": f"{len(reqs) - len(failures)}/{len(reqs)} requests were HTTP 200 + success payload",
            "threshold": "all",
            "failures": failures[:50],
        },
        "clock_offset": {
            "pass": med_skew is not None and med_skew < G0_MAX_MEDIAN_SKEW_MS,
            "observed": (
                f"median abs(server - local) = {med_skew:.0f} ms" if med_skew is not None else "no data"
            ),
            "threshold": f"< {G0_MAX_MEDIAN_SKEW_MS:.0f} ms",
            "uncertainty_ms": clock.get("median_half_rtt_ms"),
        },
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
    span = _span_s(samples)
    plan_ok = len(samples) >= G0_MIN_SAMPLES and span >= G0_MIN_SPAN_S
    failed = [name for name, c in checks.items() if not c["pass"]]
    verdict = "FAIL" if failed else ("PASS" if plan_ok else "INCOMPLETE")
    return {
        "verdict": verdict,
        "failed_checks": failed,
        "checks": checks,
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
) -> dict[str, Any]:
    return {
        "provenance": dict(provenance),
        "mode": "sampling",
        "status": {"finished": finished, "interrupted": interrupted},
        "plan": {
            "planned_samples": planned_samples,
            "interval_s": interval_s,
            "completed_samples": len(samples),
        },
        "g0": evaluate_g0(samples),
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
                [n, c["observed"], c["threshold"], "PASS" if c["pass"] else "FAIL"]
                for n, c in g0["checks"].items()
            ],
        ),
        f"\nSampling plan: {g0['plan']['samples']} samples over {g0['plan']['span_hours']} h "
        f"(required: >= {g0['plan']['min_samples']} samples, >= {g0['plan']['min_span_hours']:.0f} h) "
        f"-> plan_ok = {g0['plan']['plan_ok']}. Interrupted: {summary['status']['interrupted']}.",
        "## Latency per endpoint (ms)",
        _table(
            ["endpoint", "n", "ok", "not-200", "p50", "p95", "max"],
            [
                [k, v["n"], v["ok"], v["http_not_200"], v["p50_ms"], v["p95_ms"], v["max_ms"]]
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
    """Run the whole sampling plan; returns ``(exit_code, summary)``. Always writes all outputs."""
    writer = JsonlWriter(paths.jsonl)
    writer.write({"kind": "provenance", **provenance})
    runner = SampleRunner(fetcher, env)
    done: list[dict[str, Any]] = []
    discovery: dict[str, Any] | None = None
    history: dict[str, Any] | None = None
    interrupted = unreachable = False
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
        )
        _publish(paths, summary, render_markdown(summary))
        return summary

    try:
        for i in range(samples):
            wait = start + i * interval_s - env.monotonic()
            if wait > 0:
                env.sleep(wait)
            if i == 0:
                discovery = discover_depth_size(fetcher)
                writer.write(discovery)
                runner.depth_size = discovery["chosen_size"] or DEPTH_SIZE_CANDIDATES[-1]
            sample = runner.run(i)
            done.append(sample)
            writer.write(sample)
            log(f"sample {i + 1}/{samples} at {sample['utc_start']}: " + _one_line(sample))
            if i == 0 and all(r["status"] is None for r in sample["requests"]):
                unreachable = True
                break
            if i == 0 and kline_history:
                history = probe_kline_history(fetcher, env)
                writer.write(history)
            snapshot(finished=False)
    except KeyboardInterrupt:
        interrupted = True
    provenance["utc_end"] = env.now().isoformat()
    summary = snapshot(finished=not interrupted and not unreachable)
    writer.write(
        {
            "kind": "end",
            "utc_end": provenance["utc_end"],
            "interrupted": interrupted,
            "unreachable": unreachable,
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
    """Decides, response by response, whether the rate-limit run must stop immediately."""

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
            if status != 200:
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
    """One fixed-rate step. Serial; a slow response delays the schedule, it never causes a burst."""
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
    step = {
        "rate_rps": rate,
        "step_seconds": step_seconds,
        "requests_sent": sent,
        "achieved_rps": sent / elapsed,
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
    """Stepped, stop-at-first-throttle rate discovery. No retry is ever made after a stop."""
    spec_key, params = RATELIMIT_ENDPOINTS[endpoint]
    writer = JsonlWriter(paths.jsonl)
    writer.write({"kind": "provenance", **provenance})
    tracker = StopTracker()
    results: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
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
            log(f"step {rate} rps: {step['requests_sent']} requests, statuses {step['statuses']}")
            if failure is not None:
                break
            last_safe = rate
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
        "stopped_early": failure is not None,
        "interrupted": interrupted,
        "last_safe_rate_rps": last_safe,
        "max_rate_tested_rps": max((s["rate_rps"] for s in results), default=None),
        "failure": failure,
        "caveat": "A run that completes proves only that these rates were tolerated for the step length; it "
        "does not prove there is no limit. Limits may be per endpoint or per window.",
    }
    _write_atomic(paths.summary, _dumps(summary, indent=2) + "\n")
    _write_atomic(paths.markdown, render_ratelimit_markdown(summary))
    writer.write({"kind": "end", "utc_end": provenance["utc_end"], "stopped_early": failure is not None})
    writer.close()
    if interrupted:
        return (143 if _SIGTERM_RECEIVED else 130), summary
    return (4 if failure is not None else 0), summary


def render_ratelimit_markdown(summary: Mapping[str, Any]) -> str:
    parts = [
        "# LBank rate-limit discovery (public endpoint, stepped, stop at first throttle)",
        _provenance_md(summary["provenance"]),
        f"Endpoint: `{summary['endpoint']}` -> `{summary['request']['url']}` {summary['request']['params']}",
        f"## Result: last safe rate = {summary['last_safe_rate_rps']} req/s; "
        f"stopped early = {summary['stopped_early']}; interrupted = {summary['interrupted']}",
        _table(
            ["rate rps", "sent", "achieved rps", "statuses", "p50 ms", "p95 ms", "clean"],
            [
                [
                    s["rate_rps"],
                    s["requests_sent"],
                    s["achieved_rps"],
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


def summarize_file(path: Path, out_dir: Path) -> int:
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
    summary = build_summary(
        prov,
        samples,
        discovery=next((r for r in records if r.get("kind") == "depth_size_discovery"), None),
        history=next((r for r in records if r.get("kind") == "kline_history"), None),
        planned_samples=len(samples),
        interval_s=0.0,
        interrupted=bool(end.get("interrupted")),
        finished=bool(end),
    )
    paths = OutputPaths.for_run(out_dir, "lbank_probe_resummary", _stamp(datetime.now(UTC)))
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
        ok = ok and code2 == 4 and summary2["last_safe_rate_rps"] == 2.0
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
    if args.summarize:
        return summarize_file(args.summarize, args.out_dir)
    env = env or Env()
    ratelimit = bool(args.ratelimit)
    prov = collect_provenance(env, "ratelimit" if ratelimit else "sampling", raw_argv)
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
