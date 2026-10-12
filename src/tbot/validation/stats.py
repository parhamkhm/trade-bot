"""Statistics for the Phase 3 harness (docs/SPEC.md section 7.1 "Deflated Sharpe" and section 7.2 E).

Pure functions on daily return series. Implemented with the standard library (``statistics.NormalDist``)
and numpy only; no scipy.

Conventions, fixed here so every report uses one definition:

* Annualised Sharpe = ``mean / std(ddof=1) * sqrt(periods_per_year)`` (365 for crypto, 252 for the Stooq
  indices), risk-free rate 0. It reuses ``tbot.backtest.metrics.sharpe_ratio``; an undefined ratio (zero
  variance, fewer than two observations) is ``None``, never 0.
* The **daily, non-annualised** Sharpe is the same ratio with ``periods_per_year = 1``. It is the Sharpe
  used inside PSR, DSR and MinTRL (SPEC 7.1: "Sharpe inside PSR and SR* is the daily, non-annualised Sharpe").
* Skewness ``gamma3 = m3 / m2^1.5`` and (non-excess) kurtosis ``gamma4 = m4 / m2^2`` use population moments
  (numpy / scipy default ``bias=True``), as in Bailey & Lopez de Prado. A Normal series has ``gamma4 = 3``.
* ``PSR(SR*) = Phi((SR_hat - SR*) sqrt(T - 1) / sqrt(1 - gamma3 SR_hat + (gamma4 - 1)/4 SR_hat^2))``.
* ``SR* = sqrt(V) ((1 - g) Phi^-1(1 - 1/N) + g Phi^-1(1 - 1/(N e)))`` with ``g`` = Euler-Mascheroni, the
  expected maximum of N independent Sharpe ratios of variance ``V``; ``N = 1`` gives ``SR* = 0``.
* ``DSR = PSR(SR*)``, a probability in [0, 1]; "DSR > 0 at the 95 % level" means ``DSR >= 0.95``.
* ``MinTRL = 1 + [1 - gamma3 SR_hat + (gamma4 - 1)/4 SR_hat^2] (Z_alpha / (SR_hat - SR*))^2`` observations.
* PBO by combinatorially symmetric cross-validation (CSCV), Bailey, Borwein, Lopez de Prado & Zhu (2014).
  D-093: lambda <= 0 counts as overfit and is THE gating value (``PboResult.pbo``); the share with
  lambda < 0 is computed beside it for information (``PboResult.pbo_strict``).

References are in ``tests/validation/test_stats_reference.py``, which pins every function to a published
worked example or an independent implementation.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import NDArray

from tbot.backtest.metrics import sharpe_ratio

__all__ = [
    "EULER_MASCHERONI",
    "Dsr",
    "MinTrl",
    "PboResult",
    "annualised_sharpe",
    "daily_sharpe",
    "deflated_sharpe",
    "expected_max_sharpe",
    "kurtosis",
    "max_drawdown_of_returns",
    "min_trl",
    "min_trl_observations",
    "pbo_cscv",
    "psr",
    "skewness",
    "trial_variance",
]

EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()
FloatArray = NDArray[np.float64]


# ---------------------------------------------------------------------------------------------
# elementary statistics
# ---------------------------------------------------------------------------------------------


def _as_array(returns: Sequence[float] | FloatArray) -> FloatArray:
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("a return series must be one-dimensional")
    if not np.all(np.isfinite(arr)):
        raise ValueError("a return series must be finite")
    return arr


def annualised_sharpe(returns: Sequence[float] | FloatArray, periods_per_year: float = 365.0) -> float | None:
    """``mean / std(ddof=1) * sqrt(ppy)``; ``None`` if the variance is zero or there are < 2 values."""
    return sharpe_ratio([float(x) for x in _as_array(returns)], periods_per_year)


def daily_sharpe(returns: Sequence[float] | FloatArray) -> float | None:
    """The non-annualised Sharpe used inside PSR / DSR / MinTRL."""
    return annualised_sharpe(returns, 1.0)


def max_drawdown_of_returns(returns: Sequence[float] | FloatArray) -> float:
    """Deepest peak-to-trough fall (positive fraction) of the equity curve compounded from 1.0."""
    arr = _as_array(returns)
    if arr.size == 0:
        return 0.0
    equity = np.concatenate(([1.0], np.cumprod(1.0 + arr)))
    peak = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, 1.0 - equity / peak, 0.0)
    return float(dd.max())


def skewness(returns: Sequence[float] | FloatArray) -> float:
    """Population skewness ``m3 / m2^1.5``; 0 for a constant series."""
    arr = _as_array(returns)
    if arr.size < 2:
        return 0.0
    centred = arr - arr.mean()
    m2 = float(np.mean(centred**2))
    if m2 == 0.0:
        return 0.0
    return float(np.mean(centred**3) / m2**1.5)


def kurtosis(returns: Sequence[float] | FloatArray) -> float:
    """Population (non-excess) kurtosis ``m4 / m2^2``; 3 for a constant series (the Normal value)."""
    arr = _as_array(returns)
    if arr.size < 2:
        return 3.0
    centred = arr - arr.mean()
    m2 = float(np.mean(centred**2))
    if m2 == 0.0:
        return 3.0
    return float(np.mean(centred**4) / m2**2)


# ---------------------------------------------------------------------------------------------
# PSR / DSR / MinTRL
# ---------------------------------------------------------------------------------------------


def _sharpe_variance_factor(sr_hat: float, skew: float, kurt: float) -> float:
    """``1 - gamma3 SR + (gamma4 - 1)/4 SR^2``: the variance factor of the Sharpe estimator."""
    return 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat * sr_hat


def psr(sr_hat: float, sr_star: float, n_obs: int, skew: float, kurt: float) -> float | None:
    """Probabilistic Sharpe Ratio: the probability that the true Sharpe exceeds ``sr_star``.

    All Sharpe ratios are in the same (non-annualised) units; ``n_obs`` is T. ``None`` when the variance
    factor is not positive (the estimator's variance is undefined for such skew / kurtosis).
    """
    if n_obs < 2:
        raise ValueError("PSR needs at least 2 observations")
    factor = _sharpe_variance_factor(sr_hat, skew, kurt)
    if factor <= 0.0:
        return None
    return _NORMAL.cdf((sr_hat - sr_star) * math.sqrt(n_obs - 1) / math.sqrt(factor))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """SR*: expected maximum of ``n_trials`` independent Sharpe ratios whose variance is ``var_sr``.

    ``n_trials == 1`` gives exactly 0 (no selection happened). ``var_sr`` is the variance of the Sharpe
    ratio ACROSS trials, in the same (daily, non-annualised) units as the Sharpe that is being judged.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if var_sr < 0.0:
        raise ValueError("var_sr must be >= 0")
    if n_trials == 1:
        return 0.0
    g = EULER_MASCHERONI
    z1 = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1.0 - g) * z1 + g * z2)


