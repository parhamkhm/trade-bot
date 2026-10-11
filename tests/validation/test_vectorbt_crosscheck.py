"""G2 item 3: buy-and-hold and SMA100 against an independent implementation (CLAUDE.md section 8).

Three layers, from strictest to loosest. Every gap between the engine and vectorbt is explained by a
mechanism and bounded by a number measured on real data, not by a widened tolerance.

Layer A (always runs, no vectorbt): the engine vs a plain Decimal reference written from the SPEC without
    importing ``tbot.backtest`` / ``tbot.execution``. Fills, fees and the whole equity curve must be
    EXACTLY equal.
Layer B (needs vectorbt): the order list of the reference is fed to vectorbt, which then does the cash /
    position / fee / slippage accounting in float64. This isolates the portfolio accounting from the sizing
    rule. Measured on real BTCUSDT 1d: final equity within 5.5e-6 (relative), curve within 1.5e-5.
Layer C (needs vectorbt): the textbook "naive" vectorbt run (all-in ``targetpercent`` at the next open, no
    buffer, no step rounding). The engine is deliberately more conservative: it sizes the BUY at the signal
    close and holds back ``buy_buffer_bps`` (10 bps) of cash so the order can still be afforded at the next
    open. That holdback alone explains the whole difference (see ``test_naive_gap_is_the_buffer_holdback``).
    For SMA100 the raw gap is 0.144 %, i.e. ABOVE the literal 0.1 % bar of SPEC section 7; it is the
    buffer, not an accounting error, and the report states it as such.

Data: Binance BTCUSDT 1d, 2018-01-02 .. 2025-09-30 (before the sealed holdout), local Parquet store; a seeded
synthetic walk is the fallback so the tests never silently vanish. vectorbt is NOT a project dependency: run
``uv run --with vectorbt pytest tests/validation/test_vectorbt_crosscheck.py`` to include layers B and C.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from tbot.backtest.result import BacktestResult
from tbot.core.types import Bar, Strategy, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter
from tests.validation.data import find_parquet_root, real_bars, synthetic_bars
from tests.validation.harness import make_cfg, run
from tests.validation.reference import (
    RefParams,
    RefResult,
    pandas_signals,
    reference_orders,
    vectorbt_naive_all_in,
    vectorbt_run,
)

FEE, SLIP, CASH = 0.001, 0.0005, 10_000.0
G2_FINAL_EQUITY_TOL = 1e-3  # SPEC section 7: "<= 0.1 % final equity"

needs_store = pytest.mark.skipif(find_parquet_root() is None, reason="local Binance Parquet store not found")
SOURCES = [pytest.param("real", marks=needs_store), pytest.param("synthetic")]
STRATEGIES = ["buy_and_hold", "sma100"]

_CACHE: dict[str, list[Bar]] = {}


def _bars(source: str) -> list[Bar]:
    if source not in _CACHE:
        if source == "real":
            _CACHE[source] = real_bars(
                "BTCUSDT", Timeframe.D1, datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
            )
        else:
            _CACHE[source] = synthetic_bars(
                1500, seed=11, gap_vol=0.0002
            )  # gaps below the 10 bps buffer: valid run
    return _CACHE[source]


def _strategy(name: str) -> Strategy:
    return BuyAndHold() if name == "buy_and_hold" else SmaFilter(100)


def _kind(name: str) -> str:
    return "buy_and_hold" if name == "buy_and_hold" else "sma"


@pytest.fixture(scope="module")
def vbt_available() -> None:
    """Skip (not fail) when vectorbt is absent or cannot be imported in this environment."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import vectorbt  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
        except Exception as exc:
            pytest.skip(f"vectorbt unavailable: {exc!r}")


def _engine_equity(res: BacktestResult) -> np.ndarray:
    return np.array([float(p.equity) for p in res.equity])


def _ref(bars: list[Bar], name: str, params: RefParams | None = None) -> RefResult:
    return reference_orders(bars, pandas_signals(bars, _kind(name), 100), params or RefParams())


# ---------------------------------------------------------------------------------------------
# Layer A: engine vs Decimal reference, exact
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_engine_equals_the_decimal_reference_exactly(source: str, name: str) -> None:
    bars = _bars(source)
    res = run(_strategy(name), bars, make_cfg())
    ref = _ref(bars, name)
    engine_fills = [(f.fill.side.value, f.fill.price, f.fill.quantity, f.fill.fee) for f in res.fills]
    ref_fills = [(f.side, f.price, f.quantity, f.fee) for f in ref.fills]
    assert engine_fills == ref_fills
    assert [p.equity for p in res.equity] == ref.equity
    assert [p.cash for p in res.equity] == ref.cash
    assert [p.quantity for p in res.equity] == ref.quantity
    assert len(res.rejections) == ref.rejected_buys == 0, "a valid run needs no fill-time rejection (D-078)"
    assert res.fees_paid == sum((f.fee for f in ref.fills), Decimal(0))


