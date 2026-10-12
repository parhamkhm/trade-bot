"""Pre-flight checks that must pass before the first Phase 3 run (docs/SPEC.md section 7.2 D, F, T6).

The run script refuses to start while ANY of these fails, so a result can never be produced against an
incomplete trial log, an engine without the SPEC stop/band mechanics, or a registry whose strategy modules are
missing. Each check returns a list of plain-text problems (empty = ok); nothing here runs a backtest.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from tbot.core.types import Strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry
from tbot.validation.runs import engine_supports_stops_and_band
from tbot.validation.trials import TrialLog

__all__ = ["check_engine", "check_registry_importable", "check_trial_log", "preflight"]

EXPECTED_GRID_POINTS = 17


def check_trial_log(log: TrialLog, entries: Sequence[Phase3Entry] = PHASE3_ENTRIES) -> list[str]:
    """SPEC 7.2 D: all 17 grid points are logged (and S0 registered) before the first run."""
    keys = [e.key for e in entries if e.key != "S0"]
    problems: list[str] = []
    if len(keys) != EXPECTED_GRID_POINTS:
        problems.append(
            f"registry has {len(keys)} non-S0 points, the SPEC pre-registers {EXPECTED_GRID_POINTS}"
        )
    missing = log.missing(keys)
    if missing:
        problems.append("grid points not logged in research/EXPERIMENTS.md: " + ", ".join(missing))
    if not any(r.strip("| ").split("|")[0].strip() == "S0" for r in log.rows):
        problems.append("S0 is not registered in research/EXPERIMENTS.md")
    if log.n_non_s0 < len(keys):
        problems.append(f"{log.n_non_s0} non-S0 trial rows logged, at least {len(keys)} required")
    return problems


def check_engine() -> list[str]:
    """SPEC 7.1 stop execution and the 25 % relative band must exist in the engine under test."""
    if engine_supports_stops_and_band():
        return []
    return ["engine lacks BacktestConfig.enforce_stops / rebalance_band (the stops/band PR is not merged)"]


def check_registry_importable(
    loader: Callable[[Phase3Entry], Strategy], entries: Sequence[Phase3Entry] = PHASE3_ENTRIES
) -> list[str]:
    """Every registered strategy module can be imported and built (no stub, no missing module)."""
    problems: list[str] = []
    for entry in entries:
        try:
            loader(entry)
        except ModuleNotFoundError as exc:
            problems.append(f"{entry.key}: module not implemented ({exc.name})")
        except Exception as exc:
            problems.append(f"{entry.key}: cannot be built ({type(exc).__name__}: {exc})")
    return problems


def preflight(
    log: TrialLog,
    loader: Callable[[Phase3Entry], Strategy],
    entries: Sequence[Phase3Entry] = PHASE3_ENTRIES,
) -> list[str]:
    return [*check_trial_log(log, entries), *check_engine(), *check_registry_importable(loader, entries)]
