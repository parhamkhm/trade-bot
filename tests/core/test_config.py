"""Tests for src/tbot/core/config.py, including the live-trading safety gate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from tbot.core.config import Config, RuntimeConfig, Secrets, load_config
from tbot.core.types import Timeframe

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_yaml_loads_and_matches_schema():
    config = load_config("default", config_dir=REPO_ROOT / "config")
    assert config.runtime.mode == "backtest"
    assert config.runtime.phase == 0
    assert config.costs.taker_fee_bps == Decimal("20")
    assert config.data.symbols == ("BTCUSDT", "ETHUSDT")
    assert Timeframe.H4 in config.data.timeframes


def test_default_yaml_holdout_is_a_fixed_utc_date():
    config = load_config("default", config_dir=REPO_ROOT / "config")
    assert config.data.holdout_start == datetime(2025, 10, 1, tzinfo=UTC)
    assert config.data.holdout_start > config.data.history_start


def test_unknown_config_key_is_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"runtime": {"mode": "backtest", "unknown": 1}})


def test_live_mode_requires_phase_six():
    with pytest.raises(ValidationError, match="phase >= 6"):
        Config.model_validate({"runtime": {"mode": "live", "phase": 1}})


def test_live_orders_need_switch_phase_and_mode():
    backtest = Config()
    live_ready = Config(runtime=RuntimeConfig(mode="live", phase=6))
    on = Secrets(live_trading=True)
    off = Secrets(live_trading=False)

    assert backtest.live_orders_enabled(on) is False
    assert live_ready.live_orders_enabled(off) is False
    assert live_ready.live_orders_enabled(on) is True


def test_config_is_frozen():
    config = Config()
    with pytest.raises(ValidationError):
        config.runtime.phase = 6


def test_secrets_default_to_absent_and_never_render_values():
    secrets = Secrets(tabdeal_api_key=SecretStr("SUPERSECRET"), tabdeal_api_secret=SecretStr("ALSOSECRET"))
    assert secrets.has_tabdeal_credentials
    assert "SUPERSECRET" not in repr(secrets)
    assert "ALSOSECRET" not in str(secrets)
    assert secrets.tabdeal_api_key is not None
    assert secrets.tabdeal_api_key.get_secret_value() == "SUPERSECRET"


def test_secrets_absent_by_default():
    assert Secrets(_env_file=None).has_tabdeal_credentials is False  # type: ignore[call-arg]


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("does-not-exist", config_dir=REPO_ROOT / "config")
