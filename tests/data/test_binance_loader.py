"""Tests for src/tbot/data/binance_loader.py.

No test reaches the network: downloads are mocked with ``respx``. Fixtures under
tests/data/fixtures/ are small, hand-picked excerpts of real Binance kline rows (confirmed
against data.binance.vision while building this module) covering the millisecond era, the
microsecond era (post 2025-01-01, decision D-017) and the exact month boundary between them.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from tbot.core.config import DataConfig
from tbot.core.types import Bar, Timeframe
from tbot.data import binance_loader as bl
from tbot.data import quality as quality_mod
from tbot.data import store as store_mod

FIXTURES = Path(__file__).parent / "fixtures"


def _csv_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _zip_bytes(csv_name: str, inner_filename: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_filename, _csv_bytes(csv_name))
    return buf.getvalue()


def make_data_config(tmp_path: Path, **overrides: object) -> DataConfig:
    """Builds a ``DataConfig`` for tests. ``holdout_start`` is deliberately left at its own
    default (i.e. ``bl.CANONICAL_HOLDOUT_START``, decision D-026) unless a test explicitly wants
    to exercise the B1 mismatch-refusal path -- any other value now makes the loader refuse."""
    defaults: dict[str, object] = {
        "parquet_root": tmp_path / "parquet",
        "raw_root": tmp_path / "raw",
        "tabdeal_db": tmp_path / "tabdeal.sqlite",
        "symbols": ("BTCUSDT",),
        "timeframes": (Timeframe.H1,),
        "history_start": datetime(2024, 6, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return DataConfig(**defaults)  # type: ignore[arg-type]


def _load_sidecar(config: DataConfig, symbol: str, timeframe: Timeframe) -> store_mod.DatasetSidecar:
    path = store_mod.sidecar_path(config.parquet_root, source="binance", symbol=symbol, timeframe=timeframe)
    return store_mod.load_sidecar(path, source="binance", symbol=symbol, timeframe=timeframe)


def _write_synthetic_bars(
    config: DataConfig, *, symbol: str, start: datetime, count: int, timeframe: Timeframe = Timeframe.H1
) -> None:
    """Write ``count`` synthetic 1h bars directly into the store, bypassing CSV parsing --
    used to place rows on a specific side of the canonical holdout boundary without needing a
    real Binance fixture file that happens to straddle it."""
    records = [
        store_mod.KlineRecord(
            ts=start + i * timeframe.delta,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("1"),
            quote_volume=Decimal("100"),
            trades=1,
        )
        for i in range(count)
    ]
    by_month: dict[tuple[int, int], list[store_mod.KlineRecord]] = {}
    for r in records:
        by_month.setdefault((r.ts.year, r.ts.month), []).append(r)
    for (year, month), month_records in by_month.items():
        part_path = store_mod.month_part_path(
            config.parquet_root, source="binance", symbol=symbol, timeframe=timeframe, year=year, month=month
        )
        store_mod.write_month_part(part_path, month_records)


# --- timestamp unit detection -------------------------------------------------------------


def test_detect_timestamp_unit_ms() -> None:
    assert bl.detect_timestamp_unit(1717200000000) == "ms"


def test_detect_timestamp_unit_us() -> None:
    assert bl.detect_timestamp_unit(1735689600000000) == "us"


def test_detect_timestamp_unit_rejects_nonsense() -> None:
    with pytest.raises(bl.TimestampUnitError):
        bl.detect_timestamp_unit(42)


def test_to_utc_datetime_ms_is_exact() -> None:
    dt = bl.to_utc_datetime(1717200000000, "ms")
    assert dt == datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)


def test_to_utc_datetime_us_is_exact() -> None:
    dt = bl.to_utc_datetime(1735689600000000, "us")
    assert dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


# --- CSV parsing: millisecond era ---------------------------------------------------------


def test_parse_kline_csv_ms_era_close_ts_and_types() -> None:
    parsed = bl.parse_kline_csv_bytes(_csv_bytes("klines_1h_2024-06_ms.txt"), timeframe=Timeframe.H1)
    records = parsed.records
    assert len(records) == 3
    assert parsed.anomalies == ()
    first = records[0]
    assert first.ts == datetime(2024, 6, 1, 1, 0, 0, tzinfo=UTC)  # close = open (00:00) + 1h
    assert isinstance(first.open, Decimal)
    assert first.open == Decimal("67540.01000000")
    assert first.trades == 27480
    # strictly increasing, one hour apart
    for prev, curr in pairwise(records):
        assert curr.ts - prev.ts == Timeframe.H1.delta


# --- CSV parsing: microsecond era (D-017) -------------------------------------------------


def test_parse_kline_csv_us_era_close_ts_and_types() -> None:
    parsed = bl.parse_kline_csv_bytes(_csv_bytes("klines_1h_2025-01_us.txt"), timeframe=Timeframe.H1)
    records = parsed.records
    assert len(records) == 3
    assert parsed.anomalies == ()
    first = records[0]
    assert first.ts == datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)  # close = open (00:00) + 1h
    assert isinstance(first.close, Decimal)
    assert first.close == Decimal("94401.14000000")


# --- the exact ms -> us boundary (2024-12 -> 2025-01) -------------------------------------


def test_parse_kline_csv_handles_the_ms_to_us_boundary() -> None:
    dec_records = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_2024-12-31T23_ms_tail.txt"), timeframe=Timeframe.H1
    ).records
    jan_records = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_2025-01-01T00_us_head.txt"), timeframe=Timeframe.H1
    ).records
    assert len(dec_records) == 1
    assert len(jan_records) == 1
    last_dec_close = dec_records[0].ts
    first_jan_close = jan_records[0].ts
    assert last_dec_close == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert first_jan_close == datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)
    # exactly one bar apart across the unit switchover -- no silent order-of-magnitude shift
    assert first_jan_close - last_dec_close == Timeframe.H1.delta


# --- CSV parsing: fails loudly on bad data -------------------------------------------------


def test_parse_kline_csv_rejects_mismatched_units_within_one_row() -> None:
    # open_time looks like ms, close_time looks like us -- must never be silently accepted.
    bad_row = "1717200000000,100,101,99,100,1,1735693199999999,100,1,1,1,0\n"
    with pytest.raises(bl.TimestampUnitError):
        bl.parse_kline_csv_bytes(bad_row.encode(), timeframe=Timeframe.H1)


def test_parse_kline_csv_skips_a_header_row_defensively() -> None:
    header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,x,y,z\n"
    body = _csv_bytes("klines_1h_2024-06_ms.txt").decode()
    parsed = bl.parse_kline_csv_bytes((header + body).encode(), timeframe=Timeframe.H1)
    assert len(parsed.records) == 3
    assert parsed.anomalies == ()


# --- decision D-036: per-row source anomalies never abort the file ------------------------------


def test_parse_kline_csv_classifies_a_short_bar_and_still_stores_it_flagged() -> None:
    """close_time is nowhere near open_time + 1h - 1ms, but the row still has a trade --
    decision D-036 classifies this ``short`` and stores it at the normal label (causal: the
    label sits at or after the real data), flagged as an anomaly, instead of raising and
    aborting the whole file as the old behaviour did."""
    bad_row = "1717200000000,100,101,99,100,1,1717200005000,100,1,1,1,0\n"
    parsed = bl.parse_kline_csv_bytes(bad_row.encode(), timeframe=Timeframe.H1)

    assert len(parsed.records) == 1
    assert parsed.records[0].ts == datetime(2024, 6, 1, 1, 0, 0, tzinfo=UTC)
    assert len(parsed.anomalies) == 1
    anomaly = parsed.anomalies[0]
    assert anomaly.classification == "short"
    assert anomaly.action == "stored_flagged"
    assert anomaly.n_trades == 1
    assert anomaly.raw_open_ts == datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)


def test_parse_kline_csv_classifies_a_long_bar_and_drops_it() -> None:
    """``close_time`` lands about a whole extra interval after ``open_time + delta``: the row's
    data extends past its label, which would be look-ahead if stored there. Dropped, not raised."""
    bad_row = "1717200000000,100,101,99,100,5,1717207199999,500,10,2,250,0\n"
    parsed = bl.parse_kline_csv_bytes(bad_row.encode(), timeframe=Timeframe.H1)

    assert parsed.records == ()
    assert len(parsed.anomalies) == 1
    anomaly = parsed.anomalies[0]
    assert anomaly.classification == "long"
    assert anomaly.action == "dropped"
    assert anomaly.n_trades == 10


def test_parse_kline_csv_classifies_an_empty_irregular_bar_close_before_open() -> None:
    """Real event (decision D-036 brief): BTCUSDT 1h close_time before open_time, zero volume,
    zero trades. Dropped -- there is nothing causal to store."""
    parsed = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_anomalies_empty_irregular.txt"), timeframe=Timeframe.H1
    )

    # the two normal neighbours are stored; the zero-trade close-before-open row is dropped
    assert len(parsed.records) == 2
    assert len(parsed.anomalies) == 1
    anomaly = parsed.anomalies[0]
    assert anomaly.classification == "empty_irregular"
    assert anomaly.action == "dropped"
    assert anomaly.n_trades == 0
    assert anomaly.raw_close_ts < anomaly.raw_open_ts  # close really is before open


def test_parse_kline_csv_classifies_a_short_outage_bar_from_a_real_event() -> None:
    """Real event (decision D-036 brief): BTCUSDT 1h 2018-01-04 03:00, duration 14.838s, 34
    trades -- a short bar at the start of an exchange outage. Stored at the normal label."""
    parsed = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_anomalies_short_outage.txt"), timeframe=Timeframe.H1
    )

    assert len(parsed.records) == 3  # the short bar IS stored (flagged), plus its two neighbours
    assert len(parsed.anomalies) == 1
    anomaly = parsed.anomalies[0]
    assert anomaly.classification == "short"
    assert anomaly.action == "stored_flagged"
    assert anomaly.n_trades == 34
    assert anomaly.duration_seconds == pytest.approx(14.838)
    stored_ts = [r.ts for r in parsed.records]
    assert datetime(2018, 1, 4, 4, 0, 0, tzinfo=UTC) in stored_ts  # 03:00 bar stored at its label


def test_parse_kline_csv_classifies_a_misaligned_run_and_drops_it() -> None:
    """Real event (decision D-036 brief): roughly two days of 1h bars opening at HH:28:14.789
    instead of the hour -- off-grid, with an otherwise-normal 1h duration. Grid alignment is
    checked before duration, so these are dropped as ``misaligned`` regardless of how clean
    their close_time otherwise looks."""
    parsed = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_anomalies_misaligned_run.txt"), timeframe=Timeframe.H1
    )

    assert len(parsed.records) == 2  # only the two on-grid neighbours are stored
    assert len(parsed.anomalies) == 2
    assert all(a.classification == "misaligned" for a in parsed.anomalies)
    assert all(a.action == "dropped" for a in parsed.anomalies)
    for anomaly in parsed.anomalies:
        assert not bl._is_on_grid(anomaly.raw_open_ts, Timeframe.H1)


# --- checksum parsing -----------------------------------------------------------------------


def test_parse_checksum_text_official_format() -> None:
    text = "2e1f968fa34b9feabfb19cc6eec47a146e20d64a706f4b17d0babf4d2f475c39  BTCUSDT-1h-2024-06.zip\n"
    assert bl.parse_checksum_text(text, "BTCUSDT-1h-2024-06.zip") == (
        "2e1f968fa34b9feabfb19cc6eec47a146e20d64a706f4b17d0babf4d2f475c39"
    )


def test_parse_checksum_text_sha256sum_binary_format() -> None:
    text = "2e1f968fa34b9feabfb19cc6eec47a146e20d64a706f4b17d0babf4d2f475c39 *BTCUSDT-1h-2024-06.zip\n"
    assert bl.parse_checksum_text(text, "BTCUSDT-1h-2024-06.zip") == (
        "2e1f968fa34b9feabfb19cc6eec47a146e20d64a706f4b17d0babf4d2f475c39"
    )


def test_parse_checksum_text_missing_filename_raises() -> None:
    with pytest.raises(ValueError, match="no checksum entry"):
        bl.parse_checksum_text("deadbeef  some-other-file.zip\n", "BTCUSDT-1h-2024-06.zip")


# --- URL / month helpers ---------------------------------------------------------------------


def test_monthly_zip_url_matches_observed_shape() -> None:
    url = bl.monthly_zip_url("https://data.binance.vision", "BTCUSDT", Timeframe.H1, 2024, 6)
    assert url == "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-06.zip"


def test_checksum_url_appends_suffix() -> None:
    assert bl.checksum_url("https://x/y.zip") == "https://x/y.zip.CHECKSUM"


def test_months_between_inclusive() -> None:
    assert list(bl.months_between(2024, 11, 2025, 2)) == [
        (2024, 11),
        (2024, 12),
        (2025, 1),
        (2025, 2),
    ]


def test_latest_complete_month() -> None:
    assert bl.latest_complete_month(datetime(2026, 10, 1, tzinfo=UTC)) == (2026, 9)
    assert bl.latest_complete_month(datetime(2026, 1, 15, tzinfo=UTC)) == (2025, 12)


# --- ingest orchestration (network mocked with respx) ----------------------------------------


@respx.mock
def test_ingest_downloads_verifies_and_stores(tmp_path: Path) -> None:
    config = make_data_config(tmp_path)
    zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2024-06.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2024-06.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with httpx.Client() as client:
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 7, 15, tzinfo=UTC),
        )

    assert outcome.downloaded == ("BTCUSDT-1h-2024-06.zip",)
    assert outcome.skipped == ()
    assert outcome.rows_in_store == 3

    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    assert part_path.is_file()

    sidecar = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert sidecar.has_verified_file("BTCUSDT-1h-2024-06.zip")
    assert sidecar.files[0].sha256 == checksum_hex
    assert sidecar.rows == 3


@respx.mock
def test_ingest_rerun_is_idempotent(tmp_path: Path) -> None:
    config = make_data_config(tmp_path)
    zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2024-06.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    checksum_route = respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2024-06.zip\n")
    )
    zip_route = respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    now = datetime(2024, 7, 15, tzinfo=UTC)
    with httpx.Client() as client:
        first = bl.ingest_symbol_timeframe(
            client, symbol="BTCUSDT", timeframe=Timeframe.H1, data_config=config, now=now
        )
        second = bl.ingest_symbol_timeframe(
            client, symbol="BTCUSDT", timeframe=Timeframe.H1, data_config=config, now=now
        )

    assert first.downloaded == ("BTCUSDT-1h-2024-06.zip",)
    assert second.downloaded == ()
    assert second.skipped == ("BTCUSDT-1h-2024-06.zip",)
    # the second run must not have re-hit the network at all
    assert checksum_route.call_count == 1
    assert zip_route.call_count == 1
    assert second.rows_in_store == first.rows_in_store


@respx.mock
def test_ingest_checksum_mismatch_fails_loudly_and_writes_nothing(tmp_path: Path) -> None:
    config = make_data_config(tmp_path)
    zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2024-06.csv")
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    wrong_hex = "0" * 64
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{wrong_hex}  BTCUSDT-1h-2024-06.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with httpx.Client() as client, pytest.raises(bl.ChecksumMismatchError):
        bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 7, 15, tzinfo=UTC),
        )

    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    assert not part_path.is_file()


# --- decision D-036: ingest-level anomaly persistence + gap auto-classification -----------------


@respx.mock
def test_ingest_persists_anomalies_and_auto_classifies_the_resulting_gap_as_exchange_outage(
    tmp_path: Path,
) -> None:
    """The misaligned-run fixture drops two off-grid rows, leaving a real gap in the stored
    series. Decision D-036 (point 4): that gap must come out classified ``exchange_outage``, not
    ``unknown``, because the dropped anomaly rows already explain it -- and the anomaly rows
    themselves must be in the outcome and the persisted sidecar (point 7)."""
    config = make_data_config(tmp_path)
    zip_bytes = _zip_bytes("klines_1h_anomalies_misaligned_run.txt", "BTCUSDT-1h-2024-06.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2024-06.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with httpx.Client() as client:
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 7, 15, tzinfo=UTC),
        )

    assert len(outcome.anomalies) == 2
    assert all(a.classification == "misaligned" for a in outcome.anomalies)
    assert len(outcome.gaps) == 1
    assert outcome.gaps[0].classification == "exchange_outage"

    sidecar = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert len(sidecar.anomalies) == 2
    assert sidecar.gaps[0].classification == "exchange_outage"


def test_ingest_rerun_skip_still_reports_persisted_anomalies_report_only_style(
    tmp_path: Path,
) -> None:
    """Decision D-036 (point 7): a month whose file is already verified + on disk is skipped
    (never re-parsed), but the anomalies recorded on the FIRST run must still show up on a
    second, report-only-style run -- the report must not lose anomaly rows just because the CSV
    was not re-downloaded."""
    config = make_data_config(tmp_path)
    _ingest_fixture_month(
        config, symbol="BTCUSDT", year=2024, month=6, csv_fixture="klines_1h_anomalies_misaligned_run.txt"
    )
    parsed = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_anomalies_misaligned_run.txt"), timeframe=Timeframe.H1
    )
    sidecar_file = store_mod.sidecar_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    sidecar.replace_anomalies_in_month(2024, 6, parsed.anomalies)
    store_mod.save_sidecar(sidecar_file, sidecar)

    with httpx.Client() as client:  # no respx mock registered: a reparse would error loudly
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 7, 15, tzinfo=UTC),
        )

    assert outcome.downloaded == ()  # the month was skipped, not re-parsed
    assert len(outcome.anomalies) == 2
    assert all(a.classification == "misaligned" for a in outcome.anomalies)


# --- finding m-J: tightened gap auto-classification (rules (a) and (b), plus negatives) ---------


def test_classify_gap_rule_a_dropped_anomaly_overlapping_missing_window() -> None:
    """Rule (a): a *dropped* anomaly whose raw window overlaps the missing-bar window
    ``(from_ts, to_ts - delta]`` -> ``exchange_outage`` / ``anomaly_overlap``."""
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta  # one bar missing, at 01:00
    anomaly = store_mod.AnomalyRecord(
        raw_open_ts=from_ts + timedelta(minutes=20),
        raw_close_ts=from_ts + timedelta(minutes=40),
        duration_seconds=1200.0,
        n_trades=5,
        volume=Decimal("1"),
        classification="misaligned",
        action="dropped",
    )
    result = bl._classify_gap_from_anomalies(
        from_ts=from_ts, to_ts=to_ts, timeframe=Timeframe.H1, anomalies=(anomaly,)
    )
    assert result == ("exchange_outage", "anomaly_overlap")


def test_classify_gap_rule_a_ignores_a_stored_short_bar_not_a_dropped_row() -> None:
    """A ``short`` anomaly is ``action == "stored_flagged"``, not ``"dropped"`` -- it must never
    satisfy rule (a) even if its raw window happens to sit inside the missing-bar window."""
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta
    short_anomaly = store_mod.AnomalyRecord(
        raw_open_ts=from_ts + timedelta(minutes=20),
        raw_close_ts=from_ts + timedelta(minutes=21),
        duration_seconds=60.0,
        n_trades=3,
        volume=Decimal("1"),
        classification="short",
        action="stored_flagged",
    )
    result = bl._classify_gap_from_anomalies(
        from_ts=from_ts, to_ts=to_ts, timeframe=Timeframe.H1, anomalies=(short_anomaly,)
    )
    assert result is None


def test_classify_gap_rule_b_short_bar_ends_right_before_gap() -> None:
    """Rule (b): a stored ``short`` bar whose close label (``raw_open_ts + delta``) is exactly
    the gap's ``from_ts`` -> ``exchange_outage_after_short_bar`` / ``after_short_bar``."""
    from_ts = datetime(2024, 6, 1, 1, tzinfo=UTC)
    to_ts = from_ts + 3 * Timeframe.H1.delta
    short_anomaly = store_mod.AnomalyRecord(
        raw_open_ts=from_ts - Timeframe.H1.delta,  # 00:00 -- its stored label is from_ts (01:00)
        raw_close_ts=from_ts - timedelta(minutes=55),
        duration_seconds=300.0,
        n_trades=7,
        volume=Decimal("0.5"),
        classification="short",
        action="stored_flagged",
    )
    result = bl._classify_gap_from_anomalies(
        from_ts=from_ts, to_ts=to_ts, timeframe=Timeframe.H1, anomalies=(short_anomaly,)
    )
    assert result == ("exchange_outage_after_short_bar", "after_short_bar")


