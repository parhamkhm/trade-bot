"""Registry of the Phase 3 strategies pre-registered in docs/SPEC.md §7.2 C (S0 + the 14 grid points, D-090).

The single source the Phase 3 tests and the walk-forward harness iterate over. Each entry names the module
and factory the strategy-researcher implements; until a module exists, ``load`` raises
``ModuleNotFoundError`` and the strategy-specific test cases skip as "not implemented yet". The grid values
here must equal SPEC §7.2 C exactly.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from tbot.core.types import Strategy, Timeframe

__all__ = ["PHASE3_ENTRIES", "Phase3Entry", "load"]


@dataclass(frozen=True)
class Phase3Entry:
    key: str
    module: str
    factory: str
    timeframe: Timeframe
    params: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


def _entry(key: str, module: str, tf: Timeframe, **params: object) -> Phase3Entry:
    return Phase3Entry(key, module, "make", tf, MappingProxyType(dict(params)))


_D1 = Timeframe.D1

PHASE3_ENTRIES: tuple[Phase3Entry, ...] = (
    _entry("S0", "tbot.strategies.s0_sma100", _D1),
    _entry("C1-(10,28,50)", "tbot.strategies.donchian", _D1, lookbacks=(10, 28, 50)),
    _entry("C1-(20,55,100)", "tbot.strategies.donchian", _D1, lookbacks=(20, 55, 100)),
    _entry("C1-(40,110,200)", "tbot.strategies.donchian", _D1, lookbacks=(40, 110, 200)),
    _entry("C2-(15,45,90)", "tbot.strategies.tsmom", _D1, horizons=(15, 45, 90)),
    _entry("C2-(30,90,180)", "tbot.strategies.tsmom", _D1, horizons=(30, 90, 180)),
    _entry("C2-(60,180,360)", "tbot.strategies.tsmom", _D1, horizons=(60, 180, 360)),
    _entry("C3-1d-(8,16,32)", "tbot.strategies.ewmac", _D1, fast_spans=(8, 16, 32)),
    _entry("C3-1d-(16,32,64)", "tbot.strategies.ewmac", _D1, fast_spans=(16, 32, 64)),
    _entry("C4-p10", "tbot.strategies.vol_breakout", _D1, pct=10),
    _entry("C4-p20", "tbot.strategies.vol_breakout", _D1, pct=20),
    _entry("C4-p30", "tbot.strategies.vol_breakout", _D1, pct=30),
    _entry("C5-x5", "tbot.strategies.pullback_mr", _D1, rsi_entry=5),
    _entry("C5-x10", "tbot.strategies.pullback_mr", _D1, rsi_entry=10),
    _entry("C5-x15", "tbot.strategies.pullback_mr", _D1, rsi_entry=15),
)


def load(entry: Phase3Entry) -> Strategy:
    """Instantiate an entry; raises ``ModuleNotFoundError`` while its module is not implemented."""
    module = importlib.import_module(entry.module)
    factory = getattr(module, entry.factory)
    strategy: Strategy = factory(timeframe=entry.timeframe, **dict(entry.params))
    return strategy
