"""Phase 3 C2-C5 indicators: hand-computed values, warm-up (start independence) and truncation (causality)."""

from __future__ import annotations

import math
import random
from collections.abc import Callable

import numpy as np
import pytest
from numpy.typing import NDArray

from tbot.indicators import (
    ewma_finite,
    log_momentum_sign,
    log_return_stdev,
    rsi_cutler,
    trailing_count_at_or_below,
)

NAN = math.nan


def assert_series(actual: NDArray[np.float64], expected: list[float], atol: float = 1e-12) -> None:
    np.testing.assert_allclose(actual, np.array(expected), rtol=0, atol=atol, equal_nan=True)


# ---- hand-computed values ---------------------------------------------------------------------------


def test_ewma_finite_hand_values() -> None:
    # span 3 -> alpha 0.5, decay 0.5; K = 3 weights 1, 0.5, 0.25 (sum 1.75): (4 + 0.5 x 2 + 0.25 x 1) /
    # 1.75 = 3
    assert_series(ewma_finite([1.0, 2.0, 4.0], span=3, k=3), [NAN, NAN, 3.0])
    # the value at t uses only the last K points: dropping the first point of a 4-point series changes nothing
    expected_t2 = (2.0 + 0.5 * 1.0 + 0.25 * 9.0) / 1.75
    assert_series(ewma_finite([9.0, 1.0, 2.0, 4.0], span=3, k=3), [NAN, NAN, expected_t2, 3.0])


def test_ewma_finite_k_equals_one_is_the_input_and_a_constant_stays_constant() -> None:
    assert_series(ewma_finite([5.0, 7.0, 9.0], span=4, k=1), [5.0, 7.0, 9.0])
    out = ewma_finite([100.0] * 40, span=8, k=24)
    assert np.isnan(out[:23]).all()
    np.testing.assert_allclose(out[23:], 100.0, rtol=0, atol=1e-12)


def test_ewma_finite_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="span"):
        ewma_finite([1.0], span=0, k=1)
    with pytest.raises(ValueError, match="k >= 1"):
        ewma_finite([1.0], span=2, k=0)


def test_log_momentum_sign_hand_values() -> None:
    close = [100.0, 110.0, 100.0, 90.0]
    assert_series(log_momentum_sign(close, 1), [NAN, 1.0, -1.0, -1.0])
    assert_series(log_momentum_sign(close, 2), [NAN, NAN, 0.0, -1.0])  # 100 -> 100 is sign 0
    assert_series(log_momentum_sign(close, 3), [NAN, NAN, NAN, -1.0])
    with pytest.raises(ValueError, match="h must be"):
        log_momentum_sign(close, 0)


def test_rsi_cutler_hand_values() -> None:
    # changes +1, -0.5 -> mean U 0.5, mean D 0.25 (denominator 2 whatever the signs) -> RS 2 -> 100 - 100 / 3
    assert_series(rsi_cutler([10.0, 11.0, 10.5]), [NAN, NAN, 100.0 - 100.0 / 3.0])
    assert_series(rsi_cutler([10.0, 11.0, 12.0]), [NAN, NAN, 100.0])  # no down move
    assert_series(rsi_cutler([10.0, 10.0, 10.0]), [NAN, NAN, 100.0])  # no move at all: still "no down move"
    assert_series(rsi_cutler([10.0, 9.0, 8.0]), [NAN, NAN, 0.0])  # no up move: RS 0
    # window slides: changes (+1, -0.5) then (-0.5, +2) -> U 1.0, D 0.25 -> RS 4 -> 80
    assert_series(rsi_cutler([10.0, 11.0, 10.5, 12.5]), [NAN, NAN, 100.0 - 100.0 / 3.0, 80.0])
    with pytest.raises(ValueError, match="n must be"):
        rsi_cutler([1.0, 2.0], n=0)


def test_trailing_count_hand_values() -> None:
    assert_series(trailing_count_at_or_below([3.0, 1.0, 2.0], 3), [NAN, NAN, 2.0])  # 1 and 2 are <= 2
    assert_series(
        trailing_count_at_or_below([5.0, 5.0, 5.0], 3), [NAN, NAN, 3.0]
    )  # ties count, and so does self
    assert_series(trailing_count_at_or_below([1.0, 2.0, 3.0, 0.5], 3), [NAN, NAN, 3.0, 1.0])
    assert_series(trailing_count_at_or_below([NAN, 1.0, 2.0, 3.0], 3), [NAN, NAN, NAN, 3.0])  # NaN in window
    with pytest.raises(ValueError, match="n must be"):
        trailing_count_at_or_below([1.0], 0)


def test_log_return_stdev_hand_values() -> None:
    e = math.e
    # returns 1 and 2 -> mean 1.5, sample variance 0.5
    assert_series(log_return_stdev([1.0, e, e**3], 2), [NAN, NAN, math.sqrt(0.5)], atol=1e-12)
    assert log_return_stdev([100.0] * 5, 3)[3] == 0.0  # exactly zero for zero returns
    assert np.isnan(log_return_stdev([1.0, 2.0, 3.0, 4.0], 3)[:3]).all()
    with pytest.raises(ValueError, match="n must be >= 2"):
        log_return_stdev([1.0, 2.0], 1)


# ---- warm-up: start independence ----------------------------------------------------------------------

Indicator = Callable[[NDArray[np.float64]], NDArray[np.float64]]


def _walk(n: int, seed: int) -> NDArray[np.float64]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0.0, 0.03))
        out.append(price)
    return np.array(out)


CASES: list[tuple[str, Indicator, int]] = [
    ("ewma_finite(8, 24)", lambda x: ewma_finite(x, 8, 24), 23),
    ("ewma_finite(64, 192)", lambda x: ewma_finite(x, 64, 192), 191),
    ("log_momentum_sign(30)", lambda x: log_momentum_sign(x, 30), 30),
    ("rsi_cutler(2)", lambda x: rsi_cutler(x, 2), 2),
    ("log_return_stdev(36)", lambda x: log_return_stdev(x, 36), 36),
    ("trailing_count(60)", lambda x: trailing_count_at_or_below(log_return_stdev(x, 20), 60), 79),
]


@pytest.mark.parametrize(("name", "fn", "first_valid"), CASES, ids=[c[0] for c in CASES])
def test_values_are_start_independent_once_the_window_is_full(
    name: str, fn: Indicator, first_valid: int
) -> None:
    x = _walk(600, seed=4)
    full = fn(x)
    assert np.isnan(full[:first_valid]).all() and not np.isnan(full[first_valid:]).any(), name
    for start in (1, 7, 50, 101):
        shifted = fn(x[start:])
        np.testing.assert_allclose(shifted[first_valid:], full[start + first_valid :], rtol=0, atol=1e-9)


@pytest.mark.parametrize(("name", "fn", "first_valid"), CASES, ids=[c[0] for c in CASES])
def test_values_do_not_change_when_later_data_is_removed(name: str, fn: Indicator, first_valid: int) -> None:
    x = _walk(500, seed=8)
    full = fn(x)
    for n in (first_valid + 1, 250, 499):
        np.testing.assert_array_equal(fn(x[:n]), full[:n], err_msg=name)
