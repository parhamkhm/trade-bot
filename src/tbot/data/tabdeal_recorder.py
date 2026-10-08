"""Tabdeal public-trades recorder, order-book snapshotter and candle-sweep driver.

Tabdeal has no kline endpoint (CLAUDE.md section 6), so this module polls the public ``/trades``
endpoint, persists every trade exactly once (SQLite, dedupe by trade id), detects coverage loss
against the previously-stored trades, and periodically asks ``tbot.data.candles`` to emit any 1h
candle that is now due. It also polls the public ``/depth`` endpoint on its own interval and
stores a cumulative-depth snapshot (decision D-019) for phase 3's execution-cost model.

Reuse, not reimplementation: the depth/trade-window maths (``best_bid_ask``,
``spread_bps_and_pct``, ``cumulative_depth``, ``is_saturated``, ``parse_depth_levels``) is
imported unchanged from ``tbot.data.depth`` (decision D-025), shared with ``scripts/tabdeal_probe.py``.

Saturation (decision D-024): ``count == limit`` (``is_saturated``) is stored for raw fidelity but
is **not** an alert and **not** used to mark a candle incomplete -- on a recent-trades endpoint it
is true on nearly every poll and is uninformative by itself. ``coverage_ratio =
window_span_seconds / poll_interval_seconds`` (< 3 = at risk) is the operationally meaningful
number, logged as a warning.

**Trade ids are global across symbols, not per-symbol-contiguous** (decision D-037 --
measured from the Turkey server 2026-10-04: BTCUSDT ids step by a median of 84, max 2069, as other
symbols' trades interleave in the same id space). The old "first returned id > last_stored_id + 1"
gap test therefore fired on nearly every poll for a thin market like BTCUSDT and would have made
G1b unsatisfiable. It is replaced by an **overlap** test: a poll proves continuity iff the lowest
id it returns is ``<= last_stored_id`` (the new window reaches back into what is already stored).
When it does not, a ``window_no_overlap`` gap is recorded (see ``poll_trades_once``) -- this is
the gap reason going forward; ``id_discontinuity`` no longer exists in the vocabulary. BTCUSDT is
also thin (median inter-trade gap 46s, p99 641s, max 2120s over a 29h/1000-trade sample), which is
why the heartbeat trade-staleness default (see ``m3`` below) is 7200s, not a tighter value.

Causality / no forward-fill: an hour with zero recorded trades produces **no** candle, only a
``gaps`` row (``reason="no_trades_in_hour"``) -- see ``build_due_candles``.

Fix-round history (one line each; see ``research/EXPERIMENTS.md``-adjacent task reports for the
full narrative of each finding):

* MAJOR-1 (parse): ``price``/``qty`` accepts ``str | Decimal`` (decision D-031 made an unquoted
  JSON number arrive as ``Decimal``, not ``str``); a bare ``float`` is still rejected (no exact
  decimal recoverable from it).
* MAJOR-2 (schema): ``RecorderStore`` migrates an existing database by adding any column/table the
  current schema expects but the file predates (``PRAGMA user_version``-gated, decision D-035);
  ``CREATE TABLE IF NOT EXISTS`` alone is a no-op against an existing table.
* M-A/M-B (poll shape): a non-list HTTP-200 body, and a partial mid-batch parse failure, both now
  make ``PollOutcome.ok=False`` / record a ``partial_unparsable`` gap respectively, instead of
  looking like a healthy empty poll.
* MINOR-4 (poisoned rows): a non-finite/non-positive price or non-finite/negative qty is rejected
  in ``parse_trade_item`` before persistence, with a second defensive filter in
  ``tbot.data.candles.build_candle`` for a row written before this guard existed.
* MINOR-6/7: a newer-than-supported schema version raises ``RecorderSchemaVersionError``; a
  corrupt database raises ``RecorderDatabaseError`` naming the path.

Fourth fix round (this task):

* MAJOR-1': a candle could be written ``complete=True`` while the underlying polls across its
  close time were failing (never verified), and a later id-overlap gap found against an
  already-written candle was never reflected back onto it. See ``verified_poll_ts_ms`` /
  ``build_due_candles`` / the gap-rewrite step in ``poll_trades_once``.
* MAJOR-2': a 200 response whose every item failed to parse used to count as ``ok=True``.
* MAJOR-3: all SQLite writes for one poll (trades + poll_log + gap rows + the verified-poll
  marker) are now one atomic transaction (``RecorderStore.transaction``); ditto an hour-gap row
  plus its sweep-cursor advance.
* m1: a crossed order book (``spread_bps_and_pct`` raising) is caught, logged
  ``orderbook_crossed``, and still written with ``spread_bps=None``.
* m2: a trade whose ``ts`` is more than a day from the poll time is rejected (catches a unit bug --
  seconds/microseconds/0 -- before it corrupts the sweep).
* m3: the heartbeat carries ``last_new_trade_ts_ms``; the healthcheck can fail on a stale trade
  feed even while polling itself looks healthy.
* m4: the recorder backs off exponentially (cap 300s) once ``consecutive_errors > 5``.
* m6: the database records which symbol it was started for (``meta`` table) and refuses to open
  against a mismatched one.

Fifth fix round (this task -- second-round review, ahead of G1b deployment):

* MAJOR-A: the ``_MAX_TRADE_AGE_MS`` (24h) age window in ``parse_trade_item`` rejected ~17% of
  every normal ~29h/1000-trade poll window as "unparsable" -- a trade's own age is not a reason to
  reject it (dedupe makes re-insertion of a genuinely old trade harmless). Replaced by a pure
  timestamp-unit plausibility check (``_MIN_PLAUSIBLE_TRADE_TS_MS`` / ``_MAX_FUTURE_SKEW_MS``):
  reject only an implausible unit (seconds-scale, microsecond-scale, zero) or a timestamp more
  than 5 minutes in the future, never a merely old one.
* MAJOR-B: an HTTP-200 empty list (``[]``) counted as a verified, healthy poll unconditionally.
  Once the database already holds trades, a recent-trades endpoint legitimately returning ``[]``
  is itself suspicious (symbol mismatch, endpoint regression, a stale/cached response) -- it is no
  longer ``ok`` and no longer advances ``recorder_state.last_verified_poll_ts_ms`` in that case. A
  cold start (nothing stored yet) is unaffected: there is nothing for ``[]`` to contradict.
* m-C: ``build_due_candles`` used to seal an hour the instant the verified-poll-capped wall clock
  said it was due, with no check that the feed had actually produced any evidence of having moved
  past that hour -- a stale/cached response (or ordinary clock skew) could seal an hour too early.
  An hour is now due only when, in addition, either a stored trade exists after its close (direct
  proof) or the last verified poll is at least ``quiet_hour_timeout_seconds`` (default 7200s, i.e.
  2h, comfortably above the measured 2120s max inter-trade gap) past its close (quiet-market
  timeout). The sweep stops at the first hour that fails this, rather than skipping ahead.
* m-D: ``_sweep_now_ms`` fell back to the raw wall clock whenever no poll had ever been verified --
  unsafe for a freshly migrated pre-v3 database (``recorder_state`` starts empty even though the
  file has history). With no verified poll at all, the sweep now does nothing until the first one
  lands.
* m-E: a ``COMMIT`` that itself failed left the connection wedged inside an open transaction
  forever (every later ``transaction()`` call then raised "cannot start a transaction within a
  transaction"). ``transaction()`` now rolls back on a failed ``COMMIT`` and rolls back any
  pre-existing dangling transaction before issuing ``BEGIN``. Separately, ``run_forever`` now
  raises ``RecorderFatalError`` (causing a non-zero process exit, so Docker's ``restart:
  unless-stopped`` can recover it) after ``_MAX_CONSECUTIVE_CYCLE_EXCEPTIONS`` (default 20)
  consecutive ``process_once()`` exceptions, instead of looping inside a permanently broken
  process forever.
* m-F: ``5.0 * 2 ** (n - 5)`` in ``_next_wait_seconds`` overflows (``OverflowError``) once ``n``
  grows large enough (around 1029) -- the exponent is now capped (``min(n - 5, 16)``) before it is
  ever raised to a power, well ahead of the 300s cap being applied.
* m-G: the rewrite of an already-written candle back to ``complete=False`` (MAJOR-1', fourth fix
  round) happened entirely after the poll's own transaction committed -- a crash between the two
  left that candle ``complete=True`` forever with no trace. Schema v4 adds ``gaps.rewritten``
  (``INTEGER NOT NULL DEFAULT 0``); every sweep now reprocesses any ``window_no_overlap`` gap
  still marked ``rewritten=0`` before doing anything else, so this is self-healing on the very
  next sweep rather than permanent.
* NITs: an empty hour that falls inside an already-recorded ``window_no_overlap`` gap's span is
  recorded with that same reason, not the misleading ``no_trades_in_hour`` (it is a known data-loss
  hole, not a quiet market); the heartbeat/healthcheck now also fail when no trade has *ever* been
  recorded (``last_new_trade_ts_ms`` stays ``null`` forever) well past ``first_poll_ts_ms`` --
  previously a recorder that only ever received ``[]`` looked healthy indefinitely.

Sixth fix round (this task -- round-3 review, MINOR/NIT fixes ahead of the G1b 7-day window):

* MINOR-1: a feed that keeps answering HTTP 200 but is actually frozen/stale for longer than
  ``quiet_hour_timeout_seconds`` could let the quiet-timeout path in ``_hour_feed_has_moved_past``
  seal an hour ``complete=True`` (or skip it as ``no_trades_in_hour``) with real trades for it
  still outstanding -- the feed's own ``verified_poll_ts_ms`` keeps advancing on the wall clock
  even though the *data* it returns has not moved on at all. ``_persist_poll`` now detects, inside
  the same transaction as the insert, any newly-inserted trade (by this poll's own
  ``recorded_ts_ms``) whose ``ts_ms`` falls at or before the sweep's resumption cursor (at the
  time, ``_resumption_cursor_ms``, shared with ``build_due_candles`` -- see the seventh fix
  round's M-1 for why this is SQLite-only, via ``RecorderStore.last_swept_close_ms``, from here
  on) -- i.e. a trade for an hour already sealed one way or another. Each such hour gets a
  ``late_trades`` gap row and, once the poll's own
  transaction has committed, a correction: ``_rewrite_hour_for_late_trades`` either rewrites an
  already-written candle ``complete=False`` (delegating to ``_rewrite_candle_incomplete_if_written``)
  or, for an hour that was swept as empty/poisoned with no candle at all, writes a fresh
  ``complete=False`` candle for it now that it demonstrably has trades. Uses the same
  schema-v4 ``rewritten`` self-healing path as the ``window_no_overlap`` gap rewrite (m-G) --
  ``_recover_unrewritten_gaps`` also retries any ``late_trades`` gap left ``rewritten=0`` by a
  crash between the poll's commit and the Parquet correction.
* MINOR-2: ``_record_partial_unparsable`` used to anchor a dropped-item gap to the hours of the
  trades that DID parse in that same poll -- which, on a poll whose response spans the full
  ~29h/1000-trade window, could mark roughly 29 consecutive hours incomplete (and re-record a gap
  for every one of them, every poll) because of a single unrelated bad item. It now parses each
  dropped item's own ``time`` field leniently (even though the item failed real parsing) and
  anchors that item's gap to its own hour, falling back to the poll's own hour only when no time
  can be read at all. ``RecorderStore.record_gap_once`` also dedupes by ``(reason, hour_close_ms)``
  (``INSERT ... WHERE NOT EXISTS``) so rediscovering the same affected hour on a later poll no
  longer re-records a duplicate row. Schema v5 adds ``idx_gaps_hour_close_ms`` and
  ``idx_gaps_reason_rewritten`` (both pure index additions -- migrated the same way every other
  schema bump is, via ``_migrate_schema`` re-running ``_SCHEMA_SQL``'s ``CREATE INDEX IF NOT
  EXISTS`` statements against an already-existing table).
* MINOR-3: a sweep-only exception (e.g. a corrupt Parquet part hit by ``build_due_candles``, not
  an ordinary poll failure) used to make ``run_forever`` exit every ~95s with *no* backoff at all,
  because the poll itself kept succeeding and reset ``_consecutive_errors`` to 0 every cycle right
  before the sweep raised -- only ``_consecutive_cycle_exceptions`` (which ``_next_wait_seconds``
  never looked at) actually climbed toward the fatal-exit threshold. ``_next_wait_seconds`` now
  keys its backoff off ``max(_consecutive_errors, _consecutive_cycle_exceptions)``. ``process_once``
  also now writes the heartbeat from a ``finally`` block (so it is written even when the cycle
  raises) with a new ``last_cycle_error`` field -- the failing cycle's own error text, passed
  through the existing ``tbot.monitoring.logging.redact_secrets`` pipeline before it ever reaches
  the heartbeat file -- so the container's state is diagnosable from `docker inspect`/the
  healthcheck output, not just from the already-redacted structlog line.
* NIT: ``RecorderStore.transaction()`` could not tell a dangling leftover transaction from a prior
  crash (``conn.in_transaction`` already ``True`` on entry) apart from a *nested* call -- e.g. a bug
  calling a transaction-wrapped method from inside another one -- which shows the exact same state.
  The old code silently ``ROLLBACK``-ed either way, which for a genuine nesting bug would silently
  discard the outer, legitimate, in-progress transaction's work with no error at all. An explicit
  ``_in_transaction_block`` flag (set only by this method, never derived from sqlite3's own state)
  now makes a nested call raise ``RuntimeError`` immediately instead.
* NIT: the future-skew plausibility check's ``now_ms`` used to be sampled *before* the ``/trades``
  HTTP call -- with ``TabdealClient``'s own retry/backoff budget (up to ~360s worst case for a
  slow/retried round-trip), a genuinely fresh trade could arrive stamped further in the future than
  that stale, pre-call ``now_ms`` plus the 5-minute skew tolerance, and get wrongly dropped as
  ``partial_unparsable``. ``_fetch_and_parse`` now samples ``now_ms`` immediately after the HTTP
  call returns (including any retries), not before it starts.
* NIT: a transient forward clock jump (e.g. an NTP step) recorded as ``verified_poll_ts_ms`` (which
  only ever advances, MAX-based) stayed stuck at that too-high value forever once the real clock
  corrected back down -- permanently satisfying ``_hour_feed_has_moved_past``'s quiet-timeout check
  for every hour regardless of how little real time had actually passed since. That function now
  takes the sweep's already clock/verified-capped ``now_ms`` (``_resumption_cursor_ms``'s sibling,
  ``_sweep_now_ms``'s own ``min(real_clock_now_ms, verified_poll_ts_ms)``) instead of re-querying
  the raw, uncapped ``verified_poll_ts_ms`` itself -- it self-corrects the instant the real clock
  reading drops back below the stale verified value, with no separate write-side clamp needed.

Seventh fix round (this task -- PR2 review, ahead of merge):

* M-1 (MAJOR): ``_persist_poll``'s own ``BEGIN IMMEDIATE`` transaction -- the one wrapping
  ``insert_trades`` -- used to reach ``_detect_late_trades`` -> ``_resumption_cursor_ms`` ->
  ``candles.last_written_close_ms``, a Parquet read, to find the late-trade cursor. A
  corrupt/unreadable Parquet part raised there and rolled back ``insert_trades`` on *every*
  poll (reproduced: 30 cycles, trade count stuck; ``run_forever`` hits its fatal-exit threshold,
  restarts, repeats) -- any raw trade past the ``/trades`` endpoint's ~29h response window was
  then lost forever, with no way to ever recover it. The late-trade (and now also
  partial-unparsable) cursor lives in SQLite only from here on: ``RecorderStore.
  last_swept_close_ms`` (the existing ``sweep_cursor`` table). ``_process_due_hour`` now calls
  the new ``RecorderStore.advance_sweep_cursor`` right after a successful ``write_candle`` (not
  just on a gapped hour, which already advanced it via ``record_hour_gap``), and
  ``build_due_candles`` reconciles ``sweep_cursor = max(sweep_cursor, last_written_close_ms)``
  once at the start of every sweep (``_reconcile_sweep_cursor``), strictly outside any poll
  transaction and inside its own try/except (log + skip reconciliation on a read failure). No
  Parquet read happens inside the poll transaction, or anywhere on the trade-ingestion path, any
  more; ``_persist_poll`` no longer even takes a ``parquet_root`` parameter.
* m-1 (MINOR): ``_lenient_item_time_ms`` used to trust a dropped item's own ``time`` field at
  face value, with no plausibility check -- an implausible value (e.g. seconds-scale, a unit
  bug) anchored the resulting ``partial_unparsable`` gap to a bogus 1970-ish hour, leaving the
  item's REAL hour with no gap row at all (sealed ``complete=True`` later, with that trade
  silently missing). It now applies the exact same plausibility bounds ``parse_trade_item``
  itself uses, and falls back (via the new ``_parse_items_with_neighbors``) to the hour span
  between the item's nearest successfully-parsed neighbours in the response's own order, only
  falling back further, to the poll's own hour, when there is no parsed neighbour at all.
* m-2 (MINOR): a ``partial_unparsable`` gap recorded for an hour already swept past never
  rewrote that hour the way a ``late_trades`` gap does -- only ``_detect_late_trades`` triggered
  ``_rewrite_hour_for_late_trades``. ``_persist_poll`` now checks every newly-recorded
  ``partial_unparsable`` gap's ``hour_close_ms`` against the same SQLite sweep cursor used for
  late trades, and routes it through the identical post-commit rewrite
  (``_apply_gap_rewrites``) when it is at or before that cursor.
* m-3 (MINOR): the healthcheck's ``consecutive_errors`` rule never fired on a sweep-only failure
  -- a successful poll reset ``_consecutive_errors`` to 0 right before ``build_due_candles``
  raised, and the one counter that DID climb (``_consecutive_cycle_exceptions``, ``run_forever``
  -only) was never written to the heartbeat at all. ``HeartbeatState``/``write_heartbeat`` now
  carry ``consecutive_cycle_exceptions`` (including the current failing cycle), and
  ``deploy/healthcheck.py`` fails once it reaches ``TBOT_HEARTBEAT_MAX_CYCLE_EXCEPTIONS``
  (default 3).
* NITs: ``RecorderStore.late_trade_ts_ms`` now narrows by ``trade_id IN (...)`` (this poll's own
  ids, the primary key) before the ``recorded_ts_ms``/``ts_ms`` filters, instead of scanning the
  whole ``trades`` table on every poll; ``TabdealRecorderService.process_once`` wraps its own
  heartbeat write in a dedicated ``try``/``except`` so a failure writing it (e.g. a full disk)
  can never replace or mask the cycle's own exception, nor the ``last_cycle_error`` it carries.
* Engineering rule added to CLAUDE.md Sec.3 following this review: raw trade ingestion must
  never depend on any downstream step. ``poll_trades_once``'s post-commit Parquet rewrite
  (``_apply_post_commit_corrections``) and ``TabdealRecorderService.process_once``'s candle
  sweep and order-book poll are each independently contained -- a failure in any one of them is
  logged at error level and folded into the heartbeat (``PollOutcome.degraded_error`` /
  ``_consecutive_degraded_cycles``), but never allowed to raise out of ``poll_trades_once`` or
  ``process_once`` respectively. This is what M-1 above already did for the trade-ingestion
  transaction itself (no Parquet read inside it at all); this generalizes the same containment
  to every OTHER downstream step the recorder also runs, so a permanently broken candle sweep or
  order-book poll can degrade monitoring (via the heartbeat) without ever throttling (via
  ``run_forever``'s backoff) or killing (via ``RecorderFatalError``) trade ingestion itself.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog

from tbot.core.types import Clock
from tbot.data import candles as candles_mod
from tbot.data.depth import (
    best_bid_ask,
    cumulative_depth,
    is_saturated,
    parse_depth_levels,
    spread_bps_and_pct,
    to_decimal,
)
from tbot.execution.tabdeal_client import ProbeResult, TabdealClient
from tbot.monitoring.logging import redact_secrets

__all__ = [
    "HeartbeatState",
    "OrderbookRow",
    "PollOutcome",
    "RecorderDatabaseError",
    "RecorderFatalError",
    "RecorderSchemaVersionError",
    "RecorderSettings",
    "RecorderStore",
    "RecorderSymbolMismatchError",
    "TabdealRecorderService",
    "build_due_candles",
    "is_candle_complete",
    "parse_trade_item",
    "poll_orderbook_once",
    "poll_trades_once",
    "write_heartbeat",
]

logger = structlog.get_logger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# MAJOR-A (fifth fix round): a trade's timestamp is rejected only when it is implausible as an
# epoch-millisecond value -- never merely because it is old. Below this value it is almost
# certainly seconds-scale (or a literal 0 / unit bug): 1.5e12 ms is 2017-07-14, long before this
# exchange or this recorder existed. Above "now + _MAX_FUTURE_SKEW_MS" it is either a clock-skew
# artifact or a microsecond-scale value (which overshoots by a factor of ~1000 and is caught here
# too). A trade that is merely old (even far beyond 24h) is valid and must be inserted -- dedupe
# makes re-insertion harmless, and rejecting it as "unparsable" used to manufacture a
# partial_unparsable gap (and an incomplete candle) on nearly every poll of a normal ~29h/
# 1000-trade window.
_MIN_PLAUSIBLE_TRADE_TS_MS = 1_500_000_000_000
_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

# MINOR-3 (PR2 followups): SQLite's INTEGER PRIMARY KEY (the `trades.trade_id` column) is a
# signed 64-bit integer -- an id above this value raises sqlite3.OperationalError /
# IntegrityError INSIDE insert_trades's executemany, which aborts and rolls back the whole
# poll's transaction (every other trade in the same 1000-trade window, not just the bad one).
# Rejected here instead, in parse_trade_item, so an out-of-range id takes the normal
# "unparsable item" path (a partial_unparsable gap for its own hour) rather than taking down
# every trade in the poll.
_SQLITE_INT64_MAX = 2**63 - 1

# m4: once consecutive_errors exceeds this, run_forever backs off exponentially instead of
# retrying at the nominal poll interval (see _next_wait_seconds). m-F (fifth fix round): the
# exponent itself is capped before ``2 ** exponent`` is ever computed (see _next_wait_seconds).
_BACKOFF_THRESHOLD_ERRORS = 5
_BACKOFF_CAP_SECONDS = 300.0
_BACKOFF_MAX_EXPONENT = 16

# m-E (fifth fix round): run_forever raises RecorderFatalError (a non-zero process exit) after
# this many consecutive process_once() exceptions, instead of looping inside a permanently broken
# process forever.
_MAX_CONSECUTIVE_CYCLE_EXCEPTIONS = 20

# m-C (fifth fix round): default for RecorderSettings.quiet_hour_timeout_seconds -- see
# build_due_candles's docstring. Comfortably above the measured 2120s max BTCUSDT inter-trade gap.
_DEFAULT_QUIET_HOUR_TIMEOUT_SECONDS = 7200.0

# Cumulative-depth thresholds, as fractions of mid price -- 0.1% / 0.5% / 1% (D-019, matches the
# probe's own _DEPTH_THRESHOLDS_PCT).
_DEPTH_THRESHOLDS: tuple[Decimal, Decimal, Decimal] = (Decimal("0.001"), Decimal("0.005"), Decimal("0.01"))

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id INTEGER PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    is_buyer_maker INTEGER NOT NULL,
    recorded_ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts_ms ON trades(ts_ms);

CREATE TABLE IF NOT EXISTS poll_log (
    poll_ts_ms INTEGER NOT NULL,
    first_id INTEGER,
    last_id INTEGER,
    n_trades INTEGER NOT NULL,
    saturated INTEGER NOT NULL,
    http_status INTEGER,
    latency_ms REAL,
    window_span_seconds REAL,
    coverage_ratio REAL,
    n_items_received INTEGER
);

CREATE TABLE IF NOT EXISTS gaps (
    detected_ts_ms INTEGER NOT NULL,
    from_id INTEGER,
    to_id INTEGER,
    reason TEXT NOT NULL,
    hour_close_ms INTEGER,
    -- m-G (fifth fix round, schema v4): whether a window_no_overlap gap's rewrite onto any
    -- already-written candle it touches has been confirmed to run -- see
    -- tbot.data.tabdeal_recorder._recover_unrewritten_gaps.
    rewritten INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sweep_cursor (
    symbol TEXT PRIMARY KEY,
    last_swept_close_ms INTEGER NOT NULL
);

-- MAJOR-1' (fourth fix round): the last poll_ts_ms whose poll actually reached the exchange and
-- either proved window overlap or recorded a gap -- i.e. is trustworthy for the sweep to build on.
-- Separate table (not a sweep_cursor column) because sweep_cursor's own column is NOT NULL with no
-- natural "never swept" sentinel that would not collide with a real hour_close_ms.
CREATE TABLE IF NOT EXISTS recorder_state (
    symbol TEXT PRIMARY KEY,
    last_verified_poll_ts_ms INTEGER NOT NULL
);

-- m6: which symbol this database was recorded for, set on first use. A database must never be
-- silently reused for a different symbol -- see RecorderStore.ensure_symbol.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook (
    ts_ms INTEGER NOT NULL,
    best_bid TEXT,
    best_ask TEXT,
    spread_bps REAL,
    depth_bid_01pct TEXT NOT NULL,
    depth_ask_01pct TEXT NOT NULL,
    depth_bid_05pct TEXT NOT NULL,
    depth_ask_05pct TEXT NOT NULL,
    depth_bid_1pct TEXT NOT NULL,
    depth_ask_1pct TEXT NOT NULL
);
"""

