"""Tests for src/tbot/data/tabdeal_recorder.py.

All HTTP is mocked with respx against recorded JSON fixtures under tests/data/fixtures/ -- no
test reaches the network, and this module only exposes read-only endpoints in the first place.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from tbot.data import candles as candles_mod
from tbot.data import tabdeal_recorder as trd
from tbot.data.candles import Trade
from tbot.data.tabdeal_recorder import (
    HeartbeatState,
    RecorderSettings,
    RecorderStore,
    TabdealRecorderService,
    build_due_candles,
    is_candle_complete,
    parse_trade_item,
    poll_orderbook_once,
    poll_trades_once,
    write_heartbeat,
)
from tbot.execution.tabdeal_client import TabdealClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://api1.tabdeal.org"
READ_PREFIX = "/r/api/v1"
TRADES_URL = f"{BASE_URL}{READ_PREFIX}/trades"
DEPTH_URL = f"{BASE_URL}{READ_PREFIX}/depth"

HOUR0_MS = 1767225600000  # 2026-01-01T00:00:00Z
HOUR1_MS = 1767229200000  # 2026-01-01T01:00:00Z
HOUR2_MS = 1767232800000  # 2026-01-01T02:00:00Z
HOUR3_MS = HOUR2_MS + candles_mod.HOUR_MS


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """Deterministic Clock for tests -- never reads the real wall clock."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._now = dt


def make_client(clock: FakeClock, **overrides: Any) -> TabdealClient:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "read_prefix": READ_PREFIX,
        "write_prefix": "/api/v1",
        "clock": clock,
        "requests_per_second": 1000.0,
        "timeout_seconds": 1.0,
        "max_retries": 2,
    }
    kwargs.update(overrides)
    return TabdealClient(**kwargs)


def _seed_trade(store: RecorderStore, trade_id: int, ts_ms: int) -> None:
    store.insert_trades(
        [Trade(trade_id=trade_id, ts_ms=ts_ms, price=Decimal("100"), qty=Decimal("1"), is_buyer_maker=False)],
        recorded_ts_ms=0,
    )


# ---------------------------------------------------------------------------------
# parse_trade_item
# ---------------------------------------------------------------------------------


def test_parse_trade_item_parses_well_formed_item() -> None:
    raw = {"id": 1, "price": "100.5", "qty": "0.01", "time": 1700000000000, "isBuyerMaker": True}
    trade = parse_trade_item(raw)
    expected = Trade(
        trade_id=1, ts_ms=1700000000000, price=Decimal("100.5"), qty=Decimal("0.01"), is_buyer_maker=True
    )
    assert trade == expected


@pytest.mark.parametrize(
    "item",
    [
        "not-a-dict",
        {"price": "1", "qty": "1", "time": 1},  # missing id
        {"id": "x", "price": "1", "qty": "1", "time": 1},  # id not an int
        {"id": 1, "price": "nope", "qty": "1", "time": 1},  # bad decimal
    ],
)
def test_parse_trade_item_returns_none_on_malformed_input(item: Any) -> None:
    assert parse_trade_item(item) is None


def test_parse_trade_item_rejects_non_string_price_or_qty_to_avoid_float_rounding() -> None:
    """Minor fix 9: ``Decimal(str(item["price"]))`` already trusted whatever precision survived
    a prior JSON-number decode through Python ``float``. A JSON *number* for price/qty must be
    rejected outright -- there is no way to recover the exchange's exact decimal from here."""
    assert parse_trade_item({"id": 1, "price": 100.1, "qty": "1", "time": 1}) is None
    assert parse_trade_item({"id": 1, "price": "100.1", "qty": 0.01, "time": 1}) is None


@pytest.mark.parametrize(
    ("price", "qty"),
    [
        ("NaN", "1"),
        ("Infinity", "1"),
        ("-Infinity", "1"),
        ("0", "1"),  # non-positive price
        ("-5", "1"),  # negative price
        ("100", "NaN"),
        ("100", "Infinity"),
        ("100", "-1"),  # negative qty
    ],
)
def test_parse_trade_item_rejects_non_finite_or_non_positive_price_or_qty(price: str, qty: str) -> None:
    """MAJOR, third fix round (reported as MINOR-4): a quoted "NaN"/"Infinity" price parses to a
    valid-but-meaningless Decimal and used to be persisted as-is -- candles.py's max()/min() then
    raised decimal.InvalidOperation on it inside process_once every cycle thereafter, a permanent
    wedge needing manual database surgery to clear. Same guard depth.parse_depth_levels already
    applies to order-book levels."""
    assert parse_trade_item({"id": 1, "price": price, "qty": qty, "time": 1700000000000}) is None