def trial_variance(trial_sharpes: Sequence[float]) -> float:
    """Sample variance (ddof=1) of the daily Sharpe across trials. Needs at least two trials."""
    if len(trial_sharpes) < 2:
        raise ValueError("the variance across trials needs at least 2 trials")
    return float(np.var(np.asarray(trial_sharpes, dtype=np.float64), ddof=1))


@dataclass(frozen=True, slots=True)
class Dsr:
    """Deflated Sharpe ratio of one return series (SPEC 7.1)."""

    dsr: float | None  # PSR(SR*); None if undefined
    psr_vs_zero: float | None  # PSR(0), reported next to it
    sr_hat: float  # daily, non-annualised
    sr_star: float
    n_trials: int
    var_sr: float | None  # None when n_trials == 1
    n_obs: int
    skew: float
    kurt: float

    @property
    def passes(self) -> bool:
        return self.dsr is not None and self.dsr >= 0.95


def deflated_sharpe(returns: Sequence[float] | FloatArray, *, n_trials: int, var_sr: float | None) -> Dsr:
    """DSR of a daily return series. ``var_sr`` is required (and used) only when ``n_trials > 1``."""
    arr = _as_array(returns)
    sr_hat = daily_sharpe(arr)
    if sr_hat is None:
        raise ValueError("the Sharpe of this return series is undefined (zero variance)")
    if n_trials > 1 and var_sr is None:
        raise ValueError("var_sr (variance of the daily Sharpe across trials) is required for n_trials > 1")
    sr_star = expected_max_sharpe(n_trials, var_sr or 0.0) if n_trials > 1 else 0.0
    skew, kurt = skewness(arr), kurtosis(arr)
    return Dsr(
        dsr=psr(sr_hat, sr_star, arr.size, skew, kurt),
        psr_vs_zero=psr(sr_hat, 0.0, arr.size, skew, kurt),
        sr_hat=sr_hat,
        sr_star=sr_star,
        n_trials=n_trials,
        var_sr=var_sr if n_trials > 1 else None,
        n_obs=int(arr.size),
        skew=skew,
        kurt=kurt,
    )


