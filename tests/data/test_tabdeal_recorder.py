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

# m-D (fifth fix round): any non-None verified-poll timestamp unblocks _sweep_now_ms's "no
# verified poll -> don't sweep at all" gate -- used by tests that seed the store directly (no
# poll_trades_once call) and are not themselves about m-D's own gating. A stand-in for "this
# recorder has already been verified-polling for a long time", which is exactly what unblocks
# that gate in production.
#
# NIT (sixth fix round): this value no longer *also* bypasses m-C's quiet-hour-timeout check
# (_hour_feed_has_moved_past) the way it used to. That function used to re-query
# RecorderStore.verified_poll_ts_ms directly and compare the raw (unclamped) value, which this
# constant's sheer magnitude trivially satisfied regardless of the real clock -- but that is
# exactly the bug the fix closes (a forward clock jump recorded as verified_poll_ts_ms must not
# stay stuck satisfying the quiet-timeout forever once the real clock drops back below it).
# _hour_feed_has_moved_past now takes the sweep's own already clock/verified-capped ``now_ms``
# instead, so satisfying m-C requires either a stored trade after the hour's close (direct
# proof) or the real clock genuinely being _TRIVIAL_QUIET_TIMEOUT_SECONDS past it -- see that
# constant, used by every test below that needs m-C out of the way for an unrelated reason.
FAR_FUTURE_VERIFIED_MS = 10**15

# NIT (sixth fix round): an effectively-zero quiet_hour_timeout_seconds for tests that need
# _hour_feed_has_moved_past's quiet-timeout path satisfied trivially (no trade after the hour's
# close recorded yet) but are not themselves testing that timeout's own length -- every such test
# already waits out its grace_period_seconds on the real (fake) clock, which is enough once the
# quiet-timeout threshold itself is this small.
_TRIVIAL_QUIET_TIMEOUT_SECONDS = 0.001


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
    trade = parse_trade_item(raw, now_ms=1700000000000)
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
    assert parse_trade_item(item, now_ms=1) is None


def test_parse_trade_item_rejects_non_string_price_or_qty_to_avoid_float_rounding() -> None:
    """Minor fix 9: ``Decimal(str(item["price"]))`` already trusted whatever precision survived
    a prior JSON-number decode through Python ``float``. A JSON *number* for price/qty must be
    rejected outright -- there is no way to recover the exchange's exact decimal from here."""
    assert parse_trade_item({"id": 1, "price": 100.1, "qty": "1", "time": 1}, now_ms=1) is None
    assert parse_trade_item({"id": 1, "price": "100.1", "qty": 0.01, "time": 1}, now_ms=1) is None


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
    assert parse_trade_item(
        {"id": 1, "price": price, "qty": qty, "time": 1700000000000}, now_ms=1700000000000
    ) is None


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
    trade = parse_trade_item(item, now_ms=1700000000000)
    assert trade == Trade(
        trade_id=1,
        ts_ms=1700000000000,
        price=Decimal("61234.56789012345"),
        qty=Decimal("0.001"),
        is_buyer_maker=False,
    )


def test_parse_trade_item_accepts_an_old_trade_well_beyond_24h() -> None:
    """MAJOR-A (fifth fix round): the old 24h age window rejected a trade purely for being old --
    ~17% of a normal ~29h/1000-trade /trades window, manufacturing a partial_unparsable gap (and
    an incomplete candle) on nearly every poll. A trade's age, however large, is not itself a
    reason to reject it; dedupe makes re-insertion of an already-known-old trade harmless."""
    now_ms = 1_767_225_600_000  # 2026-01-01T00:00:00Z
    old_ms = now_ms - 29 * 60 * 60 * 1000  # 29h old -- well beyond the old 24h cutoff
    item = {"id": 1, "price": "100", "qty": "1", "time": old_ms}
    assert parse_trade_item(item, now_ms=now_ms) is not None


def test_parse_trade_item_rejects_seconds_scale_timestamp() -> None:
    """MAJOR-A: a seconds-scale (not ms) timestamp lands far below any plausible ms-epoch value
    for any date this exchange has ever operated -- must be rejected as implausible, not accepted
    as an extremely old (1970s-ish) trade."""
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms // 1000}  # seconds, not ms
    assert parse_trade_item(item, now_ms=now_ms) is None


def test_parse_trade_item_rejects_zero_timestamp() -> None:
    item = {"id": 1, "price": "100", "qty": "1", "time": 0}
    assert parse_trade_item(item, now_ms=1_767_225_600_000) is None


def test_parse_trade_item_rejects_microsecond_scale_timestamp() -> None:
    """MAJOR-A: a microsecond-scale (not ms) timestamp overshoots any plausible 'now' by a factor
    of ~1000 -- caught by the future-skew bound, the same way an outright clock-skew artifact is."""
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms * 1000}  # us, not ms
    assert parse_trade_item(item, now_ms=now_ms) is None


def test_parse_trade_item_rejects_timestamp_more_than_five_minutes_in_the_future() -> None:
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms + 6 * 60 * 1000}
    assert parse_trade_item(item, now_ms=now_ms) is None


def test_parse_trade_item_accepts_timestamp_within_five_minutes_in_the_future() -> None:
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms + 4 * 60 * 1000}
    assert parse_trade_item(item, now_ms=now_ms) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("false", False),
        ("False", False),
        ("garbage", False),
        (None, False),
    ],
)
def test_parse_trade_item_parses_is_buyer_maker_strictly(raw: Any, expected: bool) -> None:
    """NIT (fourth fix round): ``bool(item.get("isBuyerMaker", False))`` turned the *string*
    "false" into True (any non-empty string is truthy). Only a real bool or a "true"/"false"
    string (any case) is accepted; anything else defaults to False, same as a missing field."""
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms, "isBuyerMaker": raw}
    trade = parse_trade_item(item, now_ms=now_ms)
    assert trade is not None
    assert trade.is_buyer_maker is expected


def test_parse_trade_item_missing_is_buyer_maker_defaults_to_false() -> None:
    now_ms = 1_767_225_600_000
    item = {"id": 1, "price": "100", "qty": "1", "time": now_ms}
    trade = parse_trade_item(item, now_ms=now_ms)
    assert trade is not None
    assert trade.is_buyer_maker is False


