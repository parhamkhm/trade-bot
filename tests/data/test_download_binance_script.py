"""Integration test for scripts/download_binance.py's wiring of MAJOR fix M6: the written
data-quality report must reflect gap classifications already recorded in the Binance
``_dataset.json`` sidecar, not report every gap as permanently "unknown".

No network is reached: this exercises only ``--report-only``, which reads the already-written
Parquet store and sidecar directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import scripts.download_binance as download_binance

from tbot.core.types import Timeframe
from tbot.data import store as store_mod

FIXTURES = Path(__file__).parent / "fixtures"


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "test_config.yaml"
    parquet_root = (tmp_path / "parquet").as_posix()
    raw_root = (tmp_path / "raw").as_posix()
    tabdeal_db = (tmp_path / "tabdeal.sqlite").as_posix()
    config_path.write_text(
        "data:\n"
        f"  parquet_root: {parquet_root}\n"
        f"  raw_root: {raw_root}\n"
        f"  tabdeal_db: {tabdeal_db}\n"
        '  symbols: ["BTCUSDT"]\n'
        '  timeframes: ["1h"]\n'
        "  history_start: 2024-06-01T00:00:00Z\n",
        encoding="utf-8",
    )
    return config_path


def _seed_store_with_a_classified_gap(tmp_path: Path) -> tuple[datetime, datetime]:
    parquet_root = tmp_path / "parquet"
    june_part = store_mod.month_part_path(
        parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=6
    )
    store_mod.write_month_part(
        june_part,
        [
            store_mod.KlineRecord(
                ts=datetime(2024, 6, 1, 1, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=Decimal("1"),
                quote_volume=Decimal("100"),
                trades=1,
            )
        ],
    )
    august_part = store_mod.month_part_path(
        parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1, year=2024, month=8
    )
    store_mod.write_month_part(
        august_part,
        [
            store_mod.KlineRecord(
                ts=datetime(2024, 8, 1, 1, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=Decimal("1"),
                quote_volume=Decimal("100"),
                trades=1,
            )
        ],
    )

    from_ts = datetime(2024, 6, 1, 1, tzinfo=UTC)
    to_ts = datetime(2024, 8, 1, 1, tzinfo=UTC)
    sidecar_file = store_mod.sidecar_path(
        parquet_root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar = store_mod.load_sidecar(
        sidecar_file, source="binance", symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    sidecar.gaps.append(
        store_mod.GapRecord(
            from_ts=from_ts,
            to_ts=to_ts,
            missing_bars=1463,  # (2024-08-01T01:00 - 2024-06-01T01:00) in hours, minus 1
            classification="exchange_outage",
        )
    )
    store_mod.save_sidecar(sidecar_file, sidecar)
    return from_ts, to_ts


def test_report_only_applies_gap_classification_from_sidecar(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    _seed_store_with_a_classified_gap(tmp_path)
    report_dir = tmp_path / "reports"

    exit_code = download_binance.main(
        ["--config", str(config_path), "--report-only", "--report-dir", str(report_dir)]
    )
    assert exit_code == 0

    json_paths = list(report_dir.glob("data_quality_*_BTCUSDT_1h.json"))
    assert len(json_paths) == 1
    report = json.loads(json_paths[0].read_text(encoding="utf-8"))

    assert report["unclassified_gap_count"] == 0
    assert len(report["gaps"]) == 1
    assert report["gaps"][0]["classification"] == "exchange_outage"
