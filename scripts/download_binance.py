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
import structlog

from tbot.core.config import load_config
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
                    )

    if args.report or args.report_only:
        date_tag = now.strftime("%Y%m%d")
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
                df_1h = frames.get(Timeframe.H1) if timeframe in (Timeframe.H4, Timeframe.D1) else None
                # MAJOR M6: read back whatever classifications a human already recorded on the
                # sidecar's gap list -- otherwise build_quality_report has no memory of its own
                # and every gap is reported "unknown" forever, no matter how many were
                # investigated, and G1a ("no unknown left") could never be satisfied.
                sidecar = store_mod.load_sidecar(
                    store_mod.sidecar_path(
                        data_config.parquet_root, source="binance", symbol=symbol, timeframe=timeframe
                    ),
                    source="binance",
                    symbol=symbol,
                    timeframe=timeframe,
                )
                gap_classifications = {(g.from_ts, g.to_ts): g.classification for g in sidecar.gaps}
                report = quality.build_quality_report(
                    source="binance",
                    symbol=symbol,
                    timeframe=timeframe,
                    df=frames[timeframe],
                    df_1h_for_reconciliation=df_1h,
                    gap_classifications=gap_classifications,
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