# ---------------------------------------------------------------------------------
# poll_trades_once: dedupe / window-overlap / out-of-order / saturation
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_dedupes_overlapping_batch_without_a_gap(tmp_path: Path) -> None:
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=_load_json("tabdeal_trades_page1.json")),
            httpx.Response(200, json=_load_json("tabdeal_trades_overlap.json")),
        ]
    )
    # MAJOR-A (fifth fix round): the fixtures' latest trade (id 108) is at 1767229100000 -- the
    # clock must be at or after that (a trade can never be more than 5 minutes in the future).
    clock = FakeClock(start=candles_mod.ms_to_utc(1767229100000))
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
    # MAJOR-A: page1's latest trade (id 105) is at 1767228600000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767228600000))
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
    # MAJOR-A: the fixture's latest trade (id 114) is at 1767229260000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767229260000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    # Decision D-037: trade ids are global across symbols, so continuity is proven by
    # *overlap* (this poll's lowest id <= what we already have), not by "+1" adjacency. Seed the
    # fixture's own lowest id (109) so the window overlaps exactly, isolating this test's actual
    # subject -- that a shuffled-order batch still computes first_id/last_id correctly -- from the
    # overlap boundary itself (covered separately by test_poll_trades_once_detects_window_no_overlap...).
    _seed_trade(store, 109, HOUR1_MS + 1)

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
def test_poll_trades_once_detects_window_no_overlap_and_records_a_gap(tmp_path: Path) -> None:
    """Decision D-037: trade ids are global across symbols (measured
    from the Turkey server 2026-10-04: BTCUSDT ids step by a median of 84, max 2069), so the old
    "first returned id > last_stored_id + 1" test fired on nearly every poll for a thin market.
    Replaced by an overlap test: continuity is proven iff this poll's lowest id reaches back to
    ``<= last_stored_id``. The fixture here returns a window far beyond the previously-stored id
    -- unambiguous data-loss risk, not ordinary cross-symbol interleaving.
    """
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_gap.json")))
    # MAJOR-A: the fixture's latest trade (id 50118) is at 1767229600000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767229600000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 108, HOUR1_MS - 1_000)

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.gap == (108, 50115)
    assert store.all_gaps() == [(108, 50115, "window_no_overlap", None)]
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_1000_item_window_spanning_29h_has_no_gaps_and_complete_candles(
    tmp_path: Path,
) -> None:
    """MAJOR-A end to end: the exact measured shape of a real BTCUSDT /trades poll (limit=1000,
    spanning ~29h) must parse in full -- no partial_unparsable gap, no window_no_overlap gap --
    and a candle well inside that window (not the very first hour ever, which is always
    cold-start-incomplete by construction, and not the freshest hour, which m-C withholds without
    further evidence) must come back complete=True."""
    n = 1000
    span_ms = 29 * 60 * 60 * 1000  # 29h, matching the measured real-world window
    start_ms = HOUR0_MS - 2 * candles_mod.HOUR_MS
    step_ms = span_ms // (n - 1)
    body: list[dict[str, Any]] = [
        {"id": i + 1, "price": "100.0", "qty": "0.01", "time": start_ms + i * step_ms, "isBuyerMaker": False}
        for i in range(n)
    ]
    end_ms = start_ms + (n - 1) * step_ms
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(end_ms + 60_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=1000, poll_interval_seconds=5.0, clock=clock
    )
    assert outcome.ok is True
    assert outcome.n_items_received == n
    assert outcome.n_trades == n  # MAJOR-A: nothing dropped as "too old" anymore
    assert outcome.gap is None

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert store.all_gaps() == []  # no partial_unparsable, no window_no_overlap anywhere
    target_close_ms = candles_mod.hour_bounds_ms(start_ms + 5 * candles_mod.HOUR_MS)[1]
    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    rows = {row["ts"]: row for row in table.to_pylist()}
    assert rows[candles_mod.ms_to_utc(target_close_ms)]["complete"] is True
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_empty_response_on_a_cold_start_is_still_ok(tmp_path: Path) -> None:
    """A genuinely empty market (nothing stored yet -- the normal cold-start case) is not
    suspicious: there is nothing yet for ``[]`` to contradict. MAJOR-B's new "suspicious empty
    response" rule must not fire here."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_empty.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.ok is True
    assert outcome.n_trades == 0
    assert outcome.n_items_received == 0
    assert outcome.inserted == 0
    assert outcome.gap is None
    assert store.verified_poll_ts_ms("BTCUSDT") is not None
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_empty_response_after_data_already_recorded_is_not_ok(tmp_path: Path) -> None:
    """MAJOR-B (fifth fix round, inverts the old cold-start-only test): an HTTP-200 empty list
    used to count as a fully healthy, verified poll unconditionally. A recent-trades endpoint
    never legitimately returns ``[]`` once the market has ever traded -- the previously-stored
    trade is itself proof that it has. Reviewer-reproduced: `[]` for 2h20m made a hard-coded
    98/120-trade hour complete=True and silently dropped two later hours."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_empty.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 50, HOUR0_MS)

    with capture_logs() as logs:
        outcome = poll_trades_once(
            client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
        )

    assert outcome.ok is False  # MAJOR-B: must not look like a healthy, verified poll
    assert outcome.n_trades == 0
    assert outcome.n_items_received == 0  # MAJOR-1: genuinely empty, not "we dropped everything"
    assert outcome.inserted == 0
    assert outcome.first_id is None
    assert outcome.gap is None
    assert store.total_trade_count() == 1  # only the seeded trade
    assert store.verified_poll_ts_ms("BTCUSDT") is None  # MAJOR-B: no verified-poll marker
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert not any(log["event"] == "tabdeal_recorder.unparsable_trades" for log in warnings)
    assert any(log["event"] == "tabdeal_recorder.suspicious_empty_response" for log in warnings)
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_all_unparsable_non_empty_response_warns_loudly_and_is_not_ok(
    tmp_path: Path,
) -> None:
    """MAJOR-1 (second fix round) + MAJOR-2' (fourth fix round): a non-empty response from which
    nothing parsed used to be completely silent -- ``n_trades=0``, no gap, ``coverage_ratio=None``
    (no low-coverage warning, since there is no window to measure), and (the reviewer's MAJOR-2
    finding) ``PollOutcome.ok=True``, so ``_consecutive_errors`` never climbed and the healthcheck
    never tripped even though every single item in a non-empty response failed to parse. This must
    now log loudly, record ``n_items_received > 0`` with ``n_trades == 0``, and report ``ok=False``.
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

    assert outcome.ok is False  # MAJOR-2': must count toward _consecutive_errors, not look healthy
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


@respx.mock
def test_process_once_all_unparsable_climbs_consecutive_errors(tmp_path: Path) -> None:
    """MAJOR-2' end to end: the whole point is that this must actually move the healthcheck
    needle, not just log once and otherwise behave like a healthy, empty poll."""
    malformed_body = [{"price": "1", "qty": "1", "time": 1}]  # missing id -> unparsable
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=malformed_body))
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
    """MAJOR M-B, downstream half: a partial drop is invisible to the window-overlap check (ids
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
    # m-C (fifth fix round): this test is about partial-unparsable completeness, not m-C's own
    # feed-evidence gate -- bypass it (see _TRIVIAL_QUIET_TIMEOUT_SECONDS).
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    # MAJOR-A: page1's latest trade (id 105) is at 1767228600000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767228600000))
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
    # MAJOR-A: page1's latest trade (id 105) is at 1767228600000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767228600000))
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
    # MAJOR-A: the fixtures' latest trade (id 108) is at 1767229100000.
    clock = FakeClock(start=candles_mod.ms_to_utc(1767229100000))
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
    # m-D (fifth fix round): this test seeds trades directly, never through poll_trades_once, so
    # it must record a verified poll explicitly -- with no verified poll at all, the sweep now
    # does nothing (see _sweep_now_ms). _TRIVIAL_QUIET_TIMEOUT_SECONDS also clears m-C's own gate
    # for every hour here, since this test is not about either of those mechanisms.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # hour A has a trade
    _seed_trade(store, 2, HOUR2_MS + 1_000)  # hour C has a trade; hour B is empty
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR3_MS + 120_000))

    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D
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
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # hour A has a trade; hour B is empty
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR2_MS + 120_000))

    first = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert [r.ts for r in first] == [candles_mod.ms_to_utc(HOUR1_MS)]
    assert store.all_gaps().count((None, None, "no_trades_in_hour", HOUR2_MS)) == 1
    assert store.last_swept_close_ms("BTCUSDT") == HOUR2_MS

    # a second sweep shortly afterwards (hour C is not yet due) must not re-walk hour B
    clock.advance(5.0)
    second = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert second == []
    assert store.all_gaps().count((None, None, "no_trades_in_hour", HOUR2_MS)) == 1  # not 2
    store.close()


def test_build_due_candles_withholds_a_too_fresh_hour_then_writes_once_grace_elapses(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D
    _seed_trade(store, 1, HOUR0_MS + 1_000)
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 30_000))  # only 30s past close; grace is 60s

    too_early = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert too_early == []
    assert candles_mod._read_candles(parquet_root, "BTCUSDT").num_rows == 0

    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    # m-C (fifth fix round): this test is about which hour a boundary trade closes, not m-C's own
    # feed-evidence gate -- bypass it (see _TRIVIAL_QUIET_TIMEOUT_SECONDS).
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    store.record_gap(detected_ts_ms=0, from_id=100, to_id=115, reason="window_no_overlap")

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
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D/m-C
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
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D/m-C
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
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert first == []
    assert store.all_gaps().count((None, None, "poisoned_trades", HOUR1_MS)) == 1
    assert store.last_swept_close_ms("BTCUSDT") == HOUR1_MS

    # A second sweep shortly afterwards (hour B is still not due) must not re-walk hour A.
    clock.advance(5.0)
    second = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert second == []
    assert store.all_gaps().count((None, None, "poisoned_trades", HOUR1_MS)) == 1  # not 2

    # Once hour B is due, it must produce a normal, complete candle -- unaffected by hour A.
    clock.set(candles_mod.ms_to_utc(HOUR2_MS + 61_000))
    third = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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
    # Decision D-037: ids are global, so continuity needs genuine overlap, not "+1"
    # adjacency -- seed the fixture's own lowest id (201) so the poll below overlaps exactly.
    _seed_trade(store, 201, hour_minus1_ms + 1_000)
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
    # MAJOR-1' (fourth fix round): build_due_candles caps "now" at the last *verified* poll, so a
    # second verified poll at the later time is needed before the hour can become due -- jumping
    # the clock alone (without polling again) would correctly leave it un-swept.
    poll_trades_once(client, store, symbol="BTCUSDT", limit=4, poll_interval_seconds=5.0, clock=clock)
    # m-C (fifth fix round): this test is about saturation/coverage, not m-C's own feed-evidence
    # gate -- bypass it (see _TRIVIAL_QUIET_TIMEOUT_SECONDS). The cap asserted above (sweep_now_ms
    # using the real poll's own, smaller now_ms) is unaffected: this only clears the *additional*
    # m-C requirement for the hour that is already due by that cap.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )

    assert len(written) == 1
    assert written[0].complete is True
    client.close()
    store.close()


def test_build_due_candles_cold_start_first_partial_hour_is_incomplete(tmp_path: Path) -> None:
    """MAJOR M4: the very first trade ever recorded lands 30 minutes into its hour -- there is no
    ``window_no_overlap`` gap row to catch this on a fresh database (nothing precedes the first
    trade to overlap with), so without the fix this bucket was written complete=True.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D/m-C
    _seed_trade(store, 1, HOUR0_MS + 30 * 60_000)  # first trade ever, 30 min into the bucket
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))

    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )

    assert len(written) == 1
    assert written[0].n_trades == 1
    assert written[0].complete is False
    store.close()


def test_build_due_candles_second_hour_after_cold_start_is_complete(tmp_path: Path) -> None:
    """The cold-start flag must only ever hit the very first bucket, not every bucket forever."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)  # m-D/m-C
    _seed_trade(store, 1, HOUR0_MS + 30 * 60_000)  # partial first hour
    _seed_trade(store, 2, HOUR1_MS + 1_000)  # second hour has full coverage from its own start
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR2_MS + 61_000))

    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
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