def test_classify_gap_negative_unrelated_gap_followed_by_a_short_bar_stays_unknown() -> None:
    """Finding m-J's named bug: a gap whose cause is unrelated to any recorded anomaly, but is
    immediately FOLLOWED by a stored short bar (the short bar's label == gap.to_ts, not
    gap.from_ts), must stay unclassified. Also covers the related old bug where a dropped
    anomaly sitting inside the next PRESENT bar's own window (not the missing-bar window) wrongly
    "explained" an unrelated gap under the old closed-interval ``[from_ts, to_ts]`` check.
    """
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta  # one bar missing, at 01:00

    # A dropped row sitting inside the PRESENT bar's own window [to_ts, to_ts+delta) -- NOT the
    # missing-bar window (from_ts, to_ts - delta] == (00:00, 01:00] -- so it must not count.
    unrelated_dropped = store_mod.AnomalyRecord(
        raw_open_ts=to_ts + timedelta(minutes=10),
        raw_close_ts=to_ts + timedelta(minutes=20),
        duration_seconds=600.0,
        n_trades=2,
        volume=Decimal("1"),
        classification="misaligned",
        action="dropped",
    )
    # A short bar stored exactly AT to_ts (i.e. it is the present bar right after the gap, not
    # right before it) -- its label is gap.to_ts, so it must not satisfy rule (b) either.
    short_bar_after_gap = store_mod.AnomalyRecord(
        raw_open_ts=to_ts - Timeframe.H1.delta,
        raw_close_ts=to_ts - timedelta(seconds=5),
        duration_seconds=3595.0,
        n_trades=4,
        volume=Decimal("0.2"),
        classification="short",
        action="stored_flagged",
    )

    result = bl._classify_gap_from_anomalies(
        from_ts=from_ts,
        to_ts=to_ts,
        timeframe=Timeframe.H1,
        anomalies=(unrelated_dropped, short_bar_after_gap),
    )
    assert result is None