def min_trl_observations(
    sr_hat: float, sr_star: float, skew: float, kurt: float, *, confidence: float = 0.95
) -> float:
    """Minimum track-record length, in observations, for ``sr_hat`` to beat ``sr_star`` at ``confidence``.

    ``inf`` when ``sr_hat <= sr_star`` (no track record length is enough). ``confidence`` is the one-sided
    level (0.95 -> Z = 1.6449).
    """
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be in (0.5, 1)")
    if sr_hat <= sr_star:
        return math.inf
    factor = _sharpe_variance_factor(sr_hat, skew, kurt)
    if factor <= 0.0:
        return math.inf
    z = _NORMAL.inv_cdf(confidence)
    return 1.0 + factor * (z / (sr_hat - sr_star)) ** 2


@dataclass(frozen=True, slots=True)
class MinTrl:
    """MinTRL of a daily return series against a benchmark daily Sharpe, at 95 % (SPEC G3)."""

    observations: float  # days; inf when the series does not beat the benchmark
    years: float  # observations / 365
    sr_hat: float
    sr_benchmark: float
    n_obs: int  # length of the series actually available
    sufficient: bool  # n_obs >= observations


def min_trl(
    returns: Sequence[float] | FloatArray, *, sr_benchmark: float = 0.0, confidence: float = 0.95
) -> MinTrl | None:
    """MinTRL of ``returns`` against ``sr_benchmark`` (daily, non-annualised); ``None`` if undefined."""
    arr = _as_array(returns)
    sr_hat = daily_sharpe(arr)
    if sr_hat is None:
        return None
    n = min_trl_observations(sr_hat, sr_benchmark, skewness(arr), kurtosis(arr), confidence=confidence)
    return MinTrl(n, n / 365.0, sr_hat, sr_benchmark, int(arr.size), arr.size >= n)


# ---------------------------------------------------------------------------------------------
# PBO by CSCV
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PboResult:
    pbo: float  # P(logit <= 0): the GATING value (D-093)
    n_combinations: int
    n_blocks: int
    block_len: int
    dropped_leading_days: int  # earliest rows dropped so the blocks are exactly equal
    n_strategies: int
    logits: tuple[float, ...]  # one per combination, in itertools.combinations order
    pbo_strict: float  # P(logit < 0): reported for information only (D-093), never gating


