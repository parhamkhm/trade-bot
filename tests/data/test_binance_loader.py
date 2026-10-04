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
    records = bl.parse_kline_csv_bytes(_csv_bytes("klines_1h_2024-06_ms.txt"), timeframe=Timeframe.H1)
    assert len(records) == 3
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
    records = bl.parse_kline_csv_bytes(_csv_bytes("klines_1h_2025-01_us.txt"), timeframe=Timeframe.H1)
    assert len(records) == 3
    first = records[0]
    assert first.ts == datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)  # close = open (00:00) + 1h
    assert isinstance(first.close, Decimal)
    assert first.close == Decimal("94401.14000000")


# --- the exact ms -> us boundary (2024-12 -> 2025-01) -------------------------------------


def test_parse_kline_csv_handles_the_ms_to_us_boundary() -> None:
    dec_records = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_2024-12-31T23_ms_tail.txt"), timeframe=Timeframe.H1
    )
    jan_records = bl.parse_kline_csv_bytes(
        _csv_bytes("klines_1h_2025-01-01T00_us_head.txt"), timeframe=Timeframe.H1
    )
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


def test_parse_kline_csv_rejects_close_time_inconsistent_with_interval() -> None:
    # close_time does not equal open_time + 1h - 1ms: a corrupted/misaligned row.
    bad_row = "1717200000000,100,101,99,100,1,1717200005000,100,1,1,1,0\n"
    with pytest.raises(ValueError, match="close_time"):
        bl.parse_kline_csv_bytes(bad_row.encode(), timeframe=Timeframe.H1)


def test_parse_kline_csv_skips_a_header_row_defensively() -> None:
    header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,x,y,z\n"
    body = _csv_bytes("klines_1h_2024-06_ms.txt").decode()
    records = bl.parse_kline_csv_bytes((header + body).encode(), timeframe=Timeframe.H1)
    assert len(records) == 3


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


# --- load_bars / load_frame ------------------------------------------------------------------


def _ingest_fixture_month(
    config: DataConfig, *, symbol: str, year: int, month: int, csv_fixture: str
) -> None:
    records = bl.parse_kline_csv_bytes(_csv_bytes(csv_fixture), timeframe=Timeframe.H1)
    part_path = store_mod.month_part_path(
        config.parquet_root, source="binance", symbol=symbol, timeframe=Timeframe.H1, year=year, month=month
    )
    store_mod.write_month_part(part_path, records)
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