@respx.mock
def test_poll_orderbook_once_crossed_book_does_not_raise_and_stores_null_spread(tmp_path: Path) -> None:
    """m1 (fourth fix round): ``spread_bps_and_pct`` raises ``ValueError`` on a crossed book (best
    ask < best bid). Uncaught, that propagated out of ``poll_orderbook_once`` and, inside
    ``TabdealRecorderService.process_once``, skipped the heartbeat rewrite for the whole cycle and
    left ``_next_orderbook_due_ms`` unset, so the next cycle retried ``/depth`` immediately instead
    of waiting out ``orderbook_interval_seconds`` -- a crossed book was re-polled every single poll
    cycle until it uncrossed. The row must still be written, with ``spread_bps=None``, and depth
    (still meaningful on a crossed book) computed normally."""
    crossed_body = {"bids": [["101", "1"]], "asks": [["100", "1"]]}  # ask < bid
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=crossed_body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs:
        row = poll_orderbook_once(client, store, symbol="BTCUSDT", depth_limit=100, clock=clock)

    assert row is not None  # must not raise, and must still produce a row
    assert row.spread_bps is None
    assert row.best_bid == Decimal("101")
    assert row.best_ask == Decimal("100")
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert any(log["event"] == "tabdeal_recorder.orderbook_crossed" for log in warnings)

    stored = store.all_orderbook_rows()
    assert len(stored) == 1
    assert stored[0][3] is None  # spread_bps column
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------------


def test_write_heartbeat_content(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    state = HeartbeatState(
        last_poll_ts=ts,
        last_trade_id=42,
        n_trades_total=7,
        consecutive_errors=1,
        last_new_trade_ts_ms=123,
        first_poll_ts_ms=100,
    )
    write_heartbeat(path, state)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "last_poll_ts": ts.isoformat(),
        "last_trade_id": 42,
        "n_trades_total": 7,
        "consecutive_errors": 1,
        "last_new_trade_ts_ms": 123,
        "first_poll_ts_ms": 100,
        "last_cycle_error": None,  # MINOR-3 (sixth fix round): None on a cycle that never ran
    }


def test_write_heartbeat_last_new_trade_ts_ms_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    state = HeartbeatState(last_poll_ts=ts, last_trade_id=None, n_trades_total=0, consecutive_errors=0)
    write_heartbeat(path, state)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["last_new_trade_ts_ms"] is None
    assert payload["first_poll_ts_ms"] is None


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
    assert payload["last_new_trade_ts_ms"] is None  # m3: nothing new has ever been recorded
    service.close()
    client.close()


@respx.mock
def test_process_once_heartbeat_last_new_trade_ts_ms_tracks_genuinely_new_inserts(tmp_path: Path) -> None:
    """m3 (fourth fix round): ``last_new_trade_ts_ms`` must advance when a poll inserts a
    genuinely new trade, and must NOT advance on a later poll that returns only duplicates --
    it tracks "the feed is still producing new data", not merely "a poll happened"."""
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=_load_json("tabdeal_trades_page1.json")),
            httpx.Response(200, json=_load_json("tabdeal_trades_page1.json")),  # same batch again
        ]
    )
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
    first_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert first_payload["last_new_trade_ts_ms"] == trd._dt_to_ms(clock.now())

    clock.advance(5.0)
    service.process_once()  # the same trades again -> nothing new inserted
    second_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert second_payload["last_new_trade_ts_ms"] == first_payload["last_new_trade_ts_ms"]  # unchanged

    service.close()
    client.close()


def test_first_poll_ts_ms_is_min_of_poll_log(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    assert store.first_poll_ts_ms() is None  # nothing polled yet

    def _record(poll_ts_ms: int) -> None:
        store.record_poll(
            poll_ts_ms=poll_ts_ms,
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

    _record(500)
    _record(100)  # out of order -- first_poll_ts_ms is still MIN
    assert store.first_poll_ts_ms() == 100
    store.close()


@respx.mock
def test_process_once_heartbeat_carries_first_poll_ts_ms(tmp_path: Path) -> None:
    """NIT (fifth fix round): ``process_once`` must wire ``RecorderStore.first_poll_ts_ms`` into
    the heartbeat it writes -- see ``deploy/healthcheck.py``'s "no trade ever recorded" check."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_empty.json")))
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "heartbeat.json"
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )

    service.process_once()
    first_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert first_payload["first_poll_ts_ms"] == trd._dt_to_ms(clock.now())

    clock.advance(5.0)
    service.process_once()  # first_poll_ts_ms must not move on a later poll
    second_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert second_payload["first_poll_ts_ms"] == first_payload["first_poll_ts_ms"]

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


@pytest.mark.parametrize(
    ("consecutive_errors", "expected_wait"),
    [
        (5, 5.0),  # at the threshold: still the nominal interval
        (6, 10.0),  # one past: 5.0 * 2^1
        (7, 20.0),  # 5.0 * 2^2
        (10, 160.0),  # 5.0 * 2^5
        (13, 300.0),  # 5.0 * 2^8 = 1280, capped at 300
        (50, 300.0),  # deep into the backoff: still capped, never grows unbounded
        # m-F (fifth fix round): ``5.0 * 2 ** (n - 5)`` overflows (OverflowError) around n=1029,
        # outside any try/except -- the exponent must be capped before it is ever raised to a
        # power, well before that point and well past it.
        (1029, 300.0),
        (5000, 300.0),
    ],
)
def test_next_wait_seconds_backs_off_exponentially_past_the_error_threshold(
    tmp_path: Path, consecutive_errors: int, expected_wait: float
) -> None:
    """m4 (fourth fix round): once ``_consecutive_errors`` exceeds 5, the wait grows
    exponentially (cap 300s) instead of retrying a dead/rate-limiting endpoint at the nominal
    ``poll_interval_seconds`` forever."""
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=tmp_path / "heartbeat.json",
        settings=RecorderSettings(symbol="BTCUSDT", poll_interval_seconds=5.0),
        clock=clock,
    )
    service._consecutive_errors = consecutive_errors

    assert service._next_wait_seconds(elapsed_seconds=0.0) == pytest.approx(expected_wait)
    service.close()
    client.close()


def test_run_forever_backoff_wait_is_still_interruptible_by_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m4: a long backoff wait must not delay shutdown -- it is the same interruptible
    ``threading.Event.wait()`` as the normal-interval wait, just longer."""
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=tmp_path / "heartbeat.json",
        settings=RecorderSettings(symbol="BTCUSDT", poll_interval_seconds=5.0),
        clock=clock,
    )
    monkeypatch.setattr("tbot.data.tabdeal_recorder.time.monotonic", lambda: 100.0)

    waits: list[float | None] = []

    def fake_wait(self: threading.Event, timeout: float | None = None) -> bool:
        waits.append(timeout)
        service.request_stop()
        return True

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    def fake_process_once(self: TabdealRecorderService) -> None:
        self._consecutive_errors = 6  # one past the backoff threshold

    monkeypatch.setattr(TabdealRecorderService, "process_once", fake_process_once)

    service.run_forever()

    assert waits == [pytest.approx(10.0)]  # 5.0 * 2^1, not the nominal 5.0 -- and stop still landed
    service.close()
    client.close()


# ---------------------------------------------------------------------------------
# m-E: run_forever exits (raises) after too many consecutive process_once() exceptions
# ---------------------------------------------------------------------------------


def test_run_forever_raises_fatal_error_after_consecutive_cycle_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m-E (fifth fix round): run_forever must not loop forever inside a permanently broken
    process -- after ``_MAX_CONSECUTIVE_CYCLE_EXCEPTIONS`` consecutive ``process_once()``
    exceptions it must raise ``RecorderFatalError`` and let the process exit non-zero, so
    Docker's ``restart: unless-stopped`` policy (``deploy/docker-compose.yml``) can recover it."""
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=tmp_path / "heartbeat.json",
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )
    monkeypatch.setattr("tbot.data.tabdeal_recorder.time.monotonic", lambda: 0.0)

    def fake_wait(self: threading.Event, _timeout: float | None = None) -> bool:
        return False

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    def always_fails(self: TabdealRecorderService) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(TabdealRecorderService, "process_once", always_fails)

    with pytest.raises(trd.RecorderFatalError):
        service.run_forever()

    assert service._consecutive_cycle_exceptions == trd._MAX_CONSECUTIVE_CYCLE_EXCEPTIONS
    service.close()
    client.close()


