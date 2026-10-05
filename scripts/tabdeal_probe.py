"""Strictly read-only Tabdeal probe.

Answers every open question in ``docs/SPEC.md`` section 9 and the G0 criteria in section 7,
using only public market-data reads and (if credentials are present) the signed account-balances
read. It never submits, cancels or queries an order, and the real order path in
``tbot.execution`` is never imported here.

Usage::

    uv run python scripts/tabdeal_probe.py [--config NAME] [--out PATH]
        [--samples N] [--interval SECONDS] [--symbols SYM [SYM ...]]
        [--trades-limit-candidates N [N ...]]

Exit codes:

* ``0`` -- probe completed (individual checks may still have failed; see the JSON report).
* ``1`` -- the exchange is unreachable (timeout/connection failure) or outright blocked
  (HTTP 403/451) on *both* path prefixes for the most basic ``ping`` call. This is printed as
  ONE line naming the URL, status and likely cause -- no stack trace -- because that single
  result already answers G0's main question.
* ``2`` -- (M5) the probe completed and the report was written, but the Tabdeal API key has
  trade or withdrawal permission enabled (see ``report["key_permissions_unsafe"]``).
  CLAUDE.md section 3.6 requires a read-only key before phase 6 -- this must not be silently
  exit-0'd by CI or an operator's shell script.
* ``3`` -- the probe completed but the key's permissions could not be determined from the
  account response (``report["key_permissions"] == "unknown"``); verify in the Tabdeal UI.
* ``130`` / ``143`` -- (m9a) the probe was interrupted mid-run by Ctrl+C/SIGINT or by SIGTERM
  (e.g. systemd stopping the service) respectively, during what can be an hours-long
  depth-sampling loop. A partial report (``report["interrupted"] = true``) is still written to
  ``--out`` -- whatever was collected before the interruption is never silently lost.

Nothing here is a real order, and nothing here can become one: see
``src/tbot/execution/tabdeal_client.py`` for the read-only surface this script calls.
"""

from __future__ import annotations

import argparse
import itertools
import json
import signal
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from tbot.core.config import Config, Secrets, load_config
from tbot.core.types import Clock, SymbolFilters

# Re-exported unchanged from tbot.data.depth (decision D-025: the recorder used to import this
# maths from this script, the wrong dependency direction for a library module; the shared
# implementation now lives in tbot.data.depth and this script imports it back). The explicit
# "as <same name>" form is the standard re-export idiom under mypy's no_implicit_reexport.
from tbot.data.depth import best_bid_ask as best_bid_ask
from tbot.data.depth import cumulative_depth as cumulative_depth
from tbot.data.depth import is_saturated as is_saturated
from tbot.data.depth import parse_depth_levels as parse_depth_levels
from tbot.data.depth import spread_bps_and_pct as spread_bps_and_pct
from tbot.execution.tabdeal_client import ProbeResult, TabdealClient
from tbot.monitoring.logging import configure_logging, register_secrets_for_logging

__all__ = [
    "SystemClock",
    "build_symbol_filters",
    "classify_key_permissions",
    "compute_clock_skew_ms",
    "cumulative_depth",
    "describe_unreachable_failure",
    "detect_id_space",
    "detect_time_unit",
    "discover_max_trades_limit",
    "extract_nonzero_balances",
    "find_base_asset_markets",
    "find_symbol_entry",
    "ids_contiguous_in_window",
    "ids_monotonic_in_time",
    "is_saturated",
    "main",
    "probe_both_prefixes",
    "spread_bps_and_pct",
    "summarize_spreads",
    "trade_activity_stats",
    "trades_window_stats",
]

_DEFAULT_TRADES_LIMIT_CANDIDATES: tuple[int, ...] = (100, 500, 1000, 2000, 5000, 10_000)
_DEPTH_THRESHOLDS_PCT: tuple[Decimal, ...] = (Decimal("0.001"), Decimal("0.005"), Decimal("0.01"))


# ---------------------------------------------------------------------------------
# wall clock -- allowed here (scripts/ is not core/risk/execution), forbidden inside the client
# ---------------------------------------------------------------------------------


class SystemClock:
    """Real wall-clock :class:`tbot.core.types.Clock`. Handed into the execution-layer client
    from the outside so that ``tabdeal_client.py`` itself never reads the wall clock directly."""

    def now(self) -> datetime:
        return datetime.now(UTC)


# ---------------------------------------------------------------------------------
# pure helpers (unit-tested without any network access)
# ---------------------------------------------------------------------------------


def compute_clock_skew_ms(server_time_ms: int, local_before: datetime, local_after: datetime) -> float:
    """Server time minus the local clock midpoint around the request, in ms.

    Using the midpoint of "just before send" and "just after receive" cancels out roughly half
    of the round-trip latency, which a single-ended reading would not.
    """
    midpoint_ms = (local_before.timestamp() + local_after.timestamp()) / 2 * 1000
    return float(server_time_ms) - midpoint_ms


def summarize_spreads(spreads_bps: Sequence[float]) -> dict[str, float]:
    """Median/p95/min/max of repeated spread samples (G0 requires >= 30 samples)."""
    if not spreads_bps:
        return {}
    ordered = sorted(spreads_bps)
    n = len(ordered)
    quantiles = statistics.quantiles(ordered, n=100, method="inclusive") if n >= 2 else ordered
    p95 = quantiles[94] if n >= 2 else ordered[0]
    return {
        "samples": float(n),
        "median_bps": statistics.median(ordered),
        "p95_bps": p95,
        "min_bps": ordered[0],
        "max_bps": ordered[-1],
    }


TimeUnit = Literal["s", "ms", "us", "unknown"]

# m2 (reviewer finding): the recorder (tbot.data.tabdeal_recorder) assumes Tabdeal's /trades
# `time` field is already integer milliseconds -- a Binance-style assumption that has never been
# checked against this exchange specifically. Classify by order of magnitude: a current-era epoch
# value is ~10 digits in seconds, ~13 in milliseconds, ~16 in microseconds. The boundaries below
# sit mid-way between those bands (not at round powers of ten at the band edges) so they are not
# fragile to which exact year the probe happens to run in.
_SECONDS_MAGNITUDE_CEILING = 10**11
_MS_MAGNITUDE_CEILING = 10**14
_US_MAGNITUDE_CEILING = 10**17