def test_parse_trade_item_accepts_decimal_valued_price_and_qty() -> None:
    """MAJOR-1 (second fix round): decision D-031 changed ``TabdealClient`` to decode response
    bodies with ``json.loads(..., parse_float=Decimal)``, so an unquoted JSON number in a real
    ``/trades`` response now arrives here as an exact ``Decimal``, not a ``str``. The old
    ``isinstance(raw_price, str)`` guard rejected this outright and dropped every such trade --
    this is the reviewer-verified regression case."""
    item = {
        "id": 1,
        "price": Decimal("61234.56789012345"),
        "qty": Decimal("0.001"),
        "time": 1700000000000,
        "isBuyerMaker": False,
    }
    trade = parse_trade_item(item)
    assert trade == Trade(
        trade_id=1,
        ts_ms=1700000000000,
        price=Decimal("61234.56789012345"),
        qty=Decimal("0.001"),
        is_buyer_maker=False,
    )


# ---------------------------------------------------------------------------------
# poll_trades_once: dedupe / discontinuity / out-of-order / saturation
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_dedupes_overlapping_batch_without_a_gap(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=_load_json("tabdeal_trades_page1.json")),
            httpx.Response(200, json=_load_json("tabdeal_trades_overlap.json")),
        ]
    )
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    first = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )
    second = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert first.inserted == 5
    assert first.gap is None
    assert second.inserted == 3  # ids 104, 105 already stored; 106-108 are new
    assert second.gap is None
    assert store.max_trade_id() == 108
    assert store.total_trade_count() == 8
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_exact_duplicate_batch_inserts_nothing(tmp_path: Path) -> None:
    page1 = _load_json("tabdeal_trades_page1.json")
    respx.get(TRADES_URL).mock(side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page1)])
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    first = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )
    second = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert first.inserted == 5
    assert second.inserted == 0
    assert second.gap is None
    assert store.total_trade_count() == 5
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_out_of_order_ids_do_not_cause_a_false_gap(tmp_path: Path) -> None:
    body = _load_json("tabdeal_trades_out_of_order.json")
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 108, HOUR1_MS + 1)  # prev_max = 108; the fixture's ids 109-114 are contiguous

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.first_id == 109  # computed from min(), unaffected by the fixture's shuffled order
    assert outcome.last_id == 114
    assert outcome.gap is None
    assert store.max_trade_id() == 114
    assert store.all_gaps() == []
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_detects_id_discontinuity_and_records_a_gap(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_gap.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 108, HOUR1_MS - 1_000)

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.gap == (108, 115)
    assert store.all_gaps() == [(108, 115, "id_discontinuity", None)]
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_empty_response_inserts_nothing_and_raises_no_gap(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_empty.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 50, HOUR0_MS)

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.ok is True
    assert outcome.n_trades == 0
    assert outcome.n_items_received == 0  # MAJOR-1: genuinely empty, not "we dropped everything"
    assert outcome.inserted == 0
    assert outcome.first_id is None
    assert outcome.gap is None
    assert store.total_trade_count() == 1  # only the seeded trade
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert not any(log["event"] == "tabdeal_recorder.unparsable_trades" for log in warnings)
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_all_unparsable_non_empty_response_warns_loudly(tmp_path: Path) -> None:
    """MAJOR-1 (second fix round): a non-empty response from which nothing parsed used to be
    completely silent -- ``n_trades=0``, no gap, ``coverage_ratio=None`` (no low-coverage
    warning, since there is no window to measure), ``result.ok=True`` (no poll-failed warning).
    The only visible symptom was one ``empty_hour`` warning an hour later, by which point G1b
    could already be burning through days of 0% coverage. This must now log loudly and record
    ``n_items_received > 0`` with ``n_trades == 0`` so the two situations are distinguishable.
    """
    malformed_body = [
        {"id": "not-an-int", "price": "1", "qty": "1", "time": 1},
        {"price": "1", "qty": "1", "time": 1},  # missing id
    ]
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=malformed_body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.ok is True
    assert outcome.n_items_received == 2
    assert outcome.n_trades == 0
    assert outcome.coverage_ratio is None  # no low-coverage warning would fire either
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert any(
        log["event"] == "tabdeal_recorder.unparsable_trades" and log["received"] == 2 and log["parsed"] == 0
        for log in warnings
    )

    logged = store.all_poll_log()
    assert len(logged) == 1
    assert logged[0][3] == 0  # n_trades
    assert logged[0][9] == 2  # n_items_received -- "we dropped everything", not "nothing arrived"
    client.close()
    store.close()


