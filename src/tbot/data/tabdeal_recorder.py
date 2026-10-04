"""Tabdeal public-trades recorder, order-book snapshotter and candle-sweep driver.

Tabdeal has no kline endpoint (CLAUDE.md section 6), so this module polls the public ``/trades``
endpoint, persists every trade exactly once (SQLite, dedupe by trade id), detects real id
discontinuities, and periodically asks ``tbot.data.candles`` to emit any 1h candle that is now
due. It also polls the public ``/depth`` endpoint on its own interval and stores a
cumulative-depth snapshot (decision D-019) for phase 3's execution-cost model.

Reuse, not reimplementation: the depth/trade-window maths (``best_bid_ask``,
``spread_bps_and_pct``, ``cumulative_depth``, ``is_saturated``, ``parse_depth_levels``) is
imported unchanged from ``tbot.data.depth`` -- a sibling module shared with
``scripts/tabdeal_probe.py`` (decision D-025). It used to be imported from the probe script
itself, which was the wrong dependency direction for a library module; ``tbot.data.depth`` is now
the single implementation both sides import.

Saturation semantics (decision D-024, correcting the original design): Tabdeal's ``/trades`` is a
*recent-trades* endpoint, so ``count == limit`` (what ``is_saturated`` checks) is true on
essentially every poll once enough history exists and carries no information about whether a
trade was actually missed. ``saturated`` is still stored on every ``poll_log`` row for raw
fidelity, but it is **not** logged as a warning and **not** used to mark a candle incomplete. The
two metrics that matter operationally, both stored on ``poll_log``, are:

* ``coverage_ratio = window_span_seconds / poll_interval_seconds`` -- how much margin the
  returned window gives before it could plausibly evict an unseen trade. A poll is *at risk* when
  ``coverage_ratio < 3``; that is what actually gets logged as a warning here.
* a real trade-id gap (the lowest id in a poll exceeds ``last_stored_id + 1``) -- genuine data
  loss, and the only thing (together with an empty-hour gap) that marks a candle
  ``complete=False``.

Causality / no forward-fill: an hour with zero recorded trades produces **no** candle, only a
``gaps`` row (``reason="no_trades_in_hour"``) -- see ``build_due_candles``.

Second fix round, MAJOR-1: ``parse_trade_item`` used to reject any ``price``/``qty`` that was not
already a JSON ``str``. That guard predates decision D-031, which changed ``TabdealClient`` to
decode response bodies with ``json.loads(..., parse_float=Decimal)`` -- an unquoted JSON number in
``/trades`` now arrives here as an exact ``Decimal``, and the old guard silently dropped every such
trade (``n_trades=0`` with no warning, a green healthcheck, and a healthy-looking empty-hour log
once an hour). This module now accepts ``str | Decimal`` for price/qty (reusing
``tbot.data.depth.to_decimal``, which already does this losslessly) and still rejects a bare
``float`` outright, since ``str()`` on a float that round-tripped through binary floating point is
not the exchange's exact decimal literal. A poll whose response was non-empty but from which
nothing parsed now also logs loudly (``tabdeal_recorder.unparsable_trades``) and records
``n_items_received`` on ``poll_log`` so "exchange returned nothing" and "we dropped everything" are
distinguishable after the fact.

Second fix round, MAJOR-2: ``RecorderStore.__init__`` now migrates an existing (pre-fix) SQLite
file by adding any column the current schema expects but the on-disk table does not yet have
(``window_span_seconds``/``coverage_ratio``/``n_items_received`` on ``poll_log``,
``hour_close_ms`` on ``gaps``) -- ``CREATE TABLE IF NOT EXISTS`` alone is a no-op against an
existing table and previously left an old database missing those columns, so the first
``record_poll`` call after opening it raised ``OperationalError`` and ``run_forever``'s broad
``except Exception`` turned that into a silent crash loop (trades still got inserted by
``insert_trades``, which ran first, but no poll log, candle, or heartbeat was ever written again).

Third fix round: a non-list HTTP-200 body (MAJOR M-A) used to be indistinguishable from a
genuinely empty response -- ``poll_trades_once`` now logs ``unexpected_body_type`` and reports the
outcome as not ok, so ``_consecutive_errors`` climbs toward the healthcheck threshold. A partial
mid-batch parse failure (MAJOR M-B) used to warn only when *every* item failed and was invisible to
gap detection -- the warning now fires on any shortfall, and the affected hour(s) are recorded as a
``partial_unparsable`` gap so the sweep builds them ``complete=False`` (see ``has_hour_gap``). A
non-finite/non-positive price or non-finite/negative qty (reported as MINOR-4, treated as MAJOR) is
now rejected in ``parse_trade_item`` before it is ever persisted, with a second, defensive filter in
``tbot.data.candles.build_candle`` in case a poisoned row already reached the database -- previously
such a row raised ``decimal.InvalidOperation`` inside ``process_once`` on every single cycle
forever, a permanent wedge needing manual database surgery to clear. ``_EXPECTED_COLUMNS`` (MINOR-5)
is now derived by executing ``_SCHEMA_SQL`` into an in-memory connection and reading back
``PRAGMA table_info``, rather than a hand-maintained duplicate that could silently drift out of sync
with the schema above. Opening a database at a schema version newer than this code supports
(MINOR-6) now raises ``RecorderSchemaVersionError`` instead of silently "downgrading" it, and a
corrupt database (MINOR-7) now raises ``RecorderDatabaseError`` naming the path instead of a bare
``sqlite3.DatabaseError``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
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
from tbot.execution.tabdeal_client import TabdealClient

__all__ = [
    "HeartbeatState",
    "OrderbookRow",
    "PollOutcome",
    "RecorderDatabaseError",
    "RecorderSchemaVersionError",
    "RecorderSettings",
    "RecorderStore",
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
    hour_close_ms INTEGER
);

CREATE TABLE IF NOT EXISTS sweep_cursor (
    symbol TEXT PRIMARY KEY,
    last_swept_close_ms INTEGER NOT NULL
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


def _dt_to_ms(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds() * 1000)


# ---------------------------------------------------------------------------------
# schema migration (MAJOR-2, second fix round)
# ---------------------------------------------------------------------------------

# Current schema version. Bumped whenever a column is added to an existing table below --
# ``PRAGMA user_version`` is set to this after every successful migration, purely as a readable
# marker for operators inspecting the file; the actual repair logic is column-presence based (see
# ``_migrate_schema``) so it is correct even against a database several fix-rounds old.
_SCHEMA_VERSION = 2


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


def _column_specs(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """``{column_name: declared_type}`` for ``table``, via ``PRAGMA table_info`` -- empty if the
    table does not exist in ``conn``."""
    return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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

    Column additions are nullable with no default: existing rows simply get ``NULL`` for a column
    that did not exist when they were written, which is exactly the "unknown for old rows" meaning
    those columns are supposed to carry. Correct even against a database only *some* of whose
    tables were previously migrated (a partially-migrated database): each table's missing columns
    are computed independently against the reference schema, not against a single global flag.
    """
    conn.executescript(_SCHEMA_SQL)
    for table, expected_columns in _reference_schema_columns().items():
        existing = _column_specs(conn, table)
        for name, column_type in expected_columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