def test_run_forever_resets_cycle_exception_counter_after_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fatal-exit counter is about *consecutive* exceptions -- an intervening successful
    cycle must reset it, not let failures accumulate across unrelated successes forever."""
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=tmp_path / "heartbeat.json",
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )
    monkeypatch.setattr("tbot.data.tabdeal_recorder.time.monotonic", lambda: 0.0)
    calls = {"n": 0}

    def fake_wait(self: threading.Event, timeout: float | None = None) -> bool:
        calls["n"] += 1
        if calls["n"] >= 3:
            service.request_stop()
        return False

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    def flaky_process_once(self: TabdealRecorderService) -> None:
        if calls["n"] == 1:  # fails once, then succeeds -- never two in a row
            raise RuntimeError("boom")

    monkeypatch.setattr(TabdealRecorderService, "process_once", flaky_process_once)

    service.run_forever()  # must not raise -- the single failure never repeats back to back

    assert service._consecutive_cycle_exceptions == 0
    service.close()
    client.close()


# ---------------------------------------------------------------------------------
# m-E: a failed COMMIT (or a pre-existing dangling transaction) must not wedge the connection
# ---------------------------------------------------------------------------------


class _FlakyCommitConn:
    """Thin proxy around a real ``sqlite3.Connection`` that fails exactly one ``COMMIT`` --
    ``sqlite3.Connection`` is a C type and does not allow monkeypatching its ``execute`` method
    directly (``TypeError: cannot set 'execute' attribute of immutable type``), so this wraps the
    real, already-open connection a ``RecorderStore`` holds instead."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self._fail_next_commit = True

    def execute(self, sql: str, *params: Any) -> Any:
        if sql == "COMMIT" and self._fail_next_commit:
            self._fail_next_commit = False
            raise sqlite3.OperationalError("simulated commit failure")
        return self._real.execute(sql, *params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_transaction_commit_failure_is_rolled_back_and_store_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m-E (fifth fix round): a COMMIT that itself fails used to leave the connection wedged
    inside an open transaction forever -- every later ``transaction()`` call then raised
    ``sqlite3.OperationalError: cannot start a transaction within a transaction`` permanently,
    needing a process restart to clear."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    monkeypatch.setattr(store, "_conn", _FlakyCommitConn(store._conn))

    with pytest.raises(sqlite3.OperationalError), store.transaction():
        store._conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")

    assert store._conn.in_transaction is False  # rolled back, not wedged

    with store.transaction():  # must work normally afterwards
        store._conn.execute("INSERT INTO meta (key, value) VALUES ('a', 'b')")
    row = store._conn.execute("SELECT value FROM meta WHERE key = 'a'").fetchone()
    assert row == ("b",)
    store.close()


def test_transaction_rolls_back_a_pre_existing_dangling_transaction_before_beginning(
    tmp_path: Path,
) -> None:
    """m-E: if a previous crash (or the commit-failure path above) ever left the connection with
    an open transaction, the NEXT ``transaction()`` call must recover by rolling it back first,
    rather than raising "cannot start a transaction within a transaction" forever."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    store._conn.execute("BEGIN IMMEDIATE")  # simulate a dangling transaction from a prior crash
    store._conn.execute("INSERT INTO meta (key, value) VALUES ('leftover', 'uncommitted')")
    assert store._conn.in_transaction is True

    with store.transaction():
        store._conn.execute("INSERT INTO meta (key, value) VALUES ('ok', 'value')")

    row = store._conn.execute("SELECT value FROM meta WHERE key = 'leftover'").fetchone()
    assert row is None  # the dangling, never-committed insert was rolled back
    row2 = store._conn.execute("SELECT value FROM meta WHERE key = 'ok'").fetchone()
    assert row2 == ("value",)
    store.close()


# ---------------------------------------------------------------------------------
# m6: database <-> symbol identity
# ---------------------------------------------------------------------------------


def test_ensure_symbol_records_on_first_use_and_is_idempotent(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.ensure_symbol("BTCUSDT")
    store.ensure_symbol("BTCUSDT")  # same symbol again -- must not raise
    store.close()


def test_ensure_symbol_mismatch_raises_typed_error(tmp_path: Path) -> None:
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.ensure_symbol("BTCUSDT")
    with pytest.raises(trd.RecorderSymbolMismatchError, match="BTCUSDT"):
        store.ensure_symbol("ETHUSDT")
    store.close()


def test_tabdeal_recorder_service_refuses_a_database_recorded_for_another_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "trades.sqlite"
    seed_store = RecorderStore(db_path)
    seed_store.ensure_symbol("BTCUSDT")
    seed_store.close()

    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(db_path)
    try:
        with pytest.raises(trd.RecorderSymbolMismatchError):
            TabdealRecorderService(
                client=client,
                store=store,
                parquet_root=tmp_path / "parquet",
                heartbeat_file=tmp_path / "heartbeat.json",
                settings=RecorderSettings(symbol="ETHUSDT"),
                clock=clock,
            )
    finally:
        store.close()
        client.close()


# ---------------------------------------------------------------------------------
# MAJOR-3: SQLite writes are transactional
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_rolls_back_everything_on_a_failure_mid_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-3 (fourth fix round): insert_trades/record_gap/record_poll/record_verified_poll are
    one atomic transaction now. Simulate a crash between the trade insert and the poll-log write
    (``record_poll`` raising) -- nothing, not even the already-inserted trades, must survive."""
    body = _load_json("tabdeal_trades_page1.json")
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash mid-transaction")

    monkeypatch.setattr(store, "record_poll", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)

    assert store.total_trade_count() == 0  # insert_trades' work was rolled back too
    assert store.all_poll_log() == []
    client.close()
    store.close()


def test_record_hour_gap_rolls_back_the_gap_row_and_cursor_advance_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-3: the gap row and the sweep-cursor advance in ``record_hour_gap`` are one
    transaction -- a crash between them must leave neither committed, never the cursor alone
    (which would silently make the skipped hour unreachable with no trace of why)."""
    store = RecorderStore(tmp_path / "trades.sqlite")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "record_gap", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.record_hour_gap(
            symbol="BTCUSDT", close_ms=HOUR1_MS, detected_ts_ms=0, reason="no_trades_in_hour"
        )

    assert store.all_gaps() == []
    assert store.last_swept_close_ms("BTCUSDT") is None
    store.close()


# ---------------------------------------------------------------------------------
# MAJOR-1': candle completeness vs. verified-poll coverage
# ---------------------------------------------------------------------------------


@respx.mock
def test_build_due_candles_does_not_seal_an_hour_while_polls_across_its_close_are_failing(
    tmp_path: Path,
) -> None:
    """MAJOR-1' (fourth fix round): previously, ``build_due_candles`` compared the hour's close
    time against the raw wall clock, so an outage spanning an hour's close caused it to be swept
    (and wrongly marked ``no_trades_in_hour``) before a later successful poll could ever backfill
    its trades -- by then the sweep cursor had moved past it and the candle was never corrected.
    Here, every poll fails (HTTP 500) right across hour A's close; the sweep must not touch hour A
    at all while that is true.
    """
    # Call 1: one verified poll before the outage starts (establishes verified_poll_ts_ms).
    # Call 2: the outage -- every poll fails from here on.
    respx.get(TRADES_URL).mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(500)]
    )
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 1_000))
    client = make_client(clock, max_retries=0)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"
    _seed_trade(store, 1, HOUR0_MS - 1_000)  # a trade before hour A, so there is a cursor to resume from

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    # Now the outage: clock marches well past hour A's close + grace, but this poll fails.
    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 120_000))
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert written == []  # hour A must NOT have been swept as empty while unverified
    assert store.all_gaps() == []  # in particular, no wrong-reason no_trades_in_hour row
    client.close()
    store.close()


@respx.mock
def test_build_due_candles_backfills_late_trade_once_polling_recovers(tmp_path: Path) -> None:
    """MAJOR-1', continued: once a poll succeeds again, the previously-withheld hour must build
    correctly from whatever trades are now in the store -- including one that arrived late,
    during the outage, which the capped sweep never got a chance to wrongly seal as empty."""
    late_trade_body = [{"id": 2, "price": "100", "qty": "1", "time": HOUR0_MS + 30 * 60_000}]
    # m-C (fifth fix round): a poll that returns the late trade *inside* hour A is not, by itself,
    # evidence the feed has moved PAST hour A's close -- a further poll returning something
    # after HOUR1_MS (id 3, landing in hour B) is what actually proves that.
    after_hour_a_body = [{"id": 3, "price": "100", "qty": "1", "time": HOUR1_MS + 500}]
    # Call 1: verified poll before the outage. Call 2: the outage. Call 3: recovery, with the
    # late trade for hour A (id 2, contiguous with the seed). Call 4: the m-C-satisfying poll.
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(500),
            httpx.Response(200, json=late_trade_body),
            httpx.Response(200, json=after_hour_a_body),
        ]
    )
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 1_000))
    client = make_client(clock, max_retries=0)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"
    _seed_trade(store, 1, HOUR0_MS - 1_000)

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)

    # Outage spanning hour A's close -- the sweep must withhold hour A (previous test covers this
    # in isolation).
    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 30_000))
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    withheld = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert withheld == []

    # Recovery: the late trade for hour A arrives. The seed's own hour (closing HOUR0_MS) already
    # has evidence the feed moved past it -- that very trade, at HOUR0_MS + 30min -- but hour A
    # itself (closing HOUR1_MS, which CONTAINS that trade) does not yet: m-C stops the sweep right
    # there rather than sealing hour A early.
    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 120_000))
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    partially_recovered = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert [r.ts for r in partially_recovered] == [candles_mod.ms_to_utc(HOUR0_MS)]

    # A further poll returning a trade after hour A's close is the evidence m-C needs for hour A.
    clock.advance(5.0)
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    # The point under test is specifically that hour A now gets a candle at all (previously:
    # never, since the sweep cursor would have moved past it during the outage with the wrong
    # empty-hour gap).
    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR1_MS)]
    hour_a = written[0]
    assert hour_a.n_trades == 1  # the late-arriving trade made it into its own hour's candle
    client.close()
    store.close()


@respx.mock
def test_restart_whose_first_poll_fails_writes_no_new_candles(tmp_path: Path) -> None:
    """MAJOR-1', restart case: ``verified_poll_ts_ms`` persists in the database (not in-memory
    state), so a freshly constructed service whose very first poll after "restart" fails must not
    sweep any hour beyond what the previous process already verified -- even though the wall
    clock has moved far ahead."""
    db_path = tmp_path / "trades.sqlite"
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 1_000))

    # Call 1 (before "restart"): succeeds. Call 2 (after "restart"): fails.
    respx.get(TRADES_URL).mock(side_effect=[httpx.Response(200, json=[]), httpx.Response(500)])
    client_a = make_client(clock, max_retries=0)
    store_a = RecorderStore(db_path)
    _seed_trade(store_a, 1, HOUR0_MS - 1_000)
    poll_trades_once(client_a, store_a, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    store_a.close()  # simulate the process exiting
    client_a.close()

    # "Restart": a fresh client/store pair, clock has advanced well past hour A's close + grace,
    # and the first poll after restart fails.
    clock.set(candles_mod.ms_to_utc(HOUR1_MS + 120_000))
    client_b = make_client(clock, max_retries=0)
    store_b = RecorderStore(db_path)
    poll_trades_once(client_b, store_b, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store_b, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert written == []
    assert store_b.all_gaps() == []
    client_b.close()
    store_b.close()


@respx.mock
def test_poll_trades_once_rewrites_an_already_written_candle_when_a_later_gap_overlaps_it(
    tmp_path: Path,
) -> None:
    """MAJOR-1', the explicit rewrite path: a gap discovered *after* an hour's candle was already
    written (complete=True) must correct that candle in place -- the normal sweep never revisits
    an hour once it has a candle, so without this, a later-discovered gap touching an
    already-sealed hour would silently never be reflected.

    Hour Z (closing at HOUR0_MS) exists purely so hour A is not the very first hour ever swept --
    the cold-start check (MAJOR M4) mathematically always marks the first hour incomplete (its
    own earliest trade can never be <= its own open time), so hour A needs a predecessor to be
    able to start out complete=True at all.
    """
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    respx.get(TRADES_URL).mock(
        side_effect=[
            # One poll, two trades: id 50 in hour Z, id 100 in hour A. Both in the same poll so
            # there is no prior stored id for either to be checked against (prev_max is None).
            httpx.Response(
                200,
                json=[
                    {"id": 50, "price": "100", "qty": "1", "time": HOUR0_MS - 1_000},
                    {"id": 100, "price": "100", "qty": "1", "time": HOUR0_MS + 1_000},
                ],
            ),
            # A much later poll returns an id far beyond 100, with no overlap -- a
            # window_no_overlap gap whose span touches the already-written hour A.
            httpx.Response(200, json=[{"id": 99999, "price": "100", "qty": "1", "time": HOUR0_MS + 2_000}]),
        ]
    )
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    # m-C (fifth fix round): this test is about the rewrite-in-place mechanism, not m-C's own
    # feed-evidence gate -- bypass it (see _TRIVIAL_QUIET_TIMEOUT_SECONDS) so hour A seals on this
    # sweep.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR0_MS), candles_mod.ms_to_utc(HOUR1_MS)]
    hour_a = written[1]
    assert hour_a.complete is True  # sealed healthy -- no gap yet, and not the cold-start hour

    outcome = poll_trades_once(
        client,
        store,
        symbol="BTCUSDT",
        limit=500,
        poll_interval_seconds=5.0,
        clock=clock,
        parquet_root=parquet_root,
    )

    assert outcome.gap == (100, 99999)
    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    rows = {row["ts"]: row for row in table.to_pylist()}
    assert len(rows) == 2  # hour A rewritten in place, not duplicated -- hour Z untouched
    hour_a_row = rows[candles_mod.ms_to_utc(HOUR1_MS)]
    assert hour_a_row["complete"] is False  # corrected
    assert hour_a_row["n_trades"] == 2  # ids 100 and 99999 both fall in hour A
    # m-G (fifth fix round): the inline rewrite succeeded, so the gap row is marked rewritten --
    # _recover_unrewritten_gaps has nothing left to redo on a later sweep.
    assert store.unrewritten_overlap_gaps() == []
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# m-C: hour due-ness needs actual feed evidence, not just the wall clock
# ---------------------------------------------------------------------------------


@respx.mock
def test_build_due_candles_withholds_an_hour_with_a_frozen_response_until_a_trade_after_it_arrives(
    tmp_path: Path,
) -> None:
    """m-C (fifth fix round), the exact scenario from the finding: responses are effectively
    frozen (the same single trade, inside hour A) across several polls spanning hour A's close --
    nowhere near the 2h quiet-market timeout -- so no candle must be written for hour A until a
    poll actually returns a trade timestamped after its close."""
    trade_in_hour_a = {"id": 1, "price": "100", "qty": "1", "time": HOUR0_MS + 55 * 60_000}  # 00:55
    trade_after_hour_a = {"id": 2, "price": "100", "qty": "1", "time": HOUR1_MS + 1_000}
    respx.get(TRADES_URL).mock(
        side_effect=[
            httpx.Response(200, json=[trade_in_hour_a]),  # poll at 00:55
            httpx.Response(200, json=[trade_in_hour_a]),  # "frozen" repeat, 5 min later
            httpx.Response(200, json=[trade_in_hour_a]),  # "frozen" repeat, 15 min after the first
            httpx.Response(200, json=[trade_in_hour_a, trade_after_hour_a]),  # finally moves on
        ]
    )
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 55 * 60_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    clock.advance(5 * 60.0)  # 01:00 -- hour A has just closed
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    no_candle_yet = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert no_candle_yet == []

    clock.advance(15 * 60.0)  # 01:15 -- 15 minutes past close, still frozen
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    still_no_candle = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )
    assert still_no_candle == []  # 15 min << the 2h quiet-market timeout -- must not seal early

    clock.advance(60.0)
    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR1_MS)]
    assert written[0].n_trades == 1  # only the one trade that actually belongs to hour A
    client.close()
    store.close()


def test_build_due_candles_quiet_market_timeout_seals_an_hour_with_no_trailing_trade(
    tmp_path: Path,
) -> None:
    """m-C's other branch: on a genuinely quiet market, waiting forever for a trailing trade
    would starve the sweep -- once the last verified poll is at least
    ``quiet_hour_timeout_seconds`` past the hour's close, it is sealed anyway."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 1_000)  # the only trade there will ever be
    parquet_root = tmp_path / "parquet"

    # Due by grace (61s > the 60s grace period) but nowhere near the 100s quiet-market timeout
    # used here, and no trailing trade exists -- m-C must withhold it.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=HOUR1_MS + 61_000)
    too_soon = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000)),
        quiet_hour_timeout_seconds=100.0,
    )
    assert too_soon == []

    # Now past the quiet-market timeout -- sealed even with no trailing trade.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=HOUR1_MS + 101_000)
    written = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 101_000)),
        quiet_hour_timeout_seconds=100.0,
    )
    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR1_MS)]
    store.close()


