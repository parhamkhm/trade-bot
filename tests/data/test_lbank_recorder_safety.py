"""Review-round tests for the LBank recorder: parse failures (M3), persisted trade cursor (M4), migration."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from tbot.data import lbank_recorder as lr
from tbot.data.lbank_recorder import (
    SPOT_BASE_URL,
    DayStore,
    LBankRecorder,
    load_trade_cursor,
    parse_trades_checked,
)
from tests.data.test_lbank_recorder import TRADES, _recorder, _rows

TRADES_URL = f"{SPOT_BASE_URL}/v2/supplement/trades.do"
DAY_MS = 86_400_000
T0 = 1_791_637_460_000  # 2026-10-10 12:44:20 UTC
MIDNIGHT = (T0 // DAY_MS + 1) * DAY_MS  # 2026-10-11 00:00:00 UTC


def _envelope(items: list[Any]) -> dict[str, Any]:
    return {"result": "true", "error_code": 0, "data": items}


def _trade(trade_id: str, time_ms: int) -> dict[str, Any]:
    return {"id": trade_id, "time": time_ms, "price": 1.5, "qty": 2, "quoteQty": 3, "isBuyerMaker": False}


def _poll(rec: LBankRecorder, tmp_path: Path, body: dict[str, Any], at_ms: int, monkeypatch: Any) -> None:
    monkeypatch.setattr(lr, "_now_ms", lambda: at_ms)
    with respx.mock:
        respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
        store = DayStore(tmp_path)
        with httpx.Client() as client:
            rec.run_once("trades", rec._poll_trades, client, store)
        store.close()


# --- M3: parse failures are counted, kept and surfaced -----------------------------------------


def test_parse_trades_checked_keeps_the_raw_text_of_every_rejected_item() -> None:
    items: list[Any] = [
        _trade("ok", 1000),
        {"id": "no-time", "price": 1, "qty": 1},
        "just a string",
        {"id": "", "time": 5, "price": 1, "qty": 1},
        {"id": "bad-time", "time": "soon", "price": 1, "qty": 1},
        {"id": "nan", "time": Decimal("NaN"), "price": 1, "qty": 1},
    ]
    parsed = parse_trades_checked(_envelope(items))
    assert [r[0] for r in parsed.rows] == ["ok"]
    assert parsed.n_items == 6 and len(parsed.unparsed) == 5
    reasons = [reason for reason, _ in parsed.unparsed]
    assert any("KeyError" in r for r in reasons) and any("empty trade id" in r for r in reasons)
    assert json.loads(parsed.unparsed[0][1])["id"] == "no-time"
    assert parsed.unparsed[1][1] == '"just a string"'


def test_partial_garbage_stores_good_rows_raw_garbage_and_logs_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _recorder(tmp_path)
    body = _envelope([_trade("good", T0 - 1000), {"id": "broken", "time": None}])
    with capture_logs() as logs:
        _poll(rec, tmp_path, body, T0, monkeypatch)
    assert _rows(tmp_path, "SELECT trade_id FROM trades") == [("good",)]
    unparsed = _rows(tmp_path, "SELECT reason, raw FROM trades_unparsed")
    assert len(unparsed) == 1 and json.loads(unparsed[0][1])["id"] == "broken"
    assert _rows(tmp_path, "SELECT n_items, n_inserted, n_bad FROM trade_polls") == [(2, 1, 1)]
    errors = [e for e in logs if e["event"] == "lbank_recorder.trades_unparsed"]
    assert errors and errors[0]["log_level"] == "error" and "broken" in errors[0]["sample"]
    status = rec.status["trades"]
    assert status.consecutive_errors == 0  # something was saved: the poll itself succeeded
    assert status.unparsed_total == 1 and status.consecutive_bad_polls == 1
    assert status.last_new_rows_ms == T0
    payload = rec.heartbeat_payload()["streams"]["trades"]
    assert payload["unparsed_total"] == 1 and payload["consecutive_bad_polls"] == 1


def test_a_poll_where_nothing_parses_is_a_failure_not_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _recorder(tmp_path)
    body = _envelope([{"unexpected": 1}, {"schema": "changed"}])
    _poll(rec, tmp_path, body, T0, monkeypatch)
    status = rec.status["trades"]
    assert status.consecutive_errors == 1 and status.last_ok_ms is None
    assert status.last_error is not None and "unparseable" in status.last_error
    assert _rows(tmp_path, "SELECT COUNT(*) FROM trades_unparsed") == [(2,)]  # the raw payload is kept
    assert _rows(tmp_path, "SELECT stream FROM errors") == [("trades",)]
    assert _rows(tmp_path, "SELECT n_items, n_inserted, n_bad FROM trade_polls") == [(2, 0, 2)]
    _poll(rec, tmp_path, _envelope([_trade("fine", T0)]), T0 + 1000, monkeypatch)
    assert status.consecutive_errors == 0 and status.consecutive_bad_polls == 0
    assert status.last_new_rows_ms == T0 + 1000


def test_empty_trade_lists_are_counted_as_bad_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _recorder(tmp_path)
    for i in range(3):
        _poll(rec, tmp_path, _envelope([]), T0 + i * 1000, monkeypatch)
    status = rec.status["trades"]
    assert status.consecutive_bad_polls == 3 and status.last_new_rows_ms is None
    assert status.consecutive_errors == 0 and status.last_ok_ms is not None
    assert _rows(tmp_path, "SELECT n_items, n_bad FROM trade_polls") == [(0, 0)] * 3


def test_missing_data_field_is_an_error_not_an_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _recorder(tmp_path)
    _poll(rec, tmp_path, {"result": "true", "error_code": 0}, T0, monkeypatch)
    assert rec.status["trades"].consecutive_errors == 1


# --- M4: the cursor survives restarts and midnight ----------------------------------------------


def test_restart_flags_a_gap_when_the_first_poll_does_not_reach_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _poll(_recorder(tmp_path), tmp_path, _envelope([_trade("a", T0 - 5000)]), T0, monkeypatch)
    restarted = _recorder(tmp_path)  # new process: no in-memory cursor
    _poll(restarted, tmp_path, _envelope([_trade("z", T0 + 600_000)]), T0 + 601_000, monkeypatch)
    polls = _rows(tmp_path, "SELECT gap, prev_max_ts_ms FROM trade_polls ORDER BY id")
    assert polls == [(0, None), (1, T0 - 5000)]


def test_restart_with_overlapping_window_is_not_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _poll(_recorder(tmp_path), tmp_path, _envelope([_trade("a", T0 - 5000)]), T0, monkeypatch)
    _poll(
        _recorder(tmp_path),
        tmp_path,
        _envelope([_trade("a", T0 - 5000), _trade("b", T0 + 1000)]),
        T0 + 2000,
        monkeypatch,
    )
    assert [r[0] for r in _rows(tmp_path, "SELECT gap FROM trade_polls ORDER BY id")] == [0, 0]


def test_cursor_is_persisted_and_never_moves_backwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _recorder(tmp_path)
    _poll(rec, tmp_path, _envelope([_trade("a", T0)]), T0, monkeypatch)
    _poll(rec, tmp_path, _envelope([_trade("old", T0 - 9000)]), T0 + 1000, monkeypatch)
    assert _rows(tmp_path, "SELECT stream, max_ts_ms FROM trade_cursor") == [("trades", T0)]
    assert load_trade_cursor(tmp_path, T0) == T0


def test_restart_across_midnight_reads_the_cursor_from_yesterdays_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = MIDNIGHT - 30_000
    _poll(_recorder(tmp_path), tmp_path, _envelope([_trade("a", before - 1000)]), before, monkeypatch)
    assert sorted(p.name for p in tmp_path.glob("lbank-*.sqlite")) == ["lbank-2026-10-10.sqlite"]
    # the new process starts after midnight; no file for the new day exists yet
    after = MIDNIGHT + 20_000
    _poll(_recorder(tmp_path), tmp_path, _envelope([_trade("late", after - 1000)]), after, monkeypatch)
    assert sorted(p.name for p in tmp_path.glob("lbank-*.sqlite")) == [
        "lbank-2026-10-10.sqlite",
        "lbank-2026-10-11.sqlite",
    ]
    new_day = sqlite3.connect(tmp_path / "lbank-2026-10-11.sqlite")
    assert new_day.execute("SELECT gap, prev_max_ts_ms FROM trade_polls").fetchall() == [(1, before - 1000)]
    assert new_day.execute("SELECT max_ts_ms FROM trade_cursor").fetchall() == [(after - 1000,)]
    new_day.close()
    # and a third restart on the new day uses the new day's own file
    assert load_trade_cursor(tmp_path, after + 5000) == after - 1000


def test_running_process_carries_the_cursor_into_the_next_day_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _recorder(tmp_path)
    _poll(rec, tmp_path, _envelope([_trade("a", MIDNIGHT - 2000)]), MIDNIGHT - 1000, monkeypatch)
    _poll(rec, tmp_path, _envelope([_trade("a", MIDNIGHT - 2000)]), MIDNIGHT + 1000, monkeypatch)  # dup only
    new_day = sqlite3.connect(tmp_path / "lbank-2026-10-11.sqlite")
    assert new_day.execute("SELECT max_ts_ms FROM trade_cursor").fetchall() == [(MIDNIGHT - 2000,)]
    assert new_day.execute("SELECT gap FROM trade_polls").fetchall() == [(0,)]
    new_day.close()
    assert load_trade_cursor(tmp_path, MIDNIGHT + 60_000) == MIDNIGHT - 2000


def test_no_cursor_when_nothing_was_ever_recorded(tmp_path: Path) -> None:
    assert load_trade_cursor(tmp_path, T0) is None
    store = DayStore(tmp_path)
    store.conn(T0)  # an empty day file (another stream wrote first)
    store.close()
    assert load_trade_cursor(tmp_path, T0) is None


def test_an_unreadable_cursor_never_stops_ingestion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lbank-2026-10-10.sqlite").write_bytes(b"this is not a database" * 50)
    rec = _recorder(tmp_path)
    monkeypatch.setattr(lr, "_now_ms", lambda: T0)
    monkeypatch.setattr(lr, "load_trade_cursor", _boom)
    with respx.mock:
        respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=TRADES))
        store = DayStore(tmp_path / "other")
        with httpx.Client() as client:
            rec.run_once("trades", rec._poll_trades, client, store)
        store.close()
    assert rec.status["trades"].consecutive_errors == 0
    assert len(_rows(tmp_path / "other", "SELECT * FROM trades")) == 2


def _boom(*_a: object, **_k: object) -> int:
    raise sqlite3.DatabaseError("file is not a database")


# --- schema migration against the day-file format already recorded live --------------------------

OLD_SCHEMA = """
CREATE TABLE depth_snapshots (id INTEGER PRIMARY KEY, recv_ms INTEGER NOT NULL, server_ms INTEGER,
    symbol TEXT NOT NULL, bids TEXT NOT NULL, asks TEXT NOT NULL, latency_ms INTEGER NOT NULL);
