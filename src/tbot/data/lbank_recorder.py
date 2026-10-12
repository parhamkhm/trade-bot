"""Minimal raw LBank market-data recorder (pivot plan, step 0).

Records, for BTC/USDT spot on LBank:

* order-book snapshots, top 20 levels per side, every 10 s;
* public trades (the endpoint returns only the latest 600, ~4 min), polled every 30 s;
* klines (1m, 1h, 1d), re-fetched over a short trailing window and upserted;
* the BTCUSDT perpetual funding rate (public endpoint; current value only, so it must be recorded).

Non-negotiables (CLAUDE.md §3.8, Parham 2026-10-10):

* raw writes go to SQLite only, one file per UTC day (``lbank-YYYY-MM-DD.sqlite``, partitioned by the
  local *receive* time), each poll's rows in one transaction;
* ingestion never depends on any downstream step: there is no candle building, Parquet or report here,
  and the heartbeat write is isolated so its failure cannot stop a stream;
* every stream runs in its own thread with its own HTTP client and SQLite connection, so one slow or
  failing endpoint never delays another (LBank answers in 1-8 s from the Turkey server).

Trade ids are UUID strings, not increasing integers, so continuity is checked by **time**: a poll is
continuous if its oldest trade is at or before the newest trade already stored; otherwise the poll is
logged with ``gap = 1``. That cursor (the newest trade time) is persisted in the ``trade_cursor`` table
of the day file every poll writes to and, atomically, in ``<data_dir>/trade_cursor.json``; it is reloaded
on start-up from today's file, the newest earlier day file or the JSON (newest wins), so a restart (also
one across midnight UTC, or after yesterday's file was archived and removed) still flags the hole it
caused. ``gap = 2`` means UNKNOWN: the poll has trades but no cursor exists at all (first poll ever, or
every source is gone), so continuity cannot be judged; ``prev_max_ts_ms`` is NULL then. (``gap`` is
NOT NULL in day files already on disk, hence a value instead of NULL.) An empty poll has ``gap = 0``.
Duplicates within a day file are dropped by the primary key; a trade received on both sides of
midnight UTC can appear in two day files and is de-duplicated downstream by id.

Trade items that cannot be parsed are never dropped silently: their raw JSON goes to
``trades_unparsed``, the poll row carries ``n_bad``, an error is logged with a sample, a poll in which
no item at all parsed counts as a failed poll, and the heartbeat exposes ``unparsed_total``,
``consecutive_bad_polls`` and ``last_new_rows_ms`` for the healthcheck.

Schema changes are additive and idempotent (``CREATE TABLE IF NOT EXISTS`` plus a guarded
``ALTER TABLE ADD COLUMN``), so day files written by older versions keep working unchanged.

Snapshot tables (depth, trade polls, funding) use a rowid key, never ``recv_ms``: two polls in the same
millisecond or a wall-clock step backwards must never drop a raw row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog

__all__ = [
    "PERP_BASE_URL",
    "SPOT_BASE_URL",
    "DayStore",
    "LBankRecorder",
    "ParsedTrades",
    "RecorderConfig",
    "StreamStatus",
    "load_trade_cursor",
    "parse_depth",
    "parse_funding",
    "parse_klines",
    "parse_trades",
    "parse_trades_checked",
]

logger = structlog.get_logger(__name__)

SPOT_BASE_URL = "https://api.lbkex.com"
PERP_BASE_URL = "https://lbkperp.lbank.com"
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS depth_snapshots (
    id INTEGER PRIMARY KEY,
    recv_ms INTEGER NOT NULL,
    server_ms INTEGER,
    symbol TEXT NOT NULL,
    bids TEXT NOT NULL,
    asks TEXT NOT NULL,
    latency_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    quote_qty TEXT,
    is_buyer_maker INTEGER,
    recv_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts_ms);
CREATE INDEX IF NOT EXISTS idx_depth_recv ON depth_snapshots(recv_ms);
CREATE TABLE IF NOT EXISTS trade_polls (
    id INTEGER PRIMARY KEY,
    recv_ms INTEGER NOT NULL,
    n_items INTEGER NOT NULL,
    n_inserted INTEGER NOT NULL,
    min_ts_ms INTEGER,
    max_ts_ms INTEGER,
    prev_max_ts_ms INTEGER,
    gap INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    n_bad INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS klines (
    interval TEXT NOT NULL,
    open_ts_s INTEGER NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    closed INTEGER NOT NULL,
    recv_ms INTEGER NOT NULL,
    PRIMARY KEY (interval, open_ts_s)
);
CREATE INDEX IF NOT EXISTS idx_trade_polls_recv ON trade_polls(recv_ms);
CREATE TABLE IF NOT EXISTS funding (
    id INTEGER PRIMARY KEY,
    recv_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    funding_rate TEXT,
    next_fee_time_ms INTEGER,
    fee_interval_s INTEGER,
    marked_price TEXT,
    index_price TEXT,
    last_price TEXT,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_recv ON funding(recv_ms);
CREATE TABLE IF NOT EXISTS errors (
    recv_ms INTEGER NOT NULL,
    stream TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_cursor (
    stream TEXT PRIMARY KEY,
    max_ts_ms INTEGER NOT NULL,
    recv_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trades_unparsed (
    id INTEGER PRIMARY KEY,
    recv_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    raw TEXT NOT NULL
);
"""

