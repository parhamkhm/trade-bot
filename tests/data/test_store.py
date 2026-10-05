"""Tests for src/tbot/data/store.py: Parquet layout, exact-decimal round-trip, sidecar I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tbot.core.types import Timeframe
from tbot.data import store as store_mod


def _record(hour: int, *, close: str = "100") -> store_mod.KlineRecord:
    return store_mod.KlineRecord(
        ts=datetime(2024, 6, 1, hour, 0, 0, tzinfo=UTC),
        open=Decimal("99.500000000000"),
        high=Decimal("101.250000000000"),
        low=Decimal("98.750000000000"),
        close=Decimal(close),
        volume=Decimal("12.345678901234"),
        quote_volume=Decimal("1234.567890123456"),
        trades=42,
    )


def test_partition_dir_and_month_part_path_layout(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    part = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    assert part == (
        root
        / "klines"
        / "source=binance"
        / "symbol=BTCUSDT"
        / "timeframe=1h"
        / "year=2024"
        / "part-2024-06.parquet"
    )


def test_write_and_read_round_trips_exact_decimals(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    records = [_record(0, close="100.123456789012"), _record(1, close="101.000000000001")]
    part_path = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    written = store_mod.write_month_part(part_path, records)
    assert written == 2

    table = store_mod._read_symbol_timeframe(root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    assert table.num_rows == 2
    recovered = store_mod.table_to_records(table)
    assert recovered[0].close == Decimal("100.123456789012")
    assert recovered[1].close == Decimal("101.000000000001")
    assert isinstance(recovered[0].close, Decimal)


def test_write_month_part_sorts_rows_by_ts(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    part_path = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    store_mod.write_month_part(part_path, [_record(2), _record(0), _record(1)])
    table = store_mod._read_symbol_timeframe(root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    ts_values = table.column("ts").to_pylist()
    assert ts_values == sorted(ts_values)


def test_read_symbol_timeframe_missing_dataset_returns_empty(tmp_path: Path) -> None:
    table = store_mod._read_symbol_timeframe(
        tmp_path / "parquet", source="binance", symbol="NOPE", timeframe=Timeframe.H1
    )
    assert table.num_rows == 0
    assert table.schema == store_mod.KLINE_SCHEMA


def test_read_symbol_timeframe_respects_start_end(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    part_2024 = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    store_mod.write_month_part(part_2024, [_record(0), _record(1), _record(2)])

    table = store_mod._read_symbol_timeframe(
        root,
        source="binance",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        start=datetime(2024, 6, 1, 1, tzinfo=UTC),
        end=datetime(2024, 6, 1, 2, tzinfo=UTC),
    )
    assert table.num_rows == 1
    assert table.column("ts").to_pylist()[0] == datetime(2024, 6, 1, 1, tzinfo=UTC)


def test_write_month_part_overwrites_on_rerun(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    part_path = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    store_mod.write_month_part(part_path, [_record(0, close="1")])
    store_mod.write_month_part(part_path, [_record(0, close="2"), _record(1, close="3")])

    table = store_mod._read_symbol_timeframe(root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    assert table.num_rows == 2  # the second write replaced the file wholesale, no duplication


def test_sidecar_round_trip(tmp_path: Path) -> None:
    path = store_mod.sidecar_path(
        tmp_path / "parquet", source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.DatasetSidecar(
        source="binance",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        rows=3,
        first_ts=datetime(2024, 6, 1, tzinfo=UTC),
        last_ts=datetime(2024, 6, 1, 3, tzinfo=UTC),
        holdout_start=datetime(2025, 10, 1, tzinfo=UTC),
        downloaded_at=datetime(2024, 7, 1, tzinfo=UTC),
    )
    sidecar.add_file("BTCUSDT-1h-2024-06.zip", "abc123", verified=True)
    sidecar.gaps.append(
        store_mod.GapRecord(
            from_ts=datetime(2024, 6, 1, 1, tzinfo=UTC),
            to_ts=datetime(2024, 6, 1, 3, tzinfo=UTC),
            missing_bars=1,
            classification="exchange_outage",
        )
    )
    store_mod.save_sidecar(path, sidecar)

    loaded = store_mod.load_sidecar(path, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    assert loaded.rows == 3
    assert loaded.has_verified_file("BTCUSDT-1h-2024-06.zip")
    assert loaded.files[0].sha256 == "abc123"
    assert loaded.gaps[0].classification == "exchange_outage"
    assert loaded.first_ts == datetime(2024, 6, 1, tzinfo=UTC)


def test_gap_record_classified_by_round_trips_when_set() -> None:
    gap = store_mod.GapRecord(
        from_ts=datetime(2024, 6, 1, 1, tzinfo=UTC),
        to_ts=datetime(2024, 6, 1, 3, tzinfo=UTC),
        missing_bars=1,
        classification="exchange_outage",
        classified_by="anomaly_overlap",
    )
    restored = store_mod.GapRecord.from_dict(gap.to_dict())
    assert restored.classified_by == "anomaly_overlap"


def test_gap_record_classified_by_defaults_to_legacy_when_absent_from_disk() -> None:
    """MINOR-4 (sixth fix round, amends m-J): a sidecar written by a human (or by code before
    this field existed) has a non-"unknown" classification but no ``classified_by`` key at all.
    That silence only proves no CURRENT rule's name was recorded -- it is loaded as ``"legacy"``,
    not ``"manual"``, so ``_classify_fresh_gaps`` can re-check it against the current rules
    instead of trusting a possibly stale/over-broad old label forever. A human's own
    classification must now set ``classified_by: "manual"`` explicitly to be exempt from that
    re-check (see ``test_gap_record_classified_by_manual_round_trips_when_set_explicitly``)."""
    data = {
        "from": "2024-06-01T01:00:00Z",
        "to": "2024-06-01T03:00:00Z",
        "missing_bars": 1,
        "classification": "maintenance_announced",
        # no "classified_by" key -- as a hand-written or pre-D-036-amendment sidecar would have.
    }
    restored = store_mod.GapRecord.from_dict(data)
    assert restored.classification == "maintenance_announced"
    assert restored.classified_by == "legacy"


def test_gap_record_classified_by_manual_round_trips_when_set_explicitly() -> None:
    """MINOR-4: a human's own classification, written with ``classified_by: "manual"`` present
    explicitly in the sidecar, round-trips as ``"manual"`` -- never reinterpreted as
    ``"legacy"`` (the key is present, so the ``from_dict`` fallback never fires)."""
    data = {
        "from": "2024-06-01T01:00:00Z",
        "to": "2024-06-01T03:00:00Z",
        "missing_bars": 1,
        "classification": "maintenance_announced",
        "classified_by": "manual",
    }
    restored = store_mod.GapRecord.from_dict(data)
    assert restored.classification == "maintenance_announced"
    assert restored.classified_by == "manual"


def test_gap_record_unknown_classification_keeps_classified_by_none() -> None:
    data = {
        "from": "2024-06-01T01:00:00Z",
        "to": "2024-06-01T03:00:00Z",
        "missing_bars": 1,
        "classification": "unknown",
    }
    restored = store_mod.GapRecord.from_dict(data)
    assert restored.classified_by is None


def test_load_sidecar_missing_file_returns_fresh_sidecar(tmp_path: Path) -> None:
    path = store_mod.sidecar_path(
        tmp_path / "parquet", source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.load_sidecar(path, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    assert sidecar.rows == 0
    assert sidecar.files == []
    assert sidecar.verified_file_names() == set()


# --- M1/D-028: the raw reader is private, not a second public read path --------------------------


def test_read_symbol_timeframe_is_private_not_a_public_reexport() -> None:
    assert "read_symbol_timeframe" not in store_mod.__all__
    assert not hasattr(store_mod, "read_symbol_timeframe")
    assert hasattr(store_mod, "_read_symbol_timeframe")


# --- decision D-036: AnomalyRecord persistence in the sidecar -----------------------------------


def _anomaly(
    open_hour: int, *, classification: str = "short", action: str = "stored_flagged"
) -> store_mod.AnomalyRecord:
    return store_mod.AnomalyRecord(
        raw_open_ts=datetime(2024, 6, 1, open_hour, tzinfo=UTC),
        raw_close_ts=datetime(2024, 6, 1, open_hour, 0, 5, tzinfo=UTC),
        duration_seconds=5.0,
        n_trades=3,
        volume=Decimal("0.80000000"),
        classification=classification,
        action=action,
    )


def test_anomaly_record_round_trips_through_dict() -> None:
    original = _anomaly(3, classification="empty_irregular", action="dropped")
    recovered = store_mod.AnomalyRecord.from_dict(original.to_dict())
    assert recovered == original
    assert isinstance(recovered.volume, Decimal)


def test_anomaly_record_allows_close_before_open() -> None:
    """``empty_irregular`` rows can have a close_time before their open_time -- the dataclass
    must not reject that ordering, only require both timestamps to be UTC-aware."""
    record = store_mod.AnomalyRecord(
        raw_open_ts=datetime(2020, 12, 21, 14, tzinfo=UTC),
        raw_close_ts=datetime(2020, 12, 21, 13, 49, 1, tzinfo=UTC),
        duration_seconds=-759.0,
        n_trades=0,
        volume=Decimal("0"),
        classification="empty_irregular",
        action="dropped",
    )
    assert record.raw_close_ts < record.raw_open_ts


def test_sidecar_round_trips_anomalies(tmp_path: Path) -> None:
    path = store_mod.sidecar_path(
        tmp_path / "parquet", source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.DatasetSidecar(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    sidecar.anomalies.append(_anomaly(3))
    store_mod.save_sidecar(path, sidecar)

    loaded = store_mod.load_sidecar(path, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    assert len(loaded.anomalies) == 1
    assert loaded.anomalies[0].classification == "short"
    assert loaded.anomalies[0].volume == Decimal("0.80000000")


def test_replace_anomalies_in_month_drops_only_that_months_rows() -> None:
    sidecar = store_mod.DatasetSidecar(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    june_anomaly = _anomaly(3)
    july_anomaly = store_mod.AnomalyRecord(
        raw_open_ts=datetime(2024, 7, 1, 5, tzinfo=UTC),
        raw_close_ts=datetime(2024, 7, 1, 5, 0, 5, tzinfo=UTC),
        duration_seconds=5.0,
        n_trades=1,
        volume=Decimal("0.1"),
        classification="short",
        action="stored_flagged",
    )
    sidecar.anomalies = [june_anomaly, july_anomaly]

    # re-processing June must only replace June's rows, never July's.
    new_june_anomaly = _anomaly(4, classification="long", action="dropped")
    sidecar.replace_anomalies_in_month(2024, 6, [new_june_anomaly])

    assert sidecar.anomalies == [new_june_anomaly, july_anomaly]


def test_replace_anomalies_in_month_is_idempotent_on_rerun() -> None:
    """Re-ingesting the same month twice with the same parsed anomalies must not duplicate rows
    (decision D-036, point 7: idempotent re-runs)."""
    sidecar = store_mod.DatasetSidecar(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    anomaly = _anomaly(3)
    sidecar.replace_anomalies_in_month(2024, 6, [anomaly])
    sidecar.replace_anomalies_in_month(2024, 6, [anomaly])
    assert sidecar.anomalies == [anomaly]
