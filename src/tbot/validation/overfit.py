"""PBO, DSR and MinTRL for the walk-forward candidates, built from the SPEC's own definitions.

* **PBO** (SPEC 7.2 E): CSCV per candidate over its grid, on the daily manual-delay return matrix of the grid
  points over 2018-06-01 -> 2025-09-30, S = 16 equal contiguous blocks, Sharpe as the measure. A point is
  included from its first tradable close; the days before it count as flat (zero return).
* **DSR** (SPEC 7.1): daily non-annualised Sharpe of the candidate's pooled out-of-sample manual-delay series;
  ``N`` = the number of non-S0 trials in ``research/EXPERIMENTS.md``; ``V[SR]`` = variance (ddof = 1) of the
  daily Sharpe of those N trials, each measured on its own pooled out-of-sample manual-delay series
  (``fixed_pooled`` below). S0 uses N = 1, so SR* = 0.
* **MinTRL** (SPEC G3, reported, not gating): 95 % against 0 and against buy-and-hold's daily Sharpe.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import numpy as np

from tbot.validation.grid import GridPoint
from tbot.validation.runs import SeriesRun
from tbot.validation.stats import (
    Dsr,
    MinTrl,
    PboResult,
    daily_sharpe,
    deflated_sharpe,
    min_trl,
    pbo_cscv,
    trial_variance,
)

__all__ = [
    "candidate_dsr",
    "candidate_min_trl",
    "candidate_pbo",
    "pbo_matrix",
    "s0_dsr",
    "trial_daily_sharpes",
]

PBO_BLOCKS = 16


def pbo_matrix(
    points: Sequence[GridPoint],
    fixed: Mapping[str, SeriesRun],
    first_tradable: Mapping[str, datetime],
    daily_closes: Sequence[datetime],
    *,
    start: datetime,
    end: datetime,
) -> np.ndarray:
    """Rows = daily closes in ``[start, end)``; columns = points; zero before a point's first tradable."""
    rows = [c for c in daily_closes if start <= c < end]
    cols = []
    for p in points:
        run = fixed[p.key]
        by_ts = dict(zip(run.return_ts, run.returns, strict=True))
        missing = [c for c in rows if c not in by_ts]
        if missing:
            raise ValueError(f"{p.key}: no return at {len(missing)} closes, first {missing[0]}")
        first = first_tradable[p.key]
        cols.append([by_ts[c] if c >= first else 0.0 for c in rows])
    return np.asarray(cols, dtype=np.float64).T


def candidate_pbo(
    points: Sequence[GridPoint],
    fixed: Mapping[str, SeriesRun],
    first_tradable: Mapping[str, datetime],
    daily_closes: Sequence[datetime],
    *,
    start: datetime,
    end: datetime,
) -> PboResult:
    matrix = pbo_matrix(points, fixed, first_tradable, daily_closes, start=start, end=end)
    return pbo_cscv(matrix, n_blocks=PBO_BLOCKS)


def trial_daily_sharpes(fixed_pooled: Mapping[str, SeriesRun]) -> dict[str, float]:
    """Daily (non-annualised) Sharpe of each trial's pooled out-of-sample series; undefined scores 0."""
    out: dict[str, float] = {}
    for key, run in fixed_pooled.items():
        sr = daily_sharpe(run.returns)
        out[key] = 0.0 if sr is None else sr
    return out


def candidate_dsr(
    pooled_returns: Sequence[float], trial_sharpes: Mapping[str, float], logged_non_s0_trials: int
) -> Dsr | None:
    """DSR with N = the logged non-S0 trial count. ``None`` for a flat (undefined-Sharpe) series.

    Refuses to run unless a pooled series exists for every logged trial: V[SR] must be taken over exactly the
    trials that N counts, otherwise the deflation silently uses a different population.
    """
    if len(trial_sharpes) != logged_non_s0_trials:
        raise ValueError(
            f"{logged_non_s0_trials} trials are logged but {len(trial_sharpes)} trial series were supplied"
        )
    if daily_sharpe(pooled_returns) is None:
        return None
    return deflated_sharpe(
        pooled_returns, n_trials=logged_non_s0_trials, var_sr=trial_variance(list(trial_sharpes.values()))
    )


def s0_dsr(pooled_returns: Sequence[float]) -> Dsr | None:
    """S0 is one trial (N = 1, SR* = 0)."""
    if daily_sharpe(pooled_returns) is None:
        return None
    return deflated_sharpe(pooled_returns, n_trials=1, var_sr=None)


def candidate_min_trl(
    pooled_returns: Sequence[float], benchmark_returns: Sequence[float]
) -> tuple[MinTrl | None, MinTrl | None]:
    """(MinTRL against 0, MinTRL against buy-and-hold's daily Sharpe); each ``None`` if undefined."""
    vs_zero = min_trl(pooled_returns, sr_benchmark=0.0)
    bh = daily_sharpe(benchmark_returns)
    vs_bh = None if bh is None else min_trl(pooled_returns, sr_benchmark=bh)
    return vs_zero, vs_bh
