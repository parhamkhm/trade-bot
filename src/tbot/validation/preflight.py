"""Pre-flight checks that must pass before the first Phase 3 run (docs/SPEC.md section 7.2 D, F, T6).

The run script refuses to start while ANY of these fails, so a result can never be produced against an
incomplete trial log, an engine without the SPEC stop/band mechanics, a registry whose strategy modules are
missing, missing ETH data, unregistered S0 robustness variants or a flat-slippage placeholder. A missing input
is a reason to refuse, never a failed gate. Each check returns a list of plain-text problems (empty = ok);
nothing here runs a backtest.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tbot.core.types import Strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, S0_VARIANTS, Phase3Entry
from tbot.validation.runs import CostSpec, engine_supports_stops_and_band
from tbot.validation.trials import TrialLog

__all__ = [
    "ENGINE_MIN_COMMIT",
    "EXPECTED_GRID_POINTS",
    "EXPECTED_S0_VARIANTS",
    "check_data",
    "check_engine",
    "check_engine_commit",
    "check_registry_importable",
    "check_s0_variants",
    "check_slippage",
    "check_trial_log",
    "preflight",
]

EXPECTED_GRID_POINTS = 17
ENGINE_MIN_COMMIT = "8d99335"  # the Phase 2 engine merge (PR #15): the oldest engine the harness may run on
# (sma_n, vol_target) of the six SPEC 7.1 variants
EXPECTED_S0_VARIANTS = frozenset({(50, 0.30), (75, 0.30), (150, 0.30), (200, 0.30), (100, 0.20), (100, 0.40)})
_REPO = Path(__file__).resolve().parents[3]


def check_trial_log(log: TrialLog, entries: Sequence[Phase3Entry] = PHASE3_ENTRIES) -> list[str]:
    """SPEC 7.2 D: exactly the 17 grid points are logged as trials and S0 is registered, before the run."""
    keys = [e.key for e in entries if e.key != "S0"]
    problems: list[str] = []
    if len(keys) != EXPECTED_GRID_POINTS:
        problems.append(
            f"registry has {len(keys)} non-S0 points, the SPEC pre-registers {EXPECTED_GRID_POINTS}"
        )
    missing = log.missing(keys)
    if missing:
        problems.append("grid points not logged in research/EXPERIMENTS.md: " + ", ".join(missing))
    if not log.s0_registered:
        problems.append("S0 is not registered in research/EXPERIMENTS.md")
    if log.n_non_s0 != EXPECTED_GRID_POINTS:
        problems.append(
            f"{log.n_non_s0} non-S0 trial rows logged, exactly {EXPECTED_GRID_POINTS} required "
            "(S0, the S0 variants and S0-... rows are not counted)"
        )
    return problems


def check_s0_variants(variants: Sequence[Phase3Entry] = S0_VARIANTS) -> list[str]:
    """The six SPEC 7.1 variants are registered, as sensitivity runs and not as trials."""
    problems: list[str] = []
    seen = {(v.params.get("sma_n"), v.params.get("vol_target")) for v in variants}
    if len(variants) != len(EXPECTED_S0_VARIANTS) or seen != EXPECTED_S0_VARIANTS:
        problems.append(
            "S0 variants are not exactly SMA 50/75/150/200 at vol 0.30 and vol 0.20/0.40 at SMA 100"
        )
    grid_keys = {e.key for e in PHASE3_ENTRIES}
    for v in variants:
        if v.counts_as_trial:
            problems.append(f"{v.key}: an S0 variant must not count as a trial")
        if v.key in grid_keys:
            problems.append(f"{v.key}: an S0 variant must not be one of the trial entries")
    return problems


def check_engine() -> list[str]:
    """SPEC 7.1 stop execution and the 25 % relative band must exist in the engine under test."""
    if engine_supports_stops_and_band():
        return []
    return ["engine lacks BacktestConfig.enforce_stops / rebalance_band (the stops/band PR is not merged)"]


def check_engine_commit(repo: Path = _REPO, min_commit: str = ENGINE_MIN_COMMIT) -> list[str]:
    """The checked-out engine is at or after ``min_commit`` (``git merge-base --is-ancestor``)."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", min_commit, "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"cannot verify the engine commit ({type(exc).__name__}: {exc})"]
    if proc.returncode == 0:
        return []
    detail = "is not an ancestor of HEAD" if proc.returncode == 1 else f"git failed: {proc.stderr.strip()}"
    return [f"engine commit {min_commit} {detail}: the engine under test is older than the Phase 2 engine"]


def check_slippage(costs: CostSpec, *, allow_flat: bool) -> list[str]:
    """A real run needs the depth-curve slippage model; flat bps is for tests only (explicit opt-in)."""
    if costs.is_flat_slippage and not allow_flat:
        return [
            "flat slippage is a PROVISIONAL placeholder: supply the depth-curve model "
            "(--slippage-model) or, in tests only, --allow-flat-slippage-for-tests"
        ]
    return []


def check_data(missing_series: Sequence[str]) -> list[str]:
    """Every required BTC and ETH series is present (``missing_series``: ``SYMBOL/tf`` that have no bars)."""
    return [f"required data missing: {name}" for name in missing_series]


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
    *,
    variants: Sequence[Phase3Entry] = S0_VARIANTS,
    costs: CostSpec | None = None,
    allow_flat_slippage: bool = False,
    missing_series: Sequence[str] = (),
    engine_repo: Path = _REPO,
) -> list[str]:
    """Every check. ``costs=None`` skips only the slippage check (unit tests of the other checks)."""
    problems = [
        *check_trial_log(log, entries),
        *check_s0_variants(variants),
        *check_engine(),
        *check_engine_commit(engine_repo),
        *check_data(missing_series),
        *check_registry_importable(loader, [*entries, *variants]),
    ]
    if costs is not None:
        problems.extend(check_slippage(costs, allow_flat=allow_flat_slippage))
    return problems
