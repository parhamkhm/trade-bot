from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tbot.data import lbank_recorder as lr
from tbot.data.lbank_recorder import (
    PERP_BASE_URL,
    SPOT_BASE_URL,
    DayStore,
    LBankRecorder,
    RecorderConfig,
    parse_depth,
    parse_funding,
    parse_klines,
    parse_trades,
)

DEPTH = {
    "result": "true",
    "error_code": 0,
    "ts": 1791637448128,
    "data": {"asks": [["82833.91", "5.51154"], ["82833.92", "0.00097"]], "bids": [["82833.90", "0.1"]]},
}
TRADES = {
    "result": "true",
    "error_code": 0,
    "data": [
        {
            "id": "aaa",
            "time": 1791637456085,
            "price": 82833.9,
            "qty": 0.00002,
            "quoteQty": 1.656678,
            "isBuyerMaker": False,
        },
        {
            "id": "bbb",
            "time": 1791637457000,
            "price": 82834.10,
            "qty": 0.5,
            "quoteQty": 41417.05,
            "isBuyerMaker": True,
        },
    ],
}
KLINES = {"result": "true", "error_code": 0, "data": [[1791637200, 82800, 82900.5, 82700.25, 82833.9, 12.5]]}
FUNDING = {
    "result": True,
    "data": [
        {"symbol": "ETHUSDT", "fundingRate": "0.0002"},
        {
            "symbol": "BTCUSDT",
            "fundingRate": "0.0001",
            "nextFeeTime": 1791648000000,
            "positionFeeTime": 28800,
            "markedPrice": "82830.1",
            "underlyingPrice": "82825.9",
            "lastPrice": "82831.0",
        },
    ],
}


def _decoded(obj: dict[str, Any]) -> Any:
    return json.loads(json.dumps(obj), parse_float=Decimal, parse_int=Decimal)


def test_parse_depth_keeps_exact_strings() -> None:
    bids, asks, server = parse_depth(_decoded(DEPTH))
    assert asks[0] == ["82833.91", "5.51154"]
    assert bids == [["82833.90", "0.1"]]
    assert server == 1791637448128


def test_parse_trades_preserves_decimal_text_and_skips_malformed() -> None:
    payload = _decoded(TRADES)
    payload["data"].append({"id": "ccc"})  # no time/price/qty
    rows = parse_trades(payload)
    assert rows[0] == ("aaa", 1791637456085, "82833.9", "0.00002", "1.656678", 0)
    assert rows[1][5] == 1
    assert len(rows) == 2


def test_parse_klines_marks_closed_bars_only_after_their_end() -> None:
    open_s = 1791637200
    still_open = parse_klines(_decoded(KLINES), "minute1", (open_s + 30) * 1000)
    closed = parse_klines(_decoded(KLINES), "minute1", (open_s + 60) * 1000)
    assert still_open[0][7] == 0 and closed[0][7] == 1
    assert still_open[0][2:7] == ("82800", "82900.5", "82700.25", "82833.9", "12.5")


def test_parse_funding_picks_the_symbol() -> None:
    row = parse_funding(_decoded(FUNDING), "BTCUSDT")
    assert row is not None and row["fundingRate"] == "0.0001"
    assert parse_funding(_decoded(FUNDING), "XRPUSDT") is None


def test_exchange_error_raises() -> None:
    with pytest.raises(ValueError, match="exchange error"):
        parse_depth({"result": "false", "error_code": 10008, "msg": "bad symbol", "data": None})


def test_day_store_partitions_by_receive_day(tmp_path: Path) -> None:
    store = DayStore(tmp_path)
    day1, day2 = 1791590400000 - 1, 1791590400000  # 2026-10-09 23:59:59.999 and 2026-10-10 00:00
    store.write(day1, [("INSERT INTO errors VALUES (?, ?, ?)", [(day1, "x", "a")])])
    store.write(day2, [("INSERT INTO errors VALUES (?, ?, ?)", [(day2, "x", "b")])])
    store.close()
    assert sorted(p.name for p in tmp_path.glob("*.sqlite")) == [
        "lbank-2026-10-09.sqlite",
        "lbank-2026-10-10.sqlite",
    ]


def test_day_store_transaction_rolls_back(tmp_path: Path) -> None:
    store = DayStore(tmp_path)
    recv = 1791637448128
    with pytest.raises(RuntimeError), store.transaction(recv) as conn:
        conn.execute("INSERT INTO errors VALUES (?, ?, ?)", (recv, "x", "y"))
        raise RuntimeError("boom")
    assert store.conn(recv).execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 0


def _mock_all() -> None:
    respx.get(f"{SPOT_BASE_URL}/v2/depth.do").mock(return_value=httpx.Response(200, json=DEPTH))
    respx.get(f"{SPOT_BASE_URL}/v2/supplement/trades.do").mock(return_value=httpx.Response(200, json=TRADES))
    respx.get(f"{SPOT_BASE_URL}/v2/kline.do").mock(return_value=httpx.Response(200, json=KLINES))
    respx.get(f"{PERP_BASE_URL}/cfd/openApi/v1/pub/marketData").mock(
        return_value=httpx.Response(200, json=FUNDING)
    )


def _rows(data_dir: Path, sql: str) -> list[Any]:
    rows: list[Any] = []
    for path in sorted(data_dir.glob("lbank-*.sqlite")):
        conn = sqlite3.connect(path)
        rows.extend(conn.execute(sql).fetchall())
        conn.close()
    return rows