def detect_time_unit(times: Sequence[int]) -> TimeUnit:
    """Classify the unit of a ``/trades`` window's ``time`` values by magnitude (m2).

    Uses the *median* magnitude across the window, not just the first element, so one corrupted
    or zero timestamp cannot flip the classification for an otherwise-consistent window.
    Returns ``"unknown"`` for an empty window or a magnitude outside all three known bands.
    """
    if not times:
        return "unknown"
    magnitude = statistics.median(abs(t) for t in times)
    if magnitude < _SECONDS_MAGNITUDE_CEILING:
        return "s"
    if magnitude < _MS_MAGNITUDE_CEILING:
        return "ms"
    if magnitude < _US_MAGNITUDE_CEILING:
        return "us"
    return "unknown"


def ids_contiguous_in_window(ids: Sequence[int]) -> tuple[bool | None, int]:
    """``(contiguous, missing_count)`` for a window's trade ids (m2).

    ``contiguous`` is ``max(ids) - min(ids) + 1 == count`` (accounting for any duplicate id,
    which would otherwise be misread as a gap). ``(None, 0)`` when there are fewer than two
    distinct ids to judge contiguity from at all.
    """
    distinct = set(ids)
    if len(distinct) < 2:
        return None, 0
    span = max(distinct) - min(distinct) + 1
    missing = span - len(distinct)
    return missing == 0, missing


IdSpace = Literal["global", "per_symbol", "unknown"]


def _ids_and_times(trades: Any) -> list[tuple[int, int]]:
    """``(id, time)`` pairs for every well-formed item of a ``/trades`` body."""
    if not isinstance(trades, list):
        return []
    pairs: list[tuple[int, int]] = []
    for item in trades:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("time"), int):
            pairs.append((item["id"], item["time"]))
    return pairs


def ids_monotonic_in_time(trades: Any) -> bool | None:
    """Whether trade ids strictly increase with trade time inside one symbol's window.

    This is the property the recorder's continuity rule depends on (a later trade always has a
    larger id), independent of whether ids are contiguous. ``None`` with fewer than two trades.
    """
    pairs = sorted(_ids_and_times(trades), key=lambda p: (p[1], p[0]))
    if len(pairs) < 2:
        return None
    return all(b[0] > a[0] for a, b in itertools.pairwise(pairs))


def detect_id_space(trades_a: Any, trades_b: Any) -> IdSpace:
    """Tell a global (cross-market) trade-id space from per-symbol numbering.

    Two symbols' windows whose id ranges interleave without sharing a single id point to one
    global id sequence (measured on the Turkey server, 2026-10-04: BTCUSDT and ETHUSDT ids
    interleave). Shared ids point to per-symbol numbering. Disjoint ranges or identical windows
    (e.g. the same body twice) prove nothing.
    """
    ids_a = {i for i, _ in _ids_and_times(trades_a)}
    ids_b = {i for i, _ in _ids_and_times(trades_b)}
    if len(ids_a) < 2 or len(ids_b) < 2 or ids_a == ids_b:
        return "unknown"
    if ids_a & ids_b:
        return "per_symbol"
    overlap = min(ids_a) <= max(ids_b) and min(ids_b) <= max(ids_a)
    return "global" if overlap else "unknown"