# MINOR-2 (sixth fix round, schema v5): kept separate from _SCHEMA_SQL and run only AFTER
# _migrate_schema's ALTER TABLE column-backfill loop below -- idx_gaps_hour_close_ms and
# idx_gaps_reason_rewritten both index a column (hour_close_ms, rewritten) that a pre-v4 database
# does not have yet at the point _SCHEMA_SQL's own executescript runs (CREATE TABLE IF NOT EXISTS
# is a no-op against an already-existing table, so those columns are only added by the loop that
# runs after it); creating an index on them any earlier raises "no such column". Both statements
# are plain `CREATE INDEX IF NOT EXISTS`, so running this script again on an already-migrated
# database (the common case) is a cheap no-op. idx_gaps_hour_close_ms speeds up
# has_hour_gap/unrewritten_hour_gaps/record_gap_once, all keyed by hour_close_ms;
# idx_gaps_reason_rewritten speeds up unrewritten_overlap_gaps/unrewritten_hour_gaps, both
# filtered by (reason, rewritten).
_GAPS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_gaps_hour_close_ms ON gaps(hour_close_ms);
CREATE INDEX IF NOT EXISTS idx_gaps_reason_rewritten ON gaps(reason, rewritten);
"""


def _dt_to_ms(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds() * 1000)


def _redact_cycle_error(raw: str) -> str:
    """Scrub a cycle-failure message through the existing secret-redaction pipeline
    (``tbot.monitoring.logging.redact_secrets``) before it is ever written to the heartbeat file
    (MINOR-3, sixth fix round) -- the heartbeat is operational state read by
    ``deploy/healthcheck.py`` and potentially surfaced to an operator, so it must go through the
    same scrubbing as every structlog line rather than writing a raw exception message straight
    to disk."""
    return str(redact_secrets(None, "process_once", {"error": raw})["error"])


# ---------------------------------------------------------------------------------
# schema migration (MAJOR-2, second fix round)
# ---------------------------------------------------------------------------------

# Current schema version. Bumped whenever a column/table/index is added below -- ``PRAGMA
# user_version`` is set to this after every successful migration, purely as a readable marker for
# operators inspecting the file; the actual repair logic is presence-based (see
# ``_migrate_schema``) so it is correct even against a database several fix-rounds old. v3 (fourth
# fix round) added the ``recorder_state`` and ``meta`` tables. v4 (fifth fix round, m-G) added
# ``gaps.rewritten``. v5 (sixth fix round, MINOR-2) added ``idx_gaps_hour_close_ms`` and
# ``idx_gaps_reason_rewritten``.
_SCHEMA_VERSION = 5


class RecorderSchemaVersionError(RuntimeError):
    """The database's ``PRAGMA user_version`` is newer than this code's ``_SCHEMA_VERSION``.

    MINOR-6 (third fix round): without this check, opening a database a *newer* recorder version
    already migrated forward with *older* code silently "downgraded" it -- the older code's
    ``_SCHEMA_SQL``/``_migrate_schema`` know nothing about whatever table or column the newer
    version added, so every read/write would simply act as if that addition did not exist, with no
    error at all. Fail loudly instead: an operator rolling back the recorder binary needs to know
    the on-disk file is now ahead of the code, not discover it from silently missing columns.
    """


class RecorderDatabaseError(RuntimeError):
    """Wraps a ``sqlite3.DatabaseError`` raised while opening or migrating the recorder's SQLite
    file, naming the path.

    MINOR-7 (third fix round): a corrupt database (e.g. ``sqlite3.DatabaseError: file is not a
    database``) is right to fail fast rather than be silently papered over, but a *bare*
    ``sqlite3.DatabaseError`` propagating into the container's restart loop gives an operator no
    indication of *which* file is corrupt out of however many are mounted. This names it.
    """


class RecorderSymbolMismatchError(RuntimeError):
    """m6 (fourth fix round): raised by ``RecorderStore.ensure_symbol`` when a database already
    carries a ``meta`` row for a different symbol than the one this process is about to record.

    Without this, pointing the recorder at the wrong database file (a config/CLI mistake) would
    silently interleave two symbols' trades under one id space and one sweep cursor, corrupting
    both -- there is no way to separate them after the fact.
    """


class RecorderFatalError(RuntimeError):
    """m-E (fifth fix round): raised by ``TabdealRecorderService.run_forever`` after
    ``_MAX_CONSECUTIVE_CYCLE_EXCEPTIONS`` consecutive ``process_once()`` exceptions.

    Without this, a permanently broken process (a bug, not an ordinary poll failure -- those are
    already handled gracefully inside ``process_once`` without raising) looped at the backed-off
    poll interval forever, since nothing ever let the exception escape ``run_forever``'s own
    ``except Exception`` clause. Letting it propagate out gives the process a non-zero exit code,
    so Docker's ``restart: unless-stopped`` policy (``deploy/docker-compose.yml``) can actually
    restart it rather than leaving it running dead.
    """


def _column_specs(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """``{column_name: column_definition}`` for ``table``, via ``PRAGMA table_info`` -- empty if
    the table does not exist in ``conn``.

    m-G (fifth fix round): the definition includes ``NOT NULL``/``DEFAULT`` when ``PRAGMA
    table_info`` reports them, not just the bare type -- a migration-added column (e.g.
    ``gaps.rewritten INTEGER NOT NULL DEFAULT 0``, schema v4) must carry the same constraint a
    fresh ``CREATE TABLE`` would give it, not a silently weaker nullable column with no default.
    Harmless for every pre-existing migrated column, none of which declare ``NOT NULL``/
    ``DEFAULT`` in ``_SCHEMA_SQL`` in the first place.
    """
    specs: dict[str, str] = {}
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        _cid, name, col_type, notnull, dflt_value, _pk = row
        parts = [col_type]
        if notnull:
            parts.append("NOT NULL")
        if dflt_value is not None:
            parts.append(f"DEFAULT {dflt_value}")
        specs[name] = " ".join(parts)
    return specs


def _reference_schema_columns() -> dict[str, dict[str, str]]:
    """``{table_name: {column_name: declared_type}}`` for every table ``_SCHEMA_SQL`` declares.

    MINOR-5 (third fix round): previously this was a hand-maintained ``_EXPECTED_COLUMNS`` dict
    that had to be kept in sync by hand with every column added to ``_SCHEMA_SQL`` above -- the
    exact MAJOR-2 crash loop (a column the code expects missing from an on-disk table) reappears
    the moment someone adds a column to the schema and forgets to mirror it into that dict. Instead,
    execute the real schema into a throwaway in-memory database and read back what it actually
    created: there is now exactly one place column names and types are written down.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SCHEMA_SQL)
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
        return {table: _column_specs(conn, table) for table in tables}
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Create any missing table from scratch (new database) and add any column an existing table
    is missing (old database opened by newer code) -- see MAJOR-2 in the module docstring.

    Column additions use whatever ``NOT NULL``/``DEFAULT`` the reference schema declares (see
    ``_column_specs``): a plain nullable addition means existing rows get ``NULL`` -- the "unknown
    for old rows" meaning those columns are supposed to carry -- while a ``NOT NULL DEFAULT x``
    addition (e.g. ``gaps.rewritten``, schema v4) backfills existing rows with ``x`` instead, since
    SQLite requires a non-null default for a ``NOT NULL`` column added via ``ALTER TABLE``. Correct
    even against a database only *some* of whose tables were previously migrated (a
    partially-migrated database): each table's missing columns are computed independently against
    the reference schema, not against a single global flag.
    """
    conn.executescript(_SCHEMA_SQL)
    for table, expected_columns in _reference_schema_columns().items():
        existing = _column_specs(conn, table)
        for name, column_type in expected_columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
    # MINOR-2 (sixth fix round): must run after the column-backfill loop above -- see
    # _GAPS_INDEX_SQL's own comment for why creating these any earlier can fail against a
    # database that predates the columns they index.
    conn.executescript(_GAPS_INDEX_SQL)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


# ---------------------------------------------------------------------------------
# pure parsing helpers
# ---------------------------------------------------------------------------------


def _parse_is_buyer_maker(raw: Any) -> bool:
    """Strictly parse ``isBuyerMaker``: a real ``bool`` or a ``"true"``/``"false"`` string
    (case-insensitive); anything else (missing, other shapes) defaults to ``False``.

    NIT (fourth fix round): ``bool(item.get("isBuyerMaker", False))`` turns the *string*
    ``"false"`` into ``True`` (any non-empty string is truthy) -- a real bug if Tabdeal, or a
    future response shape, ever sends this field as text rather than a JSON boolean.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if raw.lower() == "true":
            return True
        if raw.lower() == "false":
            return False
    return False