@pytest.mark.parametrize(
    "body",
    [
        {"code": 1101, "msg": "unknown error"},  # Binance-style error wrapper served with 200
        "not json-shaped at all",
        {"trades": [{"id": 1, "price": "1", "qty": "1", "time": 1}]},  # a hypothetical shape change
    ],
)
@respx.mock
def test_poll_trades_once_non_list_200_body_logs_error_and_counts_as_not_ok(
    tmp_path: Path, body: Any
) -> None:
    """MAJOR M-A (third fix round): an HTTP-200 whose body is a dict or string used to compute
    ``n_items_received = len(result.body) if result.ok and isinstance(result.body, list) else 0``
    -- i.e. silently ``0``, indistinguishable from a genuinely empty response. Reviewer-measured:
    40 simulated polls over 3+ hours with a wrapper body produced zero log events at any level, a
    heartbeat rewritten every cycle with consecutive_errors=0, no gap rows, and a green healthcheck
    forever. This must now log loudly and report the outcome as not ok."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.ok is False  # must count toward _consecutive_errors, not look like success
    assert outcome.n_items_received == 0
    assert outcome.n_trades == 0
    errors = [log for log in logs if log["log_level"] == "error"]
    assert any(
        log["event"] == "tabdeal_recorder.unexpected_body_type" and log["body_type"] == type(body).__name__
        for log in errors
    )
    client.close()
    store.close()


@respx.mock
def test_process_once_non_list_body_climbs_consecutive_errors(tmp_path: Path) -> None:
    """The whole point of M-A: a wrapper body must actually move the healthcheck needle, not just
    log once and otherwise behave like a healthy, empty poll."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json={"code": 1101, "msg": "boom"}))
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "heartbeat.json"
    settings = RecorderSettings(symbol="BTCUSDT")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=settings,
        clock=clock,
    )

    service.process_once()
    service.process_once()
    payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert payload["consecutive_errors"] == 2

    service.close()
    client.close()


@respx.mock
def test_poll_trades_once_partial_unparsable_warns_even_when_some_trades_parsed(tmp_path: Path) -> None:
    """MAJOR M-B (third fix round): the old warning only fired when ``n_trades == 0``, so a
    systematic *partial* drop (one item in a multi-item batch has a bare float price) produced
    ``n_items_received=2, n_trades=1`` with no warning at all. It must now fire whenever
    ``n_trades < n_items_received``."""
    body = [
        {"id": 1, "price": "100.0", "qty": "1.0", "time": HOUR0_MS + 1_000},
        {"price": "100.5", "qty": "1.0", "time": HOUR0_MS + 2_000},  # missing "id" -> rejected
    ]
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.n_items_received == 2
    assert outcome.n_trades == 1
    assert outcome.ok is True  # the HTTP call itself succeeded; this is a data-quality warning
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert any(
        log["event"] == "tabdeal_recorder.unparsable_trades" and log["received"] == 2 and log["parsed"] == 1
        for log in warnings
    )
    client.close()
    store.close()


@respx.mock
def test_poll_and_build_due_candles_partial_unparsable_drop_marks_hour_incomplete(tmp_path: Path) -> None:
    """MAJOR M-B, downstream half: a partial drop is invisible to the id-discontinuity check (ids
    dropped *inside* a batch create no overlap gap), so without ``has_hour_gap`` the hour was
    written ``complete=True`` with trades missing -- exactly what flows into G1b's ">=99% complete"
    claim."""
    body = [
        {"id": 1, "price": "100.0", "qty": "1.0", "time": HOUR0_MS + 1_000},
        {"price": "100.5", "qty": "1.0", "time": HOUR0_MS + 2_000},  # dropped: missing "id"
        {"id": 3, "price": "101.0", "qty": "1.0", "time": HOUR0_MS + 3_000},
    ]
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert len(written) == 1
    assert written[0].n_trades == 2  # ids 1 and 3 only -- the middle item never parsed
    assert written[0].complete is False  # MAJOR M-B: must not claim full coverage
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_detects_saturation(tmp_path: Path) -> None:
    """``saturated`` is still recorded (raw fidelity, D-024) even though it is no longer an alert."""
    body = _load_json("tabdeal_trades_saturated.json")
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    # fixture trades span 1767225660000..1767225840000 ms = 180s
    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=4, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.n_trades == 4
    assert outcome.saturated is True
    assert outcome.window_span_seconds == pytest.approx(180.0)
    assert outcome.coverage_ratio == pytest.approx(36.0)  # 180 / 5
    logged = store.all_poll_log()
    assert len(logged) == 1
    assert logged[0][4] is True  # saturated column, still stored
    assert logged[0][7] == pytest.approx(180.0)  # window_span_seconds column
    assert logged[0][8] == pytest.approx(36.0)  # coverage_ratio column
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_low_coverage_ratio_warns(tmp_path: Path) -> None:
    """``coverage_ratio < 3`` -- not ``saturated`` -- is the real at-risk signal (D-024)."""
    body = _load_json("tabdeal_trades_page1.json")  # trades span 2940s
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=1000.0, clock=clock
        )

    assert outcome.coverage_ratio == pytest.approx(2.94)  # 2940 / 1000, below the risk threshold
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert any(log["event"] == "tabdeal_recorder.low_coverage" for log in warnings)
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_healthy_coverage_does_not_warn(tmp_path: Path) -> None:
    body = _load_json("tabdeal_trades_page1.json")  # trades span 2940s
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.coverage_ratio is not None
    assert outcome.coverage_ratio >= 3
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert not any(log["event"] == "tabdeal_recorder.low_coverage" for log in warnings)
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# restart resumption
# ---------------------------------------------------------------------------------


