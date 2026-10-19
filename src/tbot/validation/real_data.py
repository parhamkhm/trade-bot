"""Pre-holdout Binance bars from the local Parquet store, for the Phase 3 run script and the validation tests.

Every bar returned closes strictly before ``CANONICAL_HOLDOUT_START`` (2025-10-01); this module never passes
``allow_holdout`` and refuses a request that reaches past it, so it cannot load the sealed holdout.

The Parquet store is not committed. It is looked for in (1) the ``TBOT_VALIDATION_DATA`` environment variable,
(2) ``<repo>/data/parquet`` and (3) every ancestor directory of the repo (a git worktree lives under
``<main checkout>/.claude/worktrees/<name>``).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tbot.core.config import DataConfig
from tbot.core.types import Bar, Timeframe
from tbot.data.binance_loader import CANONICAL_HOLDOUT_START, load_bars

__all__ = ["find_parquet_root", "real_bars"]

_REPO = Path(__file__).resolve().parents[3]


def find_parquet_root() -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("TBOT_VALIDATION_DATA")
    if env:
        candidates.append(Path(env))
    for base in (_REPO, *_REPO.parents):
        candidates.append(base / "data" / "parquet")
    for path in candidates:
        if (path / "klines" / "source=binance").is_dir():
            return path
    return None


def real_bars(symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    """Pre-holdout Binance bars with ``start <= bar.ts < end``. Raises ``FileNotFoundError`` if no store."""
    if end > CANONICAL_HOLDOUT_START:
        raise ValueError(f"validation must not read past the holdout start (end={end})")
    root = find_parquet_root()
    if root is None:
        raise FileNotFoundError("no Parquet store found (set TBOT_VALIDATION_DATA)")
    bars = load_bars(
        symbol,
        timeframe,
        start=start,
        end=end,
        config=DataConfig(parquet_root=root),
        caller="tbot.validation.real_data",
        reason="Phase 3 validation (pre-holdout only)",
    )
    bars = [b for b in bars if start <= b.ts < end]
    if any(b.ts >= CANONICAL_HOLDOUT_START for b in bars):
        raise AssertionError("a bar at or after the sealed holdout start was returned")
    return bars
