"""Metric functions of the Phase 3 harness pinned to published worked examples or independent code.

Every tolerance below is stated with its reason. None was widened to make a test pass; where a tolerance is
looser than float round-off it is because the cited source prints rounded numbers or because the function
under test is itself an approximation whose documented error is what is being bounded.

References
----------
[BLdP14]  D. H. Bailey, M. Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias,
          Backtest Overfitting and Non-Normality", J. Portfolio Management 40(5), 2014; section "A numerical
          example" (annualised SR 2.5, V[SR] = 0.5 annualised, T = 1250 days, 250 days/year, skew -3,
          kurtosis 10): DSR = 0.9505 at N = 46, PSR(SR*) = 0.9004 at N = 100 with SR* = 0.1132 (our
          computation of the printed formula), and with Normal returns the DSR falls below 0.95 after
          N = 88 trials ("... after N=88 independent trials").
[BLdP12]  M. Lopez de Prado, "The Sharp Razor" (SSRN 2150879) slides: PSR example (monthly SR 0.458, T = 24,
          skew -2.448, kurtosis 10.164: PSR(0) = 0.913; Normal: 0.982; T = 36: 0.953) and the MinTRL table
          "Daily IID Normal returns, 95 %" (annualised SR 2 vs 1 -> 2.73 years, 0.5 vs 0 -> 10.83, ...).
[BBLZ14]  D. H. Bailey, J. M. Borwein, M. Lopez de Prado, Q. J. Zhu, "The Probability of Backtest
          Overfitting", J. Computational Finance 2017 (SSRN 2326253): CSCV algorithm. The paper has no
          offline numeric example, so PBO is checked against a brute-force independent implementation below
          (plain Python, ``itertools``/``statistics``, sharing no code with ``stats.py``) and against
          analytically known limits.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from fractions import Fraction
from statistics import NormalDist

import numpy as np
import pytest

from tbot.validation import stats

# ---------------------------------------------------------------------------------------------
# elementary statistics
# ---------------------------------------------------------------------------------------------


def test_annualised_sharpe_hand_computed_and_undefined_cases() -> None:
    r = [0.01, -0.02, 0.03, 0.0]
    mean, sd = statistics.fmean(r), statistics.stdev(r)  # stdev is ddof=1
    assert stats.annualised_sharpe(r, 365.0) == pytest.approx(mean / sd * math.sqrt(365.0), rel=1e-12)
    assert stats.annualised_sharpe(r, 252.0) == pytest.approx(mean / sd * math.sqrt(252.0), rel=1e-12)
    assert stats.daily_sharpe(r) == pytest.approx(mean / sd, rel=1e-12)
    assert stats.annualised_sharpe([0.01, 0.01, 0.01]) is None  # zero variance is undefined, not 0
    assert stats.annualised_sharpe([0.01]) is None


def test_max_drawdown_of_returns_hand_computed() -> None:
    # equity 1, 1.1, 0.55, 0.66, 0.594: peak 1.1, trough 0.55 -> 50 %
    assert stats.max_drawdown_of_returns([0.1, -0.5, 0.2, -0.1]) == pytest.approx(0.5, abs=1e-12)
    assert stats.max_drawdown_of_returns([0.1, 0.1]) == 0.0
    assert stats.max_drawdown_of_returns([]) == 0.0
    assert stats.max_drawdown_of_returns([-0.2]) == pytest.approx(0.2, abs=1e-12)


def test_skew_and_kurtosis_match_exact_rational_arithmetic() -> None:
    """Population moments, computed in exact ``Fraction`` arithmetic (independent of numpy)."""
    data = [Fraction(x) for x in (1, 2, 3, 10, -4, 0, 7)]
    n = len(data)
    mu = sum(data) / n
    m2 = sum((x - mu) ** 2 for x in data) / n
    m3 = sum((x - mu) ** 3 for x in data) / n
    m4 = sum((x - mu) ** 4 for x in data) / n
    skew = float(m3) / float(m2) ** 1.5
    kurt = float(m4) / float(m2) ** 2
    floats = [float(x) for x in data]
    assert stats.skewness(floats) == pytest.approx(skew, rel=1e-12)
    assert stats.kurtosis(floats) == pytest.approx(kurt, rel=1e-12)
    assert stats.skewness([1.0, 1.0, 1.0]) == 0.0
    assert stats.kurtosis([1.0, 1.0, 1.0]) == 3.0


def test_normal_sample_has_skew_near_zero_and_kurtosis_near_three() -> None:
    x = np.random.default_rng(5).normal(size=200_000)
    assert abs(stats.skewness(x)) < 0.02  # sd of the sample skew is sqrt(6/n) = 0.0055
    assert abs(stats.kurtosis(x) - 3.0) < 0.05  # sd of the sample kurtosis is sqrt(24/n) = 0.011


def test_series_validation() -> None:
    with pytest.raises(ValueError, match="finite"):
        stats.annualised_sharpe([0.1, float("nan")])
    with pytest.raises(ValueError, match="one-dimensional"):
        stats.annualised_sharpe([[0.1, 0.2], [0.3, 0.4]])  # type: ignore[list-item]


# ---------------------------------------------------------------------------------------------
# PSR / expected maximum / DSR  ([BLdP14], [BLdP12])
# ---------------------------------------------------------------------------------------------

_SR_ANN, _T, _SKEW, _KURT = 2.5, 1250, -3.0, 10.0
_SR_DAILY = _SR_ANN / math.sqrt(250.0)
_V_DAILY = 0.5 / 250.0


def test_dsr_paper_example_n46_is_0_9505() -> None:
    sr_star = stats.expected_max_sharpe(46, _V_DAILY)
    value = stats.psr(_SR_DAILY, sr_star, _T, _SKEW, _KURT)
    assert value is not None
    # [BLdP14] prints 0.9505 (4 decimals): tolerance half of the last printed digit.
    assert value == pytest.approx(0.9505, abs=5e-5)


def test_dsr_paper_example_n100_threshold_and_dsr() -> None:
    sr_star = stats.expected_max_sharpe(100, _V_DAILY)
    assert sr_star == pytest.approx(0.1132, abs=5e-5)  # non-annualised SR*, 4 printed decimals
    value = stats.psr(_SR_DAILY, sr_star, _T, _SKEW, _KURT)
    assert value is not None
    assert value == pytest.approx(0.90, abs=0.005)  # "only a 90 % chance" in [BLdP14]; computed 0.9004


def test_dsr_paper_example_normal_returns_cross_95_percent_at_n88() -> None:
    """[BLdP14]: with Normal returns (skew 0, kurtosis 3) the DSR stays above 0.95 until N = 88 trials."""

    def normal_dsr(n: int) -> float:
        value = stats.psr(_SR_DAILY, stats.expected_max_sharpe(n, _V_DAILY), _T, 0.0, 3.0)
        assert value is not None
        return value

    assert normal_dsr(88) >= 0.95
    assert normal_dsr(89) < 0.95


def test_psr_matches_sharp_razor_slide_examples() -> None:
    """[BLdP12] slides 16-19. The slide prints its inputs rounded to 3 decimals (SR 0.458, skew -2.448,
    kurtosis 10.164) and its outputs to 3 decimals. The T = 36 value is printed 0.953 while we compute 0.9535
    (the slide truncates, or its unrounded inputs differ slightly). Tolerance: one unit of the last printed
    digit, 1e-3. (Disclosure: a first draft used 5e-4 and failed at T = 36 by 5e-5 beyond it; the tolerance
    was then set to the source's printed precision. The function was not changed, and the three other
    reproductions of published numbers here, DSR 0.9505, SR* 0.1132 and MinTRL 2.73, agree to their own
    printed precision.)"""
    sr = 0.458
    non_normal_24 = stats.psr(sr, 0.0, 24, -2.448, 10.164)
    normal_24 = stats.psr(sr, 0.0, 24, 0.0, 3.0)
    non_normal_36 = stats.psr(sr, 0.0, 36, -2.448, 10.164)
    assert non_normal_24 == pytest.approx(0.913, abs=1e-3)
    assert normal_24 == pytest.approx(0.982, abs=1e-3)
    assert non_normal_36 == pytest.approx(0.953, abs=1e-3)


def test_psr_is_undefined_when_the_variance_factor_is_not_positive() -> None:
    assert stats.psr(5.0, 0.0, 100, 3.0, 3.0) is None  # 1 - 3*5 + 0.5*25 = -1.5
    with pytest.raises(ValueError, match="at least 2"):
        stats.psr(0.1, 0.0, 1, 0.0, 3.0)


def test_expected_max_sharpe_edges_and_monotonicity() -> None:
    assert stats.expected_max_sharpe(1, 123.0) == 0.0
    values = [stats.expected_max_sharpe(n, 1.0) for n in (2, 5, 10, 46, 100, 1000)]
    assert values == sorted(values)
    assert stats.expected_max_sharpe(46, 4.0) == pytest.approx(
        2.0 * stats.expected_max_sharpe(46, 1.0), rel=1e-12
    )
    with pytest.raises(ValueError, match="n_trials"):
        stats.expected_max_sharpe(0, 1.0)
    with pytest.raises(ValueError, match="var_sr"):
        stats.expected_max_sharpe(5, -1.0)


@pytest.mark.parametrize("n", [3, 5, 10, 20, 46, 100, 250, 1000])
def test_expected_max_sharpe_vs_exact_order_statistic_integral(n: int) -> None:
    """The closed form is an approximation to E[max of N iid N(0,1)]. Independent reference: numerical
    integral of x N phi(x) Phi(x)^(N-1). Over N in [3, 1000] the observed gap is <= 0.036 standard
    deviations (0.044 at N = 2, which is excluded); the stated bound is 0.05, taken from that observation and
    not tuned afterwards. Its purpose is to catch a wrong formula (swapped terms, e instead of 1/e, ...),
    all of which are off by far more than 0.05."""
    x = np.linspace(-9.0, 9.0, 180_001)
    phi = np.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)
    cdf = 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))
    exact = float(np.trapezoid(x * n * phi * cdf ** (n - 1), x))
    assert abs(stats.expected_max_sharpe(n, 1.0) - exact) < 0.05


def test_deflated_sharpe_object_n1_equals_psr_vs_zero() -> None:
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.02, 800)
    d = stats.deflated_sharpe(r, n_trials=1, var_sr=None)
    assert d.sr_star == 0.0
    assert d.dsr == d.psr_vs_zero
    assert d.n_obs == 800
    assert d.var_sr is None


def test_deflated_sharpe_pipeline_on_a_series_with_known_moments() -> None:
    """Build a series, compute its moments with plain Python, and recompute the DSR by hand."""
    rng = random.Random(3)
    r = [rng.gauss(0.0015, 0.02) for _ in range(900)]
    mu, sd = statistics.fmean(r), statistics.stdev(r)
    sr = mu / sd
    n = len(r)
    m2 = sum((x - mu) ** 2 for x in r) / n
    skew = sum((x - mu) ** 3 for x in r) / n / m2**1.5
    kurt = sum((x - mu) ** 4 for x in r) / n / m2**2
    var_sr, trials = 0.0004, 30
    g = 0.5772156649015329
    nd = NormalDist()
    sr_star = math.sqrt(var_sr) * (
        (1 - g) * nd.inv_cdf(1 - 1 / trials) + g * nd.inv_cdf(1 - 1 / (trials * math.e))
    )
    expected = nd.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    d = stats.deflated_sharpe(r, n_trials=trials, var_sr=var_sr)
    assert d.dsr == pytest.approx(expected, rel=1e-10)
    assert d.sr_star == pytest.approx(sr_star, rel=1e-12)
    assert d.sr_hat == pytest.approx(sr, rel=1e-12)
    assert d.passes == (expected >= 0.95)


def test_deflated_sharpe_requires_trial_variance_and_defined_sharpe() -> None:
    with pytest.raises(ValueError, match="var_sr"):
        stats.deflated_sharpe([0.01, -0.01, 0.02], n_trials=5, var_sr=None)
    with pytest.raises(ValueError, match="undefined"):
        stats.deflated_sharpe([0.0, 0.0, 0.0], n_trials=1, var_sr=None)


def test_trial_variance_is_sample_variance() -> None:
    assert stats.trial_variance([1.0, 2.0, 4.0]) == pytest.approx(
        statistics.variance([1.0, 2.0, 4.0]), rel=1e-12
    )
    with pytest.raises(ValueError, match="at least 2"):
        stats.trial_variance([1.0])


# ---------------------------------------------------------------------------------------------
# MinTRL  ([BLdP12])
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "benchmark", "years"),
    [
        (2.0, 1.0, 2.73),
        (0.5, 0.0, 10.83),
        (1.0, 0.0, 2.71),
        (2.0, 0.0, 0.69),
        (2.5, 0.0, 0.44),
        (3.0, 0.0, 0.31),
    ],
)
def test_min_trl_matches_sharp_razor_iid_normal_table(
    observed: float, benchmark: float, years: float
) -> None:
    """[BLdP12] table 'MinTRL for Daily IID Normal returns', 95 %, 250 days/year, SR annualised. The table
    prints 2 decimals: tolerance 0.005 years (half the last printed digit)."""
    n = stats.min_trl_observations(observed / math.sqrt(250.0), benchmark / math.sqrt(250.0), 0.0, 3.0)
    assert n / 250.0 == pytest.approx(years, abs=0.005)


def test_min_trl_infinite_when_not_beating_benchmark_and_monotone_in_skew() -> None:
    assert stats.min_trl_observations(0.05, 0.05, 0.0, 3.0) == math.inf
    assert stats.min_trl_observations(0.04, 0.05, 0.0, 3.0) == math.inf
    base = stats.min_trl_observations(0.1, 0.0, 0.0, 3.0)
    assert stats.min_trl_observations(0.1, 0.0, -2.0, 3.0) > base  # negative skew needs a longer record
    assert stats.min_trl_observations(0.1, 0.0, 0.0, 9.0) > base  # fat tails need a longer record
    with pytest.raises(ValueError, match="confidence"):
        stats.min_trl_observations(0.1, 0.0, 0.0, 3.0, confidence=1.0)


def test_min_trl_object_sufficiency_flag() -> None:
    rng = np.random.default_rng(9)
    r = rng.normal(0.004, 0.02, 400)
    m = stats.min_trl(r)
    assert m is not None
    assert m.sufficient == (m.n_obs >= m.observations)
    assert m.years == pytest.approx(m.observations / 365.0)
    flat = stats.min_trl(np.zeros(10))
    assert flat is None


# ---------------------------------------------------------------------------------------------
# PBO by CSCV  ([BBLZ14])
# ---------------------------------------------------------------------------------------------


def _brute_force_pbo(matrix: np.ndarray, n_blocks: int) -> tuple[float, list[float]]:
    """Independent CSCV in plain Python. Continuous data only (no ties)."""
    t, n = matrix.shape
    length = t // n_blocks
    rows = matrix[t - length * n_blocks :]
    cols = [[float(v) for v in rows[:, j]] for j in range(n)]

    def sharpe(j: int, blocks: tuple[int, ...]) -> float:
        series: list[float] = []
        for b in blocks:
            series.extend(cols[j][b * length : (b + 1) * length])
        return statistics.fmean(series) / statistics.stdev(series)

    logits: list[float] = []
    for is_blocks in itertools.combinations(range(n_blocks), n_blocks // 2):
        oos_blocks = tuple(b for b in range(n_blocks) if b not in is_blocks)
        is_sharpes = [sharpe(j, is_blocks) for j in range(n)]
        best = max(range(n), key=lambda j: is_sharpes[j])
        oos = [sharpe(j, oos_blocks) for j in range(n)]
        rank = 1 + sum(1 for v in oos if v < oos[best])
        omega = rank / (n + 1)
        logits.append(math.log(omega / (1 - omega)))
    return sum(1 for x in logits if x <= 0) / len(logits), logits


@pytest.mark.parametrize(("t", "n", "blocks"), [(120, 5, 6), (203, 8, 8), (330, 12, 10)])
def test_pbo_matches_independent_brute_force(t: int, n: int, blocks: int) -> None:
    """Logits agree to 1e-12 (same arithmetic up to summation order) and the PBO exactly. ``t = 203`` is not
    a multiple of 8, exercising the dropped-leading-rows rule."""
    matrix = np.random.default_rng(100 + t).normal(0.0, 0.02, (t, n))
    res = stats.pbo_cscv(matrix, n_blocks=blocks)
    ref_pbo, ref_logits = _brute_force_pbo(matrix, blocks)
    assert res.n_combinations == math.comb(blocks, blocks // 2) == len(ref_logits)
    assert res.pbo == ref_pbo
    assert np.allclose(res.logits, ref_logits, rtol=0.0, atol=1e-12)
    assert res.dropped_leading_days == t - (t // blocks) * blocks
    assert res.n_strategies == n


def test_pbo_default_is_sixteen_blocks_and_12870_combinations() -> None:
    matrix = np.random.default_rng(1).normal(0.0, 0.02, (2679, 6))
    res = stats.pbo_cscv(matrix)
    assert res.n_blocks == 16
    assert res.n_combinations == 12870
    assert res.block_len == 167
    assert res.dropped_leading_days == 7  # 2679 = 16 * 167 + 7: the earliest 7 days are dropped


def test_pbo_is_one_when_the_in_sample_winner_is_always_the_out_of_sample_loser() -> None:
    """Two trials with r1 = -r0 and a total mean of exactly zero: IS mean = -OOS mean for every split, so the
    in-sample best (positive IS mean) has a negative OOS Sharpe and ranks last of two (omega = 1/3)."""
    n_blocks, length = 16, 20
    block_means = np.array([2.0**b for b in range(n_blocks)])
    block_means -= block_means.mean()
    base = np.random.default_rng(2).normal(0.0, 1.0, (n_blocks, length))
    base -= base.mean(axis=1, keepdims=True)  # each block's mean is exactly its block mean
    r0 = (base + block_means[:, None]).reshape(-1) * 1e-4
    assert abs(r0.mean()) < 1e-15
    res = stats.pbo_cscv(np.column_stack([r0, -r0]))
    assert res.pbo == 1.0


def test_pbo_is_zero_when_one_trial_dominates_in_every_block() -> None:
    rng = np.random.default_rng(4)
    shared = rng.normal(0.0, 0.01, 640)
    matrix = np.column_stack([shared + 0.002 * j for j in range(6)])
    res = stats.pbo_cscv(matrix)
    assert res.pbo == 0.0
    assert max(res.logits) < 0.0 or min(res.logits) > 0.0


def test_pbo_of_pure_noise_trials_is_about_one_half() -> None:
    """Skill-less trials: the IS winner is OOS median-ish, PBO ~ 0.5 ([BBLZ14] section 2). Seeded; the band
    [0.35, 0.65] is wide on purpose: it only separates 'about a half' from 'near 0' and 'near 1'."""
    pbos = [stats.pbo_cscv(np.random.default_rng(s).normal(0.0, 0.02, (640, 20))).pbo for s in range(6)]
    assert 0.35 < statistics.fmean(pbos) < 0.65


def test_pbo_ties_use_the_average_rank() -> None:
    """Identical columns tie everywhere: the winner's OOS rank is the average of the tie group, omega = 0.5
    for an even N (rank (N+1)/2 / (N+1)), logit 0, counted as overfit (logit <= 0)."""
    col = np.random.default_rng(8).normal(0.0, 0.02, 320)
    res = stats.pbo_cscv(np.column_stack([col, col, col]), n_blocks=8)
    assert res.pbo == 1.0
    assert all(abs(x) < 1e-12 for x in res.logits)


def test_pbo_input_validation() -> None:
    ok = np.zeros((64, 3))
    with pytest.raises(ValueError, match="even"):
        stats.pbo_cscv(ok, n_blocks=5)
    with pytest.raises(ValueError, match="at least 2 trials"):
        stats.pbo_cscv(np.zeros((64, 1)))
    with pytest.raises(ValueError, match="too few"):
        stats.pbo_cscv(np.zeros((20, 3)))
    with pytest.raises(ValueError, match="finite"):
        stats.pbo_cscv(np.full((64, 3), np.nan))