@respx.mock
def test_ingest_classifies_a_gap_after_a_short_bar_as_exchange_outage_after_short_bar(
    tmp_path: Path,
) -> None:
    """End-to-end (rule (b)): the short-then-outage fixture has a short bar immediately followed
    by a real multi-bar gap. The gap must come out ``exchange_outage_after_short_bar``, evidenced
    by ``classified_by == "after_short_bar"`` -- not the generic ``exchange_outage`` rule (a),
    since there is no *dropped* anomaly here at all, only a stored short bar."""
    config = make_data_config(tmp_path)
    zip_bytes = _zip_bytes("klines_1h_anomalies_short_then_outage.txt", "BTCUSDT-1h-2024-06.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2024-06.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with httpx.Client() as client:
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 7, 15, tzinfo=UTC),
        )

    assert len(outcome.anomalies) == 1
    assert outcome.anomalies[0].classification == "short"
    assert len(outcome.gaps) == 1
    assert outcome.gaps[0].classification == "exchange_outage_after_short_bar"
    assert outcome.gaps[0].classified_by == "after_short_bar"


# --- decision D-036 amendment: cross-symbol corroboration (exchange_wide_outage) ----------------


def _seed_gap(
    config: DataConfig,
    *,
    symbol: str,
    timeframe: Timeframe,
    from_ts: datetime,
    to_ts: datetime,
    **kwargs: object,
) -> None:
    sidecar_file = store_mod.sidecar_path(
        config.parquet_root, source="binance", symbol=symbol, timeframe=timeframe
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source="binance", symbol=symbol, timeframe=timeframe)
    sidecar.gaps.append(
        store_mod.GapRecord(from_ts=from_ts, to_ts=to_ts, missing_bars=1, **kwargs)  # type: ignore[arg-type]
    )
    store_mod.save_sidecar(sidecar_file, sidecar)