# ---------------------------------------------------------------------------------
# pure parsing helpers
# ---------------------------------------------------------------------------------


def parse_trade_item(item: Any) -> candles_mod.Trade | None:
    """Parse one ``/trades`` response item. Returns ``None`` (never raises) on anything
    malformed -- a single bad row must not abort the whole poll.

    MAJOR-1 (second fix round): ``price``/``qty`` may be a JSON string (Tabdeal's documented
    shape) **or** an already-exact ``Decimal`` -- decision D-031 changed ``TabdealClient`` to
    decode response bodies with ``json.loads(..., parse_float=Decimal)``, so an unquoted JSON
    number now arrives here as a ``Decimal``, not a ``float``. The old guard rejected anything that
    was not already a ``str`` and therefore silently dropped every trade once D-031 landed. A bare
    ``float`` is still rejected outright (and always will be): ``str()`` on a float that
    round-tripped through binary floating point is not the exchange's exact decimal literal, and
    there is no way to recover it from here. Parsing itself is delegated to
    ``tbot.data.depth.to_decimal``, which already implements exactly this str-or-Decimal
    acceptance for the probe's ``/depth`` levels.
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
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    # MAJOR, third fix round (reported as MINOR-4): a quoted "NaN"/"Infinity" price parses to a
    # valid-but-meaningless Decimal and used to be persisted as-is -- candles.py's max()/min() then
    # raised decimal.InvalidOperation on it inside process_once every single cycle thereafter (a
    # permanent wedge, since the poisoned row is already stored). Same guard
    # tbot.data.depth.parse_depth_levels already applies to order-book levels.
    if not price.is_finite() or price <= 0 or not qty.is_finite() or qty < 0:
        return None
    is_buyer_maker = bool(item.get("isBuyerMaker", False))
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

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RecorderStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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

    def record_gap(
        self,
        *,
        detected_ts_ms: int,
        from_id: int | None,
        to_id: int | None,
        reason: str,
        hour_close_ms: int | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason, hour_close_ms) VALUES (?, ?, ?, ?, ?)",
            (detected_ts_ms, from_id, to_id, reason, hour_close_ms),
        )

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
        """
        self.record_gap(
            detected_ts_ms=detected_ts_ms,
            from_id=None,
            to_id=None,
            reason=reason,
            hour_close_ms=close_ms,
        )
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
        """``(from_id, to_id, reason, hour_close_ms)`` per row. ``hour_close_ms`` is only set on
        ``no_trades_in_hour`` rows (MINOR-8); it is ``NULL`` on ``id_discontinuity`` rows, which
        identify themselves via ``from_id``/``to_id`` instead."""
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
    """Outcome of one ``/trades`` poll, after persistence."""

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


