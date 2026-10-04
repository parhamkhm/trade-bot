"""Long-lived service: records Tabdeal public trades and order-book snapshots, and builds 1h
candles from the recorded trades (docs/SPEC.md section 5.4, decision D-019).

Usage::

    uv run python -m scripts.record_tabdeal [--config NAME] [--symbol BTCUSDT]
        [--poll-interval-seconds N] [--orderbook-interval-seconds N] [--grace-period-seconds N]
        [--trades-limit N] [--depth-limit N] [--once]

Every interval above is a conservative, PROVISIONAL default (``tbot.data.tabdeal_recorder.
RecorderSettings``) until the exchange-integrator's probe report
(``research/reports/tabdeal_probe_*.json``, field
``trades.window_stats.recommended_poll_interval_seconds``) supplies a measured recommendation
from the Turkey server. Overriding any of them needs no code edit: pass the CLI flag, or add a
``tabdeal_recorder:`` section to the loaded YAML config, e.g.::

    tabdeal_recorder:
      poll_interval_seconds: 2.0
      orderbook_interval_seconds: 60
      grace_period_seconds: 60
      trades_limit: 500
      depth_limit: 100

That section is read directly from the YAML file by this script (``_load_recorder_overrides``
below), independently of ``tbot.core.config.Config`` -- ``Config`` validates with
``extra="forbid"`` and is owned by the orchestrator, so this script does not pass the section
through it. A CLI flag always wins over the config section, which always wins over the built-in
default.

Graceful shutdown: SIGTERM/SIGINT request a stop; the in-flight poll cycle (trades, candles,
maybe order book, heartbeat) is allowed to finish before the process exits, so a restart never
leaves a half-written parquet file or SQLite row behind.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import types
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from tbot.core.config import Config, Secrets, load_config
from tbot.core.types import Clock
from tbot.data.tabdeal_recorder import RecorderSettings, RecorderStore, TabdealRecorderService
from tbot.execution.tabdeal_client import TabdealClient
from tbot.monitoring.logging import configure_logging, register_secrets_for_logging

__all__ = ["SystemClock", "build_settings", "main"]

logger = structlog.get_logger(__name__)

_DEFAULT_HEARTBEAT_FILE = Path("data/tabdeal/heartbeat.json")


class SystemClock:
    """Real wall-clock :class:`tbot.core.types.Clock`. Only ``tbot.core``/``tbot.risk``/
    ``tbot.execution`` are forbidden from reading the wall clock directly (docs/SPEC.md section
    2.1); this script is the outside world that hands a real clock in."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def _load_recorder_overrides(config_name_or_path: str, config_dir: Path) -> dict[str, Any]:
    """Best-effort read of an optional ``tabdeal_recorder:`` section from the same YAML file
    ``load_config`` loads -- the "config" half of this task's "CLI flag + config" requirement.
    Missing file or missing/malformed section -> no overrides, never an error."""
    path = Path(config_name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = config_dir / f"{config_name_or_path}.yaml"
    if not path.is_file():
        return {}
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("tabdeal_recorder")
    return section if isinstance(section, dict) else {}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        default=None,
        help="config/<name>.yaml to load (default: $TBOT_CONFIG env var, else 'default')",
    )
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--symbol", default=None, help="overrides config.runtime base/quote asset")
    parser.add_argument("--poll-interval-seconds", type=float, default=None)
    parser.add_argument("--orderbook-interval-seconds", type=float, default=None)
    parser.add_argument("--grace-period-seconds", type=float, default=None)
    parser.add_argument("--trades-limit", type=int, default=None)
    parser.add_argument("--depth-limit", type=int, default=None)
    parser.add_argument("--db-path", type=Path, default=None, help="overrides config.data.tabdeal_db")
    parser.add_argument("--parquet-root", type=Path, default=None, help="overrides config.data.parquet_root")
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=None,
        help="overrides $TBOT_HEARTBEAT_FILE / the built-in default",
    )
    parser.add_argument(
        "--once", action="store_true", help="run exactly one poll/candle/heartbeat cycle and exit"
    )
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace, config: Config) -> RecorderSettings:
    """CLI flag > YAML ``tabdeal_recorder:`` section > built-in default, per field."""
    overrides = _load_recorder_overrides(args.config, args.config_dir)
    default_symbol = f"{config.runtime.base_asset}{config.runtime.quote_asset}"
    defaults = RecorderSettings(symbol=default_symbol)

    def pick(cli_value: Any, key: str, default: Any) -> Any:
        return cli_value if cli_value is not None else overrides.get(key, default)

    return RecorderSettings(
        symbol=pick(args.symbol, "symbol", defaults.symbol),
        trades_limit=pick(args.trades_limit, "trades_limit", defaults.trades_limit),
        poll_interval_seconds=pick(
            args.poll_interval_seconds, "poll_interval_seconds", defaults.poll_interval_seconds
        ),
        orderbook_interval_seconds=pick(
            args.orderbook_interval_seconds, "orderbook_interval_seconds", defaults.orderbook_interval_seconds
        ),
        grace_period_seconds=pick(
            args.grace_period_seconds, "grace_period_seconds", defaults.grace_period_seconds
        ),
        depth_limit=pick(args.depth_limit, "depth_limit", defaults.depth_limit),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    secrets = Secrets()
    # MAJOR M-C (third fix round): register every raw credential this process holds with the
    # logging module's value registry *before* anything can log a line -- otherwise a bare
    # Tabdeal API key/secret leaking into free text (no recognizable prefix, no sensitive key
    # name) would be emitted verbatim even though this process never uses them for requests
    # (see the `api_key=None, api_secret=None` note below; they can still sit in `Secrets()`
    # because the same `.env` is shared with other services, e.g. the bot).
    register_secrets_for_logging(secrets)
    # Minor fix 7 (fix round): TBOT_CONFIG is documented in deploy/docker-compose as the way to
    # select a config file in the container, but this script only ever read --config, silently
    # ignoring the env var. ``Secrets().config`` already parses $TBOT_CONFIG (default "default");
    # honour it whenever --config was not passed explicitly.
    if args.config is None:
        args.config = secrets.config
    config = load_config(args.config, config_dir=args.config_dir)
    configure_logging(config.runtime.log_level)

    settings = build_settings(args, config)
    db_path: Path = args.db_path or config.data.tabdeal_db
    parquet_root: Path = args.parquet_root or config.data.parquet_root
    heartbeat_file: Path = args.heartbeat_file or Path(
        os.environ.get("TBOT_HEARTBEAT_FILE", str(_DEFAULT_HEARTBEAT_FILE))
    )

    clock: Clock = SystemClock()
    # Minor fix 5 (fix round): this recorder only ever calls public endpoints (/trades, /depth)
    # -- it has no business holding Tabdeal API credentials at all. Passing them explicitly as
    # None (rather than secrets.tabdeal_api_key/secret, even though they would just sit unused)
    # keeps them out of this process's memory entirely, which matters because they would
    # otherwise show up in the container env and in `docker inspect`.
    client = TabdealClient(
        base_url=config.exchange.base_url,
        read_prefix=config.exchange.read_prefix,
        write_prefix=config.exchange.write_prefix,
        clock=clock,
        api_key=None,
        api_secret=None,
        recv_window_ms=config.exchange.recv_window_ms,
        requests_per_second=config.exchange.requests_per_second,
        timeout_seconds=config.exchange.timeout_seconds,
        max_retries=config.exchange.max_retries,
    )
    store = RecorderStore(db_path)
    service = TabdealRecorderService(
        client=client,
        store=store,
        parquet_root=parquet_root,
        heartbeat_file=heartbeat_file,
        settings=settings,
        clock=clock,
    )

    def _handle_signal(signum: int, _frame: types.FrameType | None) -> None:
        logger.info("tabdeal_recorder.shutdown_requested", signal=signum)
        service.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "tabdeal_recorder.starting",
        symbol=settings.symbol,
        poll_interval_seconds=settings.poll_interval_seconds,
        orderbook_interval_seconds=settings.orderbook_interval_seconds,
        grace_period_seconds=settings.grace_period_seconds,
        db_path=str(db_path),
        parquet_root=str(parquet_root),
        heartbeat_file=str(heartbeat_file),
    )
    try:
        if args.once:
            service.process_once()
        else:
            service.run_forever()
    finally:
        service.close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