# --- MINOR-4 (sixth fix round, amends m-J): a "legacy" classification (no recorded provenance)
# is re-checked against the current rules, never trusted indefinitely like "manual" is ----------


def test_classify_fresh_gaps_rechecks_a_legacy_classification_against_current_rules() -> None:
    """A previous classification stamped ``"legacy"`` (no ``classified_by`` at all on disk --
    see ``store.GapRecord.from_dict``) must be re-derived from the current anomaly evidence, not
    trusted forever the way a genuine ``"manual"`` or current-rule classification is."""
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta
    finding = quality_mod.GapFinding(from_ts=from_ts, to_ts=to_ts, missing_bars=1)
    previous_legacy = store_mod.GapRecord(
        from_ts=from_ts,
        to_ts=to_ts,
        missing_bars=1,
        classification="some_stale_old_label",
        classified_by="legacy",
    )
    # Evidence that _classify_gap_from_anomalies's rule (a) matches: a dropped anomaly whose raw
    # window overlaps the missing-bar window (from_ts, to_ts - delta].
    anomaly = store_mod.AnomalyRecord(
        raw_open_ts=from_ts + timedelta(minutes=20),
        raw_close_ts=from_ts + timedelta(minutes=40),
        duration_seconds=1200.0,
        n_trades=5,
        volume=Decimal("1"),
        classification="misaligned",
        action="dropped",
    )

    result = bl._classify_fresh_gaps(
        [finding], previous_gaps=[previous_legacy], anomalies=[anomaly], timeframe=Timeframe.H1
    )

    assert len(result) == 1
    assert result[0].classification == "exchange_outage"  # re-derived, not the stale legacy label
    assert result[0].classified_by == "anomaly_overlap"


def test_classify_fresh_gaps_legacy_with_no_matching_rule_falls_back_to_unknown() -> None:
    """A "legacy" classification that no current rule can explain is discarded, not kept."""
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta
    finding = quality_mod.GapFinding(from_ts=from_ts, to_ts=to_ts, missing_bars=1)
    previous_legacy = store_mod.GapRecord(
        from_ts=from_ts,
        to_ts=to_ts,
        missing_bars=1,
        classification="some_stale_old_label",
        classified_by="legacy",
    )

    result = bl._classify_fresh_gaps(
        [finding], previous_gaps=[previous_legacy], anomalies=[], timeframe=Timeframe.H1
    )

    assert result[0].classification == "unknown"
    assert result[0].classified_by is None