# ---------------------------------------------------------------------------------
# m-D: the sweep must not fall back to the raw wall clock with no verified poll at all
# ---------------------------------------------------------------------------------


def test_build_due_candles_never_sweeps_with_no_poll_ever_verified(tmp_path: Path) -> None:
    """m-D (fifth fix round): a database only ever populated by direct seeding (or migrated from
    a pre-v3 schema, whose ``recorder_state`` table starts out empty regardless of real history)
    must not have its sweep fall back to the raw wall clock -- nothing is due until the first
    poll is verified, no matter how far the clock has advanced."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS + 1_000)
    _seed_trade(store, 2, HOUR2_MS + 1_000)
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR3_MS + 120_000))
    assert store.verified_poll_ts_ms("BTCUSDT") is None

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert written == []
    assert store.all_gaps() == []  # in particular, no wrong no_trades_in_hour row for hour B
    store.close()


# ---------------------------------------------------------------------------------
# NIT: an empty hour inside a known window_no_overlap gap is data loss, not a quiet market
# ---------------------------------------------------------------------------------


def test_build_due_candles_empty_hour_overlapping_a_known_gap_is_reason_window_no_overlap(
    tmp_path: Path,
) -> None:
    """NIT (fifth fix round): an empty hour that sits inside an already-recorded
    ``window_no_overlap`` gap's span is the data-loss case that very gap describes, not a quiet
    market -- it must carry that same reason, not the misleading ``no_trades_in_hour``."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    _seed_trade(store, 100, HOUR0_MS + 1_000)  # before the gap; hour A
    _seed_trade(store, 200, HOUR2_MS + 1_000)  # after the gap; hour C -- hour B is empty
    store.record_gap(detected_ts_ms=0, from_id=100, to_id=200, reason="window_no_overlap")
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR3_MS + 120_000))

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    gaps = store.all_gaps()
    hour_b_gaps = [g for g in gaps if g[3] == HOUR2_MS]
    assert len(hour_b_gaps) == 1
    assert hour_b_gaps[0][2] == "window_no_overlap"  # not "no_trades_in_hour"
    store.close()


