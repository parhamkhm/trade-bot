"""Typed configuration. YAML holds behaviour, the environment holds secrets.

Layering (documented in docs/SPEC.md section 4):

1. ``config/<name>.yaml`` — committed, no secrets, selects behaviour per mode.
2. ``.env`` / process environment, prefix ``TBOT_`` — secrets and the live kill switch.

The live order path is reachable only when ``Secrets.live_trading`` is true AND
``runtime.phase >= 6`` AND ``runtime.mode == "live"`` (CLAUDE.md section 3.6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tbot.core.types import Timeframe

__all__ = [
    "Config",
    "CostConfig",
    "DataConfig",
    "ExchangeConfig",
    "RiskConfig",
    "RuntimeConfig",
    "Secrets",
    "load_config",
]

CONFIG_DIR = Path("config")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(_Base):
    """Which mode the process runs in and which project phase the repo has reached."""

    mode: Literal["backtest", "paper", "live"] = "backtest"
    phase: int = Field(default=0, ge=0, le=7)
    quote_asset: str = "USDT"
    base_asset: str = "BTC"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class CostConfig(_Base):
    """Fees and slippage applied in every backtest (CLAUDE.md section 3.4)."""

    taker_fee_bps: Decimal = Decimal("10")  # LBank spot VIP 0, verified 2026-10-10, D-060
    maker_fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")  # provisional until the LBank G0 probe measures it (D-060)

    @field_validator("taker_fee_bps", "maker_fee_bps", "slippage_bps")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost components must be >= 0 bps")
        return value


class DataConfig(_Base):
    """Where market data lives and where the sealed holdout starts."""

    parquet_root: Path = Path("data/parquet")
    raw_root: Path = Path("data/raw")
    tabdeal_db: Path = Path("data/tabdeal/trades.sqlite")
    binance_base_url: str = "https://data.binance.vision"
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
    history_start: datetime = datetime(2018, 1, 1, tzinfo=UTC)
    # DOCUMENTATION ONLY (decision D-030). The enforced seal is the code constant
    # CANONICAL_HOLDOUT_START in tbot.data.binance_loader; a config that disagrees with it is
    # refused and logged (D-026), so editing this value cannot unseal the holdout.
    holdout_start: datetime = datetime(2025, 10, 1, tzinfo=UTC)

    @field_validator("history_start", "holdout_start")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("dates in DataConfig must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.holdout_start <= self.history_start:
            raise ValueError("holdout_start must be after history_start")
        return self


class ExchangeConfig(_Base):
    """Tabdeal endpoint and client-side throttling (limits are undocumented: stay conservative)."""

    base_url: str = "https://api1.tabdeal.org"
    read_prefix: str = "/r/api/v1"
    write_prefix: str = "/api/v1"
    recv_window_ms: int = Field(default=5_000, ge=1_000, le=60_000)
    requests_per_second: float = Field(default=5.0, gt=0, le=20)
    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=5, ge=0, le=10)


class RiskConfig(_Base):
    """Pre-set risk limits. The RiskManager (phase 4) is the only consumer."""

    max_exposure: float = Field(default=1.0, ge=0.0, le=1.0)
    annual_vol_target: float = Field(default=0.20, gt=0.0, le=2.0)
    max_daily_loss_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    max_orders_per_hour: int = Field(default=6, ge=1)
    max_data_age_seconds: int = Field(default=900, ge=1)


class Config(_Base):
    """Root configuration object."""

    runtime: RuntimeConfig = RuntimeConfig()
    costs: CostConfig = CostConfig()
    data: DataConfig = DataConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    risk: RiskConfig = RiskConfig()

    @model_validator(mode="after")
    def _phase_gate(self) -> Self:
        if self.runtime.mode == "live" and self.runtime.phase < 6:
            raise ValueError("mode 'live' requires runtime.phase >= 6")
        return self

    def live_orders_enabled(self, secrets: Secrets) -> bool:
        """The single place that may authorise a real order."""
        return secrets.live_trading and self.runtime.phase >= 6 and self.runtime.mode == "live"


class Secrets(BaseSettings):
    """Secrets and the kill switch, from the environment only. Never logged, never committed."""

    model_config = SettingsConfigDict(
        env_prefix="TBOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    tabdeal_api_key: SecretStr | None = None
    tabdeal_api_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    live_trading: bool = False
    config: str = "default"

    @property
    def has_tabdeal_credentials(self) -> bool:
        return self.tabdeal_api_key is not None and self.tabdeal_api_secret is not None


def load_config(name_or_path: str | Path = "default", *, config_dir: Path = CONFIG_DIR) -> Config:
    """Load ``config/<name>.yaml`` (or an explicit path) into a validated ``Config``."""
    path = Path(name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = config_dir / f"{path}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return Config.model_validate(raw)