def test_classify_fresh_gaps_never_revisits_a_genuine_manual_classification() -> None:
    """A classification explicitly stamped ``classified_by="manual"`` is kept untouched, exactly
    like before MINOR-4 -- the amendment only concerns the no-provenance ``"legacy"`` case."""
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = from_ts + 2 * Timeframe.H1.delta
    finding = quality_mod.GapFinding(from_ts=from_ts, to_ts=to_ts, missing_bars=1)
    previous_manual = store_mod.GapRecord(
        from_ts=from_ts,
        to_ts=to_ts,
        missing_bars=1,
        classification="maintenance_announced",
        classified_by="manual",
    )

    result = bl._classify_fresh_gaps(
        [finding], previous_gaps=[previous_manual], anomalies=[], timeframe=Timeframe.H1
    )

    assert result[0].classification == "maintenance_announced"
    assert result[0].classified_by == "manual"


def test_cross_symbol_pass_classifies_an_identical_window_as_exchange_wide_outage(
    tmp_path: Path,
) -> None:
    config = make_data_config(tmp_path, symbols=("BTCUSDT", "ETHUSDT"))
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = datetime(2024, 6, 1, 2, tzinfo=UTC)
    _seed_gap(config, symbol="BTCUSDT", timeframe=Timeframe.H1, from_ts=from_ts, to_ts=to_ts)
    _seed_gap(config, symbol="ETHUSDT", timeframe=Timeframe.H1, from_ts=from_ts, to_ts=to_ts)

    reclassified = bl.apply_cross_symbol_gap_classification(
        config, symbols=("BTCUSDT", "ETHUSDT"), timeframe=Timeframe.H1
    )

    assert reclassified == {"BTCUSDT": 1, "ETHUSDT": 1}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        sidecar = _load_sidecar(config, symbol, Timeframe.H1)
        assert sidecar.gaps[0].classification == "exchange_wide_outage"
        assert sidecar.gaps[0].classified_by == "cross_symbol"


def test_cross_symbol_pass_leaves_a_one_symbol_only_gap_unknown(tmp_path: Path) -> None:
    config = make_data_config(tmp_path, symbols=("BTCUSDT", "ETHUSDT"))
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = datetime(2024, 6, 1, 2, tzinfo=UTC)
    _seed_gap(config, symbol="BTCUSDT", timeframe=Timeframe.H1, from_ts=from_ts, to_ts=to_ts)
    # ETHUSDT has no matching gap at all.

    reclassified = bl.apply_cross_symbol_gap_classification(
        config, symbols=("BTCUSDT", "ETHUSDT"), timeframe=Timeframe.H1
    )

    assert reclassified == {"BTCUSDT": 0, "ETHUSDT": 0}
    sidecar = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert sidecar.gaps[0].classification == "unknown"
    assert sidecar.gaps[0].classified_by is None


def test_cross_symbol_pass_never_overwrites_a_manual_classification(tmp_path: Path) -> None:
    """A classification a human already recorded (``classified_by`` absent/"manual", i.e. not
    one of the auto rules) must never be revisited by the cross-symbol pass, even when the exact
    same window is also a gap in the other symbol's series."""
    config = make_data_config(tmp_path, symbols=("BTCUSDT", "ETHUSDT"))
    from_ts = datetime(2024, 6, 1, 0, tzinfo=UTC)
    to_ts = datetime(2024, 6, 1, 2, tzinfo=UTC)
    _seed_gap(
        config,
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        from_ts=from_ts,
        to_ts=to_ts,
        classification="maintenance_announced",
        classified_by="manual",
    )
    _seed_gap(config, symbol="ETHUSDT", timeframe=Timeframe.H1, from_ts=from_ts, to_ts=to_ts)

    reclassified = bl.apply_cross_symbol_gap_classification(
        config, symbols=("BTCUSDT", "ETHUSDT"), timeframe=Timeframe.H1
    )

    assert reclassified["BTCUSDT"] == 0  # untouched -- already non-"unknown"
    assert reclassified["ETHUSDT"] == 1  # still gets the cross-symbol evidence
    btc_sidecar = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert btc_sidecar.gaps[0].classification == "maintenance_announced"
    assert btc_sidecar.gaps[0].classified_by == "manual"
    eth_sidecar = _load_sidecar(config, "ETHUSDT", Timeframe.H1)
    assert eth_sidecar.gaps[0].classification == "exchange_wide_outage"
    assert eth_sidecar.gaps[0].classified_by == "cross_symbol"


# --- NIT: asymmetric close-time tolerance (late side only tolerates the one-unit epsilon) -------


def test_parse_kline_csv_classifies_a_small_late_overshoot_as_long_not_normal() -> None:
    """NIT (fix round): the old symmetric +/-1s tolerance would have accepted a raw close_time
    landing ~0.3s after its nominal close as "normal" -- storing a row whose own raw data runs
    past its label, a (small) causality violation. The late side now only tolerates the one-unit
    (1ms) epsilon baked into the close-time convention itself, so this must be ``long`` and
    dropped even though ``abs(diff_seconds) <= 1.0`` still holds.
    """
    # open=2024-06-01T00:00:00Z (ms, on-grid); close_time = open + delta + 300ms (0.301s late
    # vs. the documented "open + delta - 1ms" convention).
    bad_row = "1717200000000,100,101,99,100,5,1717203600300,500,10,2,250,0\n"
    parsed = bl.parse_kline_csv_bytes(bad_row.encode(), timeframe=Timeframe.H1)

    assert parsed.records == ()
    assert len(parsed.anomalies) == 1
    anomaly = parsed.anomalies[0]
    assert anomaly.classification == "long"
    assert anomaly.action == "dropped"
    assert anomaly.duration_seconds == pytest.approx(3600.3)


def test_parse_kline_csv_still_tolerates_the_early_side_up_to_one_second() -> None:
    """The early-side tolerance is unchanged by the NIT fix: a close_time landing up to 1s
    *before* nominal is still just "normal", not an anomaly."""
    # close_time = open + delta - 1ms - 900ms = 0.9s early, still within the +/-1s early band.
    normal_row = "1717200000000,100,101,99,100,5,1717203599100,500,10,2,250,0\n"
    parsed = bl.parse_kline_csv_bytes(normal_row.encode(), timeframe=Timeframe.H1)

    assert len(parsed.records) == 1
    assert parsed.anomalies == ()


# --- NIT (sixth fix round): exact integer arithmetic at the one-unit epsilon boundary -----------


def test_parse_kline_csv_ms_row_exactly_at_the_late_epsilon_boundary_is_normal_not_long() -> None:
    """Repro of the round-3 reviewer's float-rounding finding: a close_time landing exactly one
    unit (1ms) after the documented "open + delta - 1ms" convention -- i.e. exactly at
    "open + delta" -- sits exactly on the late-side tolerance boundary and must be ``normal``.
    The old ``float`` comparison (``timedelta.total_seconds()``) rounded this exact case to a
    hair on the wrong side (measured: ``0.0010000000002 > 0.001``) and misclassified it ``long``.
    """
    open_ms = 1_717_200_000_000
    delta_ms = 3_600_000  # Timeframe.H1
    close_time_ms = open_ms + delta_ms  # exactly "open + delta" -- the late boundary itself
    row = f"{open_ms},100,101,99,100,5,{close_time_ms},500,10,2,250,0\n"

    parsed = bl.parse_kline_csv_bytes(row.encode(), timeframe=Timeframe.H1)

    assert parsed.anomalies == ()
    assert len(parsed.records) == 1
    assert parsed.records[0].ts == datetime(2024, 6, 1, 1, 0, 0, tzinfo=UTC)


