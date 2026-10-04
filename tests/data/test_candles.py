"""Tests for src/tbot/data/candles.py: hour-bucket math, candle aggregation, Parquet round-trip.

Pure Python objects are used directly (no network, no JSON fixtures needed here -- the JSON
fixtures under tests/data/fixtures/ are for the respx-mocked recorder tests in
test_tabdeal_recorder.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tbot.data import candles as candles_mod

HOUR0_MS = 1767225600000  # 2026-01-01T00:00:00Z
HOUR1_MS = 1767229200000  # 2026-01-01T01:00:00Z
HOUR2_MS = 1767232800000  # 2026-01-01T02:00:00Z


def _trade(
    trade_id: int, ts_ms: int, price: str, qty: str, *, is_buyer_maker: bool = False
) -> candles_mod.Trade:
    return candles_mod.Trade(
        trade_id=trade_id, ts_ms=ts_ms, price=Decimal(price), qty=Decimal(qty), is_buyer_maker=is_buyer_maker
    )


# ---------------------------------------------------------------------------------
# hour_bounds_ms / is_due
# ---------------------------------------------------------------------------------


def test_hour_bounds_ms_mid_hour_timestamp_rounds_up_to_the_next_boundary() -> None:
    open_ms, close_ms = candles_mod.hour_bounds_ms(HOUR0_MS + 1_800_000)  # 00:30:00
    assert open_ms == HOUR0_MS
    assert close_ms == HOUR1_MS


def test_hour_bounds_ms_exact_boundary_closes_the_preceding_hour() -> None:
    """closed='right': a timestamp exactly on the hour closes that hour, it does not open the next."""
    open_ms, close_ms = candles_mod.hour_bounds_ms(HOUR1_MS)
    assert (open_ms, close_ms) == (HOUR0_MS, HOUR1_MS)


def test_hour_bounds_ms_one_ms_after_boundary_opens_the_next_hour() -> None:
    open_ms, close_ms = candles_mod.hour_bounds_ms(HOUR1_MS + 1)
    assert (open_ms, close_ms) == (HOUR1_MS, HOUR2_MS)


def test_ms_to_utc_round_trips_hour_boundary() -> None:
    assert candles_mod.ms_to_utc(HOUR1_MS) == datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now_ms", "expected"),
    [
        (HOUR1_MS + 59_000, False),  # 59s after close, grace=60s -> not due yet
        (HOUR1_MS + 60_000, True),  # exactly at the grace boundary -> due
        (HOUR1_MS + 61_000, True),
    ],
)
def test_is_due_respects_grace_period(now_ms: int, expected: bool) -> None:
    assert candles_mod.is_due(HOUR1_MS, now_ms, grace_period_seconds=60.0) is expected


# ---------------------------------------------------------------------------------
# pending_hour_bounds
# ---------------------------------------------------------------------------------


def test_pending_hour_bounds_empty_when_nothing_known_yet() -> None:
    bounds = list(
        candles_mod.pending_hour_bounds(
            last_written_close_ms=None, earliest_open_ms=None, now_ms=HOUR2_MS, grace_period_seconds=60.0
        )
    )
    assert bounds == []


def test_pending_hour_bounds_starts_at_earliest_trade_hour_when_nothing_written() -> None:
    bounds = list(
        candles_mod.pending_hour_bounds(
            last_written_close_ms=None,
            earliest_open_ms=HOUR0_MS + 1_000,  # first trade just after 00:00
            now_ms=HOUR1_MS + 120_000,
            grace_period_seconds=60.0,
        )
    )
    assert bounds == [(HOUR0_MS, HOUR1_MS)]


def test_pending_hour_bounds_resumes_right_after_last_written_close() -> None:
    """The restart-resumption cursor: once something is on disk, the next hour picks up right
    after it, regardless of where the earliest trade in the DB is."""
    bounds = list(
        candles_mod.pending_hour_bounds(
            last_written_close_ms=HOUR0_MS,
            earliest_open_ms=HOUR0_MS - 10 * candles_mod.HOUR_MS,  # much older, must be ignored
            now_ms=HOUR2_MS + 120_000,
            grace_period_seconds=60.0,
        )
    )
    assert bounds == [(HOUR0_MS, HOUR1_MS), (HOUR1_MS, HOUR2_MS)]


def test_pending_hour_bounds_withholds_an_hour_still_inside_its_grace_period() -> None:
    bounds = list(
        candles_mod.pending_hour_bounds(
            last_written_close_ms=HOUR0_MS,
            earliest_open_ms=None,
            now_ms=HOUR1_MS + 10_000,  # only 10s past close, grace=60s
            grace_period_seconds=60.0,
        )
    )
    assert bounds == []


# ---------------------------------------------------------------------------------
# build_candle
# ---------------------------------------------------------------------------------


def test_build_candle_aggregates_ohlcv_from_unsorted_trades() -> None:
    trades = [
        _trade(104, HOUR0_MS + 1_800_000, "101.20", "0.005"),
        _trade(101, HOUR0_MS + 60_000, "100.00", "0.010"),
        _trade(103, HOUR0_MS + 1_200_000, "99.80", "0.015"),
        _trade(105, HOUR0_MS + 3_000_000, "100.90", "0.030"),
        _trade(102, HOUR0_MS + 600_000, "100.50", "0.020"),
    ]
    record = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True)
    assert record is not None
    assert record.ts == candles_mod.ms_to_utc(HOUR1_MS)
    assert record.open == Decimal("100.00")  # earliest by ts, id 101
    assert record.high == Decimal("101.20")
    assert record.low == Decimal("99.80")
    assert record.close == Decimal("100.90")  # latest by ts, id 105
    assert record.volume == Decimal("0.080")
    assert record.n_trades == 5
    assert record.complete is True


def test_build_candle_filters_trades_outside_the_window() -> None:
    """A trade from the previous or next hour must never leak into this bar."""
    trades = [
        _trade(1, HOUR0_MS, "999.00", "1.0"),  # exactly on the OPEN boundary -> excluded
        _trade(2, HOUR0_MS + 500, "100.00", "0.5"),  # inside -> included
        _trade(3, HOUR1_MS + 1, "888.00", "1.0"),  # one ms past the CLOSE boundary -> excluded
    ]
    record = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True)
    assert record is not None
    assert record.n_trades == 1
    assert record.open == record.close == Decimal("100.00")


def test_build_candle_boundary_trade_closes_the_hour_it_lands_on() -> None:
    """A trade exactly at the close timestamp belongs to THIS bucket, not the next one."""
    trades = [_trade(301, HOUR1_MS, "100.00", "0.1")]
    this_hour = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True)
    next_hour = candles_mod.build_candle(trades, HOUR1_MS, HOUR2_MS, complete=True)
    assert this_hour is not None and this_hour.n_trades == 1
    assert next_hour is None


def test_build_candle_empty_window_returns_none_never_forward_fills() -> None:
    trades = [_trade(1, HOUR0_MS + 1_000, "100.00", "1.0")]
    assert candles_mod.build_candle(trades, HOUR1_MS, HOUR2_MS, complete=True) is None


def test_build_candle_marks_incomplete_when_told_to() -> None:
    trades = [_trade(1, HOUR0_MS + 1_000, "100.00", "1.0")]
    record = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=False)
    assert record is not None
    assert record.complete is False


def test_build_candle_excludes_a_poisoned_nan_price_row_instead_of_crashing() -> None:
    """MAJOR (reported as MINOR-4, third fix round): a quoted "NaN"/"Infinity" price that reached
    the database before ``parse_trade_item``'s own guard existed used to make ``max(t.price for t
    in ordered)`` below raise ``decimal.InvalidOperation`` every single time this hour was
    processed -- a permanent wedge. ``build_candle`` must defensively drop such a row instead of
    aggregating it, same as a row that failed to parse in the first place."""
    trades = [
        _trade(1, HOUR0_MS + 1_000, "100.00", "1.0"),
        _trade(2, HOUR0_MS + 2_000, "NaN", "1.0"),  # poisoned: must not reach max()/min()
        _trade(3, HOUR0_MS + 3_000, "105.00", "1.0"),
    ]
    record = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True)
    assert record is not None
    assert record.n_trades == 2  # the NaN row is excluded, not counted
    assert record.high == Decimal("105.00")
    assert record.low == Decimal("100.00")
    assert record.open == Decimal("100.00")
    assert record.close == Decimal("105.00")


@pytest.mark.parametrize(
    ("price", "qty"),
    [
        ("NaN", "1.0"),
        ("Infinity", "1.0"),
        ("-Infinity", "1.0"),
        ("0", "1.0"),  # non-positive price
        ("-5", "1.0"),  # negative price
        ("100", "NaN"),
        ("100", "-1.0"),  # negative qty
    ],
)
def test_build_candle_excludes_every_kind_of_poisoned_row(price: str, qty: str) -> None:
    trades = [
        _trade(1, HOUR0_MS + 1_000, "100.00", "1.0"),
        _trade(2, HOUR0_MS + 2_000, price, qty),
    ]
    record = candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True)
    assert record is not None
    assert record.n_trades == 1


def test_build_candle_all_rows_poisoned_returns_none_never_crashes() -> None:
    trades = [_trade(1, HOUR0_MS + 1_000, "NaN", "1.0")]
    assert candles_mod.build_candle(trades, HOUR0_MS, HOUR1_MS, complete=True) is None


def test_candle_record_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="UTC"):
        candles_mod.TabdealCandleRecord(
            ts=datetime(2026, 1, 1, 1, 0, 0),  # noqa: DTZ001 -- deliberately naive, must be rejected
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            complete=True,
            n_trades=1,
        )


def test_candle_record_rejects_zero_trades() -> None:
    with pytest.raises(ValueError, match="n_trades"):
        candles_mod.TabdealCandleRecord(
            ts=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            complete=True,
            n_trades=0,
        )


# ---------------------------------------------------------------------------------
# parquet I/O round trip
# ---------------------------------------------------------------------------------


def _record(
    ts: datetime, *, close: str = "100", complete: bool = True, n_trades: int = 1
) -> candles_mod.TabdealCandleRecord:
    return candles_mod.TabdealCandleRecord(
        ts=ts,
        open=Decimal("99.5"),
        high=Decimal("101.5"),
        low=Decimal("98.5"),
        close=Decimal(close),
        volume=Decimal("1.234567890123"),
        complete=complete,
        n_trades=n_trades,
    )


def test_write_and_read_candle_round_trips_exact_decimals(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    record = _record(candles_mod.ms_to_utc(HOUR1_MS), close="100.123456789012")
    candles_mod.write_candle(root, "BTCUSDT", record)

    table = candles_mod._read_candles(root, "BTCUSDT")
    assert table.num_rows == 1
    recovered = candles_mod.table_to_candle_records(table)[0]
    assert recovered.close == Decimal("100.123456789012")
    assert isinstance(recovered.close, Decimal)
    assert recovered.complete is True
    assert recovered.n_trades == 1


def test_write_candle_twice_for_same_hour_replaces_not_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    ts = candles_mod.ms_to_utc(HOUR1_MS)
    candles_mod.write_candle(root, "BTCUSDT", _record(ts, close="1", complete=False))
    candles_mod.write_candle(root, "BTCUSDT", _record(ts, close="2", complete=True))

    table = candles_mod._read_candles(root, "BTCUSDT")
    assert table.num_rows == 1
    recovered = candles_mod.table_to_candle_records(table)[0]
    assert recovered.close == Decimal("2")
    assert recovered.complete is True


def test_write_candle_appends_a_second_hour_in_the_same_month(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    candles_mod.write_candle(root, "BTCUSDT", _record(candles_mod.ms_to_utc(HOUR0_MS)))
    candles_mod.write_candle(root, "BTCUSDT", _record(candles_mod.ms_to_utc(HOUR1_MS)))

    table = candles_mod._read_candles(root, "BTCUSDT")
    assert table.num_rows == 2
    ts_values = table.column("ts").to_pylist()
    assert ts_values == sorted(ts_values)


def test_read_candles_missing_dataset_returns_empty_typed_table(tmp_path: Path) -> None:
    table = candles_mod._read_candles(tmp_path / "parquet", "NOPE")
    assert table.num_rows == 0
    assert table.schema == candles_mod.TABDEAL_KLINE_SCHEMA


def test_last_written_close_ms_none_when_nothing_written(tmp_path: Path) -> None:
    assert candles_mod.last_written_close_ms(tmp_path / "parquet", "BTCUSDT") is None


def test_last_written_close_ms_returns_the_most_recent_hour(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    candles_mod.write_candle(root, "BTCUSDT", _record(candles_mod.ms_to_utc(HOUR0_MS)))
    candles_mod.write_candle(root, "BTCUSDT", _record(candles_mod.ms_to_utc(HOUR1_MS)))
    assert candles_mod.last_written_close_ms(root, "BTCUSDT") == HOUR1_MS


# --- M1/D-028: the raw reader is private, not a second public read path --------------------------


def test_read_candles_is_private_not_a_public_reexport() -> None:
    assert "read_candles" not in candles_mod.__all__
    assert not hasattr(candles_mod, "read_candles")
    assert hasattr(candles_mod, "_read_candles")