@respx.mock
def test_restart_resumption_no_duplicates_no_skipped_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "trades.sqlite"
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=_load_json("tabdeal_trades_page1.json")),
            httpx.Response(200, json=_load_json("tabdeal_trades_overlap.json")),
        ]
    )
    clock = FakeClock()
    client = make_client(clock)

    store_a = RecorderStore(db_path)
    outcome_a = poll_trades_once(
        client, store_a, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )
    store_a.close()  # simulate the process exiting

    store_b = RecorderStore(db_path)  # simulate a restart: reopen the same SQLite file
    outcome_b = poll_trades_once(
        client, store_b, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome_a.inserted == 5
    assert outcome_b.inserted == 3
    assert outcome_b.gap is None
    assert store_b.max_trade_id() == 108
    assert store_b.total_trade_count() == 8
    store_b.close()
    client.close()


# ---------------------------------------------------------------------------------
# candle sweep: empty hour, grace period, hour-boundary trade, completeness
# ---------------------------------------------------------------------------------


def test_build_due_candles_empty_hour_writes_a_gap_row_never_a_bar(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # hour A has a trade
    _seed_trade(store, 2, HOUR2_MS + 1_000)  # hour C has a trade; hour B is empty
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR3_MS + 120_000))

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR1_MS), candles_mod.ms_to_utc(HOUR3_MS)]
    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    assert table.num_rows == 2  # hour B (HOUR1_MS, HOUR2_MS] never got a bar
    gaps = store.all_gaps()
    assert (None, None, "no_trades_in_hour", HOUR2_MS) in gaps  # MINOR-8: hour identified by close
    store.close()


def test_build_due_candles_several_empty_hours_in_one_sweep_are_individually_identifiable(
    tmp_path: Path,
) -> None:
    """MINOR-8 (second fix round): before ``hour_close_ms`` was added, several empty hours swept
    in one pass all produced the identical row ``(NULL, NULL, 'no_trades_in_hour')`` -- the gaps
    table alone could not answer G1b's "which hours were missing" once more than one hour was
    empty. Hours B and C here are both empty; their gap rows must carry distinct close times.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    hour4_ms = HOUR3_MS + candles_mod.HOUR_MS
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # hour A: (HOUR0, HOUR1] has a trade
    _seed_trade(store, 2, HOUR3_MS + 1_000)  # hour D: (HOUR3, hour4_ms] has a trade
    # hours B (HOUR1, HOUR2] and C (HOUR2, HOUR3] are both empty in between
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(hour4_ms + 120_000))

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    gaps = store.all_gaps()
    empty_hour_gaps = [g for g in gaps if g[2] == "no_trades_in_hour"]
    hour_closes = {g[3] for g in empty_hour_gaps}
    assert hour_closes == {HOUR2_MS, HOUR3_MS}  # (HOUR1,HOUR2] and (HOUR2,HOUR3] -- distinguishable
    store.close()


def test_build_due_candles_empty_hour_gap_is_not_rewalked_on_a_second_sweep(tmp_path: Path) -> None:
    """MAJOR M3: a trailing empty hour used to be re-discovered as "due" on every single poll
    forever, since only writing an actual candle advanced the old resumption cursor. Measured:
    3 empty hours x 5 polls = 15 duplicate gap rows and 15 duplicate warnings."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # hour A has a trade; hour B is empty
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR2_MS + 120_000))

    first = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert [r.ts for r in first] == [candles_mod.ms_to_utc(HOUR1_MS)]
    assert store.all_gaps().count((None, None, "no_trades_in_hour", HOUR2_MS)) == 1
    assert store.last_swept_close_ms("BTCUSDT") == HOUR2_MS

    # a second sweep shortly afterwards (hour C is not yet due) must not re-walk hour B
    clock.advance(5.0)
    second = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert second == []
    assert store.all_gaps().count((None, None, "no_trades_in_hour", HOUR2_MS)) == 1  # not 2
    store.close()


def test_build_due_candles_withholds_a_too_fresh_hour_then_writes_once_grace_elapses(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 1_000)
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 30_000))  # only 30s past close; grace is 60s

    too_early = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert too_early == []
    assert candles_mod._read_candles(parquet_root, "BTCUSDT").num_rows == 0

    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert len(written) == 1
    assert candles_mod._read_candles(parquet_root, "BTCUSDT").num_rows == 1
    store.close()


@respx.mock
def test_poll_and_build_due_candles_boundary_trade_closes_the_earlier_hour(tmp_path: Path) -> None:
    body = _load_json("tabdeal_trades_boundary.json")
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR2_MS + 120_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR1_MS), candles_mod.ms_to_utc(HOUR2_MS)]
    assert [r.n_trades for r in written] == [1, 1]  # the boundary trade (id 301) closes hour A, not hour B
    client.close()
    store.close()