def test_build_due_candles_empty_hour_without_an_overlapping_gap_is_still_no_trades_in_hour(
    tmp_path: Path,
) -> None:
    """The NIT fix must not relabel an ordinary quiet hour -- only one actually inside a recorded
    window_no_overlap gap's span."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    _seed_trade(store, 1, HOUR0_MS + 1_000)
    _seed_trade(store, 2, HOUR2_MS + 1_000)  # hour B, in between, is empty -- no gap recorded
    parquet_root = tmp_path / "parquet"
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR3_MS + 120_000))

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    gaps = store.all_gaps()
    hour_b_gaps = [g for g in gaps if g[3] == HOUR2_MS]
    assert len(hour_b_gaps) == 1
    assert hour_b_gaps[0][2] == "no_trades_in_hour"
    store.close()


# ---------------------------------------------------------------------------------
# m-G: schema v4 (gaps.rewritten) and self-healing an un-rewritten candle after a crash
# ---------------------------------------------------------------------------------


def test_build_due_candles_self_heals_an_unrewritten_gap_from_a_prior_crash(tmp_path: Path) -> None:
    """m-G (fifth fix round): a crash between a poll's own transaction committing and its inline
    candle-rewrite used to leave an already-written candle ``complete=True`` forever, with no
    trace that it was ever supposed to be corrected. The ``gaps`` table's ``rewritten=0`` marker
    lets ``build_due_candles`` retry this on its very next sweep."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    _seed_trade(store, 100, HOUR0_MS + 1_000)
    parquet_root = tmp_path / "parquet"
    candles_mod.write_candle(
        parquet_root,
        "BTCUSDT",
        candles_mod.TabdealCandleRecord(
            ts=candles_mod.ms_to_utc(HOUR1_MS),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            complete=True,
            n_trades=1,
        ),
    )
    _seed_trade(store, 50000, HOUR0_MS + 2_000)  # the "new" window's lowest id, also in hour A
    # Simulates the crash: this row was committed by a poll's transaction, but the inline rewrite
    # that should have followed it never ran -- rewritten stays at its default 0.
    store.record_gap(detected_ts_ms=0, from_id=100, to_id=50000, reason="window_no_overlap")
    assert store.unrewritten_overlap_gaps() == [(1, 100, 50000)]
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    rows = {row["ts"]: row for row in table.to_pylist()}
    assert rows[candles_mod.ms_to_utc(HOUR1_MS)]["complete"] is False  # corrected on this sweep
    assert rows[candles_mod.ms_to_utc(HOUR1_MS)]["n_trades"] == 2
    assert store.unrewritten_overlap_gaps() == []  # marked rewritten -- won't be retried again
    store.close()


def test_recorder_store_migrates_a_v3_database_gains_rewritten_column_defaulting_to_zero(
    tmp_path: Path,
) -> None:
    """m-G (fifth fix round, schema v4): a v3 database's ``gaps`` table predates the
    ``rewritten`` column -- opening it must add ``rewritten INTEGER NOT NULL DEFAULT 0`` (not a
    silently nullable column with no default), and every pre-existing gap row must read back as
    ``rewritten=0`` (never confirmed rewritten, correctly re-processed by
    ``_recover_unrewritten_gaps`` on the next sweep)."""
    db_path = tmp_path / "v3.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            trade_id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, price TEXT NOT NULL,
            qty TEXT NOT NULL, is_buyer_maker INTEGER NOT NULL, recorded_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE poll_log (
            poll_ts_ms INTEGER NOT NULL, first_id INTEGER, last_id INTEGER,
            n_trades INTEGER NOT NULL, saturated INTEGER NOT NULL, http_status INTEGER,
            latency_ms REAL, window_span_seconds REAL, coverage_ratio REAL, n_items_received INTEGER
        );
        CREATE TABLE gaps (
            detected_ts_ms INTEGER NOT NULL, from_id INTEGER, to_id INTEGER,
            reason TEXT NOT NULL, hour_close_ms INTEGER
        );
        CREATE TABLE sweep_cursor (symbol TEXT PRIMARY KEY, last_swept_close_ms INTEGER NOT NULL);
        CREATE TABLE recorder_state (symbol TEXT PRIMARY KEY, last_verified_poll_ts_ms INTEGER NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE orderbook (
            ts_ms INTEGER NOT NULL, best_bid TEXT, best_ask TEXT, spread_bps REAL,
            depth_bid_01pct TEXT NOT NULL, depth_ask_01pct TEXT NOT NULL,
            depth_bid_05pct TEXT NOT NULL, depth_ask_05pct TEXT NOT NULL,
            depth_bid_1pct TEXT NOT NULL, depth_ask_1pct TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason, hour_close_ms) "
        "VALUES (1, 10, 20, 'window_no_overlap', NULL)"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    store = RecorderStore(db_path)  # must not raise

    rows = store._conn.execute("SELECT rewritten FROM gaps").fetchall()
    assert rows == [(0,)]
    assert store.unrewritten_overlap_gaps() == [(1, 10, 20)]
    store.close()


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


def test_recorder_store_migrates_pre_v3_database_gains_recorder_state_and_meta_tables(tmp_path: Path) -> None:
    """v3 (fourth fix round) added ``recorder_state`` (MAJOR-1') and ``meta`` (m6) -- opening a
    database that predates both (the same pre-fix shape MAJOR-2's own test uses) must create them
    from scratch via ``_migrate_schema``'s ``CREATE TABLE IF NOT EXISTS``, not just add columns to
    existing tables."""
    db_path = tmp_path / "pre_v3.sqlite"
    _build_pre_fix_database(db_path)

    store = RecorderStore(db_path)  # must not raise

    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=123)
    assert store.verified_poll_ts_ms("BTCUSDT") == 123
    store.ensure_symbol("BTCUSDT")  # meta table usable too
    store.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"trades_limit": 0},
        {"depth_limit": -1},
        {"poll_interval_seconds": 0.0},
        {"orderbook_interval_seconds": -5.0},
        {"grace_period_seconds": 0.0},
        {"quiet_hour_timeout_seconds": 0.0},
        {"quiet_hour_timeout_seconds": -1.0},
    ],
)
def test_recorder_settings_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RecorderSettings(**overrides)


# ---------------------------------------------------------------------------------
# MINOR-1 (sixth fix round, round-3 review): late-arriving trades correct an hour already
# sealed one way or another (complete=True, or an empty/poisoned-hour gap with no candle).
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_corrects_both_an_already_written_candle_and_a_gapped_empty_hour_on_late_trades(
    tmp_path: Path,
) -> None:
    """Repro of the round-3 reviewer's S1 scenario (a feed that keeps answering 200 but is
    frozen/stale long enough for the quiet-hour timeout to seal hours with real trades still
    outstanding): hour A's candle is already written ``complete=True`` with 1 trade, and hour B
    was already swept as ``no_trades_in_hour`` with no candle at all. A later poll then reveals
    two trades that actually belong to those same two hours (lower ids than what is already
    stored -- D-037: ids are global and not per-symbol-increasing, so a late arrival can easily
    have a lower id than something already seen). Both hours must be corrected:
    hour A's candle rewritten ``complete=False`` with the fuller trade set, hour B given a fresh
    ``complete=False`` candle it never had, and a ``late_trades`` gap row recorded (and marked
    rewritten) for each.
    """
    respx.get(TRADES_URL).mock(
        side_effect=[
            # Poll 1: id 50 in hour Z (closing HOUR0_MS -- purely so hour A is not the very first
            # hour ever swept, same cold-start reason as the MAJOR-1' rewrite test above), id 100
            # in hour A (closing HOUR1_MS).
            httpx.Response(
                200,
                json=[
                    {"id": 50, "price": "100", "qty": "1", "time": HOUR0_MS - 1_000},
                    {"id": 100, "price": "100", "qty": "1", "time": HOUR0_MS + 1_000},
                ],
            ),
            # Poll 2 (recovery): id 90 lands in hour A (lower than 100, but a new id -- no overlap
            # gap since 90 <= prev_max 100), id 95 lands in hour B (closing HOUR2_MS).
            httpx.Response(
                200,
                json=[
                    {"id": 90, "price": "100", "qty": "1", "time": HOUR0_MS + 1_500},
                    {"id": 95, "price": "100", "qty": "1", "time": HOUR1_MS + 1_500},
                ],
            ),
        ]
    )
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    parquet_root = tmp_path / "parquet"

    poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock,
        parquet_root=parquet_root,
    )
    first_sweep = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert [r.ts for r in first_sweep] == [candles_mod.ms_to_utc(HOUR0_MS), candles_mod.ms_to_utc(HOUR1_MS)]
    hour_a_before = first_sweep[1]
    assert hour_a_before.complete is True
    assert hour_a_before.n_trades == 1

    # Hour B (closing HOUR2_MS) becomes due and gets swept as empty -- no candle, just a gap.
    # _sweep_now_ms caps "now" at the last *verified* poll (MAJOR-1'); re-verify at the later
    # clock (m-D) so the sweep's own due-check is not itself what withholds hour B here -- this
    # test is about the late-trade correction, not m-D's own gating.
    clock.set(candles_mod.ms_to_utc(HOUR2_MS + 61_000))
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    second_sweep = build_due_candles(
        store,
        symbol="BTCUSDT",
        parquet_root=parquet_root,
        grace_period_seconds=60.0,
        clock=clock,
        quiet_hour_timeout_seconds=_TRIVIAL_QUIET_TIMEOUT_SECONDS,
    )
    assert second_sweep == []
    assert (None, None, "no_trades_in_hour", HOUR2_MS) in store.all_gaps()
    assert candles_mod.last_written_close_ms(parquet_root, "BTCUSDT") == HOUR1_MS  # hour B has no candle

    # The recovery poll: both late trades arrive in one response.
    clock.set(candles_mod.ms_to_utc(HOUR2_MS + 120_000))
    poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock,
        parquet_root=parquet_root,
    )

    gaps = store.all_gaps()
    late_trade_gap_closes = {g[3] for g in gaps if g[2] == "late_trades"}
    assert late_trade_gap_closes == {HOUR1_MS, HOUR2_MS}
    assert store.unrewritten_hour_gaps("late_trades") == []  # both corrections ran, marked rewritten

    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    rows = {row["ts"]: row for row in table.to_pylist()}
    hour_a_after = rows[candles_mod.ms_to_utc(HOUR1_MS)]
    assert hour_a_after["complete"] is False  # corrected: no longer claims full coverage
    assert hour_a_after["n_trades"] == 2  # ids 100 and 90 both in hour A
    hour_b_after = rows[candles_mod.ms_to_utc(HOUR2_MS)]
    assert hour_b_after["complete"] is False  # freshly written -- was never a candle before
    assert hour_b_after["n_trades"] == 1  # id 95
    client.close()
    store.close()