CREATE TABLE trades (trade_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, price TEXT NOT NULL,
    qty TEXT NOT NULL, quote_qty TEXT, is_buyer_maker INTEGER, recv_ms INTEGER NOT NULL);
CREATE INDEX idx_trades_ts ON trades(ts_ms);
CREATE INDEX idx_depth_recv ON depth_snapshots(recv_ms);
CREATE TABLE trade_polls (id INTEGER PRIMARY KEY, recv_ms INTEGER NOT NULL, n_items INTEGER NOT NULL,
    n_inserted INTEGER NOT NULL, min_ts_ms INTEGER, max_ts_ms INTEGER, prev_max_ts_ms INTEGER,
    gap INTEGER NOT NULL, latency_ms INTEGER NOT NULL);
CREATE TABLE klines (interval TEXT NOT NULL, open_ts_s INTEGER NOT NULL, open TEXT NOT NULL,
    high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL,
    closed INTEGER NOT NULL, recv_ms INTEGER NOT NULL, PRIMARY KEY (interval, open_ts_s));
CREATE INDEX idx_trade_polls_recv ON trade_polls(recv_ms);
CREATE TABLE funding (id INTEGER PRIMARY KEY, recv_ms INTEGER NOT NULL, symbol TEXT NOT NULL,
    funding_rate TEXT, next_fee_time_ms INTEGER, fee_interval_s INTEGER, marked_price TEXT,
    index_price TEXT, last_price TEXT, raw TEXT NOT NULL);