def parse_trade_item(item: Any, *, now_ms: int) -> candles_mod.Trade | None:
    """Parse one ``/trades`` response item. Returns ``None`` (never raises) on anything
    malformed -- a single bad row must not abort the whole poll.

    ``price``/``qty`` accept ``str`` (Tabdeal's documented shape) or an already-exact ``Decimal``
    (decision D-031: ``TabdealClient`` decodes with ``json.loads(..., parse_float=Decimal)``, so an
    unquoted JSON number arrives here as ``Decimal``, not ``float``). A bare ``float`` is rejected
    outright: ``str()`` on a float that round-tripped through binary floating point is not the
    exchange's exact decimal literal, and there is no way to recover it from here.

    ``now_ms`` (MAJOR-A, fifth fix round -- supersedes m2's 24h age window): ``ts_ms`` is rejected
    only when it is implausible as an epoch-millisecond value -- below ``_MIN_PLAUSIBLE_TRADE_TS_MS``
    (almost certainly seconds-scale, or a literal ``0``/unit bug) or more than
    ``_MAX_FUTURE_SKEW_MS`` beyond ``now_ms`` (clock skew, or a microsecond-scale value, which
    overshoots by a factor of ~1000 and is caught by the same bound). A trade that is merely old
    -- even far beyond 24h, e.g. the tail of a normal ~29h/1000-trade window -- is valid and must
    be inserted: dedupe makes re-insertion harmless, and the old age-based rejection used to
    manufacture a ``partial_unparsable`` gap (and an incomplete candle) on nearly every poll.

    MINOR-3 (PR2 followups): ``int(x)`` on a non-finite ``float`` (``inf``/``nan``, however it got
    into a JSON-decoded response -- not standard JSON, but seen from real-world responses/proxies)
    raises ``OverflowError``/``ValueError`` that used to propagate out of this function entirely,
    aborting the whole poll's parse loop (``_parse_items_with_neighbors``) -- one bad item then
    blocked every other trade in the same 1000-trade response from ever being inserted, cycle
    after cycle, until that one item fell out of the feed's ~29h window (observed: ~27 of 30
    cycles raised). ``OverflowError`` is now caught like any other malformed-item error, and
    ``trade_id`` is additionally bounds-checked against ``_SQLITE_INT64_MAX`` -- SQLite's
    ``INTEGER PRIMARY KEY`` is signed 64-bit, and an id at or above ``2**63`` used to reach
    ``RecorderStore.insert_trades`` and raise there instead, inside the poll's own transaction,
    with the exact same whole-poll-blocking effect.
    """
    if not isinstance(item, dict):
        return None
    try:
        trade_id = int(item["id"])
        ts_ms = int(item["time"])
        raw_price = item["price"]
        raw_qty = item["qty"]
        if isinstance(raw_price, float) or isinstance(raw_qty, float):
            return None
        price = to_decimal(raw_price)
        qty = to_decimal(raw_qty)
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None
    if not (0 <= trade_id <= _SQLITE_INT64_MAX):
        return None
    if ts_ms < _MIN_PLAUSIBLE_TRADE_TS_MS or ts_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        return None
    # MINOR-4: a quoted "NaN"/"Infinity" price parses to a valid-but-meaningless Decimal that
    # would wedge candles.build_candle's max()/min() forever once persisted (see its own guard).
    if not price.is_finite() or price <= 0 or not qty.is_finite() or qty < 0:
        return None
    is_buyer_maker = _parse_is_buyer_maker(item.get("isBuyerMaker", False))
    return candles_mod.Trade(
        trade_id=trade_id, ts_ms=ts_ms, price=price, qty=qty, is_buyer_maker=is_buyer_maker
    )


# ---------------------------------------------------------------------------------
# SQLite store (docs/SPEC.md section 5.4 schema + the D-019 orderbook table)
# ---------------------------------------------------------------------------------