def test_parse_kline_csv_ms_row_one_unit_past_the_late_epsilon_boundary_is_still_long() -> None:
    """One more unit past the boundary above must still be ``long`` and dropped -- the fix only
    corrects the exact-boundary case, it does not loosen the tolerance itself."""
    open_ms = 1_717_200_000_000
    delta_ms = 3_600_000
    close_time_ms = open_ms + delta_ms + 1  # one ms past the boundary
    row = f"{open_ms},100,101,99,100,5,{close_time_ms},500,10,2,250,0\n"

    parsed = bl.parse_kline_csv_bytes(row.encode(), timeframe=Timeframe.H1)

    assert parsed.records == ()
    assert len(parsed.anomalies) == 1
    assert parsed.anomalies[0].classification == "long"


def test_parse_kline_csv_us_row_exactly_at_the_late_epsilon_boundary_is_normal_not_long() -> None:
    """Same boundary, microsecond era (post 2025-01-01, decision D-017) -- the fix is unit-aware,
    not just hardcoded for milliseconds."""
    open_us = 1_735_689_600_000_000  # 2025-06-01T00:00:00Z in microseconds -- within the us range
    delta_us = 3_600_000_000  # Timeframe.H1, in microseconds
    close_time_us = open_us + delta_us  # exactly "open + delta" -- the late boundary itself
    row = f"{open_us},100,101,99,100,5,{close_time_us},500,10,2,250,0\n"

    parsed = bl.parse_kline_csv_bytes(row.encode(), timeframe=Timeframe.H1)

    assert parsed.anomalies == ()
    assert len(parsed.records) == 1


def test_parse_kline_csv_us_row_one_unit_past_the_late_epsilon_boundary_is_still_long() -> None:
    open_us = 1_735_689_600_000_000
    delta_us = 3_600_000_000
    close_time_us = open_us + delta_us + 1  # one microsecond past the boundary
    row = f"{open_us},100,101,99,100,5,{close_time_us},500,10,2,250,0\n"

    parsed = bl.parse_kline_csv_bytes(row.encode(), timeframe=Timeframe.H1)

    assert parsed.records == ()
    assert len(parsed.anomalies) == 1
    assert parsed.anomalies[0].classification == "long"


# --- load_bars / load_frame ------------------------------------------------------------------


def _ingest_fixture_month(
    config: DataConfig, *, symbol: str, year: int, month: int, csv_fixture: str
) -> None:
    parsed = bl.parse_kline_csv_bytes(_csv_bytes(csv_fixture), timeframe=Timeframe.H1)
    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol=symbol, timeframe=Timeframe.H1, year=year, month=month
    )
    store_mod.write_month_part(part_path, list(parsed.records))
    sidecar_file = store_mod.sidecar_path(
        config.parquet_root, source="binance", symbol=symbol, timeframe=Timeframe.H1
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source="binance", symbol=symbol, timeframe=Timeframe.H1)
    sidecar.add_file(f"{symbol}-1h-{year:04d}-{month:02d}.zip", "deadbeef", verified=True)
    store_mod.save_sidecar(sidecar_file, sidecar)


def _ingest_june_2024_ms(config: DataConfig, *, symbol: str = "BTCUSDT") -> None:
    _ingest_fixture_month(config, symbol=symbol, year=2024, month=6, csv_fixture="klines_1h_2024-06_ms.txt")


def test_load_bars_returns_bar_objects_with_exact_decimals(tmp_path: Path) -> None:
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)

    bars = bl.load_bars("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")
    assert len(bars) == 3
    assert all(isinstance(b, Bar) for b in bars)
    assert bars[0].ts == datetime(2024, 6, 1, 1, 0, 0, tzinfo=UTC)
    assert bars[0].close == Decimal("67655.66000000")


def test_load_frame_as_float_casts_ohlcv(tmp_path: Path) -> None:
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)

    df = bl.load_frame("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")
    assert len(df) == 3
    assert df["close"].dtype == "float64"
    assert df["close"].iloc[0] == pytest.approx(67655.66)


# --- sealed holdout double lock (docs/SPEC.md section 5.2) -----------------------------------


def test_default_load_bars_excludes_holdout_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path)  # holdout_start == bl.CANONICAL_HOLDOUT_START, unmodified
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=3)

    bars = bl.load_bars("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")

    assert len(bars) == 3  # only the June 2024 bars; the synthetic post-holdout bars are excluded
    assert all(b.ts < bl.CANONICAL_HOLDOUT_START for b in bars)