def test_is_candle_complete_false_when_a_gap_overlaps_the_hour(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 100, HOUR0_MS + 1_000)
    store.insert_trades(
        [
            Trade(
                trade_id=115,
                ts_ms=HOUR1_MS + 1_000,
                price=Decimal("1"),
                qty=Decimal("1"),
                is_buyer_maker=False,
            )
        ],
        recorded_ts_ms=0,
    )
    store.record_gap(detected_ts_ms=0, from_id=100, to_id=115, reason="id_discontinuity")

    assert is_candle_complete(store, HOUR0_MS, HOUR1_MS) is False  # gap's span touches hour A
    assert is_candle_complete(store, HOUR1_MS, HOUR2_MS) is False  # and hour B
    assert is_candle_complete(store, HOUR2_MS, HOUR3_MS) is True  # well clear of the gap
    store.close()


def test_build_due_candles_poisoned_row_already_in_database_does_not_wedge_the_loop(
    tmp_path: Path,
) -> None:
    """MAJOR (reported as MINOR-4, third fix round): a poisoned row (``price=Decimal('NaN')``) that
    somehow reached the database before ``parse_trade_item``'s own guard existed -- e.g. restored
    from a backup written by old code -- used to make ``candles.build_candle``'s ``max()``/``min()``
    raise ``decimal.InvalidOperation`` on every single sweep thereafter: ``run_forever`` swallowed
    it, the heartbeat was never rewritten again, and the row being already stored meant every later
    cycle failed identically. ``build_due_candles`` must survive this without crashing; the
    defensive filter leaves the hour simply unwritten rather than forward-filled.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.insert_trades(
        [
            Trade(
                trade_id=1,
                ts_ms=HOUR0_MS + 1_000,
                price=Decimal("NaN"),
                qty=Decimal("1"),
                is_buyer_maker=False,
            )
        ],
        recorded_ts_ms=0,
    )
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))

    written = build_due_candles(  # must not raise decimal.InvalidOperation
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert written == []  # the only trade in the hour was poisoned -- nothing to write
    assert candles_mod._read_candles(parquet_root, "BTCUSDT").num_rows == 0
    assert (None, None, "poisoned_trades", HOUR1_MS) in store.all_gaps()
    store.close()


def test_build_due_candles_poisoned_hour_is_not_rewalked_and_later_healthy_hour_still_completes(
    tmp_path: Path,
) -> None:
    """Follow-up fix: a poisoned-only hour must get exactly the same "accounted for once" treatment
    MAJOR M3 already gave an empty hour -- a ``gaps`` row plus a sweep-cursor advance -- rather than
    a bare ``continue`` that left it un-advanced and therefore re-discovered as "due" by
    ``pending_hour_bounds`` on every single future poll. Also checks that this does not somehow
    taint a later, genuinely healthy hour.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.insert_trades(
        [
            Trade(  # hour A: (HOUR0, HOUR1] -- the only trade in it is poisoned
                trade_id=1,
                ts_ms=HOUR0_MS + 1_000,
                price=Decimal("NaN"),
                qty=Decimal("1"),
                is_buyer_maker=False,
            ),
            Trade(  # hour B: (HOUR1, HOUR2] -- a perfectly healthy trade
                trade_id=2,
                ts_ms=HOUR1_MS + 1_000,
                price=Decimal("100"),
                qty=Decimal("2"),
                is_buyer_maker=False,
            ),
        ],
        recorded_ts_ms=0,
    )
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))  # only hour A due so far

    first = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert first == []
    assert store.all_gaps().count((None, None, "poisoned_trades", HOUR1_MS)) == 1
    assert store.last_swept_close_ms("BTCUSDT") == HOUR1_MS

    # A second sweep shortly afterwards (hour B is still not due) must not re-walk hour A.
    clock.advance(5.0)
    second = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert second == []
    assert store.all_gaps().count((None, None, "poisoned_trades", HOUR1_MS)) == 1  # not 2

    # Once hour B is due, it must produce a normal, complete candle -- unaffected by hour A.
    clock.set(candles_mod.ms_to_utc(HOUR2_MS + 61_000))
    third = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert [r.ts for r in third] == [candles_mod.ms_to_utc(HOUR2_MS)]
    assert third[0].n_trades == 1
    assert third[0].complete is True
    assert candles_mod._read_candles(parquet_root, "BTCUSDT").num_rows == 1
    store.close()


