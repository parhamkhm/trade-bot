"""CLI: run the raw LBank market-data recorder until SIGTERM / Ctrl+C (pivot plan, step 0).

Usage::

    uv run python -m scripts.record_lbank --data-dir data/lbank

Writes one SQLite file per UTC day under ``--data-dir`` plus ``heartbeat.json`` (read by
``scripts/lbank_healthcheck.py``). Public endpoints only: no API key is read or needed.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from types import FrameType

import structlog

from tbot.data.lbank_recorder import LBankRecorder, RecorderConfig
from tbot.monitoring.logging import configure_logging

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/lbank"))
    parser.add_argument("--symbol", default="btc_usdt")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    recorder = LBankRecorder(RecorderConfig(data_dir=args.data_dir, symbol=args.symbol))

    def _stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("lbank_recorder.stopping", signal=signum)
        recorder.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    recorder.start()
    while not recorder.stopped:
        recorder.join(timeout=1.0)
    recorder.join(timeout=30.0)
    logger.info("lbank_recorder.stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