def trade_activity_stats(trades: Any) -> dict[str, float | int | None]:
    """Trades per full UTC hour (min/median/max) and the longest gap between trades.

    The recorder's staleness threshold and the G1b "hours complete" criterion both depend on
    how thin the market is, so the probe measures it instead of assuming it.
    """
    times = sorted(t for _, t in _ids_and_times(trades))
    if len(times) < 2:
        return {"full_hours": 0, "empty_full_hours": None, "trades_per_hour_min": None,
                "trades_per_hour_median": None, "trades_per_hour_max": None,
                "max_inter_trade_gap_seconds": None}
    per_hour: dict[int, int] = {}
    for t in times:
        per_hour[t // 3_600_000] = per_hour.get(t // 3_600_000, 0) + 1
    first, last = times[0] // 3_600_000, times[-1] // 3_600_000
    counts = [per_hour.get(h, 0) for h in range(first + 1, last)]  # full hours only
    max_gap = max(b - a for a, b in itertools.pairwise(times)) / 1000.0
    return {
        "full_hours": len(counts),
        "empty_full_hours": sum(1 for c in counts if c == 0) if counts else None,
        "trades_per_hour_min": min(counts) if counts else None,
        "trades_per_hour_median": statistics.median(counts) if counts else None,
        "trades_per_hour_max": max(counts) if counts else None,
        "max_inter_trade_gap_seconds": max_gap,
    }


@dataclass(frozen=True, slots=True)
class TradesWindowStats:
    count: int
    min_id: int | None
    max_id: int | None
    span_seconds: float | None
    saturated: bool
    recommended_poll_interval_seconds: float | None
    time_unit: TimeUnit
    time_unit_disagrees_with_ms: bool
    ids_contiguous: bool | None
    missing_ids: int


def trades_window_stats(trades: Any, requested_limit: int) -> TradesWindowStats:
    """Count/id-range/time-span/saturation for one ``/trades`` response, plus a recommended
    polling interval with a 3x safety margin against saturating the same ``limit`` again.

    m2 (reviewer finding): also reports the detected ``time`` unit (the recorder assumes ms --
    this checks that assumption rather than trusting it) and whether trade ids are contiguous
    across the window (the recorder assumes no gap between consecutive polls).
    """
    if not isinstance(trades, list):
        return TradesWindowStats(
            count=0,
            min_id=None,
            max_id=None,
            span_seconds=None,
            saturated=False,
            recommended_poll_interval_seconds=None,
            time_unit="unknown",
            time_unit_disagrees_with_ms=False,
            ids_contiguous=None,
            missing_ids=0,
        )
    ids: list[int] = []
    times: list[int] = []
    for item in trades:
        if not isinstance(item, dict):
            continue
        trade_id = item.get("id")
        trade_time = item.get("time")
        if isinstance(trade_id, int):
            ids.append(trade_id)
        if isinstance(trade_time, int):
            times.append(trade_time)
    count = len(trades)
    span_seconds = (max(times) - min(times)) / 1000.0 if len(times) >= 2 else None
    saturated = is_saturated(requested_limit, count)
    recommended: float | None = None
    safety_margin = 3.0
    if span_seconds and span_seconds > 0 and count > 1:
        trade_rate = count / span_seconds
        recommended = requested_limit / (safety_margin * trade_rate)
    time_unit = detect_time_unit(times)
    # Only claim a disagreement when there is at least one time value to disagree with --
    # an empty/missing `time` field is "no evidence", not "not milliseconds".
    time_unit_disagrees_with_ms = bool(times) and time_unit != "ms"
    ids_contiguous, missing_ids = ids_contiguous_in_window(ids)
    return TradesWindowStats(
        count=count,
        min_id=min(ids) if ids else None,
        max_id=max(ids) if ids else None,
        span_seconds=span_seconds,
        saturated=saturated,
        recommended_poll_interval_seconds=recommended,
        time_unit=time_unit,
        time_unit_disagrees_with_ms=time_unit_disagrees_with_ms,
        ids_contiguous=ids_contiguous,
        missing_ids=missing_ids,
    )


LimitProbeOutcome = Literal["accepted", "rejected", "inconclusive"]


@dataclass(frozen=True, slots=True)
class LimitProbeAttempt:
    requested: int
    status_code: int | None
    returned: int
    ok: bool
    outcome: LimitProbeOutcome


def _classify_trades_limit_result(result: ProbeResult) -> LimitProbeOutcome:
    """Classify one ``/trades`` limit-discovery attempt (M19).

    Only an unambiguous 4xx-other-than-429 means the server actually rejected this ``limit``
    value -- that is a real limit boundary. An exhausted 429 (rate limit, not a limit-too-large
    rejection) or a timeout/5xx/unreachable result says nothing about the true max limit; treating
    either as a rejection would understate ``max_accepted_limit`` and poison the recorder's
    poll-interval design, which assumes the discovered limit is real.
    """
    if result.ok:
        return "accepted"
    status = result.status_code
    if status is not None and 400 <= status < 500 and status != 429:
        return "rejected"
    return "inconclusive"


def discover_max_trades_limit(
    client: TabdealClient, symbol: str, prefix: str, candidates: Sequence[int]
) -> tuple[list[LimitProbeAttempt], int | None]:
    """Probe increasing ``/trades`` limits until the server unambiguously rejects one; report
    the requested and actually-returned counts, plus the classification, at every step.

    Stops at the first non-"accepted" outcome either way (a 429/timeout mid-sweep cannot be
    resumed meaningfully without re-probing), but only a "rejected" outcome is a real boundary --
    an "inconclusive" one means the true limit is simply unknown beyond ``max_accepted_limit``.
    """
    attempts: list[LimitProbeAttempt] = []
    max_accepted: int | None = None
    for limit in sorted(candidates):
        result = client.trades(symbol, limit=limit, prefix=prefix)
        returned = len(result.body) if isinstance(result.body, list) else 0
        outcome = _classify_trades_limit_result(result)
        attempts.append(
            LimitProbeAttempt(
                requested=limit,
                status_code=result.status_code,
                returned=returned,
                ok=result.ok,
                outcome=outcome,
            )
        )
        if outcome == "accepted":
            max_accepted = limit
        else:
            break
    return attempts, max_accepted


def _normalize_symbol_code(value: str) -> str:
    return value.upper().replace("_", "").replace("-", "")


def find_symbol_entry(exchange_info_body: Any, base: str, quote: str) -> dict[str, Any] | None:
    """Find the ``exchangeInfo`` entry for ``base+quote``, checking both ``symbol`` and
    ``tabdealSymbol`` fields (docs/SPEC.md open question 4)."""
    symbols = exchange_info_body.get("symbols") if isinstance(exchange_info_body, dict) else None
    if not isinstance(symbols, list):
        return None
    target = _normalize_symbol_code(f"{base}{quote}")
    for entry in symbols:
        if not isinstance(entry, dict):
            continue
        for key in ("symbol", "tabdealSymbol"):
            value = entry.get(key)
            if isinstance(value, str) and _normalize_symbol_code(value) == target:
                return entry
    return None


def find_base_asset_markets(exchange_info_body: Any, base: str) -> list[dict[str, Any]]:
    """Every market involving ``base`` -- used to report what *does* exist when the expected
    symbol is missing."""
    symbols = exchange_info_body.get("symbols") if isinstance(exchange_info_body, dict) else None
    if not isinstance(symbols, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in symbols:
        if not isinstance(entry, dict):
            continue
        base_asset = entry.get("baseAsset")
        symbol_value = str(entry.get("symbol") or entry.get("tabdealSymbol") or "")
        base_matches = isinstance(base_asset, str) and base_asset.upper() == base.upper()
        if base_matches or base.upper() in symbol_value.upper():
            out.append(entry)
    return out


def _safe_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def build_symbol_filters(entry: dict[str, Any], symbol_label: str) -> SymbolFilters | None:
    """Build a :class:`tbot.core.types.SymbolFilters` from an ``exchangeInfo`` symbol entry's
    ``filters`` array. Returns ``None`` (never raises) if any required filter is missing or
    malformed -- the caller reports that as "unknown filters", not a crash."""
    filters = entry.get("filters")
    if not isinstance(filters, list):
        return None
    tick: Decimal | None = None
    step: Decimal | None = None
    min_qty: Decimal | None = None
    min_notional: Decimal | None = None
    for f in filters:
        if not isinstance(f, dict):
            continue
        filter_type = f.get("filterType")
        if filter_type == "PRICE_FILTER" and "tickSize" in f:
            tick = _safe_decimal(f["tickSize"])
        elif filter_type == "LOT_SIZE":
            if "stepSize" in f:
                step = _safe_decimal(f["stepSize"])
            if "minQty" in f:
                min_qty = _safe_decimal(f["minQty"])
        elif filter_type in ("MIN_NOTIONAL", "NOTIONAL") and "minNotional" in f:
            min_notional = _safe_decimal(f["minNotional"])
    if tick is None or step is None or min_qty is None or min_notional is None:
        return None
    try:
        return SymbolFilters(
            symbol=symbol_label, tick_size=tick, step_size=step, min_qty=min_qty, min_notional=min_notional
        )
    except (ValueError, TypeError):
        return None


KeyPermissionState = Literal["safe", "unsafe", "unknown"]

# m9b (reviewer finding): these are the only fields that actually say anything about trade/
# withdraw permission. `canDeposit` is tracked in `found` for visibility but never drives the
# safe/unsafe/unknown classification -- a key with only `canDeposit` present tells us nothing
# about trade/withdraw permission either way.
_PERMISSION_INDICATOR_KEYS: tuple[str, ...] = ("canTrade", "canWithdraw", "permissions")


def classify_key_permissions(account_body: Any) -> tuple[KeyPermissionState, dict[str, Any]]:
    """Classify the Tabdeal API key's trade/withdraw permission from an ``account`` response.

    Three states, not two (m9b -- reviewer finding): the previous version treated "none of
    canTrade/canWithdraw/permissions are present in the response" as ``unsafe=False``, silently
    reporting "safe" by default and exiting 0 -- CLAUDE.md section 3.6 requires a read-only key
    before phase 6, and that silent fail-open meant a CI check or an operator's shell script could
    treat an *unverified* key as a confirmed-safe one. ``"unknown"`` makes the gap explicit
    instead: a human must then confirm in the Tabdeal UI that this key has no trade and no
    withdrawal permission before phase 6.

    Note for whoever reads the report: Binance-style ``canTrade``/``canWithdraw`` fields, even
    when present, may describe the *account* rather than the specific API *key* making this
    request -- CLAUDE.md section 3.6's requirement is about the key.
    """
    if not isinstance(account_body, dict):
        return "unknown", {}
    found: dict[str, Any] = {}
    for key in ("canTrade", "canWithdraw", "canDeposit", "permissions"):
        if key in account_body:
            found[key] = account_body[key]
    if not any(key in account_body for key in _PERMISSION_INDICATOR_KEYS):
        return "unknown", found
    unsafe = bool(account_body.get("canTrade")) or bool(account_body.get("canWithdraw"))
    permissions = account_body.get("permissions")
    if isinstance(permissions, list):
        upper = {str(p).upper() for p in permissions}
        if upper & {"TRADE", "WITHDRAW", "WITHDRAWALS"}:
            unsafe = True
    return ("unsafe" if unsafe else "safe"), found


def extract_nonzero_balances(account_body: Any) -> list[dict[str, str]]:
    """Balances only -- never any other account field -- and only the non-zero ones."""
    if not isinstance(account_body, dict):
        return []
    balances = account_body.get("balances")
    if not isinstance(balances, list):
        return []
    out: list[dict[str, str]] = []
    for item in balances:
        if not isinstance(item, dict):
            continue
        free = _safe_decimal(item.get("free", "0")) or Decimal("0")
        locked = _safe_decimal(item.get("locked", "0")) or Decimal("0")
        if free == 0 and locked == 0:
            continue
        out.append(
            {
                "asset": str(item.get("asset")),
                "free": str(item.get("free", "0")),
                "locked": str(item.get("locked", "0")),
            }
        )
    return out


def describe_unreachable_failure(result: ProbeResult) -> str:
    """One clear, single-line, stack-trace-free message naming the URL, status and likely cause."""
    if result.status_code == 403:
        cause = "HTTP 403 -- likely a geo-block or an IP not on the exchange's allow-list"
    elif result.status_code == 451:
        cause = "HTTP 451 -- legally blocked for this region"
    elif result.status_code is not None:
        cause = f"HTTP {result.status_code}"
    elif result.error and result.error.startswith("timeout"):
        cause = (
            "request timed out repeatedly -- likely a firewall silently dropping packets, "
            "or the wrong host"
        )
    else:
        cause = (
            f"connection failed ({result.error or 'unknown error'}) -- "
            "likely DNS failure, firewall block, or the wrong host"
        )
    return f"Tabdeal unreachable: {result.url} -> {cause}"


def probe_both_prefixes(
    client: TabdealClient, call: Callable[[str], ProbeResult], endpoint_name: str
) -> tuple[dict[str, Any], ProbeResult]:
    """Call ``call`` once against each configured path prefix; report both, and pick a winner
    (read prefix preferred when both answer) for any follow-up calls to the same endpoint."""
    read_result = call(client.read_prefix)
    write_result = call(client.write_prefix)
    winner: str | None = "read" if read_result.ok else ("write" if write_result.ok else None)
    summary = {
        "endpoint": endpoint_name,
        "read_prefix": _probe_result_summary(client.read_prefix, read_result),
        "write_prefix": _probe_result_summary(client.write_prefix, write_result),
        "answering_prefix": winner,
    }
    chosen = read_result if winner == "read" else write_result if winner == "write" else read_result
    return summary, chosen


def _probe_result_summary(prefix: str, result: ProbeResult) -> dict[str, Any]:
    return {
        "prefix": prefix,
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
        "ok": result.ok,
        "error": result.error,
        "retries": len(result.retries),
    }


def _extract_server_time_ms(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    for key in ("serverTime", "server_time", "time"):
        value = body.get(key)
        if isinstance(value, int):
            return value
    return None


def _filters_to_report(filters: SymbolFilters) -> dict[str, str]:
    report = {
        "symbol": filters.symbol,
        "tick_size": str(filters.tick_size),
        "step_size": str(filters.step_size),
        "min_qty": str(filters.min_qty),
        "min_notional": str(filters.min_notional),
    }
    if filters.max_qty is not None:
        report["max_qty"] = str(filters.max_qty)
    return report


# ---------------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tabdeal_probe.py",
        description="Strictly read-only Tabdeal probe -- answers docs/SPEC.md section 9 and G0.",
    )
    parser.add_argument(
        "--config", default="default", help="config/<name>.yaml to load (default: %(default)s)"
    )
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON report path (default: research/reports/tabdeal_probe_<UTC>.json)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=30,
        help="depth samples for the median-spread G0 check (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        # M3: SPEC section 7 G0 requires >= 30 samples spanning >= 6 hours. 30 * 720s = 21600s
        # (6h) exactly meets that; the old default of 2s gave a 60-second span -- indistinguishable
        # from a six-hour one in the report, but answering a completely different question.
        default=720.0,
        help="seconds between depth samples -- default 720s x 30 samples = 6h, matching G0 "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"], help="symbols to probe (default: %(default)s)"
    )
    parser.add_argument(
        "--trades-limit-candidates",
        nargs="+",
        type=int,
        default=list(_DEFAULT_TRADES_LIMIT_CANDIDATES),
        help="ascending /trades limit values to probe (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _default_out_path(now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return Path("research/reports") / f"tabdeal_probe_{stamp}.json"


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Best-effort split of e.g. ``BTCUSDT`` into ``("BTC", "USDT")`` for a fixed quote asset."""
    for quote in ("USDT", "USD", "IRT"):
        if symbol.upper().endswith(quote):
            return symbol.upper()[: -len(quote)], quote
    return symbol.upper(), ""


def _probe_ping(client: TabdealClient) -> tuple[dict[str, Any], ProbeResult]:
    return probe_both_prefixes(client, lambda p: client.ping(prefix=p), "ping")


def _probe_time(client: TabdealClient, clock: Clock, prefix: str) -> dict[str, Any]:
    time_summary, _time_chosen = probe_both_prefixes(client, lambda p: client.server_time(prefix=p), "time")
    before = clock.now()
    server_time_result = client.server_time(prefix=prefix)
    after = clock.now()
    server_time_ms = _extract_server_time_ms(server_time_result.body)
    if server_time_ms is not None:
        time_summary["clock_skew_ms"] = compute_clock_skew_ms(server_time_ms, before, after)
    return time_summary


def _probe_symbols(client: TabdealClient, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    info_summary, info_chosen = probe_both_prefixes(
        client, lambda p: client.exchange_info(prefix=p), "exchangeInfo"
    )
    symbols_report: dict[str, Any] = {}
    if info_chosen.ok:
        for symbol in args.symbols:
            base, quote = _split_symbol(symbol)
            entry = find_symbol_entry(info_chosen.body, base, quote)
            if entry is None:
                symbols_report[symbol] = {
                    "found": False,
                    "available_base_markets": find_base_asset_markets(info_chosen.body, base),
                }
                continue
            filters = build_symbol_filters(entry, symbol)
            symbols_report[symbol] = {
                "found": True,
                "status": entry.get("status"),
                "symbol_field": entry.get("symbol"),
                "tabdeal_symbol_field": entry.get("tabdealSymbol"),
                "filters": _filters_to_report(filters) if filters is not None else None,
                "filters_unknown": filters is None,
            }
    return info_summary, symbols_report


@dataclass(frozen=True, slots=True)
class _DepthSampleOk:
    spread_bps: float
    spread_pct: float
    depth: dict[str, dict[str, dict[str, str]]]


@dataclass(frozen=True, slots=True)
class _DepthSampleCrossed:
    """m9a (reviewer finding): a momentarily crossed book (best ask < best bid) is a real,
    if rare, exchange data condition -- never a reason to abort a sampling run that may be
    collecting evidence for hours."""

    best_bid: str
    best_ask: str


@dataclass(frozen=True, slots=True)
class _DepthSampleSkipped:
    reason: Literal["no_result", "parse_failed"]


_DepthSampleOutcome = _DepthSampleOk | _DepthSampleCrossed | _DepthSampleSkipped


def _evaluate_depth_sample(result: ProbeResult) -> _DepthSampleOutcome:
    """Classify one ``/depth`` response. Never raises: ``spread_bps_and_pct`` raises
    ``ValueError`` on a crossed book (by design, for callers that treat it as a hard error), but
    a multi-hour, many-sample probe run must survive that on any single sample -- m9a."""
    if not (result.ok and isinstance(result.body, dict)):
        return _DepthSampleSkipped(reason="no_result")
    bids = parse_depth_levels(result.body.get("bids"))
    asks = parse_depth_levels(result.body.get("asks"))
    best = best_bid_ask(bids, asks)
    if best is None:
        return _DepthSampleSkipped(reason="parse_failed")
    best_bid, best_ask = best
    try:
        bps, pct = spread_bps_and_pct(best_bid, best_ask)
    except ValueError:
        return _DepthSampleCrossed(best_bid=str(best_bid), best_ask=str(best_ask))
    mid = (best_bid + best_ask) / 2
    # M4: bid depth (what we could *sell* into) and ask depth (what we could *buy* from) are kept
    # separate -- phase 3 sizing needs them independently, and summing them into one number threw
    # that distinction away irrecoverably.
    depths: dict[str, dict[str, dict[str, str]]] = {}
    for threshold in _DEPTH_THRESHOLDS_PCT:
        bid_base, bid_quote = cumulative_depth(bids, mid, threshold, side="bid")
        ask_base, ask_quote = cumulative_depth(asks, mid, threshold, side="ask")
        depths[str(threshold)] = {
            "bid": {"base_qty": str(bid_base), "quote_notional": str(bid_quote)},
            "ask": {"base_qty": str(ask_base), "quote_notional": str(ask_quote)},
        }
    return _DepthSampleOk(spread_bps=bps, spread_pct=pct, depth=depths)


@dataclass
class _DepthWindowAccumulator:
    """Mutable accumulators for :func:`sample_depth_window` -- a plain object instead of a
    handful of loose ``list`` locals so the per-sample update and the report-rendering steps can
    each be their own small function."""

    spreads_bps: list[float] = field(default_factory=list)
    cumulative_samples: list[dict[str, Any]] = field(default_factory=list)
    crossed_samples: list[dict[str, str]] = field(default_factory=list)
    successful_sample_timestamps: list[datetime] = field(default_factory=list)

    def record(self, attempt_ts: datetime, outcome: _DepthSampleOutcome) -> None:
        if isinstance(outcome, _DepthSampleOk):
            self.spreads_bps.append(outcome.spread_bps)
            self.successful_sample_timestamps.append(attempt_ts)
            self.cumulative_samples.append(
                {"spread_bps": outcome.spread_bps, "spread_pct": outcome.spread_pct, "depth": outcome.depth}
            )
        elif isinstance(outcome, _DepthSampleCrossed):
            self.crossed_samples.append(
                {"ts": attempt_ts.isoformat(), "best_bid": outcome.best_bid, "best_ask": outcome.best_ask}
            )

    def render_into(self, depth_report: dict[str, Any]) -> None:
        """Mutate ``depth_report`` in place from the current accumulator state (m9a) -- called
        after *every* sample, not only once at the end, so a caller can persist a partial report
        to disk at any point during a run that may span hours."""
        timestamps = self.successful_sample_timestamps
        first_ts = timestamps[0] if timestamps else None
        last_ts = timestamps[-1] if timestamps else None
        span = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
        depth_report["spread_summary"] = summarize_spreads(self.spreads_bps)
        depth_report["samples"] = list(self.cumulative_samples)
        # m9a: counted and recorded, not just silently dropped -- Parham can see exactly which
        # samples were crossed and when.
        depth_report["crossed_book_samples"] = list(self.crossed_samples)
        depth_report["crossed_book_count"] = len(self.crossed_samples)
        depth_report["first_sample_ts"] = first_ts.isoformat() if first_ts else None
        depth_report["last_sample_ts"] = last_ts.isoformat() if last_ts else None
        # M3: SPEC section 7 G0 needs >= 30 samples spanning >= 6 hours of wall-clock time --
        # without recording the actual first/last sample timestamps, a 60-second run and a
        # 6-hour run produce an identical-looking report. g0_spread_plan_ok makes that check
        # explicit. MINOR-2: both span and count are successes-only, so they can never contradict
        # spread_summary["samples"] (also successes-only) in the human summary.
        depth_report["sampling_span_seconds"] = span
        depth_report["g0_spread_plan_ok"] = len(timestamps) >= 30 and span >= 21_600


def sample_depth_window(
    client: TabdealClient,
    clock: Clock,
    symbol: str,
    prefix: str,
    *,
    samples: int,
    interval_seconds: float,
    first_result: ProbeResult | None,
    depth_report: dict[str, Any],
    on_progress: Callable[[], None] | None = None,
) -> None:
    """Repeatedly sample ``/depth`` for ``symbol``, mutating ``depth_report`` in place after
    *every* attempt (m9a) -- not only once the whole loop finishes -- so a caller can persist a
    partial report to disk at any point during a run that may span hours, via ``on_progress``.

    A crossed book on any one sample (m9a) is counted and recorded, never raised: nothing here
    lets one bad sample out of (say) 30 lose the other 29.
    """
    acc = _DepthWindowAccumulator()
    for i in range(max(0, samples)):
        attempt_ts = clock.now()
        result = (
            first_result
            if i == 0 and first_result is not None and first_result.ok
            else client.depth(symbol, limit=100, prefix=prefix)
        )
        acc.record(attempt_ts, _evaluate_depth_sample(result))
        acc.render_into(depth_report)
        if on_progress is not None:
            on_progress()
        if i < samples - 1:
            time.sleep(max(0.0, interval_seconds))
    acc.render_into(depth_report)


def _probe_depth(
    client: TabdealClient,
    clock: Clock,
    args: argparse.Namespace,
    prefix: str,
    symbol: str,
    *,
    depth_report: dict[str, Any],
    on_progress: Callable[[], None] | None = None,
) -> None:
    depth_prefix_summary, depth_chosen = probe_both_prefixes(
        client, lambda p: client.depth(symbol, limit=100, prefix=p), "depth"
    )
    depth_report["prefix_discovery"] = depth_prefix_summary
    sample_depth_window(
        client,
        clock,
        symbol,
        prefix,
        samples=args.samples,
        interval_seconds=args.interval,
        first_result=depth_chosen,
        depth_report=depth_report,
        on_progress=on_progress,
    )


def _probe_trades(
    client: TabdealClient, args: argparse.Namespace, prefix: str, symbol: str
) -> dict[str, Any]:
    trades_prefix_summary, trades_chosen = probe_both_prefixes(
        client, lambda p: client.trades(symbol, limit=500, prefix=p), "trades"
    )
    stats = trades_window_stats(trades_chosen.body, 500)
    limit_attempts, max_accepted = discover_max_trades_limit(
        client, symbol, prefix, args.trades_limit_candidates
    )
    other_symbol = next((s for s in args.symbols if s != symbol), "ETHUSDT")
    other_body = client.trades(other_symbol, limit=500, prefix=prefix).body
    monotonic = ids_monotonic_in_time(trades_chosen.body)
    return {
        "prefix_discovery": trades_prefix_summary,
        "window_stats": asdict(stats),
        "activity": trade_activity_stats(trades_chosen.body),
        "max_limit_attempts": [asdict(a) for a in limit_attempts],
        "max_accepted_limit": max_accepted,
        # Informational: Tabdeal ids are global across markets (measured 2026-10-04 on the
        # Turkey server), so a per-symbol window is never contiguous and that is not a failure.
        "ids_contiguous": stats.ids_contiguous,
        "id_space": detect_id_space(trades_chosen.body, other_body),
        "id_space_compared_with": other_symbol,
        # m2: explicit G0 checks the recorder actually depends on.
        "g0_time_unit_is_ms": stats.time_unit == "ms",
        "g0_ids_monotonic_in_time": bool(monotonic),
    }


def _probe_account(client: TabdealClient, prefix: str) -> tuple[dict[str, Any], bool]:
    """Returns ``(account_report, key_permissions_unsafe)``. Prints a warning to stderr for
    either the unsafe or the unknown case -- m9b (reviewer finding): "unknown" must be just as
    loud as "unsafe", since silence is exactly what let the old fail-open bug go unnoticed."""
    if not client.has_credentials:
        return {"skipped": "no TBOT_TABDEAL_API_KEY / TBOT_TABDEAL_API_SECRET in environment"}, False

    account_result = client.account(prefix=prefix)
    state, permissions = classify_key_permissions(account_result.body)
    if state == "unknown":
        print(
            "WARNING: could not determine Tabdeal API key permissions from the account "
            "response (no canTrade/canWithdraw/permissions field found) -- verify MANUALLY in "
            "the Tabdeal UI that this key has NO trade and NO withdrawal permission before phase "
            "6 (CLAUDE.md section 3.6). Note Binance-style canTrade/canWithdraw fields, even when "
            "present, may describe the account rather than this specific key.",
            file=sys.stderr,
        )
    elif state == "unsafe":
        print(
            "WARNING: Tabdeal API key has trade or withdrawal permission enabled -- "
            "CLAUDE.md section 3.6 requires a read-only key before phase 6.",
            file=sys.stderr,
        )
    account_report = {
        "status_code": account_result.status_code,
        "ok": account_result.ok,
        "balances": extract_nonzero_balances(account_result.body) if account_result.ok else [],
        "permissions": permissions,
        "key_permissions": state,
    }
    return account_report, state == "unsafe"


def run_probe(
    client: TabdealClient,
    clock: Clock,
    args: argparse.Namespace,
    report: dict[str, Any],
    *,
    on_depth_progress: Callable[[], None] | None = None,
) -> int:
    """Run every check, mutating ``report`` in place, and return the exit code.

    ``report`` is supplied by the caller (not built here) so that if this function is
    interrupted partway through (``KeyboardInterrupt``/SIGTERM during the depth-sampling loop --
    m9a), the caller still holds a reference to whatever sections completed before that point.
    Never raises on exchange errors -- every failure is captured in the report; only a genuinely
    unreachable ``ping`` short-circuits.
    """
    report["generated_at"] = clock.now().isoformat()
    report["base_url"] = client.base_url
    report["key_permissions_unsafe"] = False

    ping_summary, ping_chosen = _probe_ping(client)
    report["ping"] = ping_summary
    if ping_summary["answering_prefix"] is None:
        report["fatal_error"] = describe_unreachable_failure(ping_chosen)
        return 1

    prefix = client.read_prefix if ping_summary["answering_prefix"] == "read" else client.write_prefix

    report["time"] = _probe_time(client, clock, prefix)

    info_summary, symbols_report = _probe_symbols(client, args)
    report["exchange_info"] = info_summary
    report["symbols"] = symbols_report

    primary_symbol = args.symbols[0] if args.symbols else "BTCUSDT"
    depth_report: dict[str, Any] = {}
    report["depth"] = {primary_symbol: depth_report}
    _probe_depth(
        client, clock, args, prefix, primary_symbol, depth_report=depth_report, on_progress=on_depth_progress
    )

    report["trades"] = _probe_trades(client, args, prefix, primary_symbol)

    account_report, unsafe = _probe_account(client, prefix)
    report["account"] = account_report
    report["key_permissions_unsafe"] = unsafe
    # Three-valued top-level field: "skipped" (no credentials), "safe", "unsafe" or "unknown".
    report["key_permissions"] = account_report.get("key_permissions", "skipped")

    # M5: an unsafe (trade/withdraw-capable) key must not exit 0 -- that would let a CI check or
    # an operator's shell script silently treat this as a pass. The report is still written.
    # Review m-I: an unverifiable key is not a pass either.
    if unsafe:
        return 2
    return 3 if report["key_permissions"] == "unknown" else 0


def _print_symbols_summary(report: dict[str, Any]) -> None:
    for symbol, info in report.get("symbols", {}).items():
        if info.get("found"):
            print(f"{symbol}: status={info.get('status')} filters_unknown={info.get('filters_unknown')}")
        else:
            print(f"{symbol}: NOT FOUND -- available BTC-like markets: {info.get('available_base_markets')}")


def _print_depth_summary(report: dict[str, Any]) -> None:
    for symbol, depth_info in report.get("depth", {}).items():
        summary = depth_info.get("spread_summary", {})
        if summary:
            median_bps = summary.get("median_bps")
            p95_bps = summary.get("p95_bps")
            print(f"{symbol} spread (bps): median={median_bps:.2f} p95={p95_bps:.2f}")
        span_s = depth_info.get("sampling_span_seconds")
        plan_ok = depth_info.get("g0_spread_plan_ok")
        print(
            f"{symbol} G0 spread sampling plan (>=30 samples, >=6h span) ok={plan_ok} "
            f"(samples={len(depth_info.get('samples', []))}, span_seconds={span_s})"
        )
        crossed_count = depth_info.get("crossed_book_count", 0)
        if crossed_count:
            # m9a: a crossed sample is evidence, not noise -- it must be visible in the summary
            # Parham actually reads, not just buried in the JSON.
            print(f"{symbol} WARNING: {crossed_count} crossed-book sample(s) observed (recorded, not fatal)")


def _print_trades_summary(trades: dict[str, Any]) -> None:
    print(f"trades: max_accepted_limit={trades.get('max_accepted_limit')}")
    ws = trades.get("window_stats", {})
    recommended_poll_s = ws.get("recommended_poll_interval_seconds")
    print(f"trades: saturated={ws.get('saturated')} recommended_poll_s={recommended_poll_s}")
    # m2: time_unit / ids_contiguous / window span, printed prominently as G0 checks -- not left
    # buried in the JSON where a silently-wrong assumption (e.g. `time` not actually in ms) could
    # pass unnoticed.
    print(
        f"trades G0: time_unit={ws.get('time_unit')} "
        f"(disagrees_with_ms={ws.get('time_unit_disagrees_with_ms')}) "
        f"ids_monotonic_in_time={trades.get('g0_ids_monotonic_in_time')} "
        f"window_span_seconds={ws.get('span_seconds')}"
    )
    print(
        f"trades info: id_space={trades.get('id_space')} "
        f"(vs {trades.get('id_space_compared_with')}) ids_contiguous={ws.get('ids_contiguous')} "
        f"missing_ids={ws.get('missing_ids')}"
    )
    activity = trades.get("activity", {})
    print(
        f"trades activity: per_full_hour min/median/max="
        f"{activity.get('trades_per_hour_min')}/{activity.get('trades_per_hour_median')}/"
        f"{activity.get('trades_per_hour_max')} empty_full_hours={activity.get('empty_full_hours')} "
        f"max_inter_trade_gap_s={activity.get('max_inter_trade_gap_seconds')}"
    )
    if trades.get("g0_ids_monotonic_in_time") is False:
        print(
            "WARNING: /trades ids do NOT increase with time -- the recorder's continuity rule "
            "depends on it.",
            file=sys.stderr,
        )
    if ws.get("time_unit_disagrees_with_ms"):
        print(
            "WARNING: /trades `time` does NOT look like milliseconds -- "
            "tbot.data.tabdeal_recorder assumes ms; candle timestamps will be wrong until "
            "this is fixed.",
            file=sys.stderr,
        )


def _print_account_summary(report: dict[str, Any]) -> None:
    if report.get("key_permissions_unsafe"):
        print("WARNING: key_permissions_unsafe = true")
    account = report.get("account", {})
    if "skipped" in account:
        print(f"account: {account['skipped']}")
    elif account:
        print(
            f"account: ok={account.get('ok')} balances={len(account.get('balances', []))} non-zero "
            f"key_permissions={account.get('key_permissions')}"
        )


def _print_human_summary(report: dict[str, Any]) -> None:
    print("=== Tabdeal probe summary ===")
    ping = report.get("ping", {})
    print(f"ping: answering_prefix={ping.get('answering_prefix')}")
    time_block = report.get("time", {})
    if "clock_skew_ms" in time_block:
        print(f"clock skew: {time_block['clock_skew_ms']:.1f} ms")
    _print_symbols_summary(report)
    _print_depth_summary(report)
    trades = report.get("trades", {})
    if trades:
        _print_trades_summary(trades)
    _print_account_summary(report)


class ProbeAbortError(Exception):
    """Raised on the main thread when SIGTERM arrives mid-probe (e.g. systemd stopping the
    service, or an operator's ``kill``) -- m9a (reviewer finding). Never raised directly;
    only by the handler :func:`_install_sigterm_handler` installs, so ``main()`` can persist
    whatever partial report already exists before exiting instead of losing it to an
    uncaught-exception traceback."""


def _install_sigterm_handler() -> Callable[[], None]:
    """Install a SIGTERM handler that raises :class:`ProbeAbortError` on receipt; return a
    callable that restores whatever handler was previously installed (call it in a ``finally``).
    """
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(_signum: int, _frame: Any) -> None:
        raise ProbeAbortError("SIGTERM received")

    signal.signal(signal.SIGTERM, _handler)

    def _restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return _restore


def _write_report_atomic(report: dict[str, Any], out_path: Path) -> None:
    """Write ``report`` to ``out_path`` as JSON, atomically (write-then-rename) -- m9a: called
    after every depth sample (not just once at the end) during a run that may span hours, so a
    reader never observes a half-written file, and a kill at any point leaves the most recently
    completed sample on disk rather than nothing at all.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(out_path)


def _build_client(config: Config, secrets: Secrets, clock: Clock) -> TabdealClient:
    return TabdealClient(
        base_url=config.exchange.base_url,
        read_prefix=config.exchange.read_prefix,
        write_prefix=config.exchange.write_prefix,
        clock=clock,
        api_key=secrets.tabdeal_api_key,
        api_secret=secrets.tabdeal_api_secret,
        recv_window_ms=config.exchange.recv_window_ms,
        requests_per_second=config.exchange.requests_per_second,
        timeout_seconds=config.exchange.timeout_seconds,
        max_retries=config.exchange.max_retries,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    config: Config = load_config(args.config, config_dir=args.config_dir)
    secrets = Secrets()
    # MAJOR M-C (third fix round): this is the ONE process that actually holds Tabdeal
    # credentials and signs real requests with them -- it must register them with the
    # logging module's value registry (so a bare key/secret leaking into free text is still
    # redacted) and install the redacting pipeline (`configure_logging`) BEFORE the client
    # that uses those credentials is even constructed. Neither call existed here before this
    # fix, so this was the one real process where a credential leak was most likely and where
    # nothing at all stood in its way.
    register_secrets_for_logging(secrets)
    configure_logging(config.runtime.log_level)
    clock: Clock = SystemClock()
    client = _build_client(config, secrets, clock)
    out_path = args.out or _default_out_path(clock.now())
    report: dict[str, Any] = {
        "generated_at": clock.now().isoformat(),
        "base_url": client.base_url,
        "key_permissions_unsafe": False,
    }

    # m9a: a long depth-sampling run must survive Ctrl+C (KeyboardInterrupt/SIGINT) and a
    # systemd/operator `kill` (SIGTERM) without losing whatever was already collected -- both
    # are caught below and the partial `report` (already kept current on disk by on_progress,
    # see sample_depth_window) is written one last time with a clear marker, instead of an
    # uncaught-exception traceback and whatever was or wasn't flushed to disk.
    restore_sigterm = _install_sigterm_handler()
    try:
        exit_code = run_probe(
            client,
            clock,
            args,
            report,
            on_depth_progress=lambda: _write_report_atomic(report, out_path),
        )
    except (KeyboardInterrupt, ProbeAbortError) as exc:
        report["interrupted"] = True
        report["interrupted_reason"] = exc.__class__.__name__
        _write_report_atomic(report, out_path)
        print(
            f"probe interrupted ({exc.__class__.__name__}) -- partial report written to {out_path}",
            file=sys.stderr,
        )
        _print_human_summary(report)
        return 143 if isinstance(exc, ProbeAbortError) else 130
    finally:
        restore_sigterm()
        client.close()

    if exit_code != 0 and "fatal_error" in report:
        print(report["fatal_error"], file=sys.stderr)
        return exit_code

    _write_report_atomic(report, out_path)
    print(f"report written to {out_path}")
    _print_human_summary(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