@respx.mock
def test_build_due_candles_saturated_but_healthy_coverage_still_complete(tmp_path: Path) -> None:
    """Decision D-024: a saturated poll with a healthy ``coverage_ratio`` is NOT a reason to mark
    a candle incomplete -- only a real trade-id gap (or an empty hour, which never gets a bar at
    all) does that. Before this fix, every candle came back ``complete=False``.

    Isolated from the M4 cold-start check: a trade and a pre-written candle for the hour before
    the one under test establish recorded coverage before ``HOUR0_MS``, so this test exercises
    only the saturation/coverage dimension, not the cold-start dimension (covered separately by
    ``test_build_due_candles_cold_start_first_partial_hour_is_incomplete``).
    """
    hour_minus1_ms = HOUR0_MS - candles_mod.HOUR_MS
    body = _load_json("tabdeal_trades_saturated.json")
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 60_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"
    _seed_trade(store, 200, hour_minus1_ms + 1_000)  # id 200, contiguous with the fixture's id 201
    candles_mod.write_candle(
        parquet_root,
        "BTCUSDT",
        candles_mod.TabdealCandleRecord(
            ts=candles_mod.ms_to_utc(HOUR0_MS),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            complete=True,
            n_trades=1,
        ),
    )

    # saturated=True (count == limit == 4), but window_span=180s / poll_interval=5s -> coverage
    # ratio 36, nowhere near the <3 at-risk threshold -- and there is no trade-id gap.
    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=4, poll_interval_seconds=5.0, clock=clock
    )
    assert outcome.saturated is True
    assert outcome.coverage_ratio == pytest.approx(36.0)

    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert len(written) == 1
    assert written[0].complete is True
    client.close()
    store.close()


def test_build_due_candles_cold_start_first_partial_hour_is_incomplete(tmp_path: Path) -> None:
    """MAJOR M4: the very first trade ever recorded lands 30 minutes into its hour -- there is no
    ``id_discontinuity`` gap row to catch this on a fresh database (nothing precedes the first
    trade to be discontinuous with), so without the fix this bucket was written complete=True.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 30 * 60_000)  # first trade ever, 30 min into the bucket
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert len(written) == 1
    assert written[0].n_trades == 1
    assert written[0].complete is False
    store.close()


def test_build_due_candles_second_hour_after_cold_start_is_complete(tmp_path: Path) -> None:
    """The cold-start flag must only ever hit the very first bucket, not every bucket forever."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 30 * 60_000)  # partial first hour
    _seed_trade(store, 2, HOUR1_MS + 1_000)  # second hour has full coverage from its own start
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR2_MS + 61_000))

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert [r.complete for r in written] == [False, True]
    store.close()


# ---------------------------------------------------------------------------------
# order-book snapshots: cumulative-depth maths against a hand-computed fixture book
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_orderbook_once_hand_computed_depth_and_spread(tmp_path: Path) -> None:
    """Fixture book: bids [[100,1],[99.6,2],[90,5]], asks [[101,1],[101.4,2],[110,5]].

    mid = (100 + 101) / 2 = 100.5. By hand:
      spread_bps  = (101 - 100) / 100.5 * 10000 = 99.502487562189...
      0.1% bound  = 0.1005 -> nothing within it on either side -> depth 0 / 0
      0.5% bound  = 0.5025 -> bid@100 (dist .5) + ask@101 (dist .5) only -> 1 / 1
      1%   bound  = 1.005  -> + bid@99.6 (dist .9, qty 2) + ask@101.4 (dist .9, qty 2) -> 3 / 3
    """
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    row = poll_orderbook_once(client, store, symbol="BTCUSDT", depth_limit=100, clock=clock)

    assert row is not None
    assert row.best_bid == Decimal("100")
    assert row.best_ask == Decimal("101")
    assert row.spread_bps is not None
    assert row.spread_bps == pytest.approx(99.502487562189, rel=1e-9)
    assert row.depth_bid_01pct == Decimal("0")
    assert row.depth_ask_01pct == Decimal("0")
    assert row.depth_bid_05pct == Decimal("1")
    assert row.depth_ask_05pct == Decimal("1")
    assert row.depth_bid_1pct == Decimal("3")
    assert row.depth_ask_1pct == Decimal("3")

    stored = store.all_orderbook_rows()
    assert len(stored) == 1
    assert stored[0][1] == "100"  # best_bid persisted as exact TEXT
    assert stored[0][2] == "101"
    client.close()
    store.close()


@respx.mock
def test_poll_orderbook_once_returns_none_on_failed_poll(tmp_path: Path) -> None:
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()
    client = make_client(clock, max_retries=0)
    store = RecorderStore(tmp_path / "trades.sqlite")

    row = poll_orderbook_once(client, store, symbol="BTCUSDT", depth_limit=100, clock=clock)

    assert row is None
    assert store.orderbook_row_count() == 0
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------------


def test_write_heartbeat_content(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    state = HeartbeatState(last_poll_ts=ts, last_trade_id=42, n_trades_total=7, consecutive_errors=1)
    write_heartbeat(path, state)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "last_poll_ts": ts.isoformat(),
        "last_trade_id": 42,
        "n_trades_total": 7,
        "consecutive_errors": 1,
    }


@respx.mock
def test_process_once_rewrites_heartbeat_even_with_zero_new_trades(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "heartbeat.json"
    settings = RecorderSettings(symbol="BTCUSDT")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=settings,
        clock=clock,
    )

    outcome = service.process_once()

    assert outcome.n_trades == 0
    assert heartbeat_file.is_file()
    payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert payload["n_trades_total"] == 0
    assert payload["last_trade_id"] is None
    assert payload["consecutive_errors"] == 0
    assert payload["last_poll_ts"] == clock.now().isoformat()
    service.close()
    client.close()


