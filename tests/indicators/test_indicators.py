"""Hand-computed values, start-independence (warm-up) and truncation tests for the shared indicators."""

from __future__ import annotations

import math
import random
from collections.abc import Callable

import numpy as np
import pytest
from numpy.typing import NDArray

from tbot.indicators import (
    atr_simple,
    donchian_lower,
    donchian_mid,
    donchian_upper,
    sigma_hat_kernel,
    sma,
    true_range,
)

NAN = math.nan


def assert_series(actual: NDArray[np.float64], expected: list[float]) -> None:
    np.testing.assert_allclose(actual, np.array(expected), rtol=0, atol=1e-12, equal_nan=True)


# ---- hand-computed values ---------------------------------------------------------------------


def test_sma_hand_computed() -> None:
    assert_series(sma([1, 2, 3, 4, 5], 3), [NAN, NAN, 2, 3, 4])
    assert_series(sma([7.0], 1), [7.0])
    assert_series(sma([1, 2], 3), [NAN, NAN])


def test_true_range_hand_computed() -> None:
    # TR1 = max(12-9, |12-9|, |9-9|) = 3 ; TR2 = max(11-9, |11-11|, |9-11|) = 2 ; index 0: no previous close
    assert_series(true_range([10, 12, 11], [8, 9, 9], [9, 11, 10]), [NAN, 3, 2])


def test_true_range_uses_a_gap_from_the_previous_close() -> None:
    # bar 1 gaps above the previous close 10: range 1 but |high - prev close| = 4 wins
    assert_series(true_range([10, 14], [10, 13], [10, 13.5]), [NAN, 4])


def test_atr_simple_is_the_simple_mean_not_wilder() -> None:
    high, low, close = [10, 12, 11, 13], [8, 9, 9, 10], [9, 11, 10, 12]
    # TR = nan, 3, 2, max(3, |13-10|, |10-10|) = 3 ; ATR2 at index 2 = (3+2)/2 ; at index 3 = (2+3)/2
    assert_series(atr_simple(high, low, close, 2), [NAN, NAN, 2.5, 2.5])
    # a Wilder recursion would give 2.5 -> (2.5 + 3)/2 = 2.75 at index 3; the simple mean must not
    assert atr_simple(high, low, close, 2)[3] != 2.75


def test_sigma_hat_kernel_hand_computed() -> None:
    # alpha 1/2, K 2: weights 1 and 1/2 (sum 3/2); r1 = r2 = ln 1.1, r3 = 0
    s = sigma_hat_kernel([100, 110, 121, 121], alpha=0.5, k=2)
    r, root = math.log(1.1), math.sqrt(365)
    assert_series(s, [NAN, NAN, r * root, r / math.sqrt(3) * root])


def test_sigma_hat_kernel_default_is_exact_for_constant_returns() -> None:
    r = 0.04
    closes = [100.0 * math.exp(r * i) for i in range(200)]
    s = sigma_hat_kernel(closes)
    assert np.isnan(s[:150]).all() and not np.isnan(s[150:]).any()  # first valid index 150 = close #151
    np.testing.assert_allclose(s[150:], r * math.sqrt(365), rtol=1e-12)


def test_sigma_hat_kernel_weights_follow_alpha_2_over_31() -> None:
    # one return at lag 3 only: sum = (1-a)^3 r^2 / sum_{k<150}(1-a)^k, a = 2/31
    a = 2 / 31
    closes = [100.0] * 160
    closes[156] = 110.0  # return r_156 = ln 1.1, r_157 = ln(100/110); both nonzero
    s = sigma_hat_kernel(closes)
    r = math.log(1.1)
    denominator = (1 - (1 - a) ** 150) / a
    expected = math.sqrt((r * r * (1 - a) ** 3 + r * r * (1 - a) ** 2) / denominator) * math.sqrt(365)
    assert s[159] == pytest.approx(expected, rel=1e-12)


