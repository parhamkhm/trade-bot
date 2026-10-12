"""PBO matrix assembly, DSR trial-count guard, S0 N = 1 and MinTRL wiring (SPEC 7.1 and 7.2 E)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tbot.validation.overfit import (
    candidate_dsr,
    candidate_min_trl,
    candidate_pbo,
    pbo_matrix,
    s0_dsr,
    trial_daily_sharpes,
)
from tbot.validation.runs import SeriesRun
from tbot.validation.stats import daily_sharpe
from tests.validation.wf_fixtures import POINTS, fake_run

T0 = datetime(2019, 1, 1, tzinfo=UTC)
N = 400
CLOSES = [T0 + timedelta(days=i + 1) for i in range(N + 1)]  # N + 1 closes -> N returns


def _noise(n: int, mean: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, 0.01) for _ in range(n)]


def _fixed(offset: int = 0) -> dict[str, SeriesRun]:
    return {
        p.key: fake_run(CLOSES, _noise(N, 0.0005 * (i + 1), 100 + i + offset)) for i, p in enumerate(POINTS)
    }


def test_pbo_matrix_is_zero_before_each_points_first_tradable() -> None:
    fixed = _fixed()
    first = {p.key: CLOSES[10 * (i + 1)] for i, p in enumerate(POINTS)}
    rows = CLOSES[1:200]
    m = pbo_matrix(POINTS, fixed, first, CLOSES, start=CLOSES[1], end=CLOSES[200])
    assert m.shape == (len(rows), 3)
    for j, p in enumerate(POINTS):
        pre = [i for i, c in enumerate(rows) if c < first[p.key]]
        assert pre and np.all(m[pre, j] == 0.0)
        assert np.any(m[len(pre) :, j] != 0.0)


def test_pbo_matrix_end_is_exclusive_and_missing_returns_raise() -> None:
    fixed = _fixed()
    first = {p.key: CLOSES[1] for p in POINTS}
    m = pbo_matrix(POINTS, fixed, first, CLOSES, start=CLOSES[5], end=CLOSES[25])
    assert m.shape[0] == 20
    extra = [*CLOSES, CLOSES[-1] + timedelta(days=1)]
    with pytest.raises(ValueError, match="no return"):
        pbo_matrix(POINTS, fixed, first, extra, start=CLOSES[5], end=extra[-1] + timedelta(days=1))


def test_candidate_pbo_uses_sixteen_blocks() -> None:
    fixed = _fixed(offset=50)
    first = {p.key: CLOSES[1] for p in POINTS}
    res = candidate_pbo(POINTS, fixed, first, CLOSES, start=CLOSES[1], end=CLOSES[-1] + timedelta(days=1))
    assert res.n_blocks == 16 and res.n_strategies == 3 and 0.0 <= res.pbo <= 1.0
    assert res.n_combinations == 12870  # C(16, 8)


def test_trial_daily_sharpes_scores_a_flat_series_zero() -> None:
    flat = fake_run(CLOSES[:50], [0.0] * 49)
    live = fake_run(CLOSES[:50], _noise(49, 0.002, 4))
    out = trial_daily_sharpes({"flat": flat, "live": live})
    assert out["flat"] == 0.0
    assert out["live"] == pytest.approx(daily_sharpe(live.returns))


def test_candidate_dsr_refuses_a_trial_count_that_differs_from_the_log() -> None:
    series = _noise(300, 0.001, 1)
    sharpes = {"a": 0.05, "b": 0.02, "c": 0.01}
    assert candidate_dsr(series, sharpes, 3) is not None
    with pytest.raises(ValueError, match="trials are logged"):
        candidate_dsr(series, sharpes, 17)


def test_candidate_dsr_is_none_for_a_flat_series_and_uses_n() -> None:
    assert candidate_dsr([0.0] * 100, {"a": 0.1, "b": 0.2}, 2) is None
    res = candidate_dsr(_noise(300, 0.001, 2), {"a": 0.05, "b": 0.02, "c": 0.01}, 3)
    assert res is not None and res.n_trials == 3 and res.sr_star > 0.0


def test_s0_dsr_is_one_trial_with_zero_benchmark() -> None:
    res = s0_dsr(_noise(300, 0.001, 3))
    assert res is not None and res.n_trials == 1 and res.sr_star == 0.0
    assert res.dsr == pytest.approx(res.psr_vs_zero)  # N = 1: DSR is the PSR against 0
    assert s0_dsr([0.0] * 50) is None


def test_more_trials_never_raise_the_dsr() -> None:
    series = _noise(400, 0.0015, 7)
    few = candidate_dsr(series, {"a": 0.05, "b": -0.03}, 2)
    six = dict(zip("abcdef", (0.05, -0.03, 0.02, -0.01, 0.04, 0.0), strict=True))
    many = candidate_dsr(series, six, 6)
    assert few is not None and many is not None
    assert few.dsr is not None and many.dsr is not None
    assert many.dsr <= few.dsr


def test_min_trl_against_zero_and_buy_and_hold() -> None:
    strat = _noise(500, 0.002, 11)
    bh = _noise(500, 0.001, 12)
    vs_zero, vs_bh = candidate_min_trl(strat, bh)
    assert vs_zero is not None and vs_bh is not None
    assert vs_zero.years < vs_bh.years  # a higher bar needs a longer record
    none_zero, none_bh = candidate_min_trl([0.0] * 30, bh)
    assert none_zero is None and none_bh is None
    _, bh_flat = candidate_min_trl(strat, [0.0] * 30)
    assert bh_flat is None
