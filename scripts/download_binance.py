"""CLI: download, verify and store Binance klines; optionally build the data-quality report.

Downloads monthly kline ZIPs for every configured symbol/timeframe from ``data.binance.vision``,
from ``config.data.history_start`` to the latest complete calendar month, verifying each file
against its published SHA-256 CHECKSUM (docs/SPEC.md section 5.1). Re-running is idempotent:
a month already verified and stored is skipped.

Usage::

    uv run python scripts/download_binance.py
    uv run python scripts/download_binance.py --symbols BTCUSDT --timeframes 1h,4h
    uv run python scripts/download_binance.py --report            # download, then report
    uv run python scripts/download_binance.py --report-only        # report from the existing store

The quality report (docs/SPEC.md section 5.3) is written as JSON + Markdown under
``research/reports/`` (override with ``--report-dir``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd  # type: ignore[import-untyped]
import structlog

from tbot.core.config import DataConfig, load_config
from tbot.core.types import Timeframe
from tbot.data import binance_loader, quality
from tbot.data import store as store_mod

logger = structlog.get_logger(__name__)

_DEFAULT_REPORT_DIR = Path("research") / "reports"
_HTTP_TIMEOUT_SECONDS = 30.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="default", help="config/<name>.yaml to load (default: default)")
    parser.add_argument(
        "--symbols", default=None, help="comma-separated symbols, overrides config.data.symbols"
    )
    parser.add_argument(
        "--timeframes", default=None, help="comma-separated timeframes (1h,4h,1d), overrides config"
    )
    parser.add_argument(
        "--report", action="store_true", help="also write the data-quality report after ingest"
    )
    parser.add_argument(
        "--report-only", action="store_true", help="skip downloading; only build the report"
    )
    parser.add_argument(
        "--report-dir", default=None, help="override the research/reports/ output directory"
    )
    return parser.parse_args(argv)


def _ingest_all(
    symbols: tuple[str, ...], timeframes: tuple[Timeframe, ...], data_config: DataConfig, now: datetime
) -> None:
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        for symbol in symbols:
            for timeframe in timeframes:
                logger.info("ingest.start", symbol=symbol, timeframe=timeframe.value)
                outcome = binance_loader.ingest_symbol_timeframe(
                    client, symbol=symbol, timeframe=timeframe, data_config=data_config, now=now
                )
                logger.info(
                    "ingest.done",
                    symbol=symbol,
                    timeframe=timeframe.value,
                    downloaded=len(outcome.downloaded),
                    skipped=len(outcome.skipped),
                    rows=outcome.rows_in_store,
                    gaps=len(outcome.gaps),
                    unclassified_gaps=sum(1 for g in outcome.gaps if g.classification == "unknown"),
                    source_anomalies=len(outcome.anomalies),
                )


def _apply_cross_symbol_pass(
    data_config: DataConfig, *, symbols: tuple[str, ...], timeframes: tuple[Timeframe, ...]
) -> None:
    """Decision D-036 amendment (finding m-J follow-up): the cross-symbol corroboration pass
    runs once per timeframe, after every symbol has been ingested (or, in ``--report-only``
    mode, straight from whatever is already on disk) -- a gap whose exact missing-bar window is
    ALSO a gap in another configured symbol's series at the same timeframe is exchange-wide
    evidence no single symbol's own anomaly rows can provide. It never touches a gap that
    already has a non-"unknown" classification (human or auto), so it can run every time without
    risk of clobbering anything.
    """
    for timeframe in timeframes:
        reclassified = binance_loader.apply_cross_symbol_gap_classification(
            data_config, symbols=symbols, timeframe=timeframe
        )
        for symbol, n in reclassified.items():
            if n > 0:
                logger.info(
                    "cross_symbol_gap_classification.applied",
                    symbol=symbol,
                    timeframe=timeframe.value,
                    n_gaps_reclassified=n,
                )


def _load_binance_sidecar(
    data_config: DataConfig, *, symbol: str, timeframe: Timeframe
) -> store_mod.DatasetSidecar:
    path = store_mod.sidecar_path(
        data_config.parquet_root, source="binance", symbol=symbol, timeframe=timeframe
    )
    return store_mod.load_sidecar(path, source="binance", symbol=symbol, timeframe=timeframe)


def _write_quality_report_for(
    *,
    symbol: str,
    timeframe: Timeframe,
    frames: dict[Timeframe, pd.DataFrame],
    data_config: DataConfig,
    report_dir: Path,
    date_tag: str,
) -> None:
    """Build and write one symbol/timeframe's data-quality report.

    MAJOR M6: ``gap_classifications``/``gap_classified_by`` read back whatever a human (or an
    auto rule) already recorded on the sidecar's gap list -- otherwise ``build_quality_report``
    has no memory of its own and every gap is reported "unknown" forever, no matter how many
    were investigated, and G1a ("no unknown left") could never be satisfied.
    """
    df_1h = frames.get(Timeframe.H1) if timeframe in (Timeframe.H4, Timeframe.D1) else None
    sidecar = _load_binance_sidecar(data_config, symbol=symbol, timeframe=timeframe)
    gap_classifications = {(g.from_ts, g.to_ts): g.classification for g in sidecar.gaps}
    gap_classified_by = {(g.from_ts, g.to_ts): g.classified_by for g in sidecar.gaps}
    anomalies_1h: list[store_mod.AnomalyRecord] = []
    if timeframe in (Timeframe.H4, Timeframe.D1):
        anomalies_1h = _load_binance_sidecar(data_config, symbol=symbol, timeframe=Timeframe.H1).anomalies

    report = quality.build_quality_report(
        source="binance",
        symbol=symbol,
        timeframe=timeframe,
        df=frames[timeframe],
        df_1h_for_reconciliation=df_1h,
        gap_classifications=gap_classifications,
        gap_classified_by=gap_classified_by,
        anomalies=sidecar.anomalies,
        anomalies_1h_for_reconciliation=anomalies_1h,
    )
    json_path, md_path = quality.write_report(report, report_dir, date_tag=date_tag)
    logger.info(
        "report.written",
        symbol=symbol,
        timeframe=timeframe.value,
        json=str(json_path),
        md=str(md_path),
        unclassified_gaps=report.unclassified_gap_count,
    )


def _run_reports(
    *,
    symbols: tuple[str, ...],
    timeframes: tuple[Timeframe, ...],
    data_config: DataConfig,
    report_dir: Path,
    now: datetime,
) -> None:
    date_tag = now.strftime("%Y%m%d")
    _apply_cross_symbol_pass(data_config, symbols=symbols, timeframes=timeframes)
    for symbol in symbols:
        frames = {
            timeframe: binance_loader.load_frame(
                symbol,
                timeframe,
                config=data_config,
                as_float=True,
                caller="scripts/download_binance.py",
                reason="quality report",
            )
            for timeframe in timeframes
        }
        for timeframe in timeframes:
            _write_quality_report_for(
                symbol=symbol,
                timeframe=timeframe,
                frames=frames,
                data_config=data_config,
                report_dir=report_dir,
                date_tag=date_tag,
            )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config)
    data_config = config.data

    symbols = tuple(args.symbols.split(",")) if args.symbols else data_config.symbols
    timeframes: tuple[Timeframe, ...] = (
        tuple(Timeframe(tf) for tf in args.timeframes.split(","))
        if args.timeframes
        else data_config.timeframes
    )
    report_dir = Path(args.report_dir) if args.report_dir else _DEFAULT_REPORT_DIR
    now = datetime.now(UTC)

    if not args.report_only:
        _ingest_all(symbols, timeframes, data_config, now)

    if args.report or args.report_only:
        _run_reports(
            symbols=symbols, timeframes=timeframes, data_config=data_config, report_dir=report_dir, now=now
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
