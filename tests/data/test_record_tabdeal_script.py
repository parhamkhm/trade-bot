"""Tests for scripts/record_tabdeal.py (minor fixes 5 and 7 from the data-work fix round).

All HTTP is mocked with respx; no test reaches a real exchange. The script only ever calls
public Tabdeal endpoints (/trades, /depth).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import respx
import scripts.record_tabdeal as record_tabdeal
import structlog

from tbot.core.config import load_config
from tbot.execution.tabdeal_client import TabdealClient
from tbot.monitoring import logging as tbot_logging
from tbot.monitoring.logging import REDACTED

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://api1.tabdeal.org"
READ_PREFIX = "/r/api/v1"
TRADES_URL = f"{BASE_URL}{READ_PREFIX}/trades"
DEPTH_URL = f"{BASE_URL}{READ_PREFIX}/depth"


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _mock_public_endpoints() -> None:
    respx.get(TRADES_URL).mock(return_value=httpx.Response(200, json=_load_json("tabdeal_trades_empty.json")))
    respx.get(DEPTH_URL).mock(
        return_value=httpx.Response(200, json=_load_json("tabdeal_depth_sample.json"))
    )


# ---------------------------------------------------------------------------------
# minor fix 5: never pass Tabdeal credentials, even when present in the environment
# ---------------------------------------------------------------------------------


@respx.mock
def test_main_never_passes_tabdeal_credentials_even_if_present(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("TBOT_TABDEAL_API_KEY", "super-secret-key")
    monkeypatch.setenv("TBOT_TABDEAL_API_SECRET", "super-secret-value")
    _mock_public_endpoints()

    captured: dict[str, Any] = {}
    real_init = TabdealClient.__init__

    def spy_init(self: TabdealClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(TabdealClient, "__init__", spy_init)

    argv = [
        "--config",
        "default",
        "--config-dir",
        str(CONFIG_DIR),
        "--once",
        "--db-path",
        str(tmp_path / "trades.sqlite"),
        "--parquet-root",
        str(tmp_path / "parquet"),
        "--heartbeat-file",
        str(tmp_path / "heartbeat.json"),
    ]
    exit_code = record_tabdeal.main(argv)

    assert exit_code == 0
    assert captured["api_key"] is None
    assert captured["api_secret"] is None


# ---------------------------------------------------------------------------------
# minor fix 7: TBOT_CONFIG (via Secrets().config) is honoured when --config is not passed
# ---------------------------------------------------------------------------------


@respx.mock
def test_main_defaults_config_to_tbot_config_env_var(tmp_path: Path, monkeypatch: Any) -> None:
    custom_config = tmp_path / "custom.yaml"
    custom_config.write_text("runtime:\n  log_level: DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("TBOT_CONFIG", str(custom_config))
    monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
    monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
    _mock_public_endpoints()

    captured: dict[str, Any] = {}

    def spy_load_config(name_or_path: str, *, config_dir: Path = CONFIG_DIR) -> Any:
        captured["name_or_path"] = name_or_path
        return load_config(name_or_path, config_dir=config_dir)

    monkeypatch.setattr("scripts.record_tabdeal.load_config", spy_load_config)

    argv = [
        # --config deliberately omitted: must fall back to $TBOT_CONFIG
        "--once",
        "--db-path",
        str(tmp_path / "trades.sqlite"),
        "--parquet-root",
        str(tmp_path / "parquet"),
        "--heartbeat-file",
        str(tmp_path / "heartbeat.json"),
    ]
    exit_code = record_tabdeal.main(argv)

    assert exit_code == 0
    assert captured["name_or_path"] == str(custom_config)


def test_build_settings_default_config_argument_is_none() -> None:
    """The argparse default must no longer be the hardcoded string "default" -- otherwise
    main() can never tell "user passed --config default" apart from "user passed nothing"."""
    args = record_tabdeal._parse_args(["--once"])
    assert args.config is None


def test_sanity_default_config_loads() -> None:
    # Guards against the test helpers above silently drifting from the real config schema.
    config = load_config("default", config_dir=CONFIG_DIR)
    assert config.exchange.base_url == BASE_URL


# ---------------------------------------------------------------------------------
# MAJOR M-C (third fix round, infra-ops half): the recorder shares a `.env` with other
# services (e.g. the bot), so `Secrets()` can carry real Tabdeal/Telegram credentials here
# even though this script itself never uses them for requests (minor fix 5 above). Before
# this fix, nothing registered those raw values with the logging module's value registry, so
# a credential leaking into free text elsewhere in this same process (no recognizable
# prefix, no sensitive key name) would have been emitted completely unredacted.
# ---------------------------------------------------------------------------------


@respx.mock
def test_main_registers_secrets_for_logging(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """Load `Secrets()` from a monkeypatched environment, run `main()`'s real startup wiring,
    and prove the raw credential is registered with the logging value registry as a side
    effect -- by logging it through the now-configured pipeline afterwards and asserting it
    comes out redacted, which is only possible if `register_secrets_for_logging(secrets)`
    actually ran during `main()` (nothing in this test calls it directly).

    Manual save/restore (rather than a module-wide autouse fixture) keeps this test's
    footprint to itself, since the rest of this file is shared with the data-engineer's
    parallel work this round.
    """
    original_excepthook = sys.excepthook
    secret_values_before = tbot_logging._SECRET_VALUES
    try:
        telegram_token = "123456789:AA-fake-bot-token-abcdefghijk"
        monkeypatch.setenv("TBOT_TELEGRAM_BOT_TOKEN", telegram_token)
        monkeypatch.delenv("TBOT_TABDEAL_API_KEY", raising=False)
        monkeypatch.delenv("TBOT_TABDEAL_API_SECRET", raising=False)
        _mock_public_endpoints()

        argv = [
            "--config",
            "default",
            "--config-dir",
            str(CONFIG_DIR),
            "--once",
            "--db-path",
            str(tmp_path / "trades.sqlite"),
            "--parquet-root",
            str(tmp_path / "parquet"),
            "--heartbeat-file",
            str(tmp_path / "heartbeat.json"),
        ]
        exit_code = record_tabdeal.main(argv)
        assert exit_code == 0

        assert structlog.is_configured()

        capsys.readouterr()  # discard main()'s own startup log line

        log = structlog.get_logger("test")
        # "value=" (not "token=") deliberately avoids also matching the embedded-param regex
        # (_EMBEDDED_PARAM_RE), which alone would redact a literal "token=..." -- this test
        # isolates the value-registry mechanism specifically.
        log.info("probe.credential_leak_check", note=f"leaked value={telegram_token}")

        rendered = capsys.readouterr().out
        assert telegram_token not in rendered
        assert REDACTED in rendered
    finally:
        structlog.reset_defaults()
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        sys.excepthook = original_excepthook
        tbot_logging._SECRET_VALUES = secret_values_before