@pytest.mark.parametrize("source", SOURCES)
def test_buy_and_hold_is_one_buy_and_never_sells(source: str) -> None:
    res = run(BuyAndHold(), _bars(source), make_cfg())
    assert [f.fill.side.value for f in res.fills] == ["BUY"]
    assert res.fills[0].fill.ts == _bars(source)[0].ts  # open of bar 1 == close of bar 0


# ---------------------------------------------------------------------------------------------
# Layer B: same orders, vectorbt does the accounting
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_engine_matches_vectorbt_accounting_for_identical_orders(
    vbt_available: None, source: str, name: str
) -> None:
    bars = _bars(source)
    res = run(_strategy(name), bars, make_cfg())
    ref = _ref(bars, name)
    pf = vectorbt_run(bars, ref, fee=FEE, slippage=SLIP, init_cash=CASH)
    vbt_equity = pf.value().to_numpy()
    eng = _engine_equity(res)

    assert pf.orders.count() == len(res.fills), "trade (order) count"
    rel_final = abs(vbt_equity[-1] / eng[-1] - 1.0)
    rel_fees = abs(float(pf.orders.fees.sum()) / float(res.fees_paid) - 1.0)
    # Why not exactly 0: vectorbt works in float64 and applies slippage as price*(1+s) WITHOUT the 0.01 tick
    # rounding the engine applies (BUY up, SELL down, always against us). With the quantities fixed, each
    # fill moves cash by at most qty * tick * (1 + fee), so the gap at bar t is bounded by the running sum of
    # that quantity over the fills so far. The bound is computed from the order list, not tuned.
    tick = float(RefParams().tick)
    bound = np.zeros(len(bars))
    for f in ref.fills:
        bound[f.row :] += float(f.quantity) * tick * (1.0 + FEE)
    abs_gap = np.abs(vbt_equity - eng)
    assert np.all(abs_gap <= bound + 1e-6), f"max excess over the tick bound: {np.max(abs_gap - bound):.3e}"
    assert rel_fees < 1e-6, f"total fees differ by {rel_fees:.2e}"
    # Measured on real BTCUSDT 1d: final 7.8e-8 (B&H) / 5.5e-6 (SMA100); both far inside the SPEC bar.
    assert rel_final < G2_FINAL_EQUITY_TOL / 10, f"final equity differs by {rel_final:.2e}"


@pytest.mark.parametrize("source", SOURCES)
def test_tick_rounding_is_the_whole_layer_b_gap(vbt_available: None, source: str) -> None:
    """Remove the engine's tick rounding from the picture and vectorbt agrees to float precision.

    The reference is rerun with a tick of 1e-12 (no practical rounding) and compared with vectorbt on the same
    order list: if the gap above really is tick rounding it must collapse to float noise.
    """
    bars = _bars(source)
    fine = _ref(bars, "sma100", replace(RefParams(), tick=Decimal("1e-12")))
    pf = vectorbt_run(bars, fine, fee=FEE, slippage=SLIP, init_cash=CASH)
    gap = abs(float(pf.value().iloc[-1]) / float(fine.equity[-1]) - 1.0)
    assert gap < 1e-9, f"residual after removing tick rounding: {gap:.2e}"


# ---------------------------------------------------------------------------------------------
# Layer C: textbook all-in vectorbt vs the engine
# ---------------------------------------------------------------------------------------------