def test_default_load_frame_excludes_holdout_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test gap closed: only ``load_bars`` used to be asserted here -- ``load_frame`` has its own,
    separate filtering branch and needs its own regression test."""
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=3)

    df = bl.load_frame("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")

    assert len(df) == 3
    assert (df["ts"] < bl.CANONICAL_HOLDOUT_START).all()


@pytest.mark.parametrize(
    ("allow_holdout", "env_value"),
    [
        (True, None),  # code says yes, environment says no
        (True, "wrong-value"),
        (False, "G4"),  # environment says yes, code never asked
    ],
)
def test_holdout_refuses_when_only_one_lock_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_holdout: bool, env_value: str | None
) -> None:
    if env_value is None:
        monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    else:
        monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, env_value)
    config = make_data_config(tmp_path)  # canonical holdout_start -- isolates the two-lock check
    _ingest_june_2024_ms(config)
    log_path = tmp_path / "holdout.md"

    with pytest.raises(bl.HoldoutLockError, match="BOTH"):
        bl.load_bars(
            "BTCUSDT", Timeframe.H1, allow_holdout=allow_holdout, config=config, holdout_log_path=log_path
        )

    assert log_path.is_file()
    assert "refused" in log_path.read_text(encoding="utf-8")


def test_holdout_unseals_with_both_locks_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, bl.HOLDOUT_UNSEAL_VALUE)
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=3)
    log_path = tmp_path / "holdout.md"

    bars = bl.load_bars(
        "BTCUSDT",
        Timeframe.H1,
        allow_holdout=True,
        config=config,
        caller="test-suite",
        reason="gate G4 rehearsal",
        holdout_log_path=log_path,
    )

    assert len(bars) == 6  # both months, holdout included
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "unsealed" in log_text
    assert "test-suite" in log_text
    assert "gate G4 rehearsal" in log_text


# --- BLOCKER B1 / decision D-026: holdout_start is a code constant, not a config value ----------


def test_holdout_start_later_than_canonical_is_refused_not_extra_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is exactly the bug the reviewer proved: a config whose ``holdout_start`` was moved
    later than canonical used to silently unseal extra months (more rows, no log entry). It must
    now raise before returning anything."""
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    moved_holdout = bl.CANONICAL_HOLDOUT_START + timedelta(days=365)
    config = make_data_config(tmp_path, holdout_start=moved_holdout)
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=3)
    log_path = tmp_path / "holdout.md"

    with pytest.raises(bl.HoldoutLockError, match="canonical"):
        bl.load_bars("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=log_path)

    assert log_path.is_file()
    assert "refused: holdout_start overridden" in log_path.read_text(encoding="utf-8")


def test_holdout_start_earlier_than_canonical_is_also_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-026 is a strict equality check, not just a "never later" check: SPEC section 5.2 says
    the loader refuses whenever the two values *differ*, in either direction, because the seal is
    not supposed to be a configuration value at all."""
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path, holdout_start=bl.CANONICAL_HOLDOUT_START - timedelta(days=30))
    _ingest_june_2024_ms(config)

    with pytest.raises(bl.HoldoutLockError, match="canonical"):
        bl.load_bars("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")


def test_holdout_start_mismatch_allowed_when_both_locks_engaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-logged, double-locked G4 run returns everything regardless of ``holdout_start``
    anyway, so a mismatched config must not block it."""
    monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, bl.HOLDOUT_UNSEAL_VALUE)
    moved_holdout = bl.CANONICAL_HOLDOUT_START + timedelta(days=365)
    config = make_data_config(tmp_path, holdout_start=moved_holdout)
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=3)

    bars = bl.load_bars(
        "BTCUSDT",
        Timeframe.H1,
        allow_holdout=True,
        config=config,
        caller="test-suite",
        reason="gate G4 rehearsal",
        holdout_log_path=tmp_path / "holdout.md",
    )

    assert len(bars) == 6


# --- quality report integration: the sealed holdout must never reach it (minor fix 2) -----------


def test_quality_report_from_default_load_never_sees_holdout_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=5)

    df = bl.load_frame("BTCUSDT", Timeframe.H1, config=config, holdout_log_path=tmp_path / "holdout.md")
    report = quality_mod.build_quality_report(
        source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, df=df
    )

    assert report.rows == 3
    assert report.last_ts is not None
    assert report.last_ts < bl.CANONICAL_HOLDOUT_START


# --- minor fix 2: the sidecar only ever reports pre-holdout numbers -----------------------------


def test_ingest_sidecar_logs_only_pre_holdout_stats(tmp_path: Path) -> None:
    config = make_data_config(tmp_path, history_start=datetime(2025, 9, 1, tzinfo=UTC))
    # pre-holdout month (Sept 2025) and post-holdout month (Oct 2025), both synthetic and
    # pre-marked verified + on disk so this ingest run makes zero network calls.
    _write_synthetic_bars(config, symbol="BTCUSDT", start=datetime(2025, 9, 1, 1, tzinfo=UTC), count=3)
    _write_synthetic_bars(config, symbol="BTCUSDT", start=bl.CANONICAL_HOLDOUT_START, count=5)
    sidecar_file = store_mod.sidecar_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1)
    sidecar.add_file("BTCUSDT-1h-2025-09.zip", "deadbeef", verified=True)
    sidecar.add_file("BTCUSDT-1h-2025-10.zip", "deadbeef", verified=True)
    store_mod.save_sidecar(sidecar_file, sidecar)

    with httpx.Client() as client:  # no respx mock: both months are already verified + on disk
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2025, 11, 15, tzinfo=UTC),
        )

    assert outcome.downloaded == ()
    assert outcome.rows_in_store == 3  # only the Sept 2025 (pre-holdout) bars are counted/logged
    sidecar_after = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert sidecar_after.rows == 3
    assert sidecar_after.last_ts == datetime(2025, 9, 1, 3, 0, 0, tzinfo=UTC)
    assert sidecar_after.last_ts is not None
    assert sidecar_after.last_ts < bl.CANONICAL_HOLDOUT_START
    assert sidecar_after.holdout_start == bl.CANONICAL_HOLDOUT_START


# --- MINOR-10 / decision D-032 (second fix round): ingestion stops at the holdout boundary ------


@respx.mock
def test_ingest_stops_at_holdout_boundary_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """By default, ``ingest_symbol_timeframe`` must never request, download or write a part file
    for Oct/Nov/Dec 2025 (the holdout year, which starts at ``CANONICAL_HOLDOUT_START`` =
    2025-10-01) even though ``latest_complete_month(now)`` reaches December 2025. Only a route for
    the pre-holdout month (Sept 2025) is registered with respx -- if the ingest loop ever requested
    a holdout month's checksum or zip, respx would raise ``AssertionError`` for the unmocked call,
    which doubles as proof no network call was even attempted for a sealed month.
    """
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path, history_start=datetime(2025, 9, 1, tzinfo=UTC))
    zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2025-09.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2025, 9)
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2025-09.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with httpx.Client() as client:
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2026, 1, 15, tzinfo=UTC),  # latest_complete_month would reach Dec 2025
        )

    assert outcome.downloaded == ("BTCUSDT-1h-2025-09.zip",)

    for year, month in ((2025, 10), (2025, 11), (2025, 12)):
        part_path = store_mod.month_part_path(
            config.parquet_root,
            source="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            year=year,
            month=month,
        )
        assert not part_path.is_file()
        raw_path = config.raw_root / "binance" / "BTCUSDT" / "1h" / f"BTCUSDT-1h-{year:04d}-{month:02d}.zip"
        assert not raw_path.is_file()


@respx.mock
def test_ingest_logs_how_many_months_were_skipped_as_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)
    config = make_data_config(tmp_path, history_start=datetime(2025, 9, 1, tzinfo=UTC))
    zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2025-09.csv")
    checksum_hex = bl.sha256_hex(zip_bytes)
    zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2025, 9)
    respx.get(bl.checksum_url(zip_url)).mock(
        return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-2025-09.zip\n")
    )
    respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    with capture_logs() as logs, httpx.Client() as client:
        bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2026, 1, 15, tzinfo=UTC),
        )

    skip_logs = [log for log in logs if log["event"] == "binance_loader.ingest_skipped_sealed_months"]
    assert len(skip_logs) == 1
    assert skip_logs[0]["n_months"] == 3  # Oct, Nov, Dec 2025


