"""Tests for src/tbot/data/quality.py: gap/duplicate/outlier detection and resampling reconciliation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from tbot.core.types import Timeframe
from tbot.data import quality as q


def _ts(hour: int, *, day: int = 1, month: int = 6, year: int = 2024) -> datetime:
    return datetime(year, month, day, hour % 24, tzinfo=UTC) + timedelta(days=hour // 24)


def _ohlcv_df(rows: list[tuple[datetime, float, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


# --- duplicates / out-of-order ----------------------------------------------------------------


def test_find_duplicate_timestamps() -> None:
    series = pd.Series([_ts(0), _ts(1), _ts(1), _ts(2)])
    assert q.find_duplicate_timestamps(series) == [_ts(1)]


def test_find_duplicate_timestamps_none_present() -> None:
    series = pd.Series([_ts(0), _ts(1), _ts(2)])
    assert q.find_duplicate_timestamps(series) == []


def test_find_out_of_order_detects_a_decrease() -> None:
    series = pd.Series([_ts(0), _ts(2), _ts(1), _ts(3)])
    findings = q.find_out_of_order(series)
    assert len(findings) == 1
    assert findings[0]["index"] == 2
    assert findings[0]["ts"] == _ts(1)
    assert findings[0]["previous_ts"] == _ts(2)


def test_find_out_of_order_strictly_increasing_is_clean() -> None:
    series = pd.Series([_ts(0), _ts(1), _ts(2)])
    assert q.find_out_of_order(series) == []


# --- gaps ---------------------------------------------------------------------------------------


def test_find_gaps_detects_a_single_missing_hour() -> None:
    series = pd.Series([_ts(0), _ts(1), _ts(3)])  # hour 2 is missing
    gaps = q.find_gaps(series, Timeframe.H1)
    assert len(gaps) == 1
    assert gaps[0].from_ts == _ts(1)
    assert gaps[0].to_ts == _ts(3)
    assert gaps[0].missing_bars == 1
    assert gaps[0].classification == "unknown"  # default until investigated


def test_find_gaps_contiguous_series_has_none() -> None:
    series = pd.Series([_ts(h) for h in range(5)])
    assert q.find_gaps(series, Timeframe.H1) == []


def test_find_gaps_ignores_duplicate_entries() -> None:
    series = pd.Series([_ts(0), _ts(0), _ts(1), _ts(2)])
    assert q.find_gaps(series, Timeframe.H1) == []


# --- zero-volume / high==low ---------------------------------------------------------------------


def test_find_zero_volume() -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 101, 99, 100, 10.0),
            (_ts(1), 100, 101, 99, 100, 0.0),
        ]
    )
    assert q.find_zero_volume(df) == [_ts(1)]


def test_find_high_eq_low() -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 101, 99, 100, 10.0),
            (_ts(1), 100, 100, 100, 100, 5.0),
        ]
    )
    assert q.find_high_eq_low(df) == [_ts(1)]


# --- return outliers ------------------------------------------------------------------------------


def test_find_return_outliers_1h_threshold_20pct() -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 100, 100, 100.0, 1.0),
            (_ts(1), 100, 100, 100, 110.0, 1.0),  # +10%, fine
            (_ts(2), 100, 100, 100, 140.0, 1.0),  # +27.3%, outlier
        ]
    )
    outliers = q.find_return_outliers(df, Timeframe.H1)
    assert len(outliers) == 1
    assert outliers[0].ts == _ts(2)
    assert outliers[0].pct_return == pytest.approx((140.0 - 110.0) / 110.0)


def test_find_return_outliers_1d_threshold_40pct() -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 100, 100, 100.0, 1.0),
            (_ts(24), 100, 100, 100, 135.0, 1.0),  # +35%, fine for 1d
            (_ts(48), 100, 100, 100, 200.0, 1.0),  # +48%, outlier for 1d
        ]
    )
    outliers = q.find_return_outliers(df, Timeframe.D1)
    assert len(outliers) == 1
    assert outliers[0].ts == _ts(48)


# --- coverage by month -----------------------------------------------------------------------------


def test_coverage_by_month_partial_month() -> None:
    series = pd.Series([_ts(h) for h in range(5)])  # 5 of 30*24 hourly bars in June 2024
    coverage = q.coverage_by_month(series, Timeframe.H1)
    assert set(coverage) == {"2024-06"}
    row = coverage["2024-06"]
    assert row["actual"] == 5
    assert row["expected"] == 30 * 24
    assert row["coverage"] == pytest.approx(5 / (30 * 24))


# --- resampling reconciliation (label='right', closed='right') ------------------------------------


def _hourly_fixture() -> pd.DataFrame:
    # 9 contiguous 1h bars (ts = close time, hours 0..8). With label='right', closed='right'
    # the 4h bar closing at 04:00 aggregates the 1h bars closing at 01:00..04:00 (row indices
    # 1..4): a bar whose ts is its CLOSE time belongs to the higher-timeframe bucket it closes
    # inside, never the one it opens inside.
    rows = []
    price = 100.0
    for h in range(9):
        o, c = price, price + 1
        rows.append((_ts(h), o, c + 0.5, o - 0.5, c, 10.0))
        price = c
    return _ohlcv_df(rows)


def test_reconcile_resample_matches_within_tolerance() -> None:
    hourly = _hourly_fixture()
    # bin (00:00,04:00] -> rows closing at 01:00..04:00; bin (04:00,08:00] -> 05:00..08:00.
    bin1 = hourly.iloc[1:5]
    bin2 = hourly.iloc[5:9]
    higher = _ohlcv_df(
        [
            (
                _ts(4),
                bin1["open"].iloc[0],
                bin1["high"].max(),
                bin1["low"].min(),
                bin1["close"].iloc[-1],
                bin1["volume"].sum(),
            ),
            (
                _ts(8),
                bin2["open"].iloc[0],
                bin2["high"].max(),
                bin2["low"].min(),
                bin2["close"].iloc[-1],
                bin2["volume"].sum(),
            ),
        ]
    )
    mismatches = q.reconcile_resample(hourly, higher, higher_timeframe=Timeframe.H4)
    assert mismatches == []


def test_reconcile_resample_detects_a_real_mismatch() -> None:
    hourly = _hourly_fixture()
    bin1 = hourly.iloc[1:5]
    higher = _ohlcv_df(
        [
            (
                _ts(4),
                bin1["open"].iloc[0],
                bin1["high"].max() + 1.0,  # deliberately wrong
                bin1["low"].min(),
                bin1["close"].iloc[-1],
                bin1["volume"].sum(),
            ),
        ]
    )
    mismatches = q.reconcile_resample(hourly, higher, higher_timeframe=Timeframe.H4)
    assert any(m.column == "high" and m.ts == _ts(4) for m in mismatches)


# --- full report ------------------------------------------------------------------------------------


def test_build_quality_report_and_write(tmp_path: Path) -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 101, 99, 100, 10.0),
            (_ts(1), 100, 101, 99, 100, 0.0),  # zero volume
            (_ts(3), 100, 100, 100, 100, 5.0),  # hour 2 missing -> gap; high==low here
        ]
    )
    report = q.build_quality_report(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, df=df)

    assert report.rows == 3
    assert len(report.gaps) == 1
    assert report.unclassified_gap_count == 1
    assert report.zero_volume_bars == [_ts(1)]
    assert report.high_eq_low_bars == [_ts(3)]

    json_path, md_path = q.write_report(report, tmp_path, date_tag="20260101")
    assert json_path.is_file()
    assert md_path.is_file()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["rows"] == 3
    assert data["unclassified_gap_count"] == 1
    assert "Data quality" in md_path.read_text(encoding="utf-8")


def test_build_quality_report_applies_gap_classifications() -> None:
    """MAJOR M6: ``find_gaps`` always returns "unknown" -- it is pure and has no memory of
    anything investigated previously. Without applying a classifications mapping,
    ``unclassified_gap_count`` could never drop below the total gap count, and gate G1a ("every
    missing bar classified, no 'unknown' left") could never be satisfied."""
    df = _ohlcv_df(
        [
            (_ts(0), 100, 101, 99, 100, 10.0),
            (_ts(3), 100, 100, 100, 100, 5.0),  # hours 1-2 missing -> one gap
        ]
    )
    classifications = {(_ts(0), _ts(3)): "exchange_outage"}

    report = q.build_quality_report(
        source="binance",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        df=df,
        gap_classifications=classifications,
    )

    assert len(report.gaps) == 1
    assert report.gaps[0].classification == "exchange_outage"
    assert report.unclassified_gap_count == 0


def test_build_quality_report_without_classifications_stays_unknown() -> None:
    df = _ohlcv_df(
        [
            (_ts(0), 100, 101, 99, 100, 10.0),
            (_ts(3), 100, 100, 100, 100, 5.0),
        ]
    )
    report = q.build_quality_report(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, df=df)
    assert report.unclassified_gap_count == 1


def test_build_quality_report_empty_frame() -> None:
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    report = q.build_quality_report(source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, df=empty)
    assert report.rows == 0
    assert report.first_ts is None
    assert report.gaps == []