# Idempotent column additions for day files created by an older schema (see ``_migrate``).
_ADDED_COLUMNS = (("trade_polls", "n_bad", "INTEGER NOT NULL DEFAULT 0"),)

# Guards the connect + pragma + executescript + migrate sequence in `DayStore.conn`. Several stream
# threads (depth, trades, funding, each kline interval) can all open or create the *same* brand-new
# day file within the same instant, at startup or at the midnight rollover. SQLite's busy_timeout
# retries ordinary SQLITE_BUSY waits, but creating a file and switching it to WAL for the first time
# is not reliably covered by that retry on every platform (observed: concurrent first-opens on
# Windows can still raise "database is locked" even with busy_timeout set first). A single
# process-wide lock around just the bootstrap step removes the race outright; it costs nothing once
# a day file is already open, since each `DayStore` then reuses its own connection.
_OPEN_LOCK = threading.Lock()

_INSERT_DEPTH = (
    "INSERT INTO depth_snapshots (recv_ms, server_ms, symbol, bids, asks, latency_ms)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)
_INSERT_POLL = (
    "INSERT INTO trade_polls (recv_ms, n_items, n_inserted, min_ts_ms, max_ts_ms, prev_max_ts_ms, gap,"
    " latency_ms, n_bad) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_UPSERT_CURSOR = (
    "INSERT INTO trade_cursor (stream, max_ts_ms, recv_ms) VALUES ('trades', ?, ?)"
    " ON CONFLICT(stream) DO UPDATE SET max_ts_ms = excluded.max_ts_ms, recv_ms = excluded.recv_ms"
)
_INSERT_UNPARSED = "INSERT INTO trades_unparsed (recv_ms, reason, raw) VALUES (?, ?, ?)"
_RAW_MAX_CHARS = 4000
_CURSOR_FILE = "trade_cursor.json"
# trade_polls.gap values: 0 continuous (or an empty poll), 1 a hole before this poll, 2 UNKNOWN: this poll
# has trades but no cursor was available (first poll ever, or the cursor and every day file are gone).
# The column is NOT NULL in files already on disk, so "unknown" is a value, not NULL; prev_max_ts_ms is NULL.
GAP_NONE, GAP_HOLE, GAP_UNKNOWN = 0, 1, 2
_CURSOR_LOOKBACK_DAYS = 7
_INSERT_FUNDING = (
    "INSERT INTO funding (recv_ms, symbol, funding_rate, next_fee_time_ms, fee_interval_s, marked_price,"
    " index_price, last_price, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_KLINE_SECONDS = {"minute1": 60, "hour1": 3600, "day1": 86400}


# --- parsing (pure) -------------------------------------------------------------------


def _loads(body: bytes) -> Any:
    """JSON with every number parsed as Decimal, so prices are never rounded through float."""
    return json.loads(body, parse_float=Decimal, parse_int=Decimal)


def _ok_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    if str(payload.get("result", "true")).lower() != "true" or payload.get("error_code") not in (
        None,
        0,
        Decimal(0),
    ):
        raise ValueError(f"exchange error: {payload.get('error_code')} {payload.get('msg')}")
    return payload.get("data")


def _levels(raw: Any) -> list[list[str]]:
    if not isinstance(raw, list):
        raise ValueError("depth side is not a list")
    return [[str(level[0]), str(level[1])] for level in raw]


def parse_depth(payload: Any) -> tuple[list[list[str]], list[list[str]], int | None]:
    """``(bids, asks, server_ms)`` from a ``/v2/depth.do`` response; levels kept as exact strings."""
    data = _ok_payload(payload)
    if not isinstance(data, dict):
        raise ValueError("depth data is not an object")
    bids, asks = _levels(data.get("bids")), _levels(data.get("asks"))
    server = data.get("timestamp", payload.get("ts"))
    return bids, asks, int(server) if server is not None else None


TradeRow = tuple[str, int, str, str, str | None, int | None]


@dataclass(frozen=True)
class ParsedTrades:
    """Result of parsing one trades response: good rows plus the raw text of every rejected item."""

    rows: list[TradeRow]
    unparsed: list[tuple[str, str]]  # (reason, raw item as JSON text, capped)
    n_items: int


def _raw_text(item: Any) -> str:
    try:
        text = json.dumps(item, default=str)
    except (TypeError, ValueError):
        text = repr(item)
    return text[:_RAW_MAX_CHARS]


def _parse_trade_item(item: Any) -> TradeRow:
    if not isinstance(item, dict):
        raise TypeError(f"item is {type(item).__name__}, not an object")
    maker = item.get("isBuyerMaker")
    trade_id = str(item["id"])
    if not trade_id:
        raise ValueError("empty trade id")
    return (
        trade_id,
        int(item["time"]),
        str(item["price"]),
        str(item["qty"]),
        str(item["quoteQty"]) if item.get("quoteQty") is not None else None,
        int(maker) if isinstance(maker, bool) else None,
    )


def parse_trades_checked(payload: Any) -> ParsedTrades:
    """Parse a trades response; malformed items are returned in ``unparsed``, never silently dropped."""
    data = _ok_payload(payload)
    if not isinstance(data, list):
        raise ValueError("trades data is not a list")
    rows: list[TradeRow] = []
    unparsed: list[tuple[str, str]] = []
    for item in data:
        try:
            rows.append(_parse_trade_item(item))
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as exc:
            unparsed.append((f"{type(exc).__name__}: {exc}"[:200], _raw_text(item)))
    return ParsedTrades(rows, unparsed, len(data))


def parse_trades(payload: Any) -> list[TradeRow]:
    """Rows ``(trade_id, ts_ms, price, qty, quote_qty, is_buyer_maker)``; malformed items skipped.

    Convenience wrapper; the recorder uses :func:`parse_trades_checked` so rejected items are kept.
    """
    return parse_trades_checked(payload).rows


def parse_klines(payload: Any, interval: str, recv_ms: int) -> list[tuple[Any, ...]]:
    """Rows for the ``klines`` table; ``closed`` is 1 once the bar's end is at or before ``recv_ms``."""
    data = _ok_payload(payload)
    if not isinstance(data, list):
        raise ValueError("kline data is not a list")
    seconds = _KLINE_SECONDS[interval]
    rows = []
    for row in data:
        open_s = int(row[0])
        closed = 1 if (open_s + seconds) * 1000 <= recv_ms else 0
        rows.append((interval, open_s, *(str(v) for v in row[1:6]), closed, recv_ms))
    return rows


def parse_funding(payload: Any, symbol: str) -> dict[str, Any] | None:
    """The perpetual row for ``symbol`` from ``marketData``, or ``None`` if it is absent."""
    data = _ok_payload(payload)
    if not isinstance(data, list):
        raise ValueError("funding data is not a list")
    for row in data:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper():
            return row
    return None


# --- storage ---------------------------------------------------------------------------


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a day file written by an older schema up to date; additive and idempotent, never lossy."""
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # Another stream thread opened the same file a moment earlier (midnight) and won the race.
                if "duplicate column name" not in str(exc):
                    raise


def _read_cursor_file(data_dir: Path) -> int | None:
    """The cursor from ``trade_cursor.json`` (independent of the day files), or ``None``."""
    try:
        value = json.loads((data_dir / _CURSOR_FILE).read_text(encoding="utf-8"))["max_ts_ms"]
        return int(value) if not isinstance(value, bool) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cursor_file(data_dir: Path, cursor: int, recv_ms: int) -> None:
    """Atomically persist the cursor next to the day files (they may be archived and removed)."""
    path = data_dir / _CURSOR_FILE
    tmp = data_dir / f"{_CURSOR_FILE}.{os.getpid()}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"max_ts_ms": cursor, "recv_ms": recv_ms}))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_cursor(path: Path) -> int | None:
    """Newest trade timestamp recorded in one day file, or ``None`` (works on pre-cursor files too)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    try:
        try:
            row = conn.execute("SELECT max_ts_ms FROM trade_cursor WHERE stream = 'trades'").fetchone()
            if row is not None:
                return int(row[0])
        except sqlite3.OperationalError:
            pass  # file written before the cursor table existed
        row = conn.execute("SELECT MAX(max_ts_ms) FROM trade_polls").fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None
    finally:
        conn.close()


def load_trade_cursor(data_dir: Path, now_ms: int) -> int | None:
    """The persisted trade cursor: newest trade time seen before this process started.

    Looks at today's day file first, then walks back up to a week, so a restart just after midnight
    still finds the cursor written into yesterday's file. ``trade_cursor.json`` (written after every
    poll, independent of the day files) is also consulted, so a cursor survives yesterday's file having
    been archived and removed; the newest of the sources wins. ``None`` means no cursor is known.
    """
    found: list[int] = []
    for back in range(_CURSOR_LOOKBACK_DAYS + 1):
        path = data_dir / f"lbank-{_utc_day(now_ms - back * 86_400_000)}.sqlite"
        if not path.exists():
            continue
        try:
            cursor = _read_cursor(path)
        except sqlite3.Error:
            logger.exception("lbank_recorder.cursor_read_failed", file=path.name)
            continue
        if cursor is not None:
            found.append(cursor)
            break
    from_file = _read_cursor_file(data_dir)
    if from_file is not None:
        found.append(from_file)
    return max(found) if found else None


class DayStore:
    """One SQLite connection for one thread, switching to a new day file at midnight UTC."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._day: str | None = None
        self._conn: sqlite3.Connection | None = None

    def path_for(self, day: str) -> Path:
        return self._dir / f"lbank-{day}.sqlite"

    def conn(self, recv_ms: int) -> sqlite3.Connection:
        day = _utc_day(recv_ms)
        if self._conn is None or day != self._day:
            self.close()
            self._dir.mkdir(parents=True, exist_ok=True)
            with _OPEN_LOCK:  # see _OPEN_LOCK: serializes first-open/create against other threads
                conn = sqlite3.connect(str(self.path_for(day)), timeout=30.0, isolation_level=None)
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                _migrate(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn, self._day = conn, day
        return self._conn

    @contextmanager
    def transaction(self, recv_ms: int) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` on the day file for ``recv_ms``; rolls back on any error."""
        conn = self.conn(recv_ms)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def write(self, recv_ms: int, statements: list[tuple[str, list[tuple[Any, ...]]]]) -> int:
        """Run every ``(sql, rows)`` in one transaction; returns the total number of rows changed."""
        changed = 0
        with self.transaction(recv_ms) as conn:
            for sql, rows in statements:
                before = conn.total_changes
                conn.executemany(sql, rows)
                changed += conn.total_changes - before
        return changed

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# --- streams ---------------------------------------------------------------------------


@dataclass
class StreamStatus:
    last_ok_ms: int | None = None
    last_attempt_ms: int | None = None
    consecutive_errors: int = 0
    rows_total: int = 0
    last_error: str | None = None
    # trades stream only (zero / None elsewhere): parse failures and idle detection (M3)
    unparsed_total: int = 0
    consecutive_bad_polls: int = 0  # polls with unparseable items or no items at all
    last_new_rows_ms: int | None = None  # last time a poll stored at least one new trade


@dataclass(frozen=True)
class RecorderConfig:
    data_dir: Path
    symbol: str = "btc_usdt"
    perp_symbol: str = "BTCUSDT"
    depth_size: int = 20
    depth_interval_s: float = 10.0
    trades_interval_s: float = 30.0
    trades_size: int = 600
    kline_interval_s: Mapping[str, float] = field(
        default_factory=lambda: {"minute1": 60.0, "hour1": 600.0, "day1": 3600.0}
    )
    kline_lookback_bars: Mapping[str, int] = field(
        default_factory=lambda: {"minute1": 10, "hour1": 6, "day1": 3}
    )
    funding_interval_s: float = 60.0
    http_timeout_s: float = 20.0
    heartbeat_interval_s: float = 10.0


class LBankRecorder:
    """Runs one thread per stream plus a heartbeat thread until ``stop()`` is called."""

    def __init__(
        self,
        config: RecorderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._cfg = config
        self._transport = transport
        # The only place the wall clock enters (recv timestamps, heartbeat, kline window); tests inject one.
        self._now_ms: Callable[[], int] = now_ms or _now_ms
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status: dict[str, StreamStatus] = {}
        self._prev_trade_max_ms: int | None = None
        self._cursor_loaded = False
        self._persisted_cursor: int | None = None
        self._started_ms = self._now_ms()
        self._threads: list[threading.Thread] = []

    # -- lifecycle --

    def start(self) -> None:
        # the heartbeat may run before any stream has written; it must find its directory
        self._cfg.data_dir.mkdir(parents=True, exist_ok=True)
        jobs: list[tuple[str, float, Callable[[httpx.Client, DayStore], int]]] = [
            ("depth", self._cfg.depth_interval_s, self._poll_depth),
            ("trades", self._cfg.trades_interval_s, self._poll_trades),
            ("funding", self._cfg.funding_interval_s, self._poll_funding),
        ]
        for interval, period in self._cfg.kline_interval_s.items():
            jobs.append((f"kline_{interval}", period, self._make_kline_job(interval)))
        for name, period, job in jobs:
            self.status[name] = StreamStatus()
            thread = threading.Thread(
                target=self._run_stream, args=(name, period, job), name=name, daemon=True
            )
            self._threads.append(thread)
        self._threads.append(threading.Thread(target=self._run_heartbeat, name="heartbeat", daemon=True))
        for thread in self._threads:
            thread.start()
        logger.info("lbank_recorder.started", streams=sorted(self.status))

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        for thread in self._threads:
            thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- loop --

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._cfg.http_timeout_s, transport=self._transport)

    def _run_stream(self, name: str, period: float, job: Callable[[httpx.Client, DayStore], int]) -> None:
        store = DayStore(self._cfg.data_dir)
        next_due = time.monotonic()
        with self._client() as client:
            while not self._stop.is_set():
                self.run_once(name, job, client, store)
                next_due += period
                now = time.monotonic()
                if next_due < now:  # a slow call: skip missed slots instead of bursting
                    next_due = now + period - ((now - next_due) % period)
                self._stop.wait(max(0.0, next_due - time.monotonic()))
        store.close()

    def run_once(
        self, name: str, job: Callable[[httpx.Client, DayStore], int], client: httpx.Client, store: DayStore
    ) -> None:
        """One attempt of one stream; never raises (errors are counted, logged and stored)."""
        status = self.status[name]
        status.last_attempt_ms = self._now_ms()
        try:
            rows = job(client, store)
        except Exception as exc:
            with self._lock:
                status.consecutive_errors += 1
                status.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("lbank_recorder.poll_failed", stream=name, error=status.last_error)
            self._store_error(store, name, status.last_error)
            return
        with self._lock:
            status.last_ok_ms = self._now_ms()
            status.consecutive_errors = 0
            status.rows_total += rows

    def _store_error(self, store: DayStore, name: str, message: str) -> None:
        try:
            recv = self._now_ms()
            store.write(recv, [("INSERT INTO errors VALUES (?, ?, ?)", [(recv, name, message)])])
        except Exception:
            logger.exception("lbank_recorder.error_log_failed", stream=name)

    def _get(self, client: httpx.Client, url: str, params: Mapping[str, Any]) -> tuple[Any, int, int]:
        started = time.monotonic()
        response = client.get(url, params=dict(params))
        response.raise_for_status()
        recv = self._now_ms()
        return _loads(response.content), recv, int((time.monotonic() - started) * 1000)

    # -- jobs --

    def _poll_depth(self, client: httpx.Client, store: DayStore) -> int:
        payload, recv, latency = self._get(
            client, f"{SPOT_BASE_URL}/v2/depth.do", {"symbol": self._cfg.symbol, "size": self._cfg.depth_size}
        )
        bids, asks, server = parse_depth(payload)
        row = (recv, server, self._cfg.symbol, json.dumps(bids), json.dumps(asks), latency)
        return store.write(
            recv,
            [
                (
                    _INSERT_DEPTH,
                    [row],
                )
            ],
        )

    def _poll_trades(self, client: httpx.Client, store: DayStore) -> int:
        payload, recv, latency = self._get(
            client,
            f"{SPOT_BASE_URL}/v2/supplement/trades.do",
            {"symbol": self._cfg.symbol, "size": self._cfg.trades_size},
        )
        try:
            parsed = parse_trades_checked(payload)
        except ValueError as exc:  # envelope error: keep the evidence before the poll is reported failed
            self._store_envelope_error(store, recv, payload, exc)
            raise
        rows = parsed.rows
        times = [r[1] for r in rows]
        lo, hi = (min(times), max(times)) if times else (None, None)
        prev = self._trade_cursor(recv)
        if lo is None:
            gap = GAP_NONE
        elif prev is None:
            gap = GAP_UNKNOWN
        else:
            gap = GAP_HOLE if lo > prev else GAP_NONE
        cursor = hi if prev is None else (prev if hi is None else max(prev, hi))
        trade_rows = [(*r, recv) for r in rows]
        n_bad = len(parsed.unparsed)
        with store.transaction(recv) as conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?)", trade_rows)
            changed = conn.total_changes - before
            conn.execute(_INSERT_POLL, (recv, parsed.n_items, changed, lo, hi, prev, gap, latency, n_bad))
            conn.executemany(_INSERT_UNPARSED, [(recv, reason, raw) for reason, raw in parsed.unparsed])
            if cursor is not None:  # persisted in the day file the poll lands in, so it survives restarts
                conn.execute(_UPSERT_CURSOR, (cursor, recv))
        self._prev_trade_max_ms = cursor
        self._persist_cursor_file(cursor, recv)
        if gap == GAP_HOLE:
            logger.warning("lbank_recorder.trade_gap", prev_max_ms=prev, min_ms=lo)
        elif gap == GAP_UNKNOWN:
            logger.info("lbank_recorder.trade_gap_unknown", min_ms=lo)  # no cursor: first poll ever, or lost
        self._note_trade_poll(parsed, changed, recv)
        return changed

    def _store_envelope_error(self, store: DayStore, recv: int, payload: Any, exc: Exception) -> None:
        """Keep the capped raw body of a response the exchange (or a proxy) answered in a wrong shape."""
        try:
            raw = _raw_text(payload)
            store.write(recv, [(_INSERT_UNPARSED, [(recv, "envelope", raw)])])
        except Exception:
            logger.exception("lbank_recorder.envelope_store_failed")
        logger.error("lbank_recorder.trades_envelope", error=str(exc)[:200], sample=_raw_text(payload)[:300])

    def _persist_cursor_file(self, cursor: int | None, recv: int) -> None:
        """Best effort: ``trade_cursor.json`` outlives day files that get archived and removed."""
        if cursor is None or cursor == self._persisted_cursor:
            return
        try:
            _write_cursor_file(self._cfg.data_dir, cursor, recv)
            self._persisted_cursor = cursor
        except OSError:
            logger.exception("lbank_recorder.cursor_file_failed")  # the day file still holds the cursor

    def _trade_cursor(self, recv_ms: int) -> int | None:
        """In-memory cursor, loaded once from the day files so a restart does not hide a gap."""
        if not self._cursor_loaded:
            try:
                self._prev_trade_max_ms = load_trade_cursor(self._cfg.data_dir, recv_ms)
                self._cursor_loaded = True
            except (sqlite3.Error, OSError):
                logger.exception("lbank_recorder.cursor_load_failed")  # ingestion goes on without it
        return self._prev_trade_max_ms

    def _note_trade_poll(self, parsed: ParsedTrades, changed: int, recv_ms: int) -> None:
        """Account for parse failures and empty polls; raises when a poll that had items saved none."""
        status = self.status.get("trades")
        n_bad = len(parsed.unparsed)
        if status is not None:
            with self._lock:
                status.unparsed_total += n_bad
                bad = n_bad > 0 or parsed.n_items == 0
                status.consecutive_bad_polls = status.consecutive_bad_polls + 1 if bad else 0
                if changed > 0:
                    status.last_new_rows_ms = recv_ms
        if n_bad:
            logger.error(
                "lbank_recorder.trades_unparsed",
                n_bad=n_bad,
                n_items=parsed.n_items,
                sample=parsed.unparsed[0][1][:300],
                reason=parsed.unparsed[0][0],
            )
        if parsed.n_items > 0 and not parsed.rows:
            # nothing was saved as a trade: never report this poll as a success
            raise ValueError(f"all {parsed.n_items} trade items unparseable: {parsed.unparsed[0][0]}")

    def _make_kline_job(self, interval: str) -> Callable[[httpx.Client, DayStore], int]:
        def job(client: httpx.Client, store: DayStore) -> int:
            bars = self._cfg.kline_lookback_bars[interval]
            start = self._now_ms() // 1000 - bars * _KLINE_SECONDS[interval]
            payload, recv, _ = self._get(
                client,
                f"{SPOT_BASE_URL}/v2/kline.do",
                {"symbol": self._cfg.symbol, "size": bars + 1, "type": interval, "time": start},
            )
            rows = parse_klines(payload, interval, recv)
            return store.write(
                recv, [("INSERT OR REPLACE INTO klines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)]
            )

        return job

    def _poll_funding(self, client: httpx.Client, store: DayStore) -> int:
        payload, recv, _ = self._get(
            client, f"{PERP_BASE_URL}/cfd/openApi/v1/pub/marketData", {"productGroup": "SwapU"}
        )
        row = parse_funding(payload, self._cfg.perp_symbol)
        if row is None:
            raise ValueError(f"{self._cfg.perp_symbol} missing from marketData")

        def text(key: str) -> str | None:
            return str(row[key]) if row.get(key) is not None else None

        def integer(key: str) -> int | None:
            return int(row[key]) if row.get(key) is not None else None

        values = (
            recv,
            self._cfg.perp_symbol,
            text("fundingRate"),
            integer("nextFeeTime"),
            integer("positionFeeTime"),
            text("markedPrice"),
            text("underlyingPrice"),
            text("lastPrice"),
            json.dumps(row, default=str),
        )
        return store.write(
            recv,
            [
                (
                    _INSERT_FUNDING,
                    [values],
                )
            ],
        )

    # -- heartbeat (isolated: its failure never touches a stream) --

    def heartbeat_payload(self) -> dict[str, Any]:
        with self._lock:
            streams = {
                name: {
                    "last_ok_ms": s.last_ok_ms,
                    "last_attempt_ms": s.last_attempt_ms,
                    "consecutive_errors": s.consecutive_errors,
                    "rows_total": s.rows_total,
                    "last_error": s.last_error,
                    "unparsed_total": s.unparsed_total,
                    "consecutive_bad_polls": s.consecutive_bad_polls,
                    "last_new_rows_ms": s.last_new_rows_ms,
                }
                for name, s in self.status.items()
            }
        return {"written_ms": self._now_ms(), "started_ms": self._started_ms, "streams": streams}

    def write_heartbeat(self) -> None:
        path = self._cfg.data_dir / "heartbeat.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.heartbeat_payload()), encoding="utf-8")
        tmp.replace(path)

    def _run_heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                self.write_heartbeat()
            except Exception:
                logger.exception("lbank_recorder.heartbeat_failed")
            self._stop.wait(self._cfg.heartbeat_interval_s)


def _now_ms() -> int:
    return int(time.time() * 1000)