def _combo_sharpes(
    block_mean: FloatArray, block_m2: FloatArray, block_len: int, combos: NDArray[np.intp]
) -> FloatArray:
    """Daily Sharpe (mean / std ddof=1; 0.0 for a zero-variance column) of every block combination at once.

    ``combos`` is ``(C, k)`` block indices. Equal-length blocks are pooled with the exact parallel-variance
    identity ``M2 = sum(M2_b) + L * sum((m_b - m)^2)`` (numerically stable, no ``sumsq - mean^2``).
    Returns ``(C, N)``.
    """
    means = block_mean[combos]  # (C, k, N)
    pooled_mean = means.mean(axis=1)  # equal block lengths
    m2 = block_m2[combos].sum(axis=1) + block_len * ((means - pooled_mean[:, None, :]) ** 2).sum(axis=1)
    n_obs = combos.shape[1] * block_len
    std = np.sqrt(m2 / (n_obs - 1))
    out = np.zeros_like(pooled_mean)
    nonzero = std > 0.0
    out[nonzero] = pooled_mean[nonzero] / std[nonzero]
    return out


def pbo_cscv(matrix: FloatArray, *, n_blocks: int = 16) -> PboResult:
    """Probability of Backtest Overfitting by CSCV on a ``(T, N)`` daily return matrix.

    ``matrix[:, j]`` is trial ``j``'s daily returns, all on the SAME days. The rows are cut into
    ``n_blocks`` equal contiguous blocks (the earliest ``T mod n_blocks`` rows are dropped so the blocks
    are exactly equal). For each of the ``C(n_blocks, n_blocks / 2)`` ways to pick half the blocks as the
    in-sample set (the rest is out-of-sample): the in-sample best trial (highest Sharpe; the first index on a
    tie) is ranked among all trials out of sample (average rank on ties, 1 = worst),
    ``omega = rank / (N + 1)``
    and ``logit = ln(omega / (1 - omega))``. ``PBO`` (``PboResult.pbo``, the gating value, D-093) is the share
    of combinations with ``logit <= 0`` (the in-sample winner is at or below the out-of-sample median).
    ``PboResult.pbo_strict`` is the share with ``logit < 0``, reported next to it for information. They differ
    only on combinations whose winner sits exactly at the median rank (``omega = 1/2``).
    """
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks must be an even number >= 2")
    arr = np.asarray(matrix, dtype=np.float64)
    if arr.ndim != 2 or not np.all(np.isfinite(arr)):
        raise ValueError("matrix must be a finite 2-D array of shape (T, N)")
    t, n = arr.shape
    if n < 2:
        raise ValueError("PBO needs at least 2 trials")
    block_len = t // n_blocks
    if block_len < 2:
        raise ValueError(f"{t} rows are too few for {n_blocks} blocks")
    dropped = t - block_len * n_blocks
    blocks = arr[dropped:].reshape(n_blocks, block_len, n)
    block_mean = blocks.mean(axis=1)  # (S, N)
    block_m2 = ((blocks - block_mean[:, None, :]) ** 2).sum(axis=1)
    half = n_blocks // 2
    in_sample = np.array(list(itertools.combinations(range(n_blocks), half)), dtype=np.intp)
    mask = np.ones((in_sample.shape[0], n_blocks), dtype=bool)
    np.put_along_axis(mask, in_sample, False, axis=1)
    out_sample = np.nonzero(mask)[1].reshape(in_sample.shape[0], half).astype(np.intp)
    sr_in = _combo_sharpes(block_mean, block_m2, block_len, in_sample)
    sr_out = _combo_sharpes(block_mean, block_m2, block_len, out_sample)
    best = np.argmax(sr_in, axis=1)  # first index on a tie
    sr_best = sr_out[np.arange(sr_out.shape[0]), best][:, None]
    below = (sr_out < sr_best).sum(axis=1)
    equal = (sr_out == sr_best).sum(axis=1)
    rank = below + (equal + 1) / 2.0  # average 1-based rank of the tied group
    omega = rank / (n + 1.0)
    logits = [float(x) for x in np.log(omega / (1.0 - omega))]
    pbo = sum(1 for x in logits if x <= 0.0) / len(logits)
    strict = sum(1 for x in logits if x < 0.0) / len(logits)
    return PboResult(pbo, len(logits), n_blocks, block_len, dropped, n, tuple(logits), strict)