@respx.mock
def test_process_once_tracks_consecutive_errors_and_resets_on_success(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=_load_json("tabdeal_trades_empty.json"))]
    )
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock, max_retries=0)  # fail fast, no backoff sleep needed
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "heartbeat.json"
    settings = RecorderSettings(symbol="BTCUSDT")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=settings,
        clock=clock,
    )

    service.process_once()
    first_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert first_payload["consecutive_errors"] == 1

    service.process_once()
    second_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert second_payload["consecutive_errors"] == 0

    service.close()
    client.close()


def test_run_forever_subtracts_cycle_duration_and_stops_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor fix 10: the wait before the next cycle must be ``poll_interval_seconds`` minus how
    long the cycle itself took (never the full nominal interval, which would silently drift the
    real poll period above the interval ``coverage_ratio`` is computed against), and a stop
    request must interrupt that wait immediately rather than running it out to completion."""
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "heartbeat.json"
    settings = RecorderSettings(symbol="BTCUSDT", poll_interval_seconds=5.0)
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=settings,
        clock=clock,
    )

    # Simulate a cycle that itself took 2s of wall-clock time.
    monotonic_values = iter([100.0, 102.0])
    monkeypatch.setattr("tbot.data.tabdeal_recorder.time.monotonic", lambda: next(monotonic_values))

    waits: list[float | None] = []

    def fake_wait(self: threading.Event, timeout: float | None = None) -> bool:
        waits.append(timeout)
        service.request_stop()  # simulate SIGTERM arriving during the wait
        return True

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    calls = 0

    def fake_process_once(self: TabdealRecorderService) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(TabdealRecorderService, "process_once", fake_process_once)

    service.run_forever()

    assert calls == 1  # loop exited after exactly one cycle, not a second one
    assert waits == [pytest.approx(3.0)]  # 5.0s interval - 2.0s cycle duration, never the full 5.0
    service.close()
    client.close()


# ---------------------------------------------------------------------------------
# RecorderSettings validation
# ---------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------
# schema migration (MAJOR-2, second fix round)
# ---------------------------------------------------------------------------------


def _build_pre_fix_database(db_path: Path) -> None:
    """Hand-build a database shaped exactly like the schema before D-024/this fix round added
    ``window_span_seconds``/``coverage_ratio``/``n_items_received`` to ``poll_log`` and
    ``hour_close_ms`` to ``gaps`` -- i.e. what a database created by old, already-deployed
    recorder code looks like on disk today."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            trade_id INTEGER PRIMARY KEY,
            ts_ms INTEGER NOT NULL,
            price TEXT NOT NULL,
            qty TEXT NOT NULL,
            is_buyer_maker INTEGER NOT NULL,
            recorded_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE poll_log (
            poll_ts_ms INTEGER NOT NULL,
            first_id INTEGER,
            last_id INTEGER,
            n_trades INTEGER NOT NULL,
            saturated INTEGER NOT NULL,
            http_status INTEGER,
            latency_ms REAL
        );
        CREATE TABLE gaps (
            detected_ts_ms INTEGER NOT NULL,
            from_id INTEGER,
            to_id INTEGER,
            reason TEXT NOT NULL
        );
        """
    )
    # A pre-existing row in each table, so the migration must be an ALTER, never a drop/recreate
    # that would silently discard history.
    conn.execute(
        "INSERT INTO poll_log (poll_ts_ms, first_id, last_id, n_trades, saturated, http_status, latency_ms) "
        "VALUES (1, 1, 1, 1, 0, 200, 5.0)"
    )
    conn.execute(
        "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason) VALUES (1, 1, 2, 'id_discontinuity')"
    )
    conn.commit()
    conn.close()


def test_recorder_store_migrates_a_pre_fix_database_instead_of_crash_looping(tmp_path: Path) -> None:
    """MAJOR-2 (second fix round): ``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing
    table, so opening a database created by old code used to leave ``poll_log`` missing
    ``window_span_seconds``/``coverage_ratio`` -- the very next ``record_poll`` call then raised
    ``sqlite3.OperationalError: table poll_log has no column named window_span_seconds``.
    Reviewer-measured: trades kept being ingested (``insert_trades`` runs first and never touches
    ``poll_log``), but ``poll_log``, candles and the heartbeat were never written again, and
    ``run_forever``'s broad ``except Exception`` turned that into a silent 5-second crash loop.
    """
    db_path = tmp_path / "pre_fix.sqlite"
    _build_pre_fix_database(db_path)

    store = RecorderStore(db_path)  # must not raise, and must not drop the pre-existing rows

    # The old row survives, with NULL for the columns it predates.
    pre_existing = store.all_poll_log()
    assert len(pre_existing) == 1
    assert pre_existing[0][:5] == (1, 1, 1, 1, False)
    assert pre_existing[0][7] is None  # window_span_seconds: unknown for a pre-fix row
    assert pre_existing[0][8] is None  # coverage_ratio
    assert pre_existing[0][9] is None  # n_items_received

    pre_existing_gaps = store.all_gaps()
    assert pre_existing_gaps == [(1, 2, "id_discontinuity", None)]

    # And the operation that used to raise now succeeds, on the same (migrated) file.
    store.record_poll(
        poll_ts_ms=2,
        first_id=3,
        last_id=4,
        n_trades=1,
        saturated=False,
        http_status=200,
        latency_ms=5.0,
        window_span_seconds=10.0,
        coverage_ratio=2.0,
        n_items_received=1,
    )
    store.record_empty_hour_gap(symbol="BTCUSDT", close_ms=HOUR1_MS, detected_ts_ms=3)

    logged = store.all_poll_log()
    assert len(logged) == 2
    assert logged[1][7] == pytest.approx(10.0)
    assert logged[1][8] == pytest.approx(2.0)
    assert logged[1][9] == 1
    assert (None, None, "no_trades_in_hour", HOUR1_MS) in store.all_gaps()
    store.close()


def _build_partially_migrated_database(db_path: Path) -> None:
    """A database where ``gaps`` was already migrated to current (has ``hour_close_ms``) but
    ``poll_log`` was not -- e.g. a crash partway through a previous ``_migrate_schema`` run, or a
    database touched by two different fix-round binaries at different times. MINOR-5's
    schema-diffing migration must repair each table independently rather than assuming an
    all-or-nothing global migration state."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE poll_log (
            poll_ts_ms INTEGER NOT NULL,
            first_id INTEGER,
            last_id INTEGER,
            n_trades INTEGER NOT NULL,
            saturated INTEGER NOT NULL,
            http_status INTEGER,
            latency_ms REAL
        );
        CREATE TABLE gaps (
            detected_ts_ms INTEGER NOT NULL,
            from_id INTEGER,
            to_id INTEGER,
            reason TEXT NOT NULL,
            hour_close_ms INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


def test_recorder_store_migrates_a_partially_migrated_database(tmp_path: Path) -> None:
    """MINOR-5 (third fix round): the previous hand-maintained ``_EXPECTED_COLUMNS`` dict happened
    to migrate every table in one pass, but there was nothing structurally preventing a database
    where only some tables were upgraded. The schema-diffing approach must still fully migrate
    ``poll_log`` here even though ``gaps`` needs no change at all.
    """
    db_path = tmp_path / "partial.sqlite"
    _build_partially_migrated_database(db_path)

    store = RecorderStore(db_path)  # must not raise

    store.record_poll(
        poll_ts_ms=1,
        first_id=None,
        last_id=None,
        n_trades=0,
        saturated=False,
        http_status=200,
        latency_ms=1.0,
        window_span_seconds=None,
        coverage_ratio=None,
        n_items_received=0,
    )
    logged = store.all_poll_log()
    assert len(logged) == 1
    assert logged[0][9] == 0  # n_items_received column now exists and accepts a value
    store.close()


def test_recorder_store_future_schema_version_is_refused(tmp_path: Path) -> None:
    """MINOR-6 (third fix round): a database at a *newer* schema version than this code supports
    must be refused outright, not silently opened and "downgraded" (older code would act as if
    whatever the newer version added simply does not exist, with no error)."""
    db_path = tmp_path / "future.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(trd._SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {trd._SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(trd.RecorderSchemaVersionError):
        RecorderStore(db_path)


def test_recorder_store_corrupt_database_raises_typed_error_naming_path(tmp_path: Path) -> None:
    """MINOR-7 (third fix round): fail-fast is right for a corrupt database, but a bare
    ``sqlite3.DatabaseError`` propagating into the container restart loop gives an operator no
    indication of which file is corrupt. It must be wrapped in a typed error naming the path."""
    db_path = tmp_path / "corrupt.sqlite"
    db_path.write_bytes(b"this is not a sqlite database file, just some bytes" * 10)

    with pytest.raises(trd.RecorderDatabaseError, match=re.escape(str(db_path))):
        RecorderStore(db_path)


def test_recorder_store_reopening_an_already_migrated_database_is_a_no_op(tmp_path: Path) -> None:
    """Opening an already-current database a second time must not error or duplicate columns."""
    db_path = tmp_path / "trades.sqlite"
    store_a = RecorderStore(db_path)
    _seed_trade(store_a, 1, HOUR0_MS)
    store_a.close()

    store_b = RecorderStore(db_path)  # second open against the same, already-current file
    assert store_b.total_trade_count() == 1
    store_b.record_poll(
        poll_ts_ms=1,
        first_id=None,
        last_id=None,
        n_trades=0,
        saturated=False,
        http_status=200,
        latency_ms=1.0,
        window_span_seconds=None,
        coverage_ratio=None,
        n_items_received=0,
    )
    store_b.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"trades_limit": 0},
        {"depth_limit": -1},
        {"poll_interval_seconds": 0.0},
        {"orderbook_interval_seconds": -5.0},
        {"grace_period_seconds": 0.0},
    ],
)
def test_recorder_settings_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RecorderSettings(**overrides)
