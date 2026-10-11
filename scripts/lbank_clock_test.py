"""Dedicated LBank clock re-test (docs/SPEC.md section 7, "G0 -- Foundations (LBank, D-073)",
bullet "Dedicated clock re-test (D-079)").

The method below was registered in the SPEC BEFORE this code existed and must not be changed::

    120 calls to the public ``timestamp.do``, spaced 30 s apart over 1 hour, one request in flight.
    Per call: t_before / t_after = host wall clock (``time.time_ns``) immediately around the request,
    RTT = t_after - t_before, offset = server_ms - (t_before + t_after) / 2.
    Select the fastest 10 % by RTT (12 calls): offset = median of their offsets,
    min_rtt = the smallest RTT of all calls.
    PASS iff |offset| + min_rtt / 2 < 500 ms.
    Any non-200 is recorded. A 429/403/418 stops the run at once (no further request) and it FAILS.
    Fewer than 108 valid calls (90 %) FAILS. Ctrl+C / SIGTERM writes partial output: INCOMPLETE.

Strictly public: HTTP GET ``/v2/timestamp.do`` on the spot v2 base URL, nothing else -- no key, no
signature, no other endpoint (``tests/scripts/test_lbank_clock_test.py`` asserts the whitelist).

Usage (the defaults ARE the registered values; run it once, from the Turkey server)::

    python -m scripts.lbank_clock_test --calls 120 --spacing 30 --out-dir DIR --host-label LABEL

Overriding ``--calls`` or ``--spacing`` produces a run that is stamped "NOT the registered run": its
outputs never contain the word PASS and the exit code is never 0.

Reading of the registered text where it leaves room (documented here, not a change of method):

* *Valid call*: HTTP 200, valid JSON, success payload, a server time in milliseconds, and a
  non-negative wall-clock RTT (a negative one means the host clock stepped backwards mid-call).
* *"Smallest RTT of all calls"* is taken over valid calls only: a fast 5xx/429 says nothing about the
  clock path and would make the criterion easier to meet. This is the conservative reading.
* *"Fastest 10 % (12 calls)"* is ``ceil(calls / 10)`` calls (12 for 120), taken from the valid calls
  ordered by (RTT, call index); with fewer valid calls than that, all valid calls are used.
* *Schedule*: call ``i`` is due at ``start + i * spacing`` (monotonic), a slow call never shifts later
  starts. A call that is only slightly late (<= min(5 s, spacing / 2)) starts immediately; a slot that
  was missed by more than that is SKIPPED (recorded, counted as not valid) -- never made up later --
  so the client can never fire a burst of catch-up requests.

Honest limits: this script cannot tell whether the host clock is NTP-synced (and cannot from inside a
container); the operator records ``chronyc tracking`` / ``timedatectl`` next to the run.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from scripts.lbank_probe import (
    BAN_LIKE_STATUSES,
    ENDPOINTS,
    Env,
    JsonlWriter,
    OutputPaths,
    collect_provenance,
    interesting_headers,
    payload_status,
    percentile,
)

__all__ = [
    "REGISTERED_CALLS",
    "REGISTERED_SPACING_S",
    "CallRecord",
    "Clock",
    "ClockEstimate",
    "build_summary",
    "criterion_met",
    "estimate_clock",
    "main",
    "run_clock_test",
    "select_fastest",
]

# ---- registered parameters (docs/SPEC.md section 7, D-079) -- do not change ------------------
REGISTERED_CALLS = 120
REGISTERED_SPACING_S = 30.0
REQUEST_TIMEOUT_S = 10.0
FASTEST_FRACTION_DENOM = 10  # fastest 10 % = ceil(calls / 10)
MIN_VALID_PERCENT = 90  # 108 of 120
THRESHOLD_MS = 500.0  # |offset| + min_rtt / 2 must be strictly below this
# ----------------------------------------------------------------------------------------------

MIN_SPACING_S = 1.0  # hard client-side floor for any override (rate ceiling 1 req/s)
MAX_LATE_START_S = 5.0
MS_EPOCH_FLOOR = 10**11  # server times below this are seconds, not milliseconds
BODY_SNIPPET_CHARS = 500
USER_AGENT = "tbot-lbank-g0-clock-test/1 (public read-only timestamp.do re-test; contact: project owner)"
SCRIPT_NAME = "scripts/lbank_clock_test.py"
NTP_NOTE = (
    "Host clock NTP/NTS sync is NOT checked by this script (it cannot be from inside a container); "
    "the operator records `chronyc tracking` / `timedatectl` on the host next to this run."
)
NOT_REGISTERED_NOTE = (
    "NOT the registered run: --calls/--spacing differ from the registered 120 calls / 30 s. "
    "Informational only; it can never decide G0."
)

# exit codes: 0 registered criterion met, 2 FAIL, 3 INCOMPLETE, 4 not the registered run,
# 130 / 143 = interrupted by SIGINT / SIGTERM (partial outputs are still written)
EXIT_PASS, EXIT_FAIL, EXIT_INCOMPLETE, EXIT_NOT_REGISTERED = 0, 2, 3, 4

# files whose working-tree state marks the reported commit "-dirty"
DIRTY_CHECK_PATHS: tuple[str, ...] = (
    "scripts/lbank_clock_test.py",
    "scripts/lbank_probe.py",
    "src/tbot/data/lbank_recorder.py",
)


# ---------------------------------------------------------------------------------
# injectable clock
# ---------------------------------------------------------------------------------


@dataclass
class Clock:
    """Host wall clock (ns), monotonic clock and sleep. Production uses the real ones."""

    wall_ns: Callable[[], int] = time.time_ns
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def utc(self) -> datetime:
        return datetime.fromtimestamp(self.wall_ns() / 1e9, tz=UTC)


# ---------------------------------------------------------------------------------
# one call
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallRecord:
    index: int
    due_s: float  # scheduled start, seconds after the run start (monotonic)
    start_s: float  # actual start, seconds after the run start (monotonic)
    t_before_ns: int
    t_after_ns: int
    mono_rtt_ms: float
    status: int | None
    error: str | None
    server_ms: int | None
    invalid_reason: str | None
    headers: dict[str, str] = field(default_factory=dict)
    body_snippet: str | None = None

    @property
    def valid(self) -> bool:
        return self.invalid_reason is None

    @property
    def rtt_ns(self) -> int:
        return self.t_after_ns - self.t_before_ns

    @property
    def rtt_ms(self) -> float:
        """Wall-clock RTT: t_after - t_before (the registered definition)."""
        return self.rtt_ns / 1e6

    @property
    def offset_ms(self) -> float | None:
        """server_ms - (t_before + t_after) / 2, in ms (positive: LBank ahead of this host)."""
        if self.server_ms is None:
            return None
        return (2 * self.server_ms * 10**6 - (self.t_before_ns + self.t_after_ns)) / 2e6

    @property
    def is_ban(self) -> bool:
        return self.status in BAN_LIKE_STATUSES

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": "call",
            "index": self.index,
            "due_s": round(self.due_s, 3),
            "start_s": round(self.start_s, 3),
            "t_before_utc": _ns_to_iso(self.t_before_ns),
            "t_before_ms": self.t_before_ns / 1e6,
            "t_after_ms": self.t_after_ns / 1e6,
            "rtt_ms": self.rtt_ms,
            "mono_rtt_ms": self.mono_rtt_ms,
            "status": self.status,
            "error": self.error,
            "server_ms": self.server_ms,
            "offset_ms": self.offset_ms,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "headers": self.headers,
            "body_snippet": self.body_snippet,
        }


def _ns_to_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat()


def _server_ms(body: Any) -> tuple[int | None, str | None]:
    """Server time in ms from a ``timestamp.do`` body, or ``(None, reason)``. Seconds are rejected,
    not scaled up: a 1 s resolution would make a 500 ms criterion meaningless."""
    data = body.get("data") if isinstance(body, dict) else None
    try:
        value = Decimal(str(data))
    except (InvalidOperation, ValueError):
        return None, "server_time_unparseable"
    if not value.is_finite() or value <= 0:
        return None, "server_time_unparseable"
    if value < MS_EPOCH_FLOOR:
        return None, "server_time_not_in_ms"
    return int(value), None


def _classify(
    status: int | None, error: str | None, content: bytes, rtt_ns: int
) -> tuple[int | None, str | None]:
    """``(server_ms, invalid_reason)`` for one finished call."""
    if error is not None:
        return None, error
    if status != 200:
        return None, f"http_{status}"
    try:
        body = json.loads(content, parse_float=Decimal)
    except ValueError:
        return None, "invalid_json"
    ok, why = payload_status(body)
    if not ok:
        return None, f"payload_error: {why}"
    server_ms, reason = _server_ms(body)
    if reason is not None:
        return None, reason
    if rtt_ns < 0:
        return server_ms, "negative_rtt_clock_stepped"
    return server_ms, None


def timed_call(client: httpx.Client, clock: Clock, index: int, due_s: float, run_start: float) -> CallRecord:
    """One GET of ``timestamp.do`` with the host wall clock read immediately around the request."""
    spec = ENDPOINTS["timestamp"]
    url = spec.base + spec.path
    status: int | None = None
    error: str | None = None
    content = b""
    headers: dict[str, str] = {}
    start_mono = clock.monotonic()
    t_before = clock.wall_ns()
    try:
        response = client.get(url)
        t_after = clock.wall_ns()
        status, content = response.status_code, response.content
        headers = interesting_headers(dict(response.headers.items()))
    except httpx.TimeoutException:
        t_after = clock.wall_ns()
        error = "timeout"
    except httpx.HTTPError as exc:
        t_after = clock.wall_ns()
        error = type(exc).__name__
    end_mono = clock.monotonic()
    server_ms, reason = _classify(status, error, content, t_after - t_before)
    snippet = (
        content.decode("utf-8", errors="replace")[:BODY_SNIPPET_CHARS] if status not in (None, 200) else None
    )
    return CallRecord(
        index=index,
        due_s=due_s,
        start_s=start_mono - run_start,
        t_before_ns=t_before,
        t_after_ns=t_after,
        mono_rtt_ms=(end_mono - start_mono) * 1000.0,
        status=status,
        error=error,
        server_ms=server_ms,
        invalid_reason=reason,
        headers=headers,
        body_snippet=snippet,
    )


# ---------------------------------------------------------------------------------
# the registered estimator (pure)
# ---------------------------------------------------------------------------------


def fastest_count(calls: int) -> int:
    """The fastest 10 %: ``ceil(calls / 10)`` (12 for 120). Integer arithmetic, no float rounding."""
    return max(1, -(-calls // FASTEST_FRACTION_DENOM))


def min_valid_calls(calls: int) -> int:
    """``ceil(0.9 * calls)`` (108 for 120). Integer arithmetic, no float rounding."""
    return -(-calls * MIN_VALID_PERCENT // 100)


def select_fastest(valid: Sequence[CallRecord], n: int) -> list[CallRecord]:
    """The ``n`` fastest valid calls by wall RTT; ties broken by lower call index (deterministic)."""
    return sorted(valid, key=lambda c: (c.rtt_ns, c.index))[:n]


def criterion_met(offset_ms: float, min_rtt_ms: float) -> bool:
    """The registered rule: |offset| + min_rtt / 2 < 500 ms (strict)."""
    return abs(offset_ms) + min_rtt_ms / 2 < THRESHOLD_MS


@dataclass(frozen=True)
class ClockEstimate:
    offset_ms: float
    min_rtt_ms: float
    bound_ms: float  # |offset| + min_rtt / 2
    fastest: tuple[CallRecord, ...]
    criterion_met: bool


def estimate_clock(calls: Sequence[CallRecord], planned_calls: int) -> ClockEstimate | None:
    """offset = median offset of the fastest 10 %; min_rtt = smallest RTT of the valid calls."""
    valid = [c for c in calls if c.valid and c.offset_ms is not None]
    if not valid:
        return None
    fastest = select_fastest(valid, fastest_count(planned_calls))
    offsets = [c.offset_ms for c in fastest if c.offset_ms is not None]
    offset = statistics.median(offsets)
    min_rtt = min(c.rtt_ms for c in valid)
    return ClockEstimate(
        offset_ms=offset,
        min_rtt_ms=min_rtt,
        bound_ms=abs(offset) + min_rtt / 2,
        fastest=tuple(fastest),
        criterion_met=criterion_met(offset, min_rtt),
    )


# ---------------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------------


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def build_summary(
    provenance: Mapping[str, Any],
    calls: Sequence[CallRecord],
    skipped: Sequence[int],
    *,
    planned_calls: int,
    spacing_s: float,
    finished: bool,
    interrupted: bool,
    stopped_on_ban: Mapping[str, Any] | None,
) -> dict[str, Any]:
    registered = planned_calls == REGISTERED_CALLS and spacing_s == REGISTERED_SPACING_S
    valid = [c for c in calls if c.valid]
    need = min_valid_calls(planned_calls)
    est = estimate_clock(calls, planned_calls)
    verdict, reason = _verdict(
        registered=registered,
        finished=finished,
        interrupted=interrupted,
        ban=stopped_on_ban,
        n_valid=len(valid),
        need=need,
        est=est,
    )
    half = est.min_rtt_ms / 2 if est else None
    return {
        "script": SCRIPT_NAME,
        "registered_run": registered,
        "registration_note": None if registered else NOT_REGISTERED_NOTE,
        "verdict": verdict,
        "verdict_reason": reason,
        "rule": (
            f"|offset| + min_rtt/2 < {THRESHOLD_MS:.0f} ms"
            if registered
            else f"informational: |offset| + min_rtt/2 versus {THRESHOLD_MS:.0f} ms (not the registered run)"
        ),
        "provenance": dict(provenance),
        "plan": {
            "calls": planned_calls,
            "spacing_s": spacing_s,
            "timeout_s": REQUEST_TIMEOUT_S,
            "fastest_n": fastest_count(planned_calls),
            "min_valid_calls": need,
            "threshold_ms": THRESHOLD_MS,
        },
        "counts": {
            "attempted": len(calls),
            "valid": len(valid),
            "not_valid": len(calls) - len(valid),
            "skipped_missed_slots": len(skipped),
            "skipped_slot_indexes": list(skipped),
            "http_status": dict(Counter(str(c.status) for c in calls)),
            "invalid_reasons": dict(Counter(c.invalid_reason for c in calls if c.invalid_reason)),
        },
        "stopped_on_ban": dict(stopped_on_ban) if stopped_on_ban else None,
        "interrupted": interrupted,
        "estimate": None
        if est is None
        else {
            "offset_ms": est.offset_ms,
            "min_rtt_ms": est.min_rtt_ms,
            "half_min_rtt_ms": half,
            "bound_ms": est.bound_ms,
            "offset_minus_half_min_rtt_ms": est.offset_ms - (half or 0.0),
            "offset_plus_half_min_rtt_ms": est.offset_ms + (half or 0.0),
            "margin_to_threshold_ms": THRESHOLD_MS - est.bound_ms,
            "criterion_met": est.criterion_met,
            "fastest_calls": [
                {"index": c.index, "rtt_ms": c.rtt_ms, "offset_ms": c.offset_ms} for c in est.fastest
            ],
        },
        "latency_ms": {
            "wall_rtt": _stats([c.rtt_ms for c in valid]),
            "monotonic_rtt": _stats([c.mono_rtt_ms for c in valid]),
            "max_abs_wall_minus_monotonic": max((abs(c.rtt_ms - c.mono_rtt_ms) for c in valid), default=None),
        },
        "offset_all_valid_ms": _stats([c.offset_ms for c in valid if c.offset_ms is not None]),
        "host_clock_sync": NTP_NOTE,
    }


def _verdict(
    *,
    registered: bool,
    finished: bool,
    interrupted: bool,
    ban: Mapping[str, Any] | None,
    n_valid: int,
    need: int,
    est: ClockEstimate | None,
) -> tuple[str, str]:
    if not registered:
        extra = f"; stopped on {ban['reason']}" if ban else ""
        return "NOT_REGISTERED_RUN", NOT_REGISTERED_NOTE + extra
    if ban:
        return "FAIL", f"{ban['reason']}: ban-type response, run stopped (registered: counts as failed)"
    if interrupted or not finished:
        return "INCOMPLETE", "run did not reach the last scheduled call (counts as not passed)"
    if n_valid < need:
        return "FAIL", f"only {n_valid} valid calls, fewer than the registered {need}"
    if est is None:
        return "FAIL", "no valid call"
    verb = "<" if est.criterion_met else ">="
    word = "PASS" if est.criterion_met else "FAIL"
    return word, f"|offset| + min_rtt/2 = {est.bound_ms:.3f} ms {verb} {THRESHOLD_MS:.0f} ms"


def exit_code(summary: Mapping[str, Any], *, sigterm: bool) -> int:
    if summary["interrupted"]:
        return 143 if sigterm else 130
    return {
        "PASS": EXIT_PASS,
        "FAIL": EXIT_FAIL,
        "INCOMPLETE": EXIT_INCOMPLETE,
        "NOT_REGISTERED_RUN": EXIT_NOT_REGISTERED,
    }[summary["verdict"]]


# ---------------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------------


def _f(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def render_markdown(summary: Mapping[str, Any], calls: Sequence[CallRecord]) -> str:
    prov = summary["provenance"]
    plan, counts, est = summary["plan"], summary["counts"], summary["estimate"]
    title = (
        "LBank clock re-test (D-079)"
        if summary["registered_run"]
        else "LBank clock test -- NOT the registered run"
    )
    lines = [
        f"# {title}",
        "",
        f"**Verdict: {summary['verdict']}** -- {summary['verdict_reason']}",
        "",
        f"- {prov['measured_from']} (host label: {prov['hostname']}; commit {prov['git_commit']}; "
        f"run ended {prov.get('utc_end')})",
        f"- Plan: {plan['calls']} calls to public `timestamp.do`, {plan['spacing_s']:g} s apart, "
        f"timeout {plan['timeout_s']:g} s, one request in flight; fastest {plan['fastest_n']} by RTT; "
        f"at least {plan['min_valid_calls']} valid calls needed.",
        f"- Rule: {summary['rule']}",
        f"- Calls: {counts['attempted']} attempted, {counts['valid']} valid, "
        f"{counts['skipped_missed_slots']} slots skipped; HTTP status counts {counts['http_status']}",
    ]
    if not summary["registered_run"]:
        lines += ["", f"> {NOT_REGISTERED_NOTE}"]
    if summary["stopped_on_ban"]:
        lines += ["", f"> Stopped on ban-type response: {summary['stopped_on_ban']}"]
    lines += ["", f"> {NTP_NOTE}", ""]
    if est:
        lines += [
            "## Estimate",
            "",
            _table(
                ["quantity", "value (ms)"],
                [
                    ["offset (median of fastest calls; + = LBank ahead of host)", _f(est["offset_ms"], 3)],
                    ["min_rtt (smallest valid RTT)", _f(est["min_rtt_ms"], 3)],
                    [
                        "offset +/- min_rtt/2",
                        f"[{_f(est['offset_minus_half_min_rtt_ms'], 3)}, "
                        f"{_f(est['offset_plus_half_min_rtt_ms'], 3)}]",
                    ],
                    ["abs(offset) + min_rtt/2", _f(est["bound_ms"], 3)],
                    ["margin to 500 ms", _f(est["margin_to_threshold_ms"], 3)],
                ],
            ),
            "",
            f"## Fastest {plan['fastest_n']} calls by RTT",
            "",
            _table(
                ["rank", "call", "RTT ms", "offset ms"],
                [
                    [str(r), str(c["index"]), _f(c["rtt_ms"], 3), _f(c["offset_ms"], 3)]
                    for r, c in enumerate(est["fastest_calls"], 1)
                ],
            ),
            "",
        ]
    lat = summary["latency_ms"]
    lines += [
        "## Latency of valid calls (ms)",
        "",
        _table(
            ["clock", "min", "p50", "p95", "max"],
            [
                [name, *(_f(lat[key][k]) for k in ("min", "p50", "p95", "max"))]
                for name, key in (("wall (t_after - t_before)", "wall_rtt"), ("monotonic", "monotonic_rtt"))
            ],
        ),
        "",
        "## All calls",
        "",
        _table(
            [
                "call",
                "start UTC",
                "HTTP",
                "valid",
                "RTT ms (wall)",
                "RTT ms (mono)",
                "server ms",
                "offset ms",
                "note",
            ],
            [
                [
                    str(c.index),
                    _ns_to_iso(c.t_before_ns),
                    str(c.status) if c.status is not None else "-",
                    "yes" if c.valid else "no",
                    _f(c.rtt_ms, 3),
                    _f(c.mono_rtt_ms, 3),
                    str(c.server_ms) if c.server_ms is not None else "-",
                    _f(c.offset_ms, 3),
                    c.invalid_reason or "",
                ]
                for c in calls
            ],
        ),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _publish(paths: OutputPaths, summary: Mapping[str, Any], calls: Sequence[CallRecord]) -> None:
    _write_atomic(paths.summary, json.dumps(summary, indent=2, default=str) + "\n")
    _write_atomic(paths.markdown, render_markdown(summary, calls))


def run_clock_test(
    client: httpx.Client,
    clock: Clock,
    *,
    calls: int,
    spacing_s: float,
    paths: OutputPaths,
    provenance: dict[str, Any],
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Run the schedule; always writes JSONL, summary JSON and markdown. Returns the summary."""
    writer = JsonlWriter(paths.jsonl)
    writer.write({"kind": "provenance", **provenance})
    done: list[CallRecord] = []
    skipped: list[int] = []
    ban: dict[str, Any] | None = None
    interrupted = False
    late_ok = min(MAX_LATE_START_S, spacing_s / 2)
    run_start = clock.monotonic()

    def snapshot(*, finished: bool) -> dict[str, Any]:
        summary = build_summary(
            provenance, done, skipped, planned_calls=calls, spacing_s=spacing_s,
            finished=finished, interrupted=interrupted, stopped_on_ban=ban,
        )  # fmt: skip
        _publish(paths, summary, done)
        return summary

    try:
        index = 0
        while index < calls:
            due = index * spacing_s
            late_by = clock.monotonic() - run_start - due
            if late_by > late_ok:  # missed slot: skip it, never catch up
                skipped.append(index)
                writer.write({"kind": "skipped_slot", "index": index, "due_s": due, "late_by_s": late_by})
                index += 1
                continue
            if late_by < 0:
                clock.sleep(-late_by)
            rec = timed_call(client, clock, index, due, run_start)
            done.append(rec)
            writer.write(rec.as_json())
            log(
                f"call {index + 1}/{calls}: HTTP {rec.status} rtt={rec.rtt_ms:.0f} ms "
                f"offset={_f(rec.offset_ms)} ms valid={rec.valid}"
            )
            snapshot(finished=False)
            if rec.is_ban:
                ban = {"reason": f"HTTP {rec.status}", "call": rec.index}
                writer.write({"kind": "stopped_on_ban", **ban})
                break
            index += 1
    except KeyboardInterrupt:
        interrupted = True
    provenance["utc_end"] = clock.utc().isoformat()
    summary = snapshot(finished=not interrupted and ban is None)
    writer.write(
        {"kind": "end", "utc_end": provenance["utc_end"], "interrupted": interrupted,
         "stopped_on_ban": ban is not None, "verdict": summary["verdict"]}
    )  # fmt: skip
    writer.close()
    return summary