@respx.mock
def test_ingest_unsealed_with_both_locks_reaches_the_holdout_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot G4 exception: with both unseal locks engaged, ingestion must still be able to
    reach the holdout year -- D-032 only changes the *default*."""
    monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, bl.HOLDOUT_UNSEAL_VALUE)
    config = make_data_config(tmp_path, history_start=datetime(2025, 9, 1, tzinfo=UTC))
    for year, month in ((2025, 9), (2025, 10)):
        zip_bytes = _zip_bytes("klines_1h_2024-06_ms.txt", f"BTCUSDT-1h-{year}-{month:02d}.csv")
        checksum_hex = bl.sha256_hex(zip_bytes)
        zip_url = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, year, month)
        respx.get(bl.checksum_url(zip_url)).mock(
            return_value=httpx.Response(200, text=f"{checksum_hex}  BTCUSDT-1h-{year}-{month:02d}.zip\n")
        )
        respx.get(zip_url).mock(return_value=httpx.Response(200, content=zip_bytes))

    log_path = tmp_path / "holdout.md"
    with httpx.Client() as client:
        outcome = bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2025, 11, 15, tzinfo=UTC),
            allow_holdout=True,
            holdout_log_path=log_path,
        )

    assert outcome.downloaded == ("BTCUSDT-1h-2025-09.zip", "BTCUSDT-1h-2025-10.zip")
    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2025, month=10
    )
    assert part_path.is_file()

    # MINOR-8 (third fix round): an ingest-time unseal now leaves the same audit trail load_bars/
    # load_frame do, via the shared _resolve_holdout_access resolver.
    log_text = log_path.read_text(encoding="utf-8")
    assert "ingest_symbol_timeframe" in log_text
    assert "unsealed" in log_text


# --- minor fix 3: the sidecar is saved after every month, not once at the end -------------------


@respx.mock
def test_ingest_saves_sidecar_incrementally_so_a_crash_mid_run_keeps_earlier_months(
    tmp_path: Path,
) -> None:
    config = make_data_config(tmp_path)
    zip1 = _zip_bytes("klines_1h_2024-06_ms.txt", "BTCUSDT-1h-2024-06.csv")
    hex1 = bl.sha256_hex(zip1)
    url1 = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 6)
    respx.get(bl.checksum_url(url1)).mock(
        return_value=httpx.Response(200, text=f"{hex1}  BTCUSDT-1h-2024-06.zip\n")
    )
    respx.get(url1).mock(return_value=httpx.Response(200, content=zip1))

    url2 = bl.monthly_zip_url(config.binance_base_url, "BTCUSDT", Timeframe.H1, 2024, 7)
    respx.get(bl.checksum_url(url2)).mock(
        return_value=httpx.Response(200, text=f"{'0' * 64}  BTCUSDT-1h-2024-07.zip\n")
    )
    respx.get(url2).mock(return_value=httpx.Response(200, content=b"not the real file"))  # mismatch

    with httpx.Client() as client, pytest.raises(bl.ChecksumMismatchError):
        bl.ingest_symbol_timeframe(
            client,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            data_config=config,
            now=datetime(2024, 8, 15, tzinfo=UTC),
        )

    sidecar = _load_sidecar(config, "BTCUSDT", Timeframe.H1)
    assert sidecar.has_verified_file("BTCUSDT-1h-2024-06.zip")  # month 1 survives the month-2 crash
    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    assert part_path.is_file()


# --- minor fix 1: the holdout-log default path is resolved explicitly, not guessed --------------


def test_resolve_repo_root_finds_the_real_checkout() -> None:
    root = bl._resolve_repo_root()
    assert (root / "pyproject.toml").is_file()


def test_resolve_repo_root_fails_loudly_when_no_marker_is_found(tmp_path: Path) -> None:
    fake_file = tmp_path / "nested" / "deep" / "module.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"pyproject\.toml"):
        bl._resolve_repo_root(start=fake_file)


# --- MINOR-1 (second fix round): the default holdout-log path is resolved lazily -----------------


def test_default_ordinary_sealed_read_never_resolves_the_default_log_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary default read (canonical ``holdout_start``, no locks engaged, no
    ``holdout_log_path`` passed) never writes a log row at all, so it must never need to resolve
    where that row *would* go either. Before the fix, ``holdout_log_path or
    _default_holdout_log_path()`` ran ``_resolve_repo_root()`` unconditionally at the call site --
    in a ``--no-editable``/wheel install (no ``pyproject.toml`` above the installed module) this
    raised ``RuntimeError`` on every single plain research read, logging or not.
    """
    monkeypatch.delenv(bl.HOLDOUT_UNSEAL_ENV, raising=False)

    def _boom() -> Path:
        raise RuntimeError("must not be called for a plain default read")

    monkeypatch.setattr(bl, "_default_holdout_log_path", _boom)
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)

    # No holdout_log_path given -- this must not touch _default_holdout_log_path at all.
    bars = bl.load_bars("BTCUSDT", Timeframe.H1, config=config)
    df = bl.load_frame("BTCUSDT", Timeframe.H1, config=config)

    assert len(bars) == 3
    assert len(df) == 3


def test_the_real_research_holdout_log_is_never_touched_by_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer finding m8: tests/conftest.py's autouse env-isolation fixture must redirect
    ``_default_holdout_log_path`` for every test, including this one, so a holdout-access
    attempt made WITHOUT an explicit ``holdout_log_path`` (exercising the real default-path
    fallback) never appends to the real ``research/HOLDOUT_LOG.md`` -- this has already happened
    once on this machine."""
    real_log = bl._resolve_repo_root() / "research" / "HOLDOUT_LOG.md"
    before = real_log.read_text(encoding="utf-8") if real_log.is_file() else None

    monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, "wrong-value")  # exercises the "refused" _log() branch
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)

    with pytest.raises(bl.HoldoutLockError):
        bl.load_bars("BTCUSDT", Timeframe.H1, allow_holdout=True, config=config)  # no holdout_log_path!

    after = real_log.read_text(encoding="utf-8") if real_log.is_file() else None
    assert after == before  # untouched -- the autouse fixture redirected the default path


def test_refused_or_unsealed_access_still_resolves_the_default_log_path_when_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lazy resolution must still actually happen on the branches that *do* log -- this is not
    just a short-circuit that silently stops logging altogether. Patches
    ``_default_holdout_log_path`` to point at a tmp file rather than letting it resolve the real
    repo root, so this test cannot write into the real ``research/HOLDOUT_LOG.md``."""
    monkeypatch.setenv(bl.HOLDOUT_UNSEAL_ENV, "wrong-value")
    config = make_data_config(tmp_path)
    _ingest_june_2024_ms(config)

    fake_default_path = tmp_path / "default_holdout.md"
    calls = 0

    def _fake_default() -> Path:
        nonlocal calls
        calls += 1
        return fake_default_path

    monkeypatch.setattr(bl, "_default_holdout_log_path", _fake_default)

    with pytest.raises(bl.HoldoutLockError, match="BOTH"):
        bl.load_bars("BTCUSDT", Timeframe.H1, allow_holdout=True, config=config)

    assert calls == 1
    assert fake_default_path.is_file()