def _recorder(tmp_path: Path) -> LBankRecorder:
    rec = LBankRecorder(RecorderConfig(data_dir=tmp_path))
    for name in ("depth", "trades", "funding", "kline_minute1"):
        rec.status[name] = lr.StreamStatus()
    return rec


@respx.mock
def test_each_stream_writes_its_raw_rows(tmp_path: Path) -> None:
    _mock_all()
    rec = _recorder(tmp_path)
    store = DayStore(tmp_path)
    with httpx.Client() as client:
        rec.run_once("depth", rec._poll_depth, client, store)
        rec.run_once("trades", rec._poll_trades, client, store)
        rec.run_once("trades", rec._poll_trades, client, store)  # same window again: deduped
        rec.run_once("funding", rec._poll_funding, client, store)
        rec.run_once("kline_minute1", rec._make_kline_job("minute1"), client, store)
    store.close()
    assert len(_rows(tmp_path, "SELECT * FROM depth_snapshots")) == 1
    assert _rows(tmp_path, "SELECT trade_id, price FROM trades ORDER BY trade_id") == [
        ("aaa", "82833.9"),
        ("bbb", "82834.1"),
    ]
    polls = _rows(tmp_path, "SELECT n_items, n_inserted, gap FROM trade_polls ORDER BY recv_ms")
    assert polls[0] == (2, 2, 0) and polls[1][1] == 0 and polls[1][2] == 0
    assert _rows(tmp_path, "SELECT funding_rate, next_fee_time_ms FROM funding") == [
        ("0.0001", 1791648000000)
    ]
    assert len(_rows(tmp_path, "SELECT * FROM klines")) == 1
    assert all(s.consecutive_errors == 0 for s in rec.status.values())


@respx.mock
def test_same_millisecond_polls_never_drop_a_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_all()
    monkeypatch.setattr(lr, "_now_ms", lambda: 1791637448128)
    rec = _recorder(tmp_path)
    store = DayStore(tmp_path)
    with httpx.Client() as client:
        for _ in range(3):
            rec.run_once("depth", rec._poll_depth, client, store)
            rec.run_once("trades", rec._poll_trades, client, store)
            rec.run_once("funding", rec._poll_funding, client, store)
    store.close()
    assert len(_rows(tmp_path, "SELECT * FROM depth_snapshots")) == 3
    assert len(_rows(tmp_path, "SELECT * FROM trade_polls")) == 3
    assert len(_rows(tmp_path, "SELECT * FROM funding")) == 3


@respx.mock
def test_trade_poll_that_does_not_overlap_is_flagged_as_gap(tmp_path: Path) -> None:
    later = {
        "result": "true",
        "error_code": 0,
        "data": [{"id": "zzz", "time": 1791637999000, "price": 1, "qty": 1}],
    }
    route = respx.get(f"{SPOT_BASE_URL}/v2/supplement/trades.do")
    route.side_effect = [httpx.Response(200, json=TRADES), httpx.Response(200, json=later)]
    rec = _recorder(tmp_path)
    store = DayStore(tmp_path)
    with httpx.Client() as client:
        rec.run_once("trades", rec._poll_trades, client, store)
        rec.run_once("trades", rec._poll_trades, client, store)
    store.close()
    assert [r[0] for r in _rows(tmp_path, "SELECT gap FROM trade_polls ORDER BY recv_ms")] == [0, 1]


@respx.mock
def test_a_failing_stream_never_stops_another(tmp_path: Path) -> None:
    _mock_all()
    respx.get(f"{SPOT_BASE_URL}/v2/supplement/trades.do").mock(return_value=httpx.Response(503))
    rec = _recorder(tmp_path)
    store = DayStore(tmp_path)
    with httpx.Client() as client:
        rec.run_once("trades", rec._poll_trades, client, store)
        rec.run_once("depth", rec._poll_depth, client, store)
    store.close()
    assert rec.status["trades"].consecutive_errors == 1
    assert rec.status["depth"].consecutive_errors == 0
    assert len(_rows(tmp_path, "SELECT * FROM depth_snapshots")) == 1
    assert _rows(tmp_path, "SELECT stream FROM errors") == [("trades",)]


@respx.mock
def test_heartbeat_failure_never_touches_ingestion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md §3.8: the heartbeat is downstream; its failure must not stop raw writes."""
    _mock_all()
    rec = LBankRecorder(
        RecorderConfig(
            data_dir=tmp_path,
            depth_interval_s=0.05,
            trades_interval_s=0.05,
            funding_interval_s=0.05,
            kline_interval_s={"minute1": 0.05},
            kline_lookback_bars={"minute1": 2},
            heartbeat_interval_s=0.01,
        )
    )

    def broken() -> None:
        raise OSError("disk full for the heartbeat")

    monkeypatch.setattr(rec, "write_heartbeat", broken)
    rec.start()
    import time

    deadline = time.monotonic() + 30  # client start-up (SSL context) can take seconds on Windows
    while time.monotonic() < deadline and rec.status["depth"].rows_total < 2:
        time.sleep(0.05)
    while time.monotonic() < deadline and rec.status["trades"].rows_total < 2:
        time.sleep(0.05)
    rec.stop()
    rec.join(timeout=5)
    assert len(_rows(tmp_path, "SELECT * FROM depth_snapshots")) >= 2
    assert len(_rows(tmp_path, "SELECT * FROM trades")) == 2
    assert not (tmp_path / "heartbeat.json").exists()


def test_heartbeat_payload_shape(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.status["depth"].last_ok_ms = 123
    rec.write_heartbeat()
    payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["streams"]["depth"]["last_ok_ms"] == 123
    assert isinstance(payload["written_ms"], int)