def _predicted_buffer_shortfall(res: BacktestResult, final_close: Decimal) -> float:
    """Closed-form prediction of (engine / naive - 1) from the engine's own books.

    At each entry the engine leaves a fraction h = 1 - (notional + fee) / cash_before of its cash uninvested.
    Held through a round trip with gross price ratio g = exit / entry, that idle slice earns 0 instead of
    g - 1 on the invested slice, so the round trip ends 1 - h (1 - 1/g) times the all-in one. Round trips
    compound; an open position at the end is marked at the last close.
    """
    by_ts = {p.ts: p for p in res.equity}
    factor, entry, hold = 1.0, None, 0.0
    for rec in res.fills:
        f = rec.fill
        if f.side.value == "BUY":
            cash_before = float(by_ts[f.ts].cash)  # equity point stamped at the previous close, pre-fill
            hold = 1.0 - float(f.price * f.quantity + f.fee) / cash_before
            entry = float(f.price)
        else:
            assert entry is not None
            factor *= 1.0 - hold * (1.0 - entry / float(f.price))
            entry = None
    if entry is not None:
        factor *= 1.0 - hold * (1.0 - entry / float(final_close))
    return factor - 1.0


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_naive_gap_is_the_buffer_holdback(vbt_available: None, source: str, name: str) -> None:
    bars = _bars(source)
    res = run(_strategy(name), bars, make_cfg())
    naive = vectorbt_naive_all_in(
        bars, pandas_signals(bars, _kind(name), 100), fee=FEE, slippage=SLIP, init_cash=CASH
    )
    measured = _engine_equity(res)[-1] / float(naive.value().iloc[-1]) - 1.0
    predicted = _predicted_buffer_shortfall(res, bars[-1].close)

    # The sign of the gap depends on the market path (idle cash loses in a rising market, gains in a falling
    # one), so only the size of the prediction error is asserted. Real BTCUSDT 1d: B&H measured -0.0750 % vs
    # predicted -0.0750 % (residual -5.7e-7); SMA100 -0.1444 % vs -0.1515 % (residual 7.1e-5). Synthetic walk:
    # residuals 4.7e-6 and 5.8e-5. The residual is a second-order term (step flooring on ~100 entries and
    # compounding of the holdback), the same size as the ablation in the next test.
    assert abs(measured - predicted) < 1.5e-4, f"measured {measured:.3e} vs predicted {predicted:.3e}"
    assert abs(predicted) > 1e-4 or name == "buy_and_hold", (
        "the buffer effect must be visible, not zero by accident"
    )


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_without_the_buffer_effect_engine_logic_matches_the_naive_run(
    vbt_available: None, source: str, name: str
) -> None:
    """Ablation: size the reference as the naive run does (no buffer, fill price known) -> gap collapses.

    This is the literal G2 comparison. The oracle sizing is NOT causal and is never a trading rule; it only
    shows that the naive vectorbt baseline and the engine's order / fee / slippage accounting agree once the
    one deliberate difference (sizing at the signal close with a safety buffer) is removed. Measured on
    real data: B&H 4.7e-6, SMA100 2.9e-5 (synthetic 5.1e-5). The residual is step flooring and tick
    rounding, which the second check below removes entirely.
    """
    bars = _bars(source)
    signals = pandas_signals(bars, _kind(name), 100)
    naive = vectorbt_naive_all_in(bars, signals, fee=FEE, slippage=SLIP, init_cash=CASH)
    oracle = _ref(bars, name, replace(RefParams(), sizing="oracle_open", buffer_bps=Decimal(0)))
    accounted = vectorbt_run(bars, oracle, fee=FEE, slippage=SLIP, init_cash=CASH)
    assert accounted.orders.count() == naive.orders.count()
    gap = abs(float(accounted.value().iloc[-1]) / float(naive.value().iloc[-1]) - 1.0)
    assert gap < G2_FINAL_EQUITY_TOL / 10, f"gap {gap:.2e}"

    # with no step flooring and no tick rounding the two are the same computation up to float noise
    exact = _ref(
        bars,
        name,
        replace(
            RefParams(),
            sizing="oracle_open",
            buffer_bps=Decimal(0),
            tick=Decimal("1e-12"),
            step=Decimal("1e-12"),
        ),
    )
    pf = vectorbt_run(bars, exact, fee=FEE, slippage=SLIP, init_cash=CASH)
    gap_exact = abs(float(pf.value().iloc[-1]) / float(naive.value().iloc[-1]) - 1.0)
    assert gap_exact < 1e-7, f"residual after removing rounding: {gap_exact:.2e}"


def test_naive_vectorbt_gap_is_reported_not_hidden(vbt_available: None) -> None:
    """The raw SMA100 gap on real data exceeds the literal 0.1 %: pin it so a change is noticed."""
    if find_parquet_root() is None:
        pytest.skip("real-data pin needs the local Parquet store")
    bars = _bars("real")
    res = run(SmaFilter(100), bars, make_cfg())
    naive = vectorbt_naive_all_in(
        bars, pandas_signals(bars, "sma", 100), fee=FEE, slippage=SLIP, init_cash=CASH
    )
    gap = _engine_equity(res)[-1] / float(naive.value().iloc[-1]) - 1.0
    assert -0.0016 < gap < -0.0013, f"SMA100 raw gap vs naive vectorbt = {gap:.5%}"


# ---------------------------------------------------------------------------------------------
# D-078: the buffer is not optional, an unaffordable next-open BUY invalidates the run
# ---------------------------------------------------------------------------------------------


@needs_store
@pytest.mark.parametrize("name", STRATEGIES)
def test_zero_buffer_on_real_data_produces_insufficient_funds_rejections(name: str) -> None:
    """Without the 10 bps buffer some entries (open gaps above the holdback) cannot be paid at next open."""
    bars = _bars("real")
    res = run(_strategy(name), bars, make_cfg(buffer_bps=Decimal(0)))
    assert len(res.rejections) > 0
    assert all("INSUFFICIENT" in r.reason.upper() for r in res.rejections)
    ref = _ref(bars, name, replace(RefParams(), buffer_bps=Decimal(0)))
    assert len(res.rejections) == ref.rejected_buys  # the reference predicts exactly the same rejections


def test_vectorbt_helpers_are_typed_loosely_but_return_a_portfolio(vbt_available: None) -> None:
    bars = synthetic_bars(300, seed=3)
    pf: Any = vectorbt_run(bars, _ref(bars, "sma100"), fee=FEE, slippage=SLIP, init_cash=CASH)
    assert len(pf.value()) == len(bars)