class RecorderStore:
    """Thin SQLite wrapper around the ``trades`` / ``poll_log`` / ``gaps`` / ``orderbook`` tables."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            on_disk_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if on_disk_version > _SCHEMA_VERSION:
                raise RecorderSchemaVersionError(
                    f"{db_path}: on-disk schema version {on_disk_version} is newer than this "
                    f"code's _SCHEMA_VERSION={_SCHEMA_VERSION} -- refusing to open it with older "
                    "code, which would silently downgrade it (MINOR-6). Upgrade the recorder "
                    "before opening this database."
                )
            _migrate_schema(conn)
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise RecorderDatabaseError(f"{db_path}: {exc}") from exc
        except Exception:
            conn.close()
            raise
        self._conn = conn
        # NIT (sixth fix round): set only by transaction() itself, never derived from sqlite3's
        # own `in_transaction` -- see that method for why the distinction matters.
        self._in_transaction_block = False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RecorderStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- atomicity (MAJOR-3, fourth fix round) -------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Wrap a block of writes in one atomic SQLite transaction (``BEGIN IMMEDIATE`` ...
        ``COMMIT``, or ``ROLLBACK`` on any exception).

        The connection is opened with ``isolation_level=None`` (autocommit), so without this,
        each ``execute``/``executemany`` call committed independently -- a process killed between
        e.g. ``insert_trades`` and ``record_gap`` permanently lost the gap row even though the
        trades it describes were already durable, and there was no way to tell afterwards that
        anything was missing.

        m-E (fifth fix round): two additional failure modes, both of which used to wedge the
        connection inside an open transaction forever -- every later call to ``transaction()``
        then raised ``sqlite3.OperationalError: cannot start a transaction within a transaction``,
        permanently, needing a process restart to clear:

        * a dangling transaction already open when this method is entered (e.g. left behind by
          one of the two cases below, or any other code path that began one and never finished
          it) is rolled back -- and logged, so it is visible that this happened -- before
          ``BEGIN`` is issued.
        * a ``COMMIT`` that itself raises (disk full, I/O error, ...) is followed by a ``ROLLBACK``
          when the connection is still inside a transaction, instead of leaving it there.

        NIT (sixth fix round): a *nested* call -- ``transaction()`` invoked again from inside an
        already-open ``with store.transaction():`` block, e.g. by a bug that calls a
        transaction-wrapped method from inside another one -- used to be indistinguishable from a
        dangling leftover transaction from a prior crash (both show ``self._conn.in_transaction ==
        True``), so the inner call's own dangling-transaction recovery above would silently
        ``ROLLBACK`` the outer, legitimate, in-progress transaction's work with no error raised at
        all. ``self._in_transaction_block`` is set only by this method (never derived from
        sqlite3's own transaction state, which cannot tell the two cases apart) and makes a nested
        call raise ``RuntimeError`` immediately instead -- the outer transaction then rolls back
        too, correctly, because an exception now propagates through its own body, not because
        anything here guessed it was stale.
        """
        if self._in_transaction_block:
            raise RuntimeError(
                "RecorderStore.transaction() does not support nesting -- a transaction is "
                "already open on this connection"
            )
        if self._conn.in_transaction:
            logger.warning("tabdeal_recorder.dangling_transaction_rolled_back")
            self._conn.execute("ROLLBACK")
        self._in_transaction_block = True
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                try:
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
        finally:
            self._in_transaction_block = False

    # -- symbol identity (m6, fourth fix round) ------------------------------------

    def ensure_symbol(self, symbol: str) -> None:
        """Record ``symbol`` in the ``meta`` table on first use; raise
        ``RecorderSymbolMismatchError`` if this database was already recorded for a different one.

        Called once, at service construction -- not on every poll -- since the symbol a database
        was opened for cannot change during a run.
        """
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'symbol'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta (key, value) VALUES ('symbol', ?)", (symbol,))
            return
        recorded_symbol = row[0]
        if recorded_symbol != symbol:
            raise RecorderSymbolMismatchError(
                f"this database was recorded for symbol {recorded_symbol!r}, refusing to run it "
                f"for {symbol!r} -- point the recorder at the right --db-path/config, or a fresh "
                "database for this symbol"
            )

    # -- trades -------------------------------------------------------------------

    def insert_trades(self, trades: Sequence[candles_mod.Trade], *, recorded_ts_ms: int) -> int:
        """Dedupe-insert trades (``INSERT OR IGNORE`` on the ``trade_id`` primary key). Returns
        the number of genuinely new rows -- duplicates (within this batch or already stored)
        count for zero."""
        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO trades (trade_id, ts_ms, price, qty, is_buyer_maker, recorded_ts_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (t.trade_id, t.ts_ms, str(t.price), str(t.qty), int(t.is_buyer_maker), recorded_ts_ms)
                for t in trades
            ],
        )
        return self._conn.total_changes - before

    def max_trade_id(self) -> int | None:
        row = self._conn.execute("SELECT MAX(trade_id) FROM trades").fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def total_trade_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return int(row[0]) if row is not None else 0

    def earliest_trade_ts_ms(self) -> int | None:
        row = self._conn.execute("SELECT MIN(ts_ms) FROM trades").fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def trade_ts_ms(self, trade_id: int) -> int | None:
        row = self._conn.execute("SELECT ts_ms FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        return row[0] if row is not None else None

    def has_trade_after(self, ts_ms: int) -> bool:
        """``True`` iff any stored trade has ``ts_ms`` strictly greater than the given value --
        m-C (fifth fix round): direct proof the feed has moved past a given hour's close, used by
        ``build_due_candles`` to gate whether that hour is actually due. Uses the existing
        ``idx_trades_ts_ms`` index."""
        row = self._conn.execute("SELECT 1 FROM trades WHERE ts_ms > ? LIMIT 1", (ts_ms,)).fetchone()
        return row is not None

    def last_recorded_ts_ms(self) -> int | None:
        """``MAX(recorded_ts_ms)`` across all trades -- the wall-clock time a genuinely new trade
        (not a duplicate) was last inserted.

        m3 (fourth fix round): this is the heartbeat's ``last_new_trade_ts_ms``. Unlike
        ``last_poll_ts``/``consecutive_errors`` (which only say the HTTP round-trip is healthy),
        this says the exchange's own trade feed is still producing data we have not already seen
        -- derived straight from ``trades`` so it survives a process restart with no extra state.
        """
        row = self._conn.execute("SELECT MAX(recorded_ts_ms) FROM trades").fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def late_trade_ts_ms(
        self, *, trade_ids: Sequence[int], recorded_ts_ms: int, max_ts_ms: int
    ) -> list[int]:
        """``ts_ms`` for every one of ``trade_ids`` (this poll's own parsed trades -- the caller
        already knows exactly which ids that is) that this exact poll newly inserted
        (``recorded_ts_ms`` set by ``insert_trades`` at insert time, never touched again by a
        later duplicate ``INSERT OR IGNORE``) and whose own timestamp is at or before
        ``max_ts_ms``.

        MINOR-1 (sixth fix round): used by ``_detect_late_trades`` to find trades that arrived
        for an hour the sweep has already passed (``max_ts_ms`` is the SQLite-only resumption
        cursor, see ``RecorderStore.last_swept_close_ms``) -- i.e. late arrivals that a prior seal
        (complete=True, or an empty/poisoned-hour gap) did not have yet.

        NIT (PR2 review): previously filtered by ``recorded_ts_ms = ?`` alone, which has no index
        and scans the whole ``trades`` table on every single poll. ``trade_id`` is the primary
        key, so narrowing by ``trade_id IN (...)`` first (this poll's own ids, at most
        ``trades_limit``) makes this an indexed, O(poll size) lookup instead of O(table size);
        the ``recorded_ts_ms``/``max_ts_ms`` filters are kept exactly as before so the result is
        unchanged, just reached faster.
        """
        if not trade_ids:
            return []
        placeholders = ",".join("?" * len(trade_ids))
        rows = self._conn.execute(
            f"SELECT ts_ms FROM trades WHERE trade_id IN ({placeholders}) "
            "AND recorded_ts_ms = ? AND ts_ms <= ?",
            (*trade_ids, recorded_ts_ms, max_ts_ms),
        ).fetchall()
        return [r[0] for r in rows]

    def trades_in_range(self, open_ms: int, close_ms: int) -> list[candles_mod.Trade]:
        """Trades with ``open_ms < ts_ms <= close_ms`` (``closed='right'``)."""
        rows = self._conn.execute(
            "SELECT trade_id, ts_ms, price, qty, is_buyer_maker FROM trades WHERE ts_ms > ? AND ts_ms <= ?",
            (open_ms, close_ms),
        ).fetchall()
        return [
            candles_mod.Trade(
                trade_id=r[0], ts_ms=r[1], price=Decimal(r[2]), qty=Decimal(r[3]), is_buyer_maker=bool(r[4])
            )
            for r in rows
        ]

    # -- poll log / gaps ------------------------------------------------------------

    def record_poll(
        self,
        *,
        poll_ts_ms: int,
        first_id: int | None,
        last_id: int | None,
        n_trades: int,
        saturated: bool,
        http_status: int | None,
        latency_ms: float | None,
        window_span_seconds: float | None,
        coverage_ratio: float | None,
        n_items_received: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO poll_log "
            "(poll_ts_ms, first_id, last_id, n_trades, saturated, http_status, latency_ms, "
            "window_span_seconds, coverage_ratio, n_items_received) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                poll_ts_ms,
                first_id,
                last_id,
                n_trades,
                int(saturated),
                http_status,
                latency_ms,
                window_span_seconds,
                coverage_ratio,
                n_items_received,
            ),
        )

    def first_poll_ts_ms(self) -> int | None:
        """``MIN(poll_ts_ms)`` across ``poll_log`` -- the wall-clock time of this recorder's very
        first poll attempt ever (successful or not), persisted across restarts since it is
        derived from the table itself, not any in-memory state.

        NIT (fifth fix round): this is the heartbeat's ``first_poll_ts_ms``, which
        ``deploy/healthcheck.py`` uses to tell a recorder that has *never* recorded a single trade
        apart from one that simply started recently -- without it, a recorder whose every poll
        returns ``[]`` (symbol mismatch, endpoint regression) looks healthy forever, since
        ``last_poll_ts`` alone keeps being rewritten on every successful poll.
        """
        row = self._conn.execute("SELECT MIN(poll_ts_ms) FROM poll_log").fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def record_gap(
        self,
        *,
        detected_ts_ms: int,
        from_id: int | None,
        to_id: int | None,
        reason: str,
        hour_close_ms: int | None = None,
    ) -> int:
        """Insert one ``gaps`` row (``rewritten`` defaults to 0) and return its ``rowid``.

        m-G (fifth fix round): the rowid lets a caller that just recorded a ``window_no_overlap``
        gap -- and immediately rewrote any already-written candle it touches -- mark that exact
        row ``rewritten=1`` afterwards (``RecorderStore.mark_gap_rewritten``), so
        ``_recover_unrewritten_gaps`` does not needlessly reprocess it on every later sweep.
        """
        cursor = self._conn.execute(
            "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason, hour_close_ms) VALUES (?, ?, ?, ?, ?)",
            (detected_ts_ms, from_id, to_id, reason, hour_close_ms),
        )
        rowid = cursor.lastrowid
        assert rowid is not None  # always set after a successful single-row INSERT
        return rowid

    def record_gap_once(
        self, *, detected_ts_ms: int, reason: str, hour_close_ms: int
    ) -> int | None:
        """Like ``record_gap``, but a no-op (returns ``None``, nothing inserted) if a row with
        this exact ``(reason, hour_close_ms)`` already exists.

        MINOR-2 (sixth fix round): used by ``_record_partial_unparsable`` so re-discovering the
        same affected hour on a later poll (e.g. a feed that keeps dropping the same malformed
        item) does not pile up a duplicate row every single poll -- ``INSERT ... SELECT ... WHERE
        NOT EXISTS`` makes the check-then-insert atomic within the caller's own transaction
        (no separate SELECT-then-INSERT race is possible, since SQLite transactions are already
        serialized on one connection).
        """
        cursor = self._conn.execute(
            "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason, hour_close_ms) "
            "SELECT ?, NULL, NULL, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM gaps WHERE reason = ? AND hour_close_ms = ?)",
            (detected_ts_ms, reason, hour_close_ms, reason, hour_close_ms),
        )
        if cursor.rowcount == 0:
            return None
        rowid = cursor.lastrowid
        assert rowid is not None
        return rowid

    def unrewritten_overlap_gaps(self) -> list[tuple[int, int, int]]:
        """``(rowid, from_id, to_id)`` for every ``window_no_overlap`` gap row not yet confirmed
        rewritten onto any already-written candle it touches -- m-G (fifth fix round), consumed by
        ``tbot.data.tabdeal_recorder._recover_unrewritten_gaps``."""
        rows = self._conn.execute(
            "SELECT rowid, from_id, to_id FROM gaps "
            "WHERE reason = 'window_no_overlap' AND rewritten = 0 "
            "AND from_id IS NOT NULL AND to_id IS NOT NULL"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def unrewritten_hour_gaps(self, reason: str) -> list[tuple[int, int]]:
        """``(rowid, hour_close_ms)`` for every ``reason`` gap row not yet confirmed rewritten --
        generalizes ``unrewritten_overlap_gaps`` (keyed by an id range) to any gap reason keyed by
        ``hour_close_ms`` instead.

        MINOR-1 (sixth fix round): consumed by ``_recover_unrewritten_gaps`` for the
        ``late_trades`` reason, the same self-healing role ``unrewritten_overlap_gaps`` plays for
        ``window_no_overlap``.
        """
        rows = self._conn.execute(
            "SELECT rowid, hour_close_ms FROM gaps "
            "WHERE reason = ? AND rewritten = 0 AND hour_close_ms IS NOT NULL",
            (reason,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def mark_gap_rewritten(self, rowid: int) -> None:
        """m-G (fifth fix round): mark one ``gaps`` row (by ``rowid``, from ``record_gap``'s
        return value or ``unrewritten_overlap_gaps``) as confirmed-rewritten."""
        self._conn.execute("UPDATE gaps SET rewritten = 1 WHERE rowid = ?", (rowid,))

    def record_hour_gap(self, *, symbol: str, close_ms: int, detected_ts_ms: int, reason: str) -> None:
        """Record a ``gaps`` row for the bucket ending at ``close_ms`` AND advance the per-symbol
        sweep cursor past it (never backwards) -- for any hour the sweep decides to skip entirely
        rather than write a candle for, whatever the reason.

        MAJOR M3 (fix round): without the cursor, ``build_due_candles`` re-discovers the same
        trailing skipped hour as "due" on every single poll forever -- the only thing that used to
        advance the resumption point was writing an actual candle, which a skipped hour never does.
        Measured (for the original ``no_trades_in_hour`` case): 3 empty hours x 5 polls = 15
        duplicate gap rows and 15 duplicate warnings at a 5s poll interval, i.e. ~720 duplicate rows
        per empty hour per hour. The cursor fixes this without touching the ``gaps`` table's
        documented shape (docs/SPEC.md section 5.4).

        MINOR-8 (second fix round): the gap row also carries ``hour_close_ms=close_ms`` -- without
        it, several skipped hours swept in one pass produced identical
        ``(NULL, NULL, reason)`` rows and the ``gaps`` table alone could not answer G1b's "which
        hours were missing".

        Third fix round: generalised from ``record_empty_hour_gap`` (which still exists, calling
        this with ``reason="no_trades_in_hour"``) to also cover an hour where every trade was
        filtered out by ``candles.build_candle``'s poisoned-row guard (``reason="poisoned_trades"``)
        -- that case has exactly the same "never re-walked, never crashes" requirement an empty
        hour does, and reusing the same cursor mechanism is what gives it that for free.

        MAJOR-3 (fourth fix round): the gap row and the cursor advance are now one atomic
        transaction -- previously a crash between the two could leave the gap unrecorded while the
        cursor had already moved past that hour, silently losing the only trace of it forever.
        """
        with self.transaction():
            self.record_gap(
                detected_ts_ms=detected_ts_ms,
                from_id=None,
                to_id=None,
                reason=reason,
                hour_close_ms=close_ms,
            )
            self.advance_sweep_cursor(symbol=symbol, close_ms=close_ms)

    def advance_sweep_cursor(self, *, symbol: str, close_ms: int) -> None:
        """Advance (never regress) the per-symbol ``sweep_cursor`` to ``close_ms``, with no gap
        row -- the bare cursor-advance half of ``record_hour_gap``, factored out so a hour that
        gets a real candle (not a gap) can also advance it.

        M-1 (PR2 review): ``_process_due_hour`` calls this right after a successful
        ``write_candle`` so the SQLite ``sweep_cursor`` tracks every hour the sweep has processed
        -- candle-written or gapped -- without ever touching Parquet. Before this, the only
        SQLite-side resumption signal was ``record_hour_gap`` (gapped hours only); a
        candle-written hour's progress lived exclusively in the Parquet file itself
        (``candles.last_written_close_ms``), which is exactly what made
        ``_detect_late_trades`` -- on the trade-ingestion path, inside the poll's own
        transaction -- need a Parquet read to find it. Also used by
        ``tabdeal_recorder._reconcile_sweep_cursor`` (called at the start of every sweep, outside
        any poll transaction) to catch the cursor up to whatever is already on disk, e.g. after a
        crash between ``write_candle`` and this call, or a database migrated from before this
        fix existed.
        """
        self._conn.execute(
            "INSERT INTO sweep_cursor (symbol, last_swept_close_ms) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "last_swept_close_ms = MAX(last_swept_close_ms, excluded.last_swept_close_ms)",
            (symbol, close_ms),
        )

    def record_empty_hour_gap(self, *, symbol: str, close_ms: int, detected_ts_ms: int) -> None:
        """Record a ``no_trades_in_hour`` gap for the bucket ending at ``close_ms`` AND advance the
        per-symbol sweep cursor past it. Thin wrapper over ``record_hour_gap`` kept for its
        existing, more specific name and call sites -- see ``record_hour_gap`` for the mechanism.
        """
        self.record_hour_gap(
            symbol=symbol, close_ms=close_ms, detected_ts_ms=detected_ts_ms, reason="no_trades_in_hour"
        )

    def record_verified_poll(self, *, symbol: str, poll_ts_ms: int) -> None:
        """Advance (never regress) ``recorder_state.last_verified_poll_ts_ms`` for ``symbol``.

        MAJOR-1' (fourth fix round): called by ``poll_trades_once`` only when a poll's outcome is
        trustworthy (``PollOutcome.ok``) -- i.e. it genuinely reached the exchange and either
        proved window overlap with what is already stored or recorded a gap. ``build_due_candles``
        caps the sweep's notion of "now" at this value so an hour is never swept while the polls
        covering it were failing (see that function's docstring).
        """
        self._conn.execute(
            "INSERT INTO recorder_state (symbol, last_verified_poll_ts_ms) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "last_verified_poll_ts_ms = MAX(last_verified_poll_ts_ms, excluded.last_verified_poll_ts_ms)",
            (symbol, poll_ts_ms),
        )

    def verified_poll_ts_ms(self, symbol: str) -> int | None:
        """The most recent ``poll_ts_ms`` recorded via ``record_verified_poll`` for ``symbol``, or
        ``None`` if no poll has ever been verified (persists across restarts)."""
        row = self._conn.execute(
            "SELECT last_verified_poll_ts_ms FROM recorder_state WHERE symbol = ?", (symbol,)
        ).fetchone()
        return int(row[0]) if row is not None else None

    def last_swept_close_ms(self, symbol: str) -> int | None:
        """The close time (epoch ms) of the most recent hour recorded as empty for ``symbol``,
        or ``None`` if none has been swept yet. Combined with
        ``candles.last_written_close_ms`` (the max of the two) this is ``build_due_candles``'s
        full resumption cursor -- a hand-written candle alone is not enough once trailing empty
        hours can also advance the sweep."""
        row = self._conn.execute(
            "SELECT last_swept_close_ms FROM sweep_cursor WHERE symbol = ?", (symbol,)
        ).fetchone()
        return int(row[0]) if row is not None else None

    def has_hour_gap(self, close_ms: int) -> bool:
        """True if any ``gaps`` row (regardless of ``reason``) was recorded with this exact
        ``hour_close_ms`` -- e.g. a ``partial_unparsable`` row (MAJOR M-B, third fix round), which
        has no trade-id range to overlap via ``gaps_overlapping`` since the dropped items may never
        have parsed an id at all. ``no_trades_in_hour`` and ``poisoned_trades`` rows also carry
        ``hour_close_ms``, but neither reaches ``is_candle_complete`` in the first place -- the
        sweep skips writing a candle for that hour entirely in both cases -- so there is no overlap
        in practice between any of these reasons and a hour that *did* get a candle written.

        NOTE for future reasons: this check is "any reason disqualifies" by design, and that is
        correct for every reason that exists today (all three above mean "this hour's candle is
        missing or untrustworthy"). If a future gap reason is ever added that is purely
        informational -- recorded against an hour that otherwise got a perfectly good candle -- it
        would need an explicit carve-out here (e.g. filtering by reason) rather than being lumped
        into this blanket check.
        """
        row = self._conn.execute("SELECT 1 FROM gaps WHERE hour_close_ms = ? LIMIT 1", (close_ms,)).fetchone()
        return row is not None

    def gaps_overlapping(self, open_ms: int, close_ms: int) -> bool:
        """True if any recorded id-discontinuity gap's surrounding known-trade timestamps
        intersect the half-open window ``(open_ms, close_ms]``."""
        rows = self._conn.execute(
            "SELECT from_id, to_id FROM gaps WHERE from_id IS NOT NULL AND to_id IS NOT NULL"
        ).fetchall()
        for from_id, to_id in rows:
            from_ts = self.trade_ts_ms(from_id)
            to_ts = self.trade_ts_ms(to_id)
            if from_ts is None or to_ts is None:
                continue
            lo, hi = (from_ts, to_ts) if from_ts <= to_ts else (to_ts, from_ts)
            if hi > open_ms and lo <= close_ms:
                return True
        return False

    # -- orderbook --------------------------------------------------------------

    def record_orderbook(self, row: OrderbookRow) -> None:
        self._conn.execute(
            "INSERT INTO orderbook "
            "(ts_ms, best_bid, best_ask, spread_bps, depth_bid_01pct, depth_ask_01pct, "
            "depth_bid_05pct, depth_ask_05pct, depth_bid_1pct, depth_ask_1pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.ts_ms,
                str(row.best_bid) if row.best_bid is not None else None,
                str(row.best_ask) if row.best_ask is not None else None,
                row.spread_bps,
                str(row.depth_bid_01pct),
                str(row.depth_ask_01pct),
                str(row.depth_bid_05pct),
                str(row.depth_ask_05pct),
                str(row.depth_bid_1pct),
                str(row.depth_ask_1pct),
            ),
        )

    def orderbook_row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()
        return int(row[0]) if row is not None else 0

    # -- read-back helpers (operational debugging + tests) ------------------------

    def all_gaps(self) -> list[tuple[int | None, int | None, str, int | None]]:
        """``(from_id, to_id, reason, hour_close_ms)`` per row. ``hour_close_ms`` is set on
        ``no_trades_in_hour``/``poisoned_trades``/``partial_unparsable`` rows (MINOR-8); it is
        ``NULL`` on ``window_no_overlap`` rows, which identify themselves via ``from_id``/``to_id``
        instead (the last previously-stored id and the new window's lowest id, respectively)."""
        rows = self._conn.execute(
            "SELECT from_id, to_id, reason, hour_close_ms FROM gaps ORDER BY detected_ts_ms"
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def all_poll_log(
        self,
    ) -> list[
        tuple[
            int,
            int | None,
            int | None,
            int,
            bool,
            int | None,
            float | None,
            float | None,
            float | None,
            int | None,
        ]
    ]:
        """Columns, in order: ``poll_ts_ms, first_id, last_id, n_trades, saturated, http_status,
        latency_ms, window_span_seconds, coverage_ratio, n_items_received``. ``n_items_received``
        (MAJOR-1) is what the exchange actually returned before parsing -- comparing it against
        ``n_trades`` is how an operator tells "exchange returned nothing" apart from "we dropped
        everything"."""
        rows = self._conn.execute(
            "SELECT poll_ts_ms, first_id, last_id, n_trades, saturated, http_status, latency_ms, "
            "window_span_seconds, coverage_ratio, n_items_received FROM poll_log ORDER BY poll_ts_ms"
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8], r[9]) for r in rows]

    def all_orderbook_rows(self) -> list[tuple[Any, ...]]:
        rows = self._conn.execute(
            "SELECT ts_ms, best_bid, best_ask, spread_bps, depth_bid_01pct, depth_ask_01pct, "
            "depth_bid_05pct, depth_ask_05pct, depth_bid_1pct, depth_ask_1pct FROM orderbook ORDER BY ts_ms"
        ).fetchall()
        return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------------
# trades polling
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """Outcome of one ``/trades`` poll, after persistence.

    Engineering rule (CLAUDE.md Sec.3, added following the PR2 review): raw trade ingestion must
    never depend on any downstream step. ``ok``/``inserted`` reflect ONLY the HTTP call, parsing,
    and the trade-insert transaction (``_persist_poll``) -- never a downstream, Parquet-touching
    correction (the post-commit window-no-overlap / late-trade / sealed-hour-partial-unparsable
    candle rewrites). A failure in one of those is captured separately, in ``degraded_error``.
    """

    ok: bool
    status_code: int | None
    latency_ms: float
    n_trades: int
    n_items_received: int
    first_id: int | None
    last_id: int | None
    saturated: bool
    window_span_seconds: float | None
    coverage_ratio: float | None
    inserted: int
    gap: tuple[int, int] | None
    error: str | None
    # Engineering rule (PR2 review): set (redacted) when a post-commit Parquet rewrite
    # (_handle_detected_gap / _apply_gap_rewrites) raised -- the trades themselves are already
    # safely committed by the time this can happen, so it never affects `ok`/`inserted` above.
    # `None` when no such correction was attempted this poll, or all of them succeeded.
    degraded_error: str | None = None


def _window_span_seconds(trades: Sequence[candles_mod.Trade]) -> float | None:
    """Span, in seconds, between the earliest and latest trade in one poll's response.

    ``None`` when fewer than two trades were returned -- there is no window to measure.
    """
    if len(trades) < 2:
        return None
    times = [t.ts_ms for t in trades]
    return (max(times) - min(times)) / 1000.0


@dataclass(frozen=True, slots=True)
class _ParsedResponse:
    """Everything derived from one ``/trades`` HTTP round-trip, before any store write."""

    result: ProbeResult
    trades: list[candles_mod.Trade]
    n_items_received: int
    unexpected_body_type: bool
    # MINOR-2 (sixth fix round): the raw response items that failed parse_trade_item, kept
    # separately from `trades` so _record_partial_unparsable can anchor each one's own gap to its
    # own (leniently-read) time instead of to whatever hours the trades that DID parse happen to
    # span.
    #
    # m-1 (PR2 review): each entry also carries the ts_ms of the nearest successfully-parsed
    # trade immediately before and after it in the response's own order (None at either end when
    # there is no such neighbour) -- see _parse_items_with_neighbors and
    # _record_partial_unparsable's own docstring for why this replaces trusting the dropped
    # item's own (possibly implausible, e.g. seconds-scale) "time" field at face value.
    dropped_items: list[tuple[Any, int | None, int | None]]
    # NIT (sixth fix round): sampled AFTER client.trades() returns (including any retries it took
    # internally) -- see that function's docstring for why sampling it beforehand was wrong.
    now_ms: int


def _fetch_and_parse(client: TabdealClient, symbol: str, limit: int, *, clock: Clock) -> _ParsedResponse:
    """Call ``/trades`` and parse its body. Never raises -- a malformed body or item degrades to
    an empty/partial ``trades`` list, which the caller turns into the right warnings/gap rows.

    NIT (sixth fix round): the future-skew plausibility check inside ``parse_trade_item`` used to
    receive a ``now_ms`` sampled *before* this call -- with ``TabdealClient``'s own retry/backoff
    budget (up to ~360s worst case for a slow/retried round-trip), a genuinely fresh trade could
    arrive stamped further in the future than that stale, pre-call ``now_ms`` plus the 5-minute
    skew tolerance, and get wrongly rejected. ``clock.now()`` is now read once the response has
    actually arrived, and that same ``now_ms`` is returned for the caller to reuse for every other
    purpose this poll needs "now" for (``recorded_ts_ms``, ``detected_ts_ms``, ``poll_ts_ms``) --
    one poll, one "now".
    """
    result = client.trades(symbol, limit=limit)
    now_ms = _dt_to_ms(clock.now())
    body_is_list = isinstance(result.body, list)
    unexpected_body_type = result.ok and not body_is_list
    n_items_received = len(result.body) if result.ok and body_is_list else 0
    trades: list[candles_mod.Trade] = []
    dropped_items: list[tuple[Any, int | None, int | None]] = []
    if result.ok and body_is_list:
        trades, dropped_items = _parse_items_with_neighbors(result.body, now_ms=now_ms)
    return _ParsedResponse(
        result=result,
        trades=trades,
        n_items_received=n_items_received,
        unexpected_body_type=unexpected_body_type,
        dropped_items=dropped_items,
        now_ms=now_ms,
    )


def _parse_items_with_neighbors(
    items: Sequence[Any], *, now_ms: int
) -> tuple[list[candles_mod.Trade], list[tuple[Any, int | None, int | None]]]:
    """Parse every ``/trades`` response item, in response order. Returns the trades that parsed
    and, for each item that did not, ``(item, prev_ts_ms, next_ts_ms)`` -- the ``ts_ms`` of the
    nearest successfully-parsed trade immediately before and after it in the response's own
    order (``None`` at either end when there is no such neighbour).

    m-1 (PR2 review): used by ``_record_partial_unparsable`` to anchor a dropped item's gap to
    the hour span between its real neighbours when its own ``time`` field is missing or
    implausible, instead of trusting that field at face value -- the round-3 reviewer's exact
    repro was a single item with a seconds-scale ``time`` (a unit bug), which used to anchor the
    gap to a bogus 1970-ish hour while the item's REAL hour (where a trade is genuinely missing)
    got no gap row at all and was later sealed ``complete=True``. Two linear passes (not the
    naive O(n^2) "scan backward/forward from each dropped index"): a forward pass tracks the
    most recently parsed ``ts_ms`` for ``prev_ts_ms``, a backward pass does the same for
    ``next_ts_ms``.
    """
    parsed = [parse_trade_item(item, now_ms=now_ms) for item in items]
    trades = [t for t in parsed if t is not None]
    n = len(parsed)
    prev_ts: list[int | None] = [None] * n
    last_ts: int | None = None
    for i in range(n):
        prev_ts[i] = last_ts
        trade = parsed[i]
        if trade is not None:
            last_ts = trade.ts_ms
    next_ts: list[int | None] = [None] * n
    upcoming_ts: int | None = None
    for i in range(n - 1, -1, -1):
        next_ts[i] = upcoming_ts
        trade = parsed[i]
        if trade is not None:
            upcoming_ts = trade.ts_ms
    dropped = [(items[i], prev_ts[i], next_ts[i]) for i in range(n) if parsed[i] is None]
    return trades, dropped


@dataclass(frozen=True, slots=True)
class _PollMetrics:
    """Everything ``poll_trades_once`` derives from a parsed response before touching the store."""

    n_trades: int
    first_id: int | None
    last_id: int | None
    saturated: bool
    window_span_seconds: float | None
    coverage_ratio: float | None
    ok: bool
    empty_after_data: bool
    gap: tuple[int, int] | None


def _derive_poll_metrics(
    parsed: _ParsedResponse, prev_max: int | None, limit: int, poll_interval_seconds: float
) -> _PollMetrics:
    trades = parsed.trades
    n_trades = len(trades)
    first_id = min((t.trade_id for t in trades), default=None)
    last_id = max((t.trade_id for t in trades), default=None)
    window_span_seconds = _window_span_seconds(trades)
    coverage_ratio = window_span_seconds / poll_interval_seconds if window_span_seconds is not None else None
    result = parsed.result
    # MAJOR-B (fifth fix round): an HTTP-200 empty list is only unremarkable on a cold start
    # (``prev_max is None`` -- nothing stored yet for ``[]`` to contradict). Once the database
    # already holds trades, a recent-trades endpoint legitimately returning ``[]`` is itself
    # suspicious (symbol mismatch, endpoint regression, a stale/cached response) -- it must not
    # count as a healthy, verified poll.
    empty_after_data = parsed.n_items_received == 0 and prev_max is not None
    ok = (
        result.ok
        and not parsed.unexpected_body_type
        and not (parsed.n_items_received > 0 and n_trades == 0)
        and not empty_after_data
    )
    return _PollMetrics(
        n_trades=n_trades,
        first_id=first_id,
        last_id=last_id,
        saturated=is_saturated(limit, n_trades),
        window_span_seconds=window_span_seconds,
        coverage_ratio=coverage_ratio,
        ok=ok,
        empty_after_data=empty_after_data,
        gap=_detect_window_gap(first_id, prev_max) if result.ok else None,
    )


def _detect_window_gap(first_id: int | None, prev_max: int | None) -> tuple[int, int] | None:
    """``(prev_max, first_id)`` iff this poll's lowest id does not reach back far enough to
    overlap what is already stored -- see the module docstring's note on global, non-contiguous
    trade ids (decision D-037). ``None`` on a cold start (``prev_max is None``): there
    is nothing yet to overlap with, and ``is_candle_complete``'s cold-start check covers that case
    separately.
    """
    if first_id is None or prev_max is None:
        return None
    if first_id > prev_max:
        return (prev_max, first_id)
    return None


def _lenient_item_time_ms(item: Any, *, now_ms: int) -> int | None:
    """Best-effort ``time`` field extraction for a response item that already failed
    ``parse_trade_item`` -- used only to anchor its ``partial_unparsable`` gap to roughly the
    right hour (MINOR-2, sixth fix round), never to validate or store the item itself. Returns
    ``None`` (never raises) for anything not even readable as an integer-ish value.

    m-1 (PR2 review): also returns ``None`` when the value fails the exact same plausibility
    bounds ``parse_trade_item`` itself uses (``_MIN_PLAUSIBLE_TRADE_TS_MS`` /
    ``_MAX_FUTURE_SKEW_MS``). Previously an implausible value (e.g. a seconds-scale unit bug --
    the round-3 reviewer's exact repro) was trusted at face value and anchored the gap to a bogus
    1970-ish hour -- nowhere near the item's real hour, which then got no gap row at all and was
    later swept ``complete=True`` with that trade silently missing. The caller
    (``_record_partial_unparsable``) falls back to anchoring by the item's parsed neighbours in
    response order when this returns ``None``.
    """
    if not isinstance(item, dict):
        return None
    try:
        time_ms = int(item["time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if time_ms < _MIN_PLAUSIBLE_TRADE_TS_MS or time_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        return None
    return time_ms


def _record_partial_unparsable(
    store: RecorderStore,
    dropped_items: Sequence[tuple[Any, int | None, int | None]],
    now_ms: int,
) -> list[tuple[int, int]]:
    """MAJOR M-B, amended MINOR-2 (sixth fix round), amended m-1/m-2 (PR2 review): mark each hour
    a dropped item actually belongs to as having a known shortfall, so the candle sweep builds it
    ``complete=False`` rather than treating a partial drop as full coverage. Returns ``(rowid,
    hour_close_ms)`` for every row this call newly inserted (not an already-existing dedup
    no-op) -- m-2 (PR2 review): ``_persist_poll`` uses this to find any newly-recorded
    ``partial_unparsable`` gap whose hour is at or before the sweep's own resumption cursor, i.e.
    an hour already sealed one way or another, so it goes through the same rewrite path
    ``late_trades`` already uses (``_rewrite_hour_for_late_trades``) instead of silently leaving
    that already-sealed candle wrong forever.

    MINOR-2: previously this anchored every affected hour to the hours of the trades that DID
    parse in the same poll -- on a poll whose response spans the full ~29h/1000-trade window,
    one unrelated bad item could mark roughly 29 consecutive hours incomplete, and re-record a
    gap for every one of them on every single poll. Each dropped item's own ``time`` field was
    read leniently (even though the rest of the item failed real parsing) and used as that item's
    own anchor.

    m-1 (PR2 review): that lenient read is now also checked for plausibility
    (``_lenient_item_time_ms``) -- an implausible own time (e.g. seconds-scale) no longer anchors
    the gap to a bogus hour. When the own time is missing or implausible, the anchor is instead
    the hour SPAN between the item's nearest successfully-parsed neighbours in response order
    (``prev_ts_ms``/``next_ts_ms``, from ``_parse_items_with_neighbors``) -- tightly bounding the
    affected hours to roughly where the bad item actually sits in the feed, rather than the whole
    poll's time range. Only an item with no readable time AND no parsed neighbour at all (every
    item in the response failed to parse) falls back to the hour containing the poll itself.
    ``record_gap_once`` dedupes by ``(reason, hour_close_ms)`` so rediscovering the same affected
    hour on a later poll is a no-op, not another row.
    """
    recorded: list[tuple[int, int]] = []
    for item, prev_ts_ms, next_ts_ms in dropped_items:
        item_time_ms = _lenient_item_time_ms(item, now_ms=now_ms)
        if item_time_ms is not None:
            close_candidates = [candles_mod.hour_bounds_ms(item_time_ms)[1]]
        else:
            neighbor_ts_ms = [ts for ts in (prev_ts_ms, next_ts_ms) if ts is not None]
            if neighbor_ts_ms:
                close_candidates = _gap_touched_hour_closes(min(neighbor_ts_ms), max(neighbor_ts_ms))
            else:
                close_candidates = [candles_mod.hour_bounds_ms(now_ms)[1]]
        for close_ms in close_candidates:
            rowid = store.record_gap_once(
                detected_ts_ms=now_ms, reason="partial_unparsable", hour_close_ms=close_ms
            )
            if rowid is not None:
                recorded.append((rowid, close_ms))
    return recorded


def _gap_touched_hour_closes(lo_ts_ms: int, hi_ts_ms: int) -> list[int]:
    """Every hour-bucket close time whose ``(open, close]`` window could contain a timestamp
    between ``lo_ts_ms`` and ``hi_ts_ms`` (order-independent), inclusive of both ends."""
    start_close = candles_mod.hour_bounds_ms(min(lo_ts_ms, hi_ts_ms))[1]
    end_close = candles_mod.hour_bounds_ms(max(lo_ts_ms, hi_ts_ms))[1]
    closes = []
    close_ms = start_close
    while close_ms <= end_close:
        closes.append(close_ms)
        close_ms += candles_mod.HOUR_MS
    return closes


def _rewrite_candle_incomplete_if_written(
    store: RecorderStore, parquet_root: Path, symbol: str, close_ms: int
) -> None:
    """If hour ``close_ms`` already has a candle on disk, rewrite it ``complete=False``.

    MAJOR-1' (fourth fix round): a gap discovered *after* an hour was already swept (its candle
    already written, possibly ``complete=True``) used to never be reflected back onto that
    candle -- the sweep cursor had moved on, and nothing ever revisits a hour once it has a
    candle. ``candles_mod.write_candle`` upserts by ``ts``, so this corrects the row in place
    rather than duplicating it. A no-op if the hour has not been swept yet (the normal sweep will
    build it correctly once it is due) or has no trades (defensive; should not happen since trades
    are never deleted).
    """
    last_written_ms = candles_mod.last_written_close_ms(parquet_root, symbol)
    if last_written_ms is None or close_ms > last_written_ms:
        return
    open_ms = close_ms - candles_mod.HOUR_MS
    trades = store.trades_in_range(open_ms, close_ms)
    record = candles_mod.build_candle(trades, open_ms, close_ms, complete=False)
    if record is not None:
        candles_mod.write_candle(parquet_root, symbol, record)
        logger.warning("tabdeal_recorder.candle_rewritten_incomplete", symbol=symbol, hour_close_ms=close_ms)


def _resumption_cursor_ms(store: RecorderStore, parquet_root: Path, symbol: str) -> int | None:
    """The hour-close cursor the sweep has already passed -- the max of "last candle actually
    written" and "last hour swept as an empty/poisoned/etc. gap" (MAJOR M3) -- or ``None`` if
    nothing has been swept at all yet.

    Used by ``build_due_candles`` (to know where to resume) and by ``_rewrite_hour_for_late_trades``
    / ``_rewrite_candle_incomplete_if_written`` (both post-commit, outside any poll transaction).

    M-1 (PR2 review): this reads Parquet (``candles.last_written_close_ms``) and must therefore
    never be called from the trade-ingestion path (``_persist_poll``'s transaction) -- that is
    exactly the incident this fix round closes. ``_detect_late_trades`` no longer uses this
    function; it takes a plain ``cursor_ms`` from ``RecorderStore.last_swept_close_ms`` (SQLite
    only) instead. See the module docstring's seventh fix round and
    ``RecorderStore.advance_sweep_cursor``.
    """
    last_written_ms = candles_mod.last_written_close_ms(parquet_root, symbol)
    last_swept_ms = store.last_swept_close_ms(symbol)
    candidates = [v for v in (last_written_ms, last_swept_ms) if v is not None]
    return max(candidates) if candidates else None


def _rewrite_hour_for_late_trades(
    store: RecorderStore, parquet_root: Path, symbol: str, close_ms: int
) -> None:
    """Correct hour ``close_ms`` after a trade arrives for it *after* that hour was already swept
    past (MINOR-1, sixth fix round) -- either because its candle is already written (rewrite it
    ``complete=False`` with the now-fuller trade set, delegating to
    ``_rewrite_candle_incomplete_if_written``) or because it was swept as an empty/poisoned gap
    with no candle at all (the sweep cursor already passed it via ``record_hour_gap`` -- write a
    fresh ``complete=False`` candle for it now that it demonstrably has trades, rather than leaving
    the hour candle-less forever with a now-provably-wrong ``no_trades_in_hour``/``poisoned_trades``
    gap).

    A no-op if the hour has not been swept at all yet -- the ordinary sweep will build it
    correctly (``complete=False``, via ``is_candle_complete``'s ``has_hour_gap`` check, since the
    ``late_trades`` gap row this function's caller already recorded is itself enough) once it
    becomes due.
    """
    last_written_ms = candles_mod.last_written_close_ms(parquet_root, symbol)
    if last_written_ms is not None and close_ms <= last_written_ms:
        _rewrite_candle_incomplete_if_written(store, parquet_root, symbol, close_ms)
        return
    last_swept_ms = store.last_swept_close_ms(symbol)
    if last_swept_ms is None or close_ms > last_swept_ms:
        return  # not yet swept at all -- the ordinary sweep will build it correctly when due
    open_ms = close_ms - candles_mod.HOUR_MS
    trades = store.trades_in_range(open_ms, close_ms)
    record = candles_mod.build_candle(trades, open_ms, close_ms, complete=False)
    if record is not None:
        candles_mod.write_candle(parquet_root, symbol, record)
        logger.warning(
            "tabdeal_recorder.late_trades_candle_written", symbol=symbol, hour_close_ms=close_ms
        )


def _handle_detected_gap(
    store: RecorderStore,
    parquet_root: Path | None,
    symbol: str,
    gap: tuple[int, int],
    gap_rowid: int,
) -> None:
    """Log a detected window-no-overlap gap and, if ``parquet_root`` is given, immediately
    correct any already-written candle it touches (see ``_rewrite_candle_incomplete_if_written``).
    Pulled out of ``poll_trades_once`` purely to keep that function's transaction block the
    visually dominant thing in it (m12).

    m-G (fifth fix round): on success, marks the gap row ``rewritten=1`` (``gap_rowid``, from
    ``record_gap``'s return value) so ``_recover_unrewritten_gaps`` does not reprocess it on every
    later sweep. Left at its default ``0`` when ``parquet_root`` is ``None`` (nothing was even
    attempted) or the touched hours' timestamps are not resolvable -- a later sweep with
    ``parquet_root`` available will pick it up via ``_recover_unrewritten_gaps``.
    """
    logger.warning("tabdeal_recorder.gap_detected", symbol=symbol, from_id=gap[0], to_id=gap[1])
    if parquet_root is None:
        return
    lo_ts, hi_ts = store.trade_ts_ms(gap[0]), store.trade_ts_ms(gap[1])
    if lo_ts is None or hi_ts is None:
        return
    for close_ms in _gap_touched_hour_closes(lo_ts, hi_ts):
        _rewrite_candle_incomplete_if_written(store, parquet_root, symbol, close_ms)
    store.mark_gap_rewritten(gap_rowid)


def _detect_late_trades(
    store: RecorderStore,
    *,
    now_ms: int,
    trade_ids: Sequence[int],
    cursor_ms: int | None,
) -> list[int]:
    """Hour-close buckets (deduped, ascending) of this poll's newly-inserted trades (identified
    by ``trade_id in trade_ids`` -- this poll's own parsed trades -- AND ``recorded_ts_ms ==
    now_ms``) whose own ``ts_ms`` falls at or before ``cursor_ms`` -- i.e. trades that arrived
    for an hour already sealed one way or another (MINOR-1, sixth fix round: a feed that answers
    HTTP 200 but is actually frozen/stale can let the quiet-hour timeout seal an hour, or an
    empty/poisoned hour, before all of that hour's real trades have actually arrived).

    M-1 (PR2 review): ``cursor_ms`` is supplied by the caller -- it must be
    ``RecorderStore.last_swept_close_ms`` (the SQLite-only sweep cursor), never
    ``_resumption_cursor_ms`` (which also reads the Parquet store via
    ``candles.last_written_close_ms``). This function runs on the trade-ingestion path, inside
    ``_persist_poll``'s own ``BEGIN IMMEDIATE`` transaction -- a corrupt/unreadable Parquet part
    reached from here used to roll back ``insert_trades`` on every single poll, permanently
    losing any trade past the ``/trades`` endpoint's ~29h response window. ``_process_due_hour``
    now keeps the SQLite cursor caught up for every hour it processes (candle-written or
    gapped), and ``build_due_candles``'s own ``_reconcile_sweep_cursor`` catches it up further
    (outside any poll transaction, try/except-guarded) from whatever is already on disk.

    ``[]`` when ``cursor_ms`` is ``None`` (nothing has been swept yet) or ``trade_ids`` is empty.
    """
    if cursor_ms is None or not trade_ids:
        return []
    late_ts_ms = store.late_trade_ts_ms(trade_ids=trade_ids, recorded_ts_ms=now_ms, max_ts_ms=cursor_ms)
    return sorted({candles_mod.hour_bounds_ms(ts)[1] for ts in late_ts_ms})


@dataclass(frozen=True, slots=True)
class _PersistResult:
    """Everything ``_persist_poll`` hands back for ``poll_trades_once`` to act on once the
    transaction has committed."""

    inserted: int
    overlap_gap_rowid: int | None
    # MINOR-1 (sixth fix round), extended m-2 (PR2 review): (rowid, hour_close_ms) per gap row
    # just recorded whose hour is already sealed one way or another -- a late_trades gap
    # (MINOR-1) or a partial_unparsable gap newly recorded for an hour at or before the sweep's
    # own cursor (m-2) -- for the post-commit Parquet correction
    # (_rewrite_hour_for_late_trades) + mark_gap_rewritten. Both reasons get exactly the same
    # treatment: _rewrite_hour_for_late_trades only cares about close_ms, not why it is late.
    rewrite_gaps: tuple[tuple[int, int], ...]


def _persist_poll(
    store: RecorderStore,
    *,
    now_ms: int,
    symbol: str,
    parsed: _ParsedResponse,
    m: _PollMetrics,
) -> _PersistResult:
    """The single atomic transaction (MAJOR-3) for one poll: the trades, any window-overlap gap
    row, any partial-unparsable gap rows, any late-trades gap rows (MINOR-1), the ``poll_log``
    row, and -- only when ``m.ok`` -- the verified-poll marker ``build_due_candles`` caps the
    sweep against.

    M-1 (PR2 review): no longer takes ``parquet_root`` and never touches Parquet in any way --
    the resumption cursor used to gate late-trade and sealed-hour partial-unparsable rewrites now
    comes exclusively from the SQLite ``sweep_cursor`` table (``store.last_swept_close_ms``),
    which ``_process_due_hour`` keeps advancing for every hour it processes (candle-written or
    gapped), not just gapped ones. Previously, ``_detect_late_trades`` read
    ``candles.last_written_close_ms`` (a Parquet read) from *inside* this function's own ``BEGIN
    IMMEDIATE`` transaction -- a corrupt/unreadable Parquet part raised there and rolled back
    ``insert_trades`` on every single poll, permanently losing any trade past the ``/trades``
    endpoint's ~29h response window. The actual Parquet rewrite this function's callers still do
    (``_handle_detected_gap``, ``_apply_gap_rewrites``) happens strictly after this transaction
    has committed, in ``poll_trades_once`` -- a failure there can no longer roll back a trade that
    is already safely stored.
    """
    result = parsed.result
    gap_rowid: int | None = None
    rewrite_gaps: list[tuple[int, int]] = []
    with store.transaction():
        inserted = store.insert_trades(parsed.trades, recorded_ts_ms=now_ms) if parsed.trades else 0
        if m.gap is not None:
            gap_rowid = store.record_gap(
                detected_ts_ms=now_ms, from_id=m.gap[0], to_id=m.gap[1], reason="window_no_overlap"
            )
        # M-1/m-2 (PR2 review): SQLite-only, safe inside this transaction -- see this function's
        # own docstring for why no Parquet read may happen here.
        cursor_ms = store.last_swept_close_ms(symbol)
        for rowid, close_ms in _record_partial_unparsable(store, parsed.dropped_items, now_ms):
            if cursor_ms is not None and close_ms <= cursor_ms:
                rewrite_gaps.append((rowid, close_ms))
        if inserted > 0:
            trade_ids = [t.trade_id for t in parsed.trades]
            for close_ms in _detect_late_trades(
                store, now_ms=now_ms, trade_ids=trade_ids, cursor_ms=cursor_ms
            ):
                rowid = store.record_gap(
                    detected_ts_ms=now_ms,
                    from_id=None,
                    to_id=None,
                    reason="late_trades",
                    hour_close_ms=close_ms,
                )
                rewrite_gaps.append((rowid, close_ms))
        store.record_poll(
            poll_ts_ms=now_ms,
            first_id=m.first_id,
            last_id=m.last_id,
            n_trades=m.n_trades,
            saturated=m.saturated,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            window_span_seconds=m.window_span_seconds,
            coverage_ratio=m.coverage_ratio,
            n_items_received=parsed.n_items_received,
        )
        if m.ok:
            store.record_verified_poll(symbol=symbol, poll_ts_ms=now_ms)
    return _PersistResult(
        inserted=inserted, overlap_gap_rowid=gap_rowid, rewrite_gaps=tuple(rewrite_gaps)
    )


def _log_poll_outcome(
    *,
    symbol: str,
    parsed: _ParsedResponse,
    n_trades: int,
    coverage_ratio: float | None,
    window_span_seconds: float | None,
    poll_interval_seconds: float,
    empty_after_data: bool,
) -> None:
    """All of ``poll_trades_once``'s non-essential (logging-only) side effects, pulled out so the
    transactional store-write block above it is easy to see at a glance (m12)."""
    result = parsed.result
    if parsed.unexpected_body_type:
        logger.error(
            "tabdeal_recorder.unexpected_body_type", symbol=symbol, body_type=type(result.body).__name__
        )
    if n_trades < parsed.n_items_received:
        logger.warning(
            "tabdeal_recorder.unparsable_trades",
            symbol=symbol,
            received=parsed.n_items_received,
            parsed=n_trades,
        )
    if coverage_ratio is not None and coverage_ratio < 3:
        logger.warning(
            "tabdeal_recorder.low_coverage",
            symbol=symbol,
            coverage_ratio=coverage_ratio,
            window_span_seconds=window_span_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    if empty_after_data:
        # MAJOR-B (fifth fix round): see _derive_poll_metrics.
        logger.warning("tabdeal_recorder.suspicious_empty_response", symbol=symbol)
    if not result.ok:
        logger.warning(
            "tabdeal_recorder.poll_failed", symbol=symbol, status=result.status_code, error=result.error
        )


def _apply_gap_rewrites(
    store: RecorderStore,
    parquet_root: Path | None,
    symbol: str,
    rewrite_gaps: tuple[tuple[int, int], ...],
) -> None:
    """Correct every hour MINOR-1's late-trade detection, or m-2's sealed-hour
    partial-unparsable detection, flagged this poll (``_rewrite_hour_for_late_trades``) and mark
    each gap row rewritten. Pulled out of ``poll_trades_once`` purely to keep that function's own
    flow easy to see at a glance (m12), the same reason ``_handle_detected_gap`` was split out
    for the ``window_no_overlap`` case.

    MINOR-2 (PR2 followups): one row's rewrite raising (e.g. a transient disk error) used to
    propagate straight out of this function, abandoning every OTHER row still in
    ``rewrite_gaps`` for this poll -- not just leaving the failing row ``rewritten=0`` (correct,
    so a later sweep retries it), but silently skipping rows after it that would have succeeded.
    Each row's rewrite is now independently contained: a failure is logged at error level and
    that row is left ``rewritten=0`` for ``_recover_unrewritten_gaps`` to retry on the next
    sweep, but every other row in this call still gets attempted regardless. Once every row has
    been attempted, this still raises (one aggregated exception) if any row failed -- this
    function's own caller (``_apply_post_commit_corrections``) is exactly what turns that into
    the required "log at error level and mark the healthcheck degraded" (CLAUDE.md Sec.3) via
    ``PollOutcome.degraded_error``/``last_cycle_error``; swallowing it here entirely would make a
    gap-rewrite failure invisible to the heartbeat.
    """
    if parquet_root is None:
        return
    failures: list[str] = []
    for rowid, close_ms in rewrite_gaps:
        try:
            _rewrite_hour_for_late_trades(store, parquet_root, symbol, close_ms)
        except Exception as exc:
            message = _redact_cycle_error(str(exc))
            logger.error(
                "tabdeal_recorder.gap_rewrite_failed",
                symbol=symbol,
                hour_close_ms=close_ms,
                error=message,
            )
            failures.append(f"hour_close_ms={close_ms}: {message}")
            continue
        store.mark_gap_rewritten(rowid)
    if failures:
        summary = "; ".join(failures)
        raise RuntimeError(f"{len(failures)}/{len(rewrite_gaps)} gap rewrite(s) failed: {summary}")


def _apply_post_commit_corrections(
    store: RecorderStore,
    parquet_root: Path | None,
    symbol: str,
    *,
    gap: tuple[int, int] | None,
    gap_rowid: int | None,
    rewrite_gaps: tuple[tuple[int, int], ...],
) -> str | None:
    """Run every post-commit, Parquet-touching candle correction for this poll
    (``_handle_detected_gap``, ``_apply_gap_rewrites``) and return a redacted error string if
    either raised -- never raises itself.

    Engineering rule (CLAUDE.md Sec.3, PR2 review): "raw trade ingestion must never depend on any
    downstream step." By the time this runs, ``_persist_poll``'s transaction has already
    committed -- the trades, the poll_log row, and the gap rows it recorded are all durable. A
    Parquet failure here (a corrupt part, a full disk, ...) must stop only this correction, never
    unwind or block the ingestion that already succeeded, and never propagate out of
    ``poll_trades_once`` -- it is logged at error level (an alert) and surfaced to the caller as
    ``PollOutcome.degraded_error`` instead, which ``TabdealRecorderService.process_once`` folds
    into the heartbeat so the healthcheck can flag it, without affecting ``ok``/``inserted`` or
    making the recorder back off/retry as though the poll itself had failed.
    """
    try:
        if gap is not None and gap_rowid is not None:
            _handle_detected_gap(store, parquet_root, symbol, gap, gap_rowid)
        _apply_gap_rewrites(store, parquet_root, symbol, rewrite_gaps)
    except Exception as exc:
        message = _redact_cycle_error(str(exc))
        logger.error(
            "tabdeal_recorder.downstream_candle_correction_failed", symbol=symbol, error=message
        )
        return message
    return None


def poll_trades_once(
    client: TabdealClient,
    store: RecorderStore,
    *,
    symbol: str,
    limit: int,
    poll_interval_seconds: float,
    clock: Clock,
    parquet_root: Path | None = None,
) -> PollOutcome:
    """One ``/trades`` poll: fetch, parse, detect a window-overlap gap against the
    previously-stored max id, and persist all of it in one transaction (``_persist_poll``,
    MAJOR-3). Resumption is cursor-free: ``prev_max`` is read from the store itself, so a
    restarted process resumes exactly where the previous one left off. ``ok`` (MAJOR-2') is
    ``True`` only when the HTTP call succeeded, the body was a list, and items did not arrive
    with none of them parsing. ``parquet_root``, when given, lets a newly-discovered gap
    immediately correct an already-written candle (``_rewrite_candle_incomplete_if_written``)
    and lets a late-arriving trade (or a sealed-hour partial-unparsable item, m-2) correct an
    hour already sealed one way or another (``_apply_gap_rewrites``, MINOR-1/m-2); most tests
    omit it. M-1 (PR2 review): all of this Parquet access happens here, strictly after
    ``_persist_poll``'s own transaction has committed -- never inside it. See ``_persist_poll``'s
    docstring.

    Engineering rule (PR2 review): a failure in that post-commit correction
    (``_apply_post_commit_corrections``) never raises out of this function -- it is reported via
    ``PollOutcome.degraded_error`` instead, so a broken downstream (Parquet) step can never make
    this function's own caller treat an otherwise-successful poll as failed.
    """
    prev_max = store.max_trade_id()
    parsed = _fetch_and_parse(client, symbol, limit, clock=clock)
    now_ms = parsed.now_ms  # NIT: sampled inside _fetch_and_parse, after the HTTP round-trip
    result = parsed.result
    m = _derive_poll_metrics(parsed, prev_max, limit, poll_interval_seconds)
    persisted = _persist_poll(store, now_ms=now_ms, symbol=symbol, parsed=parsed, m=m)
    inserted = persisted.inserted

    degraded_error = _apply_post_commit_corrections(
        store,
        parquet_root,
        symbol,
        gap=m.gap,
        gap_rowid=persisted.overlap_gap_rowid,
        rewrite_gaps=persisted.rewrite_gaps,
    )

    _log_poll_outcome(
        symbol=symbol,
        parsed=parsed,
        n_trades=m.n_trades,
        coverage_ratio=m.coverage_ratio,
        window_span_seconds=m.window_span_seconds,
        poll_interval_seconds=poll_interval_seconds,
        empty_after_data=m.empty_after_data,
    )

    return PollOutcome(
        ok=m.ok,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        n_trades=m.n_trades,
        n_items_received=parsed.n_items_received,
        first_id=m.first_id,
        last_id=m.last_id,
        saturated=m.saturated,
        window_span_seconds=m.window_span_seconds,
        coverage_ratio=m.coverage_ratio,
        inserted=inserted,
        gap=m.gap,
        error=result.error,
        degraded_error=degraded_error,
    )


# ---------------------------------------------------------------------------------
# order-book snapshots (decision D-019)
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderbookRow:
    ts_ms: int
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread_bps: float | None
    depth_bid_01pct: Decimal
    depth_ask_01pct: Decimal
    depth_bid_05pct: Decimal
    depth_ask_05pct: Decimal
    depth_bid_1pct: Decimal
    depth_ask_1pct: Decimal


def poll_orderbook_once(
    client: TabdealClient, store: RecorderStore, *, symbol: str, depth_limit: int, clock: Clock
) -> OrderbookRow | None:
    """One ``/depth`` poll -> one ``orderbook`` row. Returns ``None`` (logged) on a failed poll
    or an empty/one-sided book -- no row is written in that case, there is nothing to measure.

    m1 (fourth fix round): a crossed book (best ask < best bid -- a bad/transient snapshot)
    used to make ``spread_bps_and_pct`` raise ``ValueError`` straight out of this function,
    uncaught. In ``TabdealRecorderService.process_once`` that skipped the heartbeat rewrite for
    the whole cycle (the exception propagated past it) and left ``_next_orderbook_due_ms`` unset,
    so the next cycle retried ``/depth`` immediately instead of waiting out
    ``orderbook_interval_seconds`` -- a crossed book then re-polled every poll cycle (every 5s)
    until the book uncrossed. Caught here instead: the row is still written, with
    ``spread_bps=None`` (depth itself is still meaningful on a crossed book; the spread is not).
    """
    result = client.depth(symbol, limit=depth_limit)
    now_ms = _dt_to_ms(clock.now())
    if not result.ok or not isinstance(result.body, dict):
        logger.warning("tabdeal_recorder.orderbook_poll_failed", symbol=symbol, status=result.status_code)
        return None

    bids = parse_depth_levels(result.body.get("bids"))
    asks = parse_depth_levels(result.body.get("asks"))
    best = best_bid_ask(bids, asks)
    if best is None:
        logger.warning("tabdeal_recorder.orderbook_empty_book", symbol=symbol)
        return None
    best_bid, best_ask = best
    try:
        spread_bps, _spread_pct = spread_bps_and_pct(best_bid, best_ask)
    except ValueError:
        logger.warning(
            "tabdeal_recorder.orderbook_crossed",
            symbol=symbol,
            best_bid=str(best_bid),
            best_ask=str(best_ask),
        )
        spread_bps = None
    mid = (best_bid + best_ask) / 2

    depth_by_threshold: dict[Decimal, tuple[Decimal, Decimal]] = {}
    for threshold in _DEPTH_THRESHOLDS:
        bid_base, _bid_quote = cumulative_depth(bids, mid, threshold, side="bid")
        ask_base, _ask_quote = cumulative_depth(asks, mid, threshold, side="ask")
        depth_by_threshold[threshold] = (bid_base, ask_base)

    row = OrderbookRow(
        ts_ms=now_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_bps=spread_bps,
        depth_bid_01pct=depth_by_threshold[_DEPTH_THRESHOLDS[0]][0],
        depth_ask_01pct=depth_by_threshold[_DEPTH_THRESHOLDS[0]][1],
        depth_bid_05pct=depth_by_threshold[_DEPTH_THRESHOLDS[1]][0],
        depth_ask_05pct=depth_by_threshold[_DEPTH_THRESHOLDS[1]][1],
        depth_bid_1pct=depth_by_threshold[_DEPTH_THRESHOLDS[2]][0],
        depth_ask_1pct=depth_by_threshold[_DEPTH_THRESHOLDS[2]][1],
    )
    store.record_orderbook(row)
    return row


# ---------------------------------------------------------------------------------
# candle sweep
# ---------------------------------------------------------------------------------


def is_candle_complete(store: RecorderStore, open_ms: int, close_ms: int) -> bool:
    """``False`` when a window-no-overlap gap overlaps the bucket -- never from the
    uninformative ``saturated`` flag (decision D-024). An empty-hour gap never reaches this
    function at all: ``build_due_candles`` skips writing a candle for that hour in the first
    place.

    MAJOR M4 (fix round): also ``False`` when the store has no recorded coverage of the bucket's
    *start* -- i.e. the earliest trade ever recorded landed after ``open_ms``. On a fresh
    database there is no ``window_no_overlap`` gap row to catch this (there is nothing before the
    first trade to overlap with), so without this check the very first candle after a cold start
    was written ``complete=True`` even though recording plainly started partway through that hour
    (measured: first trade at 00:30, bar for 01:00 written complete=True with n_trades=1) --
    exactly what G1b's ">=99% complete" gate and the phase-3 basis model trust.

    MAJOR M-B (third fix round): also ``False`` when a ``partial_unparsable`` gap was recorded for
    this exact hour (via ``has_hour_gap``) -- a poll that dropped some items mid-batch leaves no
    id-range for ``gaps_overlapping`` to catch, since the dropped rows may never have parsed an id.
    """
    if store.gaps_overlapping(open_ms, close_ms):
        return False
    if store.has_hour_gap(close_ms):
        return False
    earliest_ms = store.earliest_trade_ts_ms()
    return earliest_ms is None or earliest_ms <= open_ms


def _sweep_now_ms(store: RecorderStore, symbol: str, clock_now_ms: int) -> int | None:
    """The "now" ``build_due_candles`` is allowed to treat an hour as due against, or ``None`` if
    the sweep must not run at all yet.

    MAJOR-1' (fourth fix round): if a poll has ever been verified (see
    ``RecorderStore.record_verified_poll``), the sweep is capped at that timestamp, never the raw
    wall clock -- otherwise, while polls are failing across an hour's close time, the sweep kept
    advancing on the wall clock alone and declared that hour "empty" (wrong reason) before a
    later successful poll could backfill its late-arriving trades; by the time it was backfilled,
    the sweep cursor had already moved past it and the candle was never corrected.

    m-D (fifth fix round): if no poll has *ever* been verified, the old behaviour fell back to the
    raw wall clock -- unsafe for a database migrated from a pre-v3 schema, whose
    ``recorder_state`` table starts out empty even though the file has real history, and unsafe in
    general since there is then no actual evidence the feed is live at all. The sweep now does
    nothing (returns ``None``) until the very first poll is verified, rather than guessing from
    the wall clock.
    """
    verified_ms = store.verified_poll_ts_ms(symbol)
    if verified_ms is None:
        return None
    return min(clock_now_ms, verified_ms)


def _hour_feed_has_moved_past(
    store: RecorderStore, close_ms: int, quiet_hour_timeout_seconds: float, *, now_ms: int
) -> bool:
    """m-C (fifth fix round): whether there is actual evidence the trade feed has moved past the
    hour closing at ``close_ms``, independent of what the (verified-poll-capped) wall clock says.

    Either a stored trade exists after ``close_ms`` (direct proof -- some later poll already saw
    data beyond this hour), or ``now_ms`` is at least ``quiet_hour_timeout_seconds`` past
    ``close_ms`` (a quiet-market timeout: the feed may simply have nothing new to say, and
    waiting forever for a trade that may not come for a while would starve the sweep on a
    genuinely dead-quiet market). Without this, ``build_due_candles`` could seal an hour the
    instant the wall clock (or a stale/cached response) said it was due, with no actual evidence
    the feed had moved on -- exactly the stale/cached-response and clock-skew risk this closes.

    NIT (sixth fix round): ``now_ms`` must be the caller's already clock/verified-capped value
    (``_sweep_now_ms``'s ``min(real_clock_now_ms, verified_poll_ts_ms)``), not a fresh,
    independent re-query of the raw ``verified_poll_ts_ms``. ``record_verified_poll`` only ever
    advances (``MAX``-based), so a transient forward clock jump (e.g. an NTP step) recorded once
    stays stuck at that too-high value forever even after the real clock corrects back down --
    re-querying it directly here would then permanently satisfy this check for every hour
    regardless of how little real time had actually passed since. Reusing the capped value
    self-corrects the instant the real clock reading drops back below the stale verified one.
    """
    if store.has_trade_after(close_ms):
        return True
    return now_ms >= close_ms + int(quiet_hour_timeout_seconds * 1000)


def _recover_unrewritten_gaps(store: RecorderStore, parquet_root: Path, symbol: str) -> None:
    """m-G (fifth fix round): re-attempt any ``window_no_overlap`` gap's candle-rewrite that never
    got to run (crash between the poll's own transaction committing and the inline rewrite in
    ``_handle_detected_gap``) -- see ``RecorderStore.unrewritten_overlap_gaps``. Idempotent and
    cheap when there is nothing to recover (the common case): called at the very start of every
    sweep so a crash at that exact point self-heals on the next sweep rather than leaving an
    already-written candle ``complete=True`` forever.

    MINOR-1 (sixth fix round), extended MINOR-2 (PR2 followups): also re-attempts any
    ``late_trades`` OR ``partial_unparsable`` gap left ``rewritten=0`` by a crash, or by a prior
    sweep's own rewrite attempt failing (``_apply_gap_rewrites``), between a poll's own
    transaction committing and its correction (``_rewrite_hour_for_late_trades``) -- the same
    self-healing role for both gap reasons. ``_persist_poll`` (m-2) gives a sealed-hour
    ``partial_unparsable`` row exactly the same treatment as a ``late_trades`` row -- see
    ``_PersistResult.rewrite_gaps`` -- so both must be retried here, or a row whose first rewrite
    attempt failed (e.g. a transient I/O error) stays ``rewritten=0`` and its candle stays wrong
    (``complete=True`` with a known-missing trade) forever. Each row is independently contained
    (logged at error level, left ``rewritten=0`` for the NEXT sweep) so one persistently-failing
    row never blocks another row, or a later sweep, from being retried -- but once every row has
    been attempted, this still raises (one aggregated exception) if any row failed, the same
    "attempt everything, then surface the failure" shape as ``_apply_gap_rewrites``. This
    function's caller, ``build_due_candles``, has no try/except of its own around it on
    purpose: that failure must propagate out to ``_run_downstream_steps``'s own containment
    (``candle_sweep_failed``), which is what turns it into the required "log at error level and
    mark the healthcheck degraded" (CLAUDE.md Sec.3) via the heartbeat.
    """
    for rowid, from_id, to_id in store.unrewritten_overlap_gaps():
        lo_ts, hi_ts = store.trade_ts_ms(from_id), store.trade_ts_ms(to_id)
        if lo_ts is not None and hi_ts is not None:
            for close_ms in _gap_touched_hour_closes(lo_ts, hi_ts):
                _rewrite_candle_incomplete_if_written(store, parquet_root, symbol, close_ms)
        store.mark_gap_rewritten(rowid)
    failures: list[str] = []
    for reason in ("late_trades", "partial_unparsable"):
        for rowid, close_ms in store.unrewritten_hour_gaps(reason):
            try:
                _rewrite_hour_for_late_trades(store, parquet_root, symbol, close_ms)
            except Exception as exc:
                message = _redact_cycle_error(str(exc))
                logger.error(
                    "tabdeal_recorder.gap_rewrite_recovery_failed",
                    symbol=symbol,
                    hour_close_ms=close_ms,
                    reason=reason,
                    error=message,
                )
                failures.append(f"{reason} hour_close_ms={close_ms}: {message}")
                continue
            store.mark_gap_rewritten(rowid)
    if failures:
        summary = "; ".join(failures)
        raise RuntimeError(f"{len(failures)} unrewritten-gap recovery failure(s): {summary}")


def _handle_poisoned_hour(store: RecorderStore, symbol: str, close_ms: int, now_ms: int) -> None:
    """``candles_mod.build_candle`` returned ``None`` for a non-empty, already window-filtered
    trade list -- the only way that happens is its defensive poisoned-row guard (a non-finite or
    non-positive price, or negative qty, that reached the database before
    ``parse_trade_item``'s own guard existed) filtering out every trade. Given the exact same
    "accounted for once" treatment as an empty hour -- a ``poisoned_trades`` gap row plus a
    sweep-cursor advance -- rather than a bare ``continue``, which would leave the hour
    un-advanced and re-discovered as "due" on every future poll forever.
    """
    store.record_hour_gap(symbol=symbol, close_ms=close_ms, detected_ts_ms=now_ms, reason="poisoned_trades")
    logger.warning("tabdeal_recorder.poisoned_hour", symbol=symbol, hour_close_ms=close_ms)


def _record_empty_hour(store: RecorderStore, symbol: str, open_ms: int, close_ms: int, now_ms: int) -> None:
    """NIT (fifth fix round): an empty hour that falls inside an already-recorded
    ``window_no_overlap`` gap's span is a known data-loss hole, not a quiet market -- it must
    carry that same reason, not the misleading ``no_trades_in_hour``."""
    overlaps_gap = store.gaps_overlapping(open_ms, close_ms)
    reason = "window_no_overlap" if overlaps_gap else "no_trades_in_hour"
    store.record_hour_gap(symbol=symbol, close_ms=close_ms, detected_ts_ms=now_ms, reason=reason)
    logger.warning("tabdeal_recorder.empty_hour", symbol=symbol, hour_close_ms=close_ms, reason=reason)


def _process_due_hour(
    store: RecorderStore, parquet_root: Path, symbol: str, open_ms: int, close_ms: int, now_ms: int
) -> candles_mod.TabdealCandleRecord | None:
    """Everything ``build_due_candles`` does for exactly one hour already confirmed due (grace
    period + m-C's feed-evidence gate) -- an empty-hour/poisoned-hour gap row, or a written
    candle. Pulled out purely to keep ``build_due_candles`` itself at a glance-able size."""
    trades = store.trades_in_range(open_ms, close_ms)
    if not trades:
        _record_empty_hour(store, symbol, open_ms, close_ms, now_ms)
        return None
    complete = is_candle_complete(store, open_ms, close_ms)
    record = candles_mod.build_candle(trades, open_ms, close_ms, complete=complete)
    if record is None:
        _handle_poisoned_hour(store, symbol, close_ms, now_ms)
        return None
    candles_mod.write_candle(parquet_root, symbol, record)
    # M-1 (PR2 review): advance the SQLite sweep_cursor for a candle-written hour too, not just
    # a gapped one (record_hour_gap already did this for those) -- this is what lets
    # _detect_late_trades rely on store.last_swept_close_ms alone, with zero Parquet reads
    # anywhere on the trade-ingestion path. See RecorderStore.advance_sweep_cursor.
    store.advance_sweep_cursor(symbol=symbol, close_ms=close_ms)
    logger.info(
        "tabdeal_recorder.candle_written",
        symbol=symbol,
        ts=record.ts.isoformat(),
        n_trades=record.n_trades,
        complete=record.complete,
    )
    return record


def _reconcile_sweep_cursor(store: RecorderStore, parquet_root: Path, symbol: str) -> None:
    """Catch the SQLite ``sweep_cursor`` up to whatever candle is already on disk, in case it
    lags behind (M-1, PR2 review) -- e.g. a crash between ``write_candle`` and
    ``RecorderStore.advance_sweep_cursor`` in ``_process_due_hour``, or a database that predates
    this fix (its cursor never advanced for a candle-written hour at all, only a gapped one).

    Read failure (e.g. a corrupt/unreadable Parquet part) is caught, logged, and this
    reconciliation is simply skipped for this sweep -- the cursor stays at its current, still
    safe (if possibly stale) value, and reconciliation is retried on the next sweep. This must
    never prevent a poll's own trade insert, which is why it lives here, at the very start of
    ``build_due_candles``, strictly outside any poll transaction -- never on
    ``_detect_late_trades``'s path (see that function's own docstring for the incident this
    separation fixes). The rest of this sweep (below) may still raise on the same corrupt part
    while actually trying to build candles -- that is a separate, loud sweep failure, not this
    best-effort catch-up.
    """
    try:
        last_written_ms = candles_mod.last_written_close_ms(parquet_root, symbol)
    except Exception:
        logger.warning(
            "tabdeal_recorder.sweep_cursor_reconcile_failed", symbol=symbol, exc_info=True
        )
        return
    if last_written_ms is not None:
        store.advance_sweep_cursor(symbol=symbol, close_ms=last_written_ms)


def build_due_candles(
    store: RecorderStore,
    *,
    symbol: str,
    parquet_root: Path,
    grace_period_seconds: float,
    clock: Clock,
    quiet_hour_timeout_seconds: float = _DEFAULT_QUIET_HOUR_TIMEOUT_SECONDS,
) -> list[candles_mod.TabdealCandleRecord]:
    """Write every hourly candle that is now due (closed, past its grace period, not yet on
    disk). An hour with zero recorded trades writes a ``gaps`` row instead of a forward-filled
    bar -- it is simply skipped, never synthesised. See ``_sweep_now_ms`` for why "due" is capped
    at the last verified poll, not the raw wall clock, and ``_hour_feed_has_moved_past`` (m-C) for
    the additional feed-evidence gate applied to every candidate hour, oldest first -- the sweep
    stops (not just skips) at the first hour that fails it, since resumption is strictly
    sequential. ``_process_due_hour`` does the actual per-hour work.

    m-G (fifth fix round): before anything else, re-attempts any gap row left ``rewritten=0`` by a
    crash between a poll's own transaction committing and its inline candle-rewrite (see
    ``_recover_unrewritten_gaps``) -- this runs even when the rest of the sweep below ends up
    doing nothing (e.g. no poll has ever been verified yet).

    M-1 (PR2 review): also reconciles the SQLite sweep cursor against the Parquet store
    (``_reconcile_sweep_cursor``) before anything else -- see that function's own docstring.
    """
    _reconcile_sweep_cursor(store, parquet_root, symbol)
    _recover_unrewritten_gaps(store, parquet_root, symbol)
    now_ms = _sweep_now_ms(store, symbol, _dt_to_ms(clock.now()))
    if now_ms is None:  # m-D: no poll has ever been verified -- nothing is due yet.
        return []
    # MAJOR M3 (via _resumption_cursor_ms): the resumption cursor is the max of "last candle
    # actually written" and "last empty hour already swept" -- using only the former re-walks
    # every trailing empty hour on every single poll forever, since an empty hour never writes a
    # candle to advance it.
    last_close_ms = _resumption_cursor_ms(store, parquet_root, symbol)
    earliest_ms = store.earliest_trade_ts_ms()
    written: list[candles_mod.TabdealCandleRecord] = []
    for open_ms, close_ms in candles_mod.pending_hour_bounds(
        last_written_close_ms=last_close_ms,
        earliest_open_ms=earliest_ms,
        now_ms=now_ms,
        grace_period_seconds=grace_period_seconds,
    ):
        if not _hour_feed_has_moved_past(store, close_ms, quiet_hour_timeout_seconds, now_ms=now_ms):
            break  # m-C: no evidence yet the feed has moved past this hour -- stop, don't skip.
        record = _process_due_hour(store, parquet_root, symbol, open_ms, close_ms, now_ms)
        if record is not None:
            written.append(record)
    return written


# ---------------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeartbeatState:
    last_poll_ts: datetime
    last_trade_id: int | None
    n_trades_total: int
    consecutive_errors: int
    # m3 (fourth fix round): wall-clock time the last genuinely new trade was recorded (not just
    # "a poll succeeded") -- see RecorderStore.last_recorded_ts_ms and deploy/healthcheck.py.
    last_new_trade_ts_ms: int | None = None
    # NIT (fifth fix round): wall-clock time of this recorder's very first poll attempt ever --
    # see RecorderStore.first_poll_ts_ms and deploy/healthcheck.py. Lets the healthcheck fail a
    # recorder that has never recorded a single trade (last_new_trade_ts_ms stays null forever)
    # once it has had long enough to see one, instead of looking healthy indefinitely.
    first_poll_ts_ms: int | None = None
    # MINOR-3 (sixth fix round): the most recent process_once() cycle's own error text (``None``
    # on a cycle that completed normally -- this is not sticky), scrubbed through
    # tbot.monitoring.logging.redact_secrets before it ever reaches this file. See
    # TabdealRecorderService.process_once and deploy/healthcheck.py.
    last_cycle_error: str | None = None
    # m-3 (PR2 review), wording corrected (PR2 followups): consecutive cycles with ANY problem --
    # a genuine process_once() exception, or a downstream step (candle sweep, order-book poll,
    # post-commit Parquet rewrite) that was caught and logged without raising -- including the
    # current cycle if it is the one failing. Despite the field's name (kept for backwards
    # compatibility with deploy/healthcheck.py's existing payload contract), this mirrors
    # TabdealRecorderService._consecutive_degraded_cycles, NOT _consecutive_cycle_exceptions: a
    # sweep-only failure (e.g. build_due_candles hitting a corrupt Parquet part) used to be
    # invisible to deploy/healthcheck.py entirely, because the poll itself kept succeeding every
    # cycle -- which reset both consecutive_errors AND _consecutive_cycle_exceptions to 0 right
    # before the sweep raised -- and _consecutive_degraded_cycles (the counter that DID climb)
    # was never written to the heartbeat at all. 0 on a cycle that completed normally -- also not
    # sticky, same as last_cycle_error.
    consecutive_cycle_exceptions: int = 0


def write_heartbeat(path: Path, state: HeartbeatState) -> None:
    """Rewrite the heartbeat file. deploy/healthcheck.py reads this file's contents (not just its
    mtime), so it must be rewritten on every poll, even one with zero new trades."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_poll_ts": state.last_poll_ts.isoformat(),
        "last_trade_id": state.last_trade_id,
        "n_trades_total": state.n_trades_total,
        "consecutive_errors": state.consecutive_errors,
        "last_new_trade_ts_ms": state.last_new_trade_ts_ms,
        "first_poll_ts_ms": state.first_poll_ts_ms,
        "last_cycle_error": state.last_cycle_error,
        "consecutive_cycle_exceptions": state.consecutive_cycle_exceptions,
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(path)


# ---------------------------------------------------------------------------------
# service wiring
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecorderSettings:
    """Recorder-specific knobs. Every interval here is provisional until the probe's measured
    recommendation (``research/reports/tabdeal_probe_*.json``) replaces it -- see
    ``scripts/record_tabdeal.py`` for how each one is overridable from the CLI or config without
    a code edit.

    ``trades_limit=1000`` (fourth fix round, raised from 500): measured from the Turkey server
    2026-10-04, ``limit=1000`` is accepted and, for BTCUSDT (a thin market), covers roughly 29
    hours of trades -- a wide safety margin against the window-no-overlap gap check for any poll
    interval short of a day-long outage.

    ``quiet_hour_timeout_seconds=7200`` (m-C, fifth fix round): see
    ``tbot.data.tabdeal_recorder._hour_feed_has_moved_past`` -- how long, with no trade recorded
    after an hour's close, the last verified poll must be past that close before the hour is swept
    anyway (a quiet market, not a stale/cached response). Comfortably above the measured 2120s max
    BTCUSDT inter-trade gap.
    """

    symbol: str = "BTCUSDT"
    trades_limit: int = 1000
    poll_interval_seconds: float = 5.0
    orderbook_interval_seconds: float = 60.0
    grace_period_seconds: float = 60.0
    depth_limit: int = 100
    quiet_hour_timeout_seconds: float = _DEFAULT_QUIET_HOUR_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in ("trades_limit", "depth_limit"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        for name in (
            "poll_interval_seconds",
            "orderbook_interval_seconds",
            "grace_period_seconds",
            "quiet_hour_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")


@dataclass(slots=True)
class TabdealRecorderService:
    """Owns one poll/candle/heartbeat cycle. ``process_once`` is the unit the tests drive;
    ``run_forever`` just calls it on a timer until ``request_stop`` is set (SIGTERM/SIGINT,
    wired up in ``scripts/record_tabdeal.py``)."""

    client: TabdealClient
    store: RecorderStore
    parquet_root: Path
    heartbeat_file: Path
    settings: RecorderSettings
    clock: Clock
    _consecutive_errors: int = field(default=0, init=False)
    # m-E (fifth fix round): distinct from _consecutive_errors -- counts consecutive
    # process_once() exceptions (a bug/crash), not ordinary poll failures process_once already
    # handles gracefully. See run_forever. Engineering rule (PR2 review): this must NEVER climb
    # because of a downstream-only failure (candle sweep, order-book poll, a post-commit
    # Parquet rewrite) -- those are fully contained inside process_once (see
    # _consecutive_degraded_cycles below) and never raise out of it, specifically so this
    # counter -- which drives run_forever's backoff AND its RecorderFatalError exit -- stays
    # tied to genuine ingestion-path bugs only. Raw trade ingestion must never be throttled or
    # killed by a problem in a downstream step.
    _consecutive_cycle_exceptions: int = field(default=0, init=False)
    # Engineering rule (PR2 review): consecutive cycles with ANY problem -- a genuine
    # process_once() exception (ingestion-path), or a downstream step (candle sweep, order-book
    # poll, post-commit Parquet rewrite) that process_once caught and logged without raising.
    # Owned entirely by process_once itself (never by run_forever, unlike
    # _consecutive_cycle_exceptions above) so it keeps accumulating correctly across a run of
    # purely-downstream-degraded cycles, which never raise and therefore never touch
    # _consecutive_cycle_exceptions. This is what feeds the heartbeat's
    # consecutive_cycle_exceptions field that deploy/healthcheck.py's m-3 threshold reads.
    _consecutive_degraded_cycles: int = field(default=0, init=False)
    _next_orderbook_due_ms: int | None = field(default=None, init=False)
    _stop: bool = field(default=False, init=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)

    def __post_init__(self) -> None:
        # m6 (fourth fix round): refuse to run against a database recorded for another symbol --
        # see RecorderStore.ensure_symbol. Checked once here, not on every poll.
        self.store.ensure_symbol(self.settings.symbol)
        # NIT (PR2 followups): reconcile the SQLite sweep_cursor against whatever is already on
        # disk once at construction time too, not only after the first poll of this process
        # (``build_due_candles``'s own call to ``_reconcile_sweep_cursor``, inside
        # ``_run_downstream_steps``). Without this, a process restarted right after a deploy or a
        # crash runs its first poll -- and ``_detect_late_trades`` inside THAT poll's own
        # transaction -- against a cursor that may already lag what Parquet shows, so a trade
        # that is late relative to the true last-swept hour but not relative to the stale cursor
        # goes undetected as "late" and never gets its correcting rewrite. Never allowed to block
        # startup: ``_reconcile_sweep_cursor`` already contains its own read failure (a corrupt/
        # unreadable Parquet part) internally, but this call is additionally wrapped here in case
        # advancing the cursor itself (a SQLite write) raises -- logged and skipped either way,
        # since the ordinary per-poll reconcile call retries this on every later sweep regardless.
        try:
            _reconcile_sweep_cursor(self.store, self.parquet_root, self.settings.symbol)
        except Exception:
            logger.error(
                "tabdeal_recorder.startup_sweep_cursor_reconcile_failed",
                symbol=self.settings.symbol,
                exc_info=True,
            )

    def request_stop(self) -> None:
        self._stop = True
        self._stop_event.set()

    def close(self) -> None:
        self.store.close()

    def _run_downstream_steps(self, outcome: PollOutcome) -> list[str]:
        """Run the candle sweep and order-book poll, each independently contained, and return
        every redacted error message collected (``outcome.degraded_error`` included) -- empty if
        everything succeeded. Never raises.

        Engineering rule (CLAUDE.md Sec.3, added following the PR2 review): "raw trade ingestion
        must never depend on any downstream step (candle building, Parquet, quality checks). A
        downstream failure may stop only that step, raise an alert (log at error level) and mark
        the healthcheck degraded/unhealthy -- trades keep being recorded." A failure in either
        step is logged at error level (the "alert") and collected here, but never propagates --
        see ``process_once``'s own docstring for why that matters (it must never touch
        ``_consecutive_cycle_exceptions``, which drives backoff/fatal-exit). ``poll_trades_once``
        applies the same containment to its own post-commit Parquet rewrite one level down (see
        ``PollOutcome.degraded_error``, folded in here as this cycle's first entry).
        """
        degraded: list[str] = []
        if outcome.degraded_error is not None:
            degraded.append(outcome.degraded_error)
        try:
            build_due_candles(
                self.store,
                symbol=self.settings.symbol,
                parquet_root=self.parquet_root,
                grace_period_seconds=self.settings.grace_period_seconds,
                clock=self.clock,
                quiet_hour_timeout_seconds=self.settings.quiet_hour_timeout_seconds,
            )
        except Exception as exc:
            message = _redact_cycle_error(str(exc))
            logger.error(
                "tabdeal_recorder.candle_sweep_failed", symbol=self.settings.symbol, error=message
            )
            degraded.append(message)
        try:
            self._maybe_poll_orderbook()
        except Exception as exc:
            message = _redact_cycle_error(str(exc))
            logger.error(
                "tabdeal_recorder.orderbook_poll_failed", symbol=self.settings.symbol, error=message
            )
            degraded.append(message)
        return degraded

    def process_once(self) -> PollOutcome:
        """One poll/sweep/orderbook cycle.

        Engineering rule (CLAUDE.md Sec.3, PR2 review): raw trade ingestion must never depend on
        any downstream step. ``poll_trades_once`` (the poll + trade-insert transaction) is the
        ONLY step whose exception is allowed to propagate out of this method -- a genuine
        ingestion-path failure, which ``run_forever``'s existing backoff/fatal-exit machinery
        (``_consecutive_cycle_exceptions``) exists to handle. Every downstream step
        (``_run_downstream_steps``: the candle sweep, the order-book poll, and
        ``poll_trades_once``'s own post-commit Parquet rewrite) is independently contained
        instead -- see that method's docstring. ``_consecutive_degraded_cycles`` (below, owned
        entirely by this method, never by ``run_forever``) is what actually climbs for those,
        so it keeps accumulating correctly across a run of purely-downstream-degraded cycles --
        none of which raise, and therefore none of which would ever touch ``run_forever``'s own
        ``_consecutive_cycle_exceptions`` -- while that counter itself, and the backoff/fatal-exit
        it drives, stays tied to genuine ingestion-path bugs only.

        MINOR-3 (sixth fix round): the heartbeat is written from a ``finally`` block, rewritten
        even when this cycle raises, carrying ``last_cycle_error`` (scrubbed through the existing
        redaction pipeline, every message folded together when more than one step degraded in
        the same cycle) -- ``None`` on a cycle with no problem at all; not sticky. m-3 (PR2
        review): it also carries ``consecutive_cycle_exceptions``, fed from
        ``self._consecutive_degraded_cycles`` -- a sweep-only failure used to be invisible to
        ``deploy/healthcheck.py`` entirely (the poll kept succeeding, resetting
        ``consecutive_errors`` to 0 right before the sweep raised).

        NIT (PR2 review): the heartbeat write itself is wrapped in its own ``try``/``except`` --
        a failure there (e.g. a full disk) must not replace or mask whatever exception this cycle
        itself raised, nor prevent this method from returning/raising normally either way.
        """
        last_cycle_error: str | None = None
        try:
            outcome = poll_trades_once(
                self.client,
                self.store,
                symbol=self.settings.symbol,
                limit=self.settings.trades_limit,
                poll_interval_seconds=self.settings.poll_interval_seconds,
                clock=self.clock,
                parquet_root=self.parquet_root,
            )
            self._consecutive_errors = 0 if outcome.ok else self._consecutive_errors + 1

            degraded = self._run_downstream_steps(outcome)
            if degraded:
                last_cycle_error = "; ".join(degraded)
        except Exception as exc:
            last_cycle_error = _redact_cycle_error(str(exc))
            raise
        finally:
            self._consecutive_degraded_cycles = (
                self._consecutive_degraded_cycles + 1 if last_cycle_error is not None else 0
            )
            try:
                write_heartbeat(
                    self.heartbeat_file,
                    HeartbeatState(
                        last_poll_ts=self.clock.now(),
                        last_trade_id=self.store.max_trade_id(),
                        n_trades_total=self.store.total_trade_count(),
                        consecutive_errors=self._consecutive_errors,
                        last_new_trade_ts_ms=self.store.last_recorded_ts_ms(),
                        first_poll_ts_ms=self.store.first_poll_ts_ms(),
                        last_cycle_error=last_cycle_error,
                        consecutive_cycle_exceptions=self._consecutive_degraded_cycles,
                    ),
                )
            except Exception:
                logger.exception(
                    "tabdeal_recorder.heartbeat_write_failed", symbol=self.settings.symbol
                )
        return outcome

    def _maybe_poll_orderbook(self) -> None:
        """Poll ``/depth`` if ``orderbook_interval_seconds`` has elapsed since the last poll.
        Pulled out of ``process_once`` purely to keep that function's own flow (poll, sweep,
        heartbeat) easy to see at a glance."""
        now_ms = _dt_to_ms(self.clock.now())
        if self._next_orderbook_due_ms is not None and now_ms < self._next_orderbook_due_ms:
            return
        poll_orderbook_once(
            self.client,
            self.store,
            symbol=self.settings.symbol,
            depth_limit=self.settings.depth_limit,
            clock=self.clock,
        )
        self._next_orderbook_due_ms = now_ms + int(self.settings.orderbook_interval_seconds * 1000)

    def _next_wait_seconds(self, elapsed_seconds: float) -> float:
        """Seconds to wait before the next cycle, given how long this one took.

        Minor fix 10 (fix round): subtracts the measured cycle duration from the nominal
        ``poll_interval_seconds`` so the real poll period does not silently drift above it.

        m4 (fourth fix round): once ``_consecutive_errors`` exceeds ``_BACKOFF_THRESHOLD_ERRORS``,
        the base interval grows exponentially (doubling per additional consecutive error, capped
        at ``_BACKOFF_CAP_SECONDS``) instead of retrying a dead/rate-limiting endpoint at the
        nominal poll interval forever -- a 403/451/DNS failure was otherwise hammered every
        ``poll_interval_seconds`` with no relief. The wait is still a ``threading.Event.wait()``
        (see ``run_forever``), so it remains interruptible by ``request_stop()`` regardless of
        length -- a long backoff never delays shutdown.

        MINOR-3 (sixth fix round): the backoff now keys off ``max(_consecutive_errors,
        _consecutive_cycle_exceptions)``, not ``_consecutive_errors`` alone. A sweep-only
        exception (e.g. a corrupt Parquet part hit by ``build_due_candles``, not an ordinary poll
        failure) kept the *poll* itself succeeding every cycle, which reset ``_consecutive_errors``
        to 0 right before the sweep raised -- only ``_consecutive_cycle_exceptions`` (which this
        method never looked at) actually climbed toward ``run_forever``'s fatal-exit threshold,
        so the process retried at the full, un-backed-off nominal poll interval the entire way
        there (measured: exits every ~95s with zero backoff).
        """
        base = self.settings.poll_interval_seconds
        effective_errors = max(self._consecutive_errors, self._consecutive_cycle_exceptions)
        if effective_errors > _BACKOFF_THRESHOLD_ERRORS:
            # m-F (fifth fix round): the exponent is capped *before* ``2 ** exponent`` is ever
            # computed -- ``5.0 * 2 ** (n - 5)`` previously overflowed (``OverflowError: int too
            # large to convert to float``) once ``n`` grew large enough (around 1029), outside any
            # try/except, which would have crashed run_forever's loop entirely. Capping the
            # exponent at ``_BACKOFF_MAX_EXPONENT`` (65536x the base interval) already exceeds
            # ``_BACKOFF_CAP_SECONDS`` for any sane ``poll_interval_seconds``, so the subsequent
            # ``min(..., _BACKOFF_CAP_SECONDS)`` is still what actually bounds the result.
            exponent = min(effective_errors - _BACKOFF_THRESHOLD_ERRORS, _BACKOFF_MAX_EXPONENT)
            base = min(base * (2**exponent), _BACKOFF_CAP_SECONDS)
        return max(0.0, base - elapsed_seconds)

    def run_forever(self) -> None:
        """Runs ``process_once`` on a timer until ``request_stop`` is set (SIGTERM/SIGINT, wired
        up in ``scripts/record_tabdeal.py``). See ``_next_wait_seconds`` for the wait's length and
        why a deadline-based ``threading.Event.wait()`` (not ``time.sleep``) is used: per PEP 475,
        ``time.sleep`` resumes after an interrupting signal rather than returning early, so a
        shutdown request arriving mid-sleep could wait out the rest of the interval; ``Event.wait``
        returns the instant ``request_stop()`` calls ``.set()``.

        m-E (fifth fix round): raises ``RecorderFatalError`` after
        ``_MAX_CONSECUTIVE_CYCLE_EXCEPTIONS`` consecutive ``process_once()`` exceptions, instead of
        looping inside a permanently broken process forever at the (backed-off) poll interval --
        this is a distinct counter from ``_consecutive_errors`` (which tracks ordinary poll
        failures ``process_once`` already handles gracefully, without raising). Letting this
        propagate out of ``run_forever`` gives the process a non-zero exit code, so Docker's
        ``restart: unless-stopped`` policy (``deploy/docker-compose.yml``) can actually restart it.
        """
        while not self._stop:
            cycle_start = time.monotonic()
            try:
                self.process_once()
            except Exception:
                logger.exception("tabdeal_recorder.cycle_failed", symbol=self.settings.symbol)
                self._consecutive_errors += 1
                self._consecutive_cycle_exceptions += 1
                if self._consecutive_cycle_exceptions >= _MAX_CONSECUTIVE_CYCLE_EXCEPTIONS:
                    raise RecorderFatalError(
                        f"{self._consecutive_cycle_exceptions} consecutive process_once() "
                        f"exceptions for symbol {self.settings.symbol!r} -- exiting so the "
                        "container restart policy can recover"
                    ) from None
            else:
                self._consecutive_cycle_exceptions = 0
            if self._stop:
                break
            elapsed = time.monotonic() - cycle_start
            remaining = self._next_wait_seconds(elapsed)
            if remaining > 0:
                self._stop_event.wait(remaining)