def test_donchian_hand_computed() -> None:
    high, low = [1, 3, 2, 5, 4], [0, 1, 1, 2, 3]
    assert_series(donchian_upper(high, 2), [NAN, NAN, 3, 3, 5])
    assert_series(donchian_lower(low, 2), [NAN, NAN, 0, 1, 1])
    assert_series(donchian_mid(high, low, 2), [NAN, NAN, 1.5, 2, 3])


def test_donchian_excludes_the_current_bar() -> None:
    assert donchian_upper([1, 1, 1, 100], 3)[3] == 1.0  # the spike at t is not in its own channel
    assert donchian_lower([5, 5, 5, 0.1], 3)[3] == 5.0


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_windows_raise(bad: int) -> None:
    with pytest.raises(ValueError, match="n must be"):
        sma([1.0], bad)
    with pytest.raises(ValueError, match="n must be"):
        atr_simple([1.0], [1.0], [1.0], bad)
    with pytest.raises(ValueError, match="n must be"):
        donchian_upper([1.0], bad)


# ---- start-independence (warm-up) and truncation ----------------------------------------------


def _walk(n: int, seed: int = 3) -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(seed)
    close, high, low = [], [], []
    price = 100.0
    for _ in range(n):
        price *= math.exp(rng.gauss(0, 0.03))
        close.append(price)
        high.append(price * (1 + abs(rng.gauss(0, 0.01))))
        low.append(price * (1 - abs(rng.gauss(0, 0.01))))
    return high, low, close


H, LO, C = _walk(600)
Ohlc = tuple[list[float], list[float], list[float]]


def _sma100(d: Ohlc) -> NDArray[np.float64]:
    return sma(d[2], 100)


def _atr20(d: Ohlc) -> NDArray[np.float64]:
    return atr_simple(d[0], d[1], d[2], 20)


def _sigma(d: Ohlc) -> NDArray[np.float64]:
    return sigma_hat_kernel(d[2])


def _upper55(d: Ohlc) -> NDArray[np.float64]:
    return donchian_upper(d[0], 55)


def _lower55(d: Ohlc) -> NDArray[np.float64]:
    return donchian_lower(d[1], 55)


def _mid200(d: Ohlc) -> NDArray[np.float64]:
    return donchian_mid(d[0], d[1], 200)


# (name, function of (high, low, close), first index with a full window)
INDICATORS: list[tuple[str, Callable[[Ohlc], NDArray[np.float64]], int]] = [
    ("sma100", _sma100, 99),
    ("atr20", _atr20, 20),
    ("sigma_hat", _sigma, 150),
    ("donchian_upper55", _upper55, 55),
    ("donchian_lower55", _lower55, 55),
    ("donchian_mid200", _mid200, 200),
]


@pytest.mark.parametrize(("name", "fn", "window"), INDICATORS, ids=[i[0] for i in INDICATORS])
@pytest.mark.parametrize("start", [1, 37, 211])
def test_values_are_start_independent_after_their_window(
    name: str, fn: Callable[[Ohlc], NDArray[np.float64]], window: int, start: int
) -> None:
    full = fn((H, LO, C))
    late = fn((H[start:], LO[start:], C[start:]))
    assert np.isnan(late[:window]).all() and not np.isnan(late[window:]).any(), name
    # identical (<= 1e-9 required; fsum / max make it exact) from the first full window of the late start on
    diff = np.abs(full[start + window :] - late[window:])
    assert diff.max() <= 1e-9 and np.array_equal(full[start + window :], late[window:]), name


@pytest.mark.parametrize(("name", "fn", "window"), INDICATORS, ids=[i[0] for i in INDICATORS])
@pytest.mark.parametrize("cut", [160, 301, 599])
def test_values_do_not_use_later_bars(
    name: str, fn: Callable[[Ohlc], NDArray[np.float64]], window: int, cut: int
) -> None:
    full = fn((H, LO, C))
    truncated = fn((H[:cut], LO[:cut], C[:cut]))
    assert np.array_equal(full[:cut], truncated, equal_nan=True), name