def _window_span_seconds(trades: Sequence[candles_mod.Trade]) -> float | None:
    """Span, in seconds, between the earliest and latest trade in one poll's response.

    ``None`` when fewer than two trades were returned -- there is no window to measure.
    """
    if len(trades) < 2:
        return None
    times = [t.ts_ms for t in trades]
    return (max(times) - min(times)) / 1000.0


def poll_trades_once(
    client: TabdealClient,
    store: RecorderStore,
    *,
    symbol: str,
    limit: int,
    poll_interval_seconds: float,
    clock: Clock,
) -> PollOutcome:
    """One ``/trades`` poll: fetch, parse, dedupe-insert, detect an id discontinuity against the
    previously-stored max id, and record the poll (plus any detected gap).

    Resumption: ``prev_max`` is read from the store itself (not from any in-memory state), so a
    freshly restarted process resumes exactly where the previous one left off -- a batch that
    overlaps already-stored ids inserts zero new rows (``INSERT OR IGNORE``) and raises no gap.

    Saturation (D-024): ``saturated`` (``count == limit``) is recorded on every row for raw
    fidelity but is never itself a warning or a reason to mark anything incomplete -- on a
    recent-trades endpoint it is true on almost every poll and says nothing about data loss. The
    metric that is actually alerted on is ``coverage_ratio = window_span_seconds /
    poll_interval_seconds``: a poll is at risk of having evicted an unseen trade when that ratio
    drops below 3, which is what triggers the warning here.

    MAJOR-1 (second fix round): ``n_items_received`` (the raw response length, before parsing) is
    tracked separately from ``n_trades`` (the number that actually parsed) and persisted on
    ``poll_log``. When the response was non-empty but every item failed to parse, that is logged
    loudly (``tabdeal_recorder.unparsable_trades``) instead of looking identical to "the exchange
    returned nothing this poll" -- the two used to be indistinguishable after the fact.

    MAJOR M-A (third fix round): an HTTP-200 whose body is not a list at all (a Binance-style
    ``{"code":1101,"msg":...}`` error wrapper served with status 200, or a shape change to
    ``{"trades":[...]}``) used to compute ``n_items_received = 0`` via the same
    ``isinstance(result.body, list)`` guard that also gates parsing -- indistinguishable from a
    genuinely empty response, with no warning, no error, and ``PollOutcome.ok=True`` forever
    (``_consecutive_errors`` never climbs, the healthcheck never trips). This now mirrors what
    ``poll_orderbook_once`` already does for ``/depth``: log
    ``tabdeal_recorder.unexpected_body_type`` and report the outcome as *not* ok, so
    ``_consecutive_errors`` climbs exactly as it would for an HTTP error.

    MAJOR M-B (third fix round): the unparsable warning used to fire only when ``n_trades == 0``,
    so a *partial* drop (e.g. one item in a multi-item batch has a bare float price) produced
    ``n_items_received=2, n_trades=1`` with no warning at all -- invisible both operationally and
    to gap detection, since the id-discontinuity check only compares this poll's minimum id to the
    stored maximum and never notices an id missing *inside* a batch. The warning now fires whenever
    ``n_trades < n_items_received`` (total or partial), and the hour(s) the surviving parsed trades
    fall into (or, if every item in the poll was unparsable, the hour containing the poll itself)
    are recorded as a ``gaps`` row and therefore built ``complete=False`` -- see
    ``is_candle_complete``.
    """
    prev_max = store.max_trade_id()
    result = client.trades(symbol, limit=limit)
    now_ms = _dt_to_ms(clock.now())

    body_is_list = isinstance(result.body, list)
    unexpected_body_type = result.ok and not body_is_list
    ok = result.ok and not unexpected_body_type

    n_items_received = len(result.body) if result.ok and body_is_list else 0
    trades: list[candles_mod.Trade] = []
    if result.ok and body_is_list:
        for item in result.body:
            trade = parse_trade_item(item)
            if trade is not None:
                trades.append(trade)

    n_trades = len(trades)
    first_id = min((t.trade_id for t in trades), default=None)
    last_id = max((t.trade_id for t in trades), default=None)
    saturated = is_saturated(limit, n_trades)
    window_span_seconds = _window_span_seconds(trades)
    coverage_ratio = window_span_seconds / poll_interval_seconds if window_span_seconds is not None else None

    inserted = store.insert_trades(trades, recorded_ts_ms=now_ms) if trades else 0

    gap: tuple[int, int] | None = None
    if result.ok and first_id is not None and prev_max is not None and first_id > prev_max + 1:
        gap = (prev_max, first_id)
        store.record_gap(detected_ts_ms=now_ms, from_id=prev_max, to_id=first_id, reason="id_discontinuity")
        logger.warning("tabdeal_recorder.gap_detected", symbol=symbol, from_id=prev_max, to_id=first_id)

    if n_trades < n_items_received:
        # MAJOR M-B: mark every hour touched by this poll's surviving trades as having a known
        # shortfall, so the candle sweep builds it complete=False rather than silently treating a
        # partial drop as a fully-covered hour. If nothing at all parsed, there is no trade
        # timestamp to anchor on -- fall back to the hour containing the poll itself, since a
        # recent-trades endpoint's response is always close to "now".
        affected_close_ms = {candles_mod.hour_bounds_ms(t.ts_ms)[1] for t in trades}
        if not affected_close_ms:
            affected_close_ms = {candles_mod.hour_bounds_ms(now_ms)[1]}
        for close_ms in sorted(affected_close_ms):
            store.record_gap(
                detected_ts_ms=now_ms,
                from_id=None,
                to_id=None,
                reason="partial_unparsable",
                hour_close_ms=close_ms,
            )

    store.record_poll(
        poll_ts_ms=now_ms,
        first_id=first_id,
        last_id=last_id,
        n_trades=n_trades,
        saturated=saturated,
        http_status=result.status_code,
        latency_ms=result.latency_ms,
        window_span_seconds=window_span_seconds,
        coverage_ratio=coverage_ratio,
        n_items_received=n_items_received,
    )
    if unexpected_body_type:
        logger.error(
            "tabdeal_recorder.unexpected_body_type",
            symbol=symbol,
            body_type=type(result.body).__name__,
        )
    if n_trades < n_items_received:
        logger.warning(
            "tabdeal_recorder.unparsable_trades",
            symbol=symbol,
            received=n_items_received,
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
    if not result.ok:
        logger.warning(
            "tabdeal_recorder.poll_failed", symbol=symbol, status=result.status_code, error=result.error
        )

    return PollOutcome(
        ok=ok,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        n_trades=n_trades,
        n_items_received=n_items_received,
        first_id=first_id,
        last_id=last_id,
        saturated=saturated,
        window_span_seconds=window_span_seconds,
        coverage_ratio=coverage_ratio,
        inserted=inserted,
        gap=gap,
        error=result.error,
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
    or an empty/one-sided book -- no row is written in that case, there is nothing to measure."""
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
    spread_bps, _spread_pct = spread_bps_and_pct(best_bid, best_ask)
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
    """``False`` when a real trade-id gap overlaps the bucket -- never from the uninformative
    ``saturated`` flag (decision D-024). An empty-hour gap never reaches this function at all:
    ``build_due_candles`` skips writing a candle for that hour in the first place.

    MAJOR M4 (fix round): also ``False`` when the store has no recorded coverage of the bucket's
    *start* -- i.e. the earliest trade ever recorded landed after ``open_ms``. On a fresh
    database there is no ``id_discontinuity`` gap row to catch this (there is nothing before the
    first trade to be discontinuous with), so without this check the very first candle after a
    cold start was written ``complete=True`` even though recording plainly started partway
    through that hour (measured: first trade at 00:30, bar for 01:00 written complete=True with
    n_trades=1) -- exactly what G1b's ">=99% complete" gate and the phase-3 basis model trust.

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


def build_due_candles(
    store: RecorderStore,
    *,
    symbol: str,
    parquet_root: Path,
    grace_period_seconds: float,
    clock: Clock,
) -> list[candles_mod.TabdealCandleRecord]:
    """Write every hourly candle that is now due (closed, past its grace period, not yet on
    disk). An hour with zero recorded trades writes a ``gaps`` row instead of a forward-filled
    bar -- it is simply skipped, never synthesised."""
    now_ms = _dt_to_ms(clock.now())
    last_written_ms = candles_mod.last_written_close_ms(parquet_root, symbol)
    last_swept_ms = store.last_swept_close_ms(symbol)
    # MAJOR M3: the resumption cursor is the max of "last candle actually written" and "last
    # empty hour already swept" -- using only the former re-walks every trailing empty hour on
    # every single poll forever, since an empty hour never writes a candle to advance it.
    candidates = [v for v in (last_written_ms, last_swept_ms) if v is not None]
    last_close_ms = max(candidates) if candidates else None
    earliest_ms = store.earliest_trade_ts_ms()
    written: list[candles_mod.TabdealCandleRecord] = []
    for open_ms, close_ms in candles_mod.pending_hour_bounds(
        last_written_close_ms=last_close_ms,
        earliest_open_ms=earliest_ms,
        now_ms=now_ms,
        grace_period_seconds=grace_period_seconds,
    ):
        trades = store.trades_in_range(open_ms, close_ms)
        if not trades:
            store.record_empty_hour_gap(symbol=symbol, close_ms=close_ms, detected_ts_ms=now_ms)
            logger.warning("tabdeal_recorder.empty_hour", symbol=symbol, hour_close_ms=close_ms)
            continue
        complete = is_candle_complete(store, open_ms, close_ms)
        record = candles_mod.build_candle(trades, open_ms, close_ms, complete=complete)
        if record is None:
            # ``trades`` is non-empty here (checked above) and already window-filtered by
            # ``trades_in_range``, so the only way ``build_candle`` can still return ``None`` is its
            # defensive poisoned-row guard filtering out every trade (a non-finite/non-positive
            # price or negative qty that reached the database before ``parse_trade_item``'s own
            # guard existed). Third fix round: this is given the exact same treatment as an empty
            # hour -- a ``poisoned_trades`` gap row plus a sweep-cursor advance -- rather than a
            # bare ``continue``, which left the hour silently un-written AND un-advanced, so
            # ``pending_hour_bounds`` re-discovered it as "due" and re-attempted it on every single
            # future poll forever (the same re-walk-forever shape MAJOR M3 already fixed for empty
            # hours).
            store.record_hour_gap(
                symbol=symbol, close_ms=close_ms, detected_ts_ms=now_ms, reason="poisoned_trades"
            )
            logger.warning("tabdeal_recorder.poisoned_hour", symbol=symbol, hour_close_ms=close_ms)
            continue
        candles_mod.write_candle(parquet_root, symbol, record)
        written.append(record)
        logger.info(
            "tabdeal_recorder.candle_written",
            symbol=symbol,
            ts=record.ts.isoformat(),
            n_trades=record.n_trades,
            complete=record.complete,
        )
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


def write_heartbeat(path: Path, state: HeartbeatState) -> None:
    """Rewrite the heartbeat file. The deployment's HEALTHCHECK only looks at this file's mtime
    (deploy/Dockerfile), so it must be rewritten on every poll, even one with zero new trades."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_poll_ts": state.last_poll_ts.isoformat(),
        "last_trade_id": state.last_trade_id,
        "n_trades_total": state.n_trades_total,
        "consecutive_errors": state.consecutive_errors,
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
    a code edit."""

    symbol: str = "BTCUSDT"
    trades_limit: int = 500
    poll_interval_seconds: float = 5.0
    orderbook_interval_seconds: float = 60.0
    grace_period_seconds: float = 60.0
    depth_limit: int = 100

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in ("trades_limit", "depth_limit"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        for name in ("poll_interval_seconds", "orderbook_interval_seconds", "grace_period_seconds"):
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
    _next_orderbook_due_ms: int | None = field(default=None, init=False)
    _stop: bool = field(default=False, init=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)

    def request_stop(self) -> None:
        self._stop = True
        self._stop_event.set()

    def close(self) -> None:
        self.store.close()

    def process_once(self) -> PollOutcome:
        outcome = poll_trades_once(
            self.client,
            self.store,
            symbol=self.settings.symbol,
            limit=self.settings.trades_limit,
            poll_interval_seconds=self.settings.poll_interval_seconds,
            clock=self.clock,
        )
        self._consecutive_errors = 0 if outcome.ok else self._consecutive_errors + 1

        build_due_candles(
            self.store,
            symbol=self.settings.symbol,
            parquet_root=self.parquet_root,
            grace_period_seconds=self.settings.grace_period_seconds,
            clock=self.clock,
        )

        now_ms = _dt_to_ms(self.clock.now())
        if self._next_orderbook_due_ms is None or now_ms >= self._next_orderbook_due_ms:
            poll_orderbook_once(
                self.client,
                self.store,
                symbol=self.settings.symbol,
                depth_limit=self.settings.depth_limit,
                clock=self.clock,
            )
            self._next_orderbook_due_ms = now_ms + int(self.settings.orderbook_interval_seconds * 1000)

        write_heartbeat(
            self.heartbeat_file,
            HeartbeatState(
                last_poll_ts=self.clock.now(),
                last_trade_id=self.store.max_trade_id(),
                n_trades_total=self.store.total_trade_count(),
                consecutive_errors=self._consecutive_errors,
            ),
        )
        return outcome

    def run_forever(self) -> None:
        """Minor fix 10 (fix round): two problems with the old ``time.sleep(poll_interval_seconds)``.

        1. It ignored how long the cycle itself took, so the *real* poll period drifted above
           ``poll_interval_seconds`` while ``coverage_ratio`` kept being computed against the
           nominal value -- flattering the D-024 risk metric exactly when the recorder is running
           slow. Fixed by subtracting the measured cycle duration from the wait.
        2. Per PEP 475, ``time.sleep`` resumes after an interrupting signal (SIGTERM/SIGINT)
           rather than returning early, so a shutdown request arriving mid-sleep could still wait
           out the rest of the interval. A deadline-based ``threading.Event.wait()`` returns the
           instant ``request_stop()`` calls ``.set()``, so shutdown is immediate.
        """
        while not self._stop:
            cycle_start = time.monotonic()
            try:
                self.process_once()
            except Exception:
                logger.exception("tabdeal_recorder.cycle_failed", symbol=self.settings.symbol)
                self._consecutive_errors += 1
            if self._stop:
                break
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, self.settings.poll_interval_seconds - elapsed)
            if remaining > 0:
                self._stop_event.wait(remaining)