def test_build_due_candles_self_heals_an_unrewritten_late_trades_gap_from_a_prior_crash(
    tmp_path: Path,
) -> None:
    """MINOR-1 (sixth fix round): a crash between a poll's own transaction committing (which
    recorded the ``late_trades`` gap row) and its inline correction in ``poll_trades_once``
    (``_rewrite_hour_for_late_trades``) must self-heal on the very next sweep -- the same
    self-healing role ``unrewritten_overlap_gaps``/schema-v4 ``rewritten`` plays for
    ``window_no_overlap`` (m-G), now generalized via ``unrewritten_hour_gaps``.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=FAR_FUTURE_VERIFIED_MS)
    parquet_root = tmp_path / "parquet"
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
    _seed_trade(store, 1, HOUR0_MS - 1_000)  # the trade the candle above was built from
    _seed_trade(store, 2, HOUR0_MS - 500)  # the late trade for the same hour
    # Simulates the crash: the gap row was committed, but the inline rewrite that should have
    # followed it never ran -- rewritten stays at its default 0.
    rowid = store.record_gap(
        detected_ts_ms=0, from_id=None, to_id=None, reason="late_trades", hour_close_ms=HOUR0_MS
    )
    assert store.unrewritten_hour_gaps("late_trades") == [(rowid, HOUR0_MS)]
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 1_000))

    build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    table = candles_mod._read_candles(parquet_root, "BTCUSDT")
    rows = {row["ts"]: row for row in table.to_pylist()}
    assert rows[candles_mod.ms_to_utc(HOUR0_MS)]["complete"] is False  # corrected on this sweep
    assert rows[candles_mod.ms_to_utc(HOUR0_MS)]["n_trades"] == 2
    assert store.unrewritten_hour_gaps("late_trades") == []  # marked rewritten
    store.close()


# ---------------------------------------------------------------------------------
# MINOR-2 (sixth fix round): a partial-unparsable gap is anchored to the dropped item's own
# (leniently-read) time, not to the hours of the trades that happened to parse in the same poll,
# and is deduped per (reason, hour_close_ms).
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_anchors_partial_unparsable_gap_to_the_dropped_items_own_hour(
    tmp_path: Path,
) -> None:
    """Repro of the round-3 reviewer's finding: one dropped item anchored at its own hour, far
    from the hours of this poll's other, successfully-parsed trades, must mark only ITS OWN hour
    incomplete -- not every hour the surviving trades happen to span."""
    dropped_item_hour_close = HOUR0_MS - 20 * candles_mod.HOUR_MS  # 20h before anything else here
    body = [
        {"id": 1, "price": "100.0", "qty": "1.0", "time": HOUR0_MS + 1_000},
        # Dropped (missing "id"), but its own "time" is readable and sits in an unrelated,
        # much-earlier hour.
        {"price": "100.5", "qty": "1.0", "time": dropped_item_hour_close - 1_000},
        {"id": 3, "price": "101.0", "qty": "1.0", "time": HOUR0_MS + 3_000},
    ]
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 61_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)

    gaps = store.all_gaps()
    assert [(g[2], g[3]) for g in gaps] == [("partial_unparsable", dropped_item_hour_close)]
    # The hour the surviving trades (ids 1, 3) actually belong to must NOT be marked incomplete
    # by this mechanism -- has_hour_gap(HOUR1_MS) would wrongly disqualify it otherwise.
    assert store.has_hour_gap(HOUR1_MS) is False
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_dropped_item_with_no_readable_time_falls_back_to_the_polls_own_hour(
    tmp_path: Path,
) -> None:
    """A dropped item with no readable ``time`` at all (missing entirely, or non-numeric) has
    nothing of its own to anchor to -- falls back to the hour containing the poll itself, same as
    before MINOR-2."""
    body = [
        {"id": 1, "price": "100.0", "qty": "1.0", "time": "not-a-number"},  # dropped, unreadable time
    ]
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 30 * 60_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)

    # Clock is 30min into the hour ending at HOUR1_MS -- that is the poll's own hour.
    assert store.all_gaps() == [(None, None, "partial_unparsable", HOUR1_MS)]
    client.close()
    store.close()


@respx.mock
def test_poll_trades_once_repeated_partial_unparsable_for_the_same_hour_records_only_one_gap_row(
    tmp_path: Path,
) -> None:
    """MINOR-2: the old anchoring re-recorded a gap for every affected hour on every single poll
    that kept re-encountering the same unparsable item -- ``record_gap_once`` dedupes per
    ``(reason, hour_close_ms)`` so polling the same broken item repeatedly is a no-op after the
    first time."""
    body = [{"price": "100.5", "qty": "1.0", "time": HOUR0_MS + 1_000}]  # dropped every time: no "id"
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=body))
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR0_MS + 30 * 60_000))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    for _ in range(5):
        poll_trades_once(client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock)
        clock.advance(5.0)

    assert store.all_gaps() == [(None, None, "partial_unparsable", HOUR1_MS)]  # exactly one row
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# schema v5 (MINOR-2, sixth fix round): gaps(hour_close_ms) / gaps(reason, rewritten) indexes
# ---------------------------------------------------------------------------------


def test_recorder_store_migrates_a_v4_database_gains_the_v5_gap_indexes(tmp_path: Path) -> None:
    """A v4 database (``gaps.rewritten`` already present, but neither new index) must gain both
    indexes on open, purely via ``_migrate_schema``'s post-column-backfill ``_GAPS_INDEX_SQL``
    step -- and ``PRAGMA user_version`` must advance to the new ``_SCHEMA_VERSION``."""
    db_path = tmp_path / "v4.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            trade_id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, price TEXT NOT NULL,
            qty TEXT NOT NULL, is_buyer_maker INTEGER NOT NULL, recorded_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE poll_log (
            poll_ts_ms INTEGER NOT NULL, first_id INTEGER, last_id INTEGER,
            n_trades INTEGER NOT NULL, saturated INTEGER NOT NULL, http_status INTEGER,
            latency_ms REAL, window_span_seconds REAL, coverage_ratio REAL, n_items_received INTEGER
        );
        CREATE TABLE gaps (
            detected_ts_ms INTEGER NOT NULL, from_id INTEGER, to_id INTEGER,
            reason TEXT NOT NULL, hour_close_ms INTEGER,
            rewritten INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sweep_cursor (symbol TEXT PRIMARY KEY, last_swept_close_ms INTEGER NOT NULL);
        CREATE TABLE recorder_state (symbol TEXT PRIMARY KEY, last_verified_poll_ts_ms INTEGER NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE orderbook (
            ts_ms INTEGER NOT NULL, best_bid TEXT, best_ask TEXT, spread_bps REAL,
            depth_bid_01pct TEXT NOT NULL, depth_ask_01pct TEXT NOT NULL,
            depth_bid_05pct TEXT NOT NULL, depth_ask_05pct TEXT NOT NULL,
            depth_bid_1pct TEXT NOT NULL, depth_ask_1pct TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO gaps (detected_ts_ms, from_id, to_id, reason, hour_close_ms) "
        "VALUES (1, NULL, NULL, 'no_trades_in_hour', 1000)"
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    store = RecorderStore(db_path)  # must not raise

    index_names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'gaps'"
        ).fetchall()
    }
    assert "idx_gaps_hour_close_ms" in index_names
    assert "idx_gaps_reason_rewritten" in index_names
    on_disk_version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert on_disk_version == trd._SCHEMA_VERSION
    # Pre-existing row survives the migration untouched.
    assert store.all_gaps() == [(None, None, "no_trades_in_hour", 1000)]
    store.close()


def test_recorder_store_fresh_database_also_gets_the_v5_gap_indexes(tmp_path: Path) -> None:
    """A brand-new database creates ``gaps`` with every current column in one
    ``CREATE TABLE IF NOT EXISTS`` -- the post-column-backfill index step must still run (and
    find nothing missing) rather than only ever firing for a migrated-from-old database."""
    store = RecorderStore(tmp_path / "fresh.sqlite")
    index_names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'gaps'"
        ).fetchall()
    }
    assert "idx_gaps_hour_close_ms" in index_names
    assert "idx_gaps_reason_rewritten" in index_names
    store.close()


# ---------------------------------------------------------------------------------
# NIT (sixth fix round): RecorderStore.transaction() raises on nesting instead of silently
# rolling back the outer, legitimate transaction.
# ---------------------------------------------------------------------------------


def test_transaction_raises_on_nesting_instead_of_silently_rolling_back_the_outer_one(
    tmp_path: Path,
) -> None:
    """A nested ``transaction()`` call used to be indistinguishable from a dangling leftover
    transaction from a prior crash (both show ``conn.in_transaction == True``), so it was
    silently ``ROLLBACK``-ed -- discarding the outer, legitimate, in-progress transaction's work
    with no error at all. It must instead raise loudly, and must not be confused with the
    legitimate dangling-transaction recovery path (no
    ``tabdeal_recorder.dangling_transaction_rolled_back`` warning for this case)."""
    store = RecorderStore(tmp_path / "trades.sqlite")

    with capture_logs() as logs, pytest.raises(RuntimeError, match="nesting"), store.transaction():
        store._conn.execute("INSERT INTO meta (key, value) VALUES ('outer', 'work')")
        with store.transaction():
            pass

    assert not any(
        log.get("event") == "tabdeal_recorder.dangling_transaction_rolled_back" for log in logs
    )
    assert store._conn.in_transaction is False  # the outer transaction's own except-rollback ran
    row = store._conn.execute("SELECT value FROM meta WHERE key = 'outer'").fetchone()
    assert row is None  # the whole thing rolled back, correctly, since the body raised
    store.close()


def test_transaction_after_a_failed_nested_attempt_still_works_normally(tmp_path: Path) -> None:
    """The nesting guard must not leave ``_in_transaction_block`` stuck ``True`` after the
    ``RuntimeError`` propagates -- a later, ordinary (non-nested) ``transaction()`` call must
    still work."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    with pytest.raises(RuntimeError, match="nesting"), store.transaction(), store.transaction():
        pass

    with store.transaction():
        store._conn.execute("INSERT INTO meta (key, value) VALUES ('a', 'b')")
    row = store._conn.execute("SELECT value FROM meta WHERE key = 'a'").fetchone()
    assert row == ("b",)
    store.close()