CREATE INDEX idx_funding_recv ON funding(recv_ms);
CREATE TABLE errors (recv_ms INTEGER NOT NULL, stream TEXT NOT NULL, message TEXT NOT NULL);
"""


def _old_format_file(path: Path, last_trade_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(OLD_SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO trades VALUES ('old-1', ?, '1', '1', '1', 0, ?)", (last_trade_ms, T0 - 60_000))
    conn.execute(
        "INSERT INTO trade_polls (recv_ms, n_items, n_inserted, min_ts_ms, max_ts_ms, prev_max_ts_ms, gap,"
        " latency_ms) VALUES (?, 1, 1, ?, ?, NULL, 0, 5)",
        (T0 - 60_000, last_trade_ms, last_trade_ms),
    )
    conn.commit()
    conn.close()


def test_recording_into_an_old_schema_day_file_migrates_it_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lbank-2026-10-10.sqlite"
    _old_format_file(path, T0 - 70_000)
    _poll(_recorder(tmp_path), tmp_path, _envelope([_trade("new", T0 - 1000)]), T0, monkeypatch)
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(trade_polls)")]
    assert columns[-1] == "n_bad" and columns[:9] == [
        "id",
        "recv_ms",
        "n_items",
        "n_inserted",
        "min_ts_ms",
        "max_ts_ms",
        "prev_max_ts_ms",
        "gap",
        "latency_ms",
    ]
    assert conn.execute("SELECT trade_id FROM trades ORDER BY trade_id").fetchall() == [("new",), ("old-1",)]
    old_poll, new_poll = conn.execute("SELECT n_bad, gap, prev_max_ts_ms FROM trade_polls ORDER BY id")
    assert old_poll == (0, 0, None)  # the pre-existing row keeps its meaning, n_bad defaults to 0
    assert new_poll == (0, 1, T0 - 70_000)  # the cursor came from the old file's trade_polls (gap: 69 s hole)
    assert conn.execute("PRAGMA user_version").fetchone() == (lr.SCHEMA_VERSION,)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "lbank-2026-10-10.sqlite"
    _old_format_file(path, T0 - 70_000)
    for _ in range(3):
        store = DayStore(tmp_path)
        store.conn(T0)
        store.close()
    conn = sqlite3.connect(path)
    assert [r[1] for r in conn.execute("PRAGMA table_info(trade_polls)")].count("n_bad") == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone() == (1,)
    conn.close()


def test_old_file_without_any_cursor_table_still_yields_a_cursor(tmp_path: Path) -> None:
    _old_format_file(tmp_path / "lbank-2026-10-10.sqlite", T0 - 70_000)
    assert load_trade_cursor(tmp_path, T0) == T0 - 70_000
