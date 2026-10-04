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

Nothing here is a real order, and nothing here can become one: see
``src/tbot/execution/tabdeal_client.py`` for the read-only surface this script calls.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
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
    "compute_clock_skew_ms",
    "cumulative_depth",
    "describe_unreachable_failure",
    "discover_max_trades_limit",
    "extract_nonzero_balances",
    "find_base_asset_markets",
    "find_symbol_entry",
    "is_saturated",
    "main",
    "probe_both_prefixes",
    "spread_bps_and_pct",
    "summarize_spreads",
    "trades_window_stats",
    "unsafe_key_permissions",
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


@dataclass(frozen=True, slots=True)
class TradesWindowStats:
    count: int
    min_id: int | None
    max_id: int | None
    span_seconds: float | None
    saturated: bool
    recommended_poll_interval_seconds: float | None


def trades_window_stats(trades: Any, requested_limit: int) -> TradesWindowStats:
    """Count/id-range/time-span/saturation for one ``/trades`` response, plus a recommended
    polling interval with a 3x safety margin against saturating the same ``limit`` again."""
    if not isinstance(trades, list):
        return TradesWindowStats(0, None, None, None, False, None)
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
    return TradesWindowStats(
        count=count,
        min_id=min(ids) if ids else None,
        max_id=max(ids) if ids else None,
        span_seconds=span_seconds,
        saturated=saturated,
        recommended_poll_interval_seconds=recommended,
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


def unsafe_key_permissions(account_body: Any) -> tuple[bool, dict[str, Any]]:
    """Return ``(unsafe, permissions_found)``. ``unsafe`` is True if trade or withdraw
    permission is enabled on the key -- CLAUDE.md section 3.6 requires read-only keys pre-live."""
    if not isinstance(account_body, dict):
        return False, {}
    found: dict[str, Any] = {}
    for key in ("canTrade", "canWithdraw", "canDeposit", "permissions"):
        if key in account_body:
            found[key] = account_body[key]
    unsafe = bool(account_body.get("canTrade")) or bool(account_body.get("canWithdraw"))
    permissions = account_body.get("permissions")
    if isinstance(permissions, list):
        upper = {str(p).upper() for p in permissions}
        if upper & {"TRADE", "WITHDRAW", "WITHDRAWALS"}:
            unsafe = True
    return unsafe, found


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


def run_probe(client: TabdealClient, clock: Clock, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Run every check and return ``(report, exit_code)``. Never raises on exchange errors --
    every failure is captured in the report; only a genuinely unreachable ``ping`` short-circuits."""
    report: dict[str, Any] = {
        "generated_at": clock.now().isoformat(),
        "base_url": client.base_url,
        "key_permissions_unsafe": False,
    }

    ping_summary, ping_chosen = probe_both_prefixes(client, lambda p: client.ping(prefix=p), "ping")
    report["ping"] = ping_summary
    if ping_summary["answering_prefix"] is None:
        report["fatal_error"] = describe_unreachable_failure(ping_chosen)
        return report, 1

    prefix = client.read_prefix if ping_summary["answering_prefix"] == "read" else client.write_prefix

    # -- time / clock skew ---------------------------------------------------------
    time_summary, _time_chosen = probe_both_prefixes(client, lambda p: client.server_time(prefix=p), "time")
    report["time"] = time_summary
    before = clock.now()
    server_time_result = client.server_time(prefix=prefix)
    after = clock.now()
    server_time_ms = _extract_server_time_ms(server_time_result.body)
    if server_time_ms is not None:
        report["time"]["clock_skew_ms"] = compute_clock_skew_ms(server_time_ms, before, after)

    # -- exchangeInfo / symbol + filter discovery -----------------------------------
    info_summary, info_chosen = probe_both_prefixes(
        client, lambda p: client.exchange_info(prefix=p), "exchangeInfo"
    )
    report["exchange_info"] = info_summary
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
    report["symbols"] = symbols_report

    # -- depth: repeated sampling for median spread + cumulative depth -------------
    depth_report: dict[str, Any] = {}
    primary_symbol = args.symbols[0] if args.symbols else "BTCUSDT"
    depth_prefix_summary, depth_chosen = probe_both_prefixes(
        client, lambda p: client.depth(primary_symbol, limit=100, prefix=p), "depth"
    )
    depth_report["prefix_discovery"] = depth_prefix_summary
    spreads_bps: list[float] = []
    cumulative_samples: list[dict[str, Any]] = []
    # MINOR-2 (second fix round): this must record the timestamp of each *successful* depth
    # sample only. The previous version appended a timestamp unconditionally, before even
    # attempting the call -- so a run with (say) 25 failed calls out of 30 attempts over 6 hours
    # still reported a full 6-hour span and g0_spread_plan_ok=True, while spread_summary["samples"]
    # (correctly derived from spreads_bps, i.e. successes only) said 5. Those two numbers
    # contradicting each other on adjacent lines of the human summary is exactly the kind of G0
    # evidence that must never be handed to Parham.
    successful_sample_timestamps: list[datetime] = []
    for i in range(max(0, args.samples)):
        attempt_ts = clock.now()
        if i == 0 and depth_chosen.ok:
            result = depth_chosen
        else:
            result = client.depth(primary_symbol, limit=100, prefix=prefix)
        if result.ok and isinstance(result.body, dict):
            bids = parse_depth_levels(result.body.get("bids"))
            asks = parse_depth_levels(result.body.get("asks"))
            best = best_bid_ask(bids, asks)
            if best is not None:
                best_bid, best_ask = best
                bps, pct = spread_bps_and_pct(best_bid, best_ask)
                spreads_bps.append(bps)
                successful_sample_timestamps.append(attempt_ts)
                mid = (best_bid + best_ask) / 2
                # M4: bid depth (what we could *sell* into) and ask depth (what we could *buy*
                # from) are kept separate -- phase 3 sizing needs them independently, and summing
                # them into one number threw that distinction away irrecoverably.
                depths: dict[str, dict[str, dict[str, str]]] = {}
                for threshold in _DEPTH_THRESHOLDS_PCT:
                    bid_base, bid_quote = cumulative_depth(bids, mid, threshold, side="bid")
                    ask_base, ask_quote = cumulative_depth(asks, mid, threshold, side="ask")
                    depths[str(threshold)] = {
                        "bid": {"base_qty": str(bid_base), "quote_notional": str(bid_quote)},
                        "ask": {"base_qty": str(ask_base), "quote_notional": str(ask_quote)},
                    }
                cumulative_samples.append({"spread_bps": bps, "spread_pct": pct, "depth": depths})
        if i < args.samples - 1:
            time.sleep(max(0.0, args.interval))
    depth_report["spread_summary"] = summarize_spreads(spreads_bps)
    depth_report["samples"] = cumulative_samples
    # M3: SPEC section 7 G0 needs >= 30 samples spanning >= 6 hours of wall-clock time -- without
    # recording the actual first/last sample timestamps, a 60-second run and a 6-hour run produce
    # an identical-looking report. g0_spread_plan_ok makes that check explicit and unmissable.
    # MINOR-2: both the span and the count below are now based on successful samples only, so they
    # can never contradict spread_summary["samples"] (also successes-only) in the human summary.
    first_sample_ts = successful_sample_timestamps[0] if successful_sample_timestamps else None
    last_sample_ts = successful_sample_timestamps[-1] if successful_sample_timestamps else None
    sampling_span_seconds = (
        (last_sample_ts - first_sample_ts).total_seconds() if first_sample_ts and last_sample_ts else 0.0
    )
    depth_report["first_sample_ts"] = first_sample_ts.isoformat() if first_sample_ts else None
    depth_report["last_sample_ts"] = last_sample_ts.isoformat() if last_sample_ts else None
    depth_report["sampling_span_seconds"] = sampling_span_seconds
    depth_report["g0_spread_plan_ok"] = (
        len(successful_sample_timestamps) >= 30 and sampling_span_seconds >= 21_600
    )
    report["depth"] = {primary_symbol: depth_report}

    # -- trades: window stats + saturation + max-limit discovery -------------------
    trades_prefix_summary, trades_chosen = probe_both_prefixes(
        client, lambda p: client.trades(primary_symbol, limit=500, prefix=p), "trades"
    )
    stats = trades_window_stats(trades_chosen.body, 500)
    limit_attempts, max_accepted = discover_max_trades_limit(
        client, primary_symbol, prefix, args.trades_limit_candidates
    )
    report["trades"] = {
        "prefix_discovery": trades_prefix_summary,
        "window_stats": asdict(stats),
        "max_limit_attempts": [asdict(a) for a in limit_attempts],
        "max_accepted_limit": max_accepted,
    }

    # -- private: account balances only, only if credentials are present -----------
    if client.has_credentials:
        account_result = client.account(prefix=prefix)
        unsafe, permissions = unsafe_key_permissions(account_result.body)
        report["key_permissions_unsafe"] = unsafe
        report["account"] = {
            "status_code": account_result.status_code,
            "ok": account_result.ok,
            "balances": extract_nonzero_balances(account_result.body) if account_result.ok else [],
            "permissions": permissions,
        }
        if unsafe:
            print(
                "WARNING: Tabdeal API key has trade or withdrawal permission enabled -- "
                "CLAUDE.md section 3.6 requires a read-only key before phase 6.",
                file=sys.stderr,
            )
    else:
        report["account"] = {"skipped": "no TBOT_TABDEAL_API_KEY / TBOT_TABDEAL_API_SECRET in environment"}

    # M5: an unsafe (trade/withdraw-capable) key must not exit 0 -- that would let a CI check or
    # an operator's shell script silently treat this as a pass. The report is still written.
    exit_code = 2 if report["key_permissions_unsafe"] else 0
    return report, exit_code


def _print_human_summary(report: dict[str, Any]) -> None:
    print("=== Tabdeal probe summary ===")
    ping = report.get("ping", {})
    print(f"ping: answering_prefix={ping.get('answering_prefix')}")
    time_block = report.get("time", {})
    if "clock_skew_ms" in time_block:
        print(f"clock skew: {time_block['clock_skew_ms']:.1f} ms")
    for symbol, info in report.get("symbols", {}).items():
        if info.get("found"):
            print(f"{symbol}: status={info.get('status')} filters_unknown={info.get('filters_unknown')}")
        else:
            print(f"{symbol}: NOT FOUND -- available BTC-like markets: {info.get('available_base_markets')}")
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
    trades = report.get("trades", {})
    if trades:
        print(f"trades: max_accepted_limit={trades.get('max_accepted_limit')}")
        ws = trades.get("window_stats", {})
        recommended_poll_s = ws.get("recommended_poll_interval_seconds")
        print(f"trades: saturated={ws.get('saturated')} recommended_poll_s={recommended_poll_s}")
    if report.get("key_permissions_unsafe"):
        print("WARNING: key_permissions_unsafe = true")
    account = report.get("account", {})
    if "skipped" in account:
        print(f"account: {account['skipped']}")
    elif account:
        print(f"account: ok={account.get('ok')} balances={len(account.get('balances', []))} non-zero")


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
    client = TabdealClient(
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
    try:
        report, exit_code = run_probe(client, clock, args)
    finally:
        client.close()

    if exit_code != 0 and "fatal_error" in report:
        print(report["fatal_error"], file=sys.stderr)
        return exit_code

    out_path = args.out or _default_out_path(clock.now())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"report written to {out_path}")
    _print_human_summary(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