# ---------------------------------------------------------------------------------
# provenance / CLI
# ---------------------------------------------------------------------------------


def resolve_git_commit() -> str:
    """``TBOT_GIT_COMMIT`` if set, else ``git rev-parse HEAD`` with ``-dirty`` when any of
    :data:`DIRTY_CHECK_PATHS` has uncommitted changes; ``unknown`` if git is unavailable."""
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
            cwd=repo, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not head:
        return "unknown"
    return f"{head}-dirty" if dirty else head


def make_provenance(clock: Clock, argv: Sequence[str], host_label: str | None) -> dict[str, Any]:
    prov = collect_provenance(Env(now=clock.utc), "clock-retest", argv, host_label=host_label)
    commit = resolve_git_commit()
    prov.update(
        script=SCRIPT_NAME,
        git_commit=commit,
        git_commit_unknown=commit == "unknown",
        user_agent=USER_AGENT,
        local_clock_sync=NTP_NOTE,
    )
    return prov


class SigtermGuard:
    """Turn SIGTERM into KeyboardInterrupt so partial outputs get written."""

    def __init__(self) -> None:
        self.received = False
        self._previous: Any = None
        self._installed = False

    def _handler(self, _signum: int, _frame: FrameType | None) -> None:
        self.received = True
        raise KeyboardInterrupt

    def __enter__(self) -> SigtermGuard:
        try:
            self._previous = signal.signal(signal.SIGTERM, self._handler)
            self._installed = True
        except ValueError:  # not in the main thread
            self._installed = False
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._installed:
            signal.signal(signal.SIGTERM, self._previous)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.lbank_clock_test",
        description="Registered LBank clock re-test (D-079): public timestamp.do only. "
        "The defaults are the registered values.",
    )
    p.add_argument("--calls", type=int, default=REGISTERED_CALLS, help="number of calls (registered: 120)")
    p.add_argument(
        "--spacing",
        type=float,
        default=REGISTERED_SPACING_S,
        help="seconds between call starts (registered: 30)",
    )
    p.add_argument("--out-dir", type=Path, default=Path("data/lbank_clock_test"))
    p.add_argument("--host-label", default=None, help="host name to record (a container reports its id)")
    return p


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if args.calls < 1:
        parser.error("--calls must be >= 1")
    if args.spacing < MIN_SPACING_S:
        parser.error(f"--spacing must be >= {MIN_SPACING_S:g} s")
    clock = clock or Clock()
    prov = make_provenance(clock, raw_argv, args.host_label)
    registered = args.calls == REGISTERED_CALLS and args.spacing == REGISTERED_SPACING_S
    prov["registered_run"] = registered
    if not registered:
        _stderr(NOT_REGISTERED_NOTE)
    stamp = clock.utc().strftime("%Y%m%dT%H%M%SZ")
    paths = OutputPaths.for_run(args.out_dir, "lbank_clock_test", stamp)
    with (
        SigtermGuard() as guard,
        httpx.Client(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
            follow_redirects=False,
        ) as client,
    ):
        summary = run_clock_test(
            client, clock, calls=args.calls, spacing_s=args.spacing, paths=paths, provenance=prov, log=_stderr
        )
    code = exit_code(summary, sigterm=guard.received)
    print(f"verdict={summary['verdict']} exit={code} outputs: {paths.summary} {paths.markdown} {paths.jsonl}")
    return code


if __name__ == "__main__":
    sys.exit(main())