# ---------------------------------------------------------------------------------
# NIT (sixth fix round): the future-skew plausibility check samples now_ms AFTER the HTTP
# round-trip completes, not before it starts.
# ---------------------------------------------------------------------------------


@respx.mock
def test_poll_trades_once_samples_the_plausibility_clock_after_the_http_round_trip(
    tmp_path: Path,
) -> None:
    """A slow/retried round-trip that takes real wall-clock time between the pre-call and
    post-call ``now`` used to make a genuinely fresh trade look implausibly far in the future
    relative to the STALE, pre-call ``now_ms`` -- and get wrongly dropped as
    ``partial_unparsable``. Simulated here by advancing the clock from inside the HTTP handler
    itself (``respx``'s ``side_effect`` runs synchronously at "HTTP call" time, standing in for
    time TabdealClient's own retry/backoff budget would have consumed for real).
    """
    pre_call_ms = HOUR0_MS
    advance_seconds = 300.0  # 5 minutes -- simulates a slow/retried round-trip
    # 310s after the PRE-call clock (would exceed the 5-minute skew tolerance against a stale,
    # pre-call now_ms), but only 10s after the POST-call clock (comfortably within tolerance).
    trade_ts_ms = pre_call_ms + 310_000

    def _slow_response(request: httpx.Request) -> httpx.Response:
        clock.advance(advance_seconds)
        return httpx.Response(
            200, json=[{"id": 1, "price": "100", "qty": "1", "time": trade_ts_ms}]
        )

    respx.get(TRADES_URL).mock(side_effect=_slow_response)
    clock = FakeClock(start=candles_mod.ms_to_utc(pre_call_ms))
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")

    outcome = poll_trades_once(
        client, store, symbol="BTCUSDT", limit=500, poll_interval_seconds=5.0, clock=clock
    )

    assert outcome.n_items_received == 1
    assert outcome.n_trades == 1  # accepted, not dropped as implausible
    assert outcome.ok is True
    assert store.max_trade_id() == 1
    assert store.all_gaps() == []  # no partial_unparsable gap recorded
    client.close()
    store.close()


# ---------------------------------------------------------------------------------
# NIT (sixth fix round): a transient forward clock jump recorded as verified_poll_ts_ms must not
# permanently satisfy the quiet-hour-timeout check once the real clock corrects back down.
# ---------------------------------------------------------------------------------


def test_build_due_candles_quiet_timeout_self_heals_after_a_forward_clock_jump_corrects(
    tmp_path: Path,
) -> None:
    """A transient forward clock jump (e.g. an NTP step) recorded as ``verified_poll_ts_ms``
    (which only ever advances, MAX-based) stays stuck at that too-high value on disk forever --
    even after the real clock corrects back down. Before the fix, ``_hour_feed_has_moved_past``
    re-queried that raw, stuck-high value directly and would have sealed an hour (or recorded a
    wrong-reason empty-hour gap) the instant it was otherwise due, regardless of how little real
    time had actually passed since the glitch. It must instead behave as though the verified
    timestamp were capped at the real current clock.
    """
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS - 1_000)
    # Simulates the glitch: a verified-poll marker recorded far in the future, well beyond
    # anything the real clock will reach in this test.
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=HOUR0_MS + 10 * 365 * 24 * 3600_000)
    parquet_root = tmp_path / "parquet"
    # The real clock "corrects" back down to just past hour A's close + grace -- nowhere near the
    # 2h default quiet-market timeout counted from THIS point.
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 90_000))

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    assert written == []  # must NOT be sealed on the strength of the stale, too-high marker
    assert store.all_gaps() == []
    store.close()


def test_build_due_candles_quiet_timeout_still_fires_once_real_time_has_actually_passed(
    tmp_path: Path,
) -> None:
    """Sibling of the self-heal test above: the quiet-timeout path must still work normally
    (using the real clock) once genuinely enough real time has passed, proving the fix does not
    simply disable the quiet-timeout path altogether."""
    store = RecorderStore(tmp_path / "trades.sqlite")
    _seed_trade(store, 1, HOUR0_MS - 1_000)
    store.record_verified_poll(symbol="BTCUSDT", poll_ts_ms=HOUR0_MS + 10 * 365 * 24 * 3600_000)
    parquet_root = tmp_path / "parquet"
    # Genuinely 2h + past hour A's close -- the quiet-timeout threshold itself, counted on the
    # real clock, is satisfied this time.
    clock = FakeClock(start=candles_mod.ms_to_utc(HOUR1_MS + 7_201_000))  # 2h0m1s past close

    written = build_due_candles(
        store, symbol="BTCUSDT", parquet_root=parquet_root, grace_period_seconds=60.0, clock=clock
    )

    # Hour Z (closing HOUR0_MS, the only hour with a trade) is swept via the quiet-timeout path;
    # hour A (closing HOUR1_MS) right after it has no trades and is correctly identified as empty
    # -- both prove the quiet-timeout mechanism itself still works on the real clock.
    assert [r.ts for r in written] == [candles_mod.ms_to_utc(HOUR0_MS)]
    assert (None, None, "no_trades_in_hour", HOUR1_MS) in store.all_gaps()
    store.close()


# ---------------------------------------------------------------------------------
# MINOR-3 (sixth fix round): backoff keys off max(consecutive_errors, consecutive_cycle_exceptions);
# the heartbeat is written from a `finally` with a redacted last_cycle_error.
# ---------------------------------------------------------------------------------


def test_next_wait_seconds_backs_off_on_cycle_exceptions_even_while_polls_keep_succeeding(
    tmp_path: Path,
) -> None:
    """Repro of the round-3 reviewer's finding: a sweep-only bug (``build_due_candles`` raising)
    kept the poll itself succeeding every cycle, which reset ``_consecutive_errors`` to 0 right
    before the sweep raised -- only ``_consecutive_cycle_exceptions`` climbed, but
    ``_next_wait_seconds`` never looked at it, so the process retried at the full nominal poll
    interval forever with zero backoff."""
    clock = FakeClock()
    store = RecorderStore(tmp_path / "trades.sqlite")
    client = make_client(clock)
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=tmp_path / "hb.json",
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )
    service._consecutive_errors = 0
    service._consecutive_cycle_exceptions = 20  # well past _BACKOFF_THRESHOLD_ERRORS (5)

    wait = service._next_wait_seconds(elapsed_seconds=0.0)

    assert wait > service.settings.poll_interval_seconds  # backed off, not the nominal interval
    client.close()
    store.close()


@respx.mock
def test_process_once_writes_heartbeat_with_redacted_last_cycle_error_when_the_sweep_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR-3: the heartbeat must still be rewritten (from a ``finally``) even when
    ``process_once`` itself raises, carrying that cycle's own error text -- scrubbed through the
    existing redaction pipeline (``tbot.monitoring.logging.redact_secrets``) before it is ever
    written to disk."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=[]))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "hb.json"
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )

    def _boom(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("sweep bug: api_key=do-not-leak-this-secret")

    monkeypatch.setattr(trd, "build_due_candles", _boom)

    with pytest.raises(RuntimeError, match="sweep bug"):
        service.process_once()

    payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert payload["last_cycle_error"] is not None
    assert "sweep bug" in payload["last_cycle_error"]
    assert "do-not-leak-this-secret" not in payload["last_cycle_error"]  # redacted
    client.close()
    store.close()


@respx.mock
def test_process_once_heartbeat_last_cycle_error_is_not_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``last_cycle_error`` reflects only the MOST RECENT cycle -- a cycle that fails and is then
    followed by one that succeeds must clear it back to ``None``, not leave the old error
    visible forever."""
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(DEPTH_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json")))
    clock = FakeClock()
    client = make_client(clock)
    store = RecorderStore(tmp_path / "trades.sqlite")
    heartbeat_file = tmp_path / "hb.json"
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=tmp_path / "parquet",
        heartbeat_file=heartbeat_file,
        settings=RecorderSettings(symbol="BTCUSDT"),
        clock=clock,
    )

    real_build_due_candles = trd.build_due_candles
    calls = {"n": 0}

    def _boom_once(*args: Any, **kwargs: Any) -> list[Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sweep bug")
        return real_build_due_candles(*args, **kwargs)

    monkeypatch.setattr(trd, "build_due_candles", _boom_once)

    with pytest.raises(RuntimeError):
        service.process_once()
    payload_after_failure = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert payload_after_failure["last_cycle_error"] is not None

    service.process_once()
    payload_after_recovery = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    assert payload_after_recovery["last_cycle_error"] is None
    client.close()
    store.close()
