"""G2 item 3: buy-and-hold and SMA100 against an independent implementation (CLAUDE.md section 8).

Three layers, from strictest to loosest. Every gap between the engine and vectorbt is explained by a
mechanism and bounded by a number measured on real data, not by a widened tolerance.

Layer A (always runs, no vectorbt): the engine vs a plain Decimal reference written from the SPEC without
    importing ``tbot.backtest`` / ``tbot.execution``. Fills, fees, resizes and the whole equity curve must be
    EXACTLY equal, for the default engine config (buy buffer 0, BUY capped at fill time) and for the 10 bps
    buffer configuration of the earlier engine.
Layer B (needs vectorbt): the order list of the reference is fed to vectorbt, which then does the cash /
    position / fee / slippage accounting in float64. This isolates the portfolio accounting from the sizing
    rule. Measured on real BTCUSDT 1d: final equity within 5.5e-6 (relative), curve within 1.5e-5.
Layer C (needs vectorbt): the textbook "naive" vectorbt run (all-in ``targetpercent`` at the next open, no
    buffer, no step rounding), fee 10 bps and slippage 5 bps per side.
    * OFFICIAL G2 comparison (``test_g2_official_...``): the DEFAULT engine config. Since the engine caps a
      BUY at fill time to what cash affords at the real fill price, it spends (almost) all cash exactly as
      the naive run does, so the two have IDENTICAL sizing assumptions. Measured on real data: SMA100
      -1.28e-05, buy-and-hold -4.73e-06 in final equity, i.e. 0.0013 % / 0.0005 % against the 0.1 % bar.
    * LITERAL buffer-10 comparison (``test_literal_buffer10_...``): the engine as it was when the G2 numbers
      were first measured (``buy_buffer_bps = 10``) leaves 10 bps of cash idle at each entry; that alone
      explains -0.144 % (SMA100) / -0.075 % (B&H). Kept as documented evidence; it asserts the explanation,
      not the 0.1 % bar, because the buffer predates the run and is no longer the default.

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
from tests.validation.harness import BPS_BUFFER_LEGACY, make_cfg, run
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
            )  # small open gaps: any fill-time cap is an ordinary price_move
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


def _assert_engine_equals_reference(res: BacktestResult, ref: RefResult) -> None:
    engine_fills = [(f.fill.side.value, f.fill.price, f.fill.quantity, f.fill.fee) for f in res.fills]
    ref_fills = [(f.side, f.price, f.quantity, f.fee) for f in ref.fills]
    assert engine_fills == ref_fills
    assert [p.equity for p in res.equity] == ref.equity
    assert [p.cash for p in res.equity] == ref.cash
    assert [p.quantity for p in res.equity] == ref.quantity
    assert [(z.requested_quantity, z.capped_quantity) for z in res.resizes] == [
        (z.requested, z.filled) for z in ref.resizes
    ], "fill-time BUY caps must match the reference one for one"
    assert len(res.rejections) == ref.rejected_buys == 0, "a valid run needs no fill-time rejection"
    assert res.fees_paid == sum((f.fee for f in ref.fills), Decimal(0))


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_engine_equals_the_decimal_reference_exactly(source: str, name: str) -> None:
    """Default engine config (buffer 0, BUY capped at fill time) against the independent Decimal reference."""
    bars = _bars(source)
    _assert_engine_equals_reference(run(_strategy(name), bars, make_cfg()), _ref(bars, name))


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_engine_equals_the_decimal_reference_exactly_with_the_legacy_buffer(source: str, name: str) -> None:
    """The 10 bps buffer configuration (the engine's default until 2026-10-11) is still reproduced exactly."""
    bars = _bars(source)
    params = replace(RefParams(), buffer_bps=BPS_BUFFER_LEGACY)
    res = run(_strategy(name), bars, make_cfg(buffer_bps=BPS_BUFFER_LEGACY))
    _assert_engine_equals_reference(res, _ref(bars, name, params))
    assert not res.resizes, "a 10 bps buffer covers every open gap in the sample: nothing to cap"


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
def test_g2_official_identical_sizing_matches_naive_vectorbt(
    vbt_available: None, source: str, name: str
) -> None:
    """OFFICIAL G2 item 3: default engine config vs the naive all-in vectorbt run, final equity within 0.1 %.

    Identical sizing assumptions: both spend (almost) all cash at the next open (the engine caps its BUY to
    what cash affords at the real fill price; vectorbt's ``targetpercent`` does the same), fee 10 bps and
    slippage 5 bps per side, no extra buffer. Measured on real BTCUSDT 1d, 2018-01-02 .. 2025-09-30:
    SMA100 -1.28e-05, buy-and-hold -4.73e-06 (residual: step flooring and tick rounding, see the ablation
    below). The bar is the pre-registered 0.1 %; it is not widened.
    """
    bars = _bars(source)
    cfg = make_cfg()
    assert cfg.buy_buffer_bps == 0, "the official run uses the engine's default sizing"
    res = run(_strategy(name), bars, cfg)
    naive = vectorbt_naive_all_in(
        bars, pandas_signals(bars, _kind(name), 100), fee=FEE, slippage=SLIP, init_cash=CASH
    )
    assert naive.orders.count() == len(res.fills), "trade (order) count"
    gap = _engine_equity(res)[-1] / float(naive.value().iloc[-1]) - 1.0
    assert abs(gap) <= G2_FINAL_EQUITY_TOL, f"final equity differs by {gap:+.3e} (bar {G2_FINAL_EQUITY_TOL})"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_literal_buffer10_gap_is_the_buffer_holdback(vbt_available: None, source: str, name: str) -> None:
    """Documented clarification, NOT the G2 criterion: the 10 bps buffer engine vs naive vectorbt.

    With ``buy_buffer_bps = 10`` the engine leaves ~10 bps of cash idle at each entry. Measured on real
    BTCUSDT 1d: SMA100 -0.1444 % and buy-and-hold -0.0750 % in final equity, against the closed-form
    prediction below of -0.1515 % and -0.0750 % (residual 7.1e-5 / 5.7e-7). The 10 bps buffer predated the
    G2 run (it was the engine's default), so the SMA100 number above 0.1 % is explained by sizing, not by an
    accounting error. This test asserts the explanation (prediction vs measurement), never the 0.1 % bar.
    """
    bars = _bars(source)
    res = run(_strategy(name), bars, make_cfg(buffer_bps=BPS_BUFFER_LEGACY))
    naive = vectorbt_naive_all_in(
        bars, pandas_signals(bars, _kind(name), 100), fee=FEE, slippage=SLIP, init_cash=CASH
    )
    measured = _engine_equity(res)[-1] / float(naive.value().iloc[-1]) - 1.0
    predicted = _predicted_buffer_shortfall(res, bars[-1].close)

    # The sign of the gap depends on the market path (idle cash loses in a rising market, gains in a falling
    # one), so only the size of the prediction error is asserted. Synthetic walk: residuals 4.7e-6 and 5.8e-5.
    # The residual is a second-order term (step flooring on ~100 entries and compounding of the holdback).
    assert abs(measured - predicted) < 1.5e-4, f"measured {measured:.3e} vs predicted {predicted:.3e}"
    assert abs(predicted) > 1e-4 or name == "buy_and_hold", (
        "the buffer effect must be visible, not zero by accident"
    )
    if source == "real":  # pin the documented numbers so a change is noticed (they are not a pass/fail bar)
        low, high = {"buy_and_hold": (-0.0009, -0.0006), "sma100": (-0.0016, -0.0013)}[name]
        assert low < measured < high, f"{name} buffer-10 gap vs naive = {measured:.5%}"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", STRATEGIES)
def test_without_the_sizing_difference_engine_logic_matches_the_naive_run(
    vbt_available: None, source: str, name: str
) -> None:
    """Ablation: size the reference as the naive run does (fill price known, no buffer) -> the same orders.

    The oracle sizing is NOT causal and is never a trading rule; it shows that the naive vectorbt baseline
    and the engine's order / fee / slippage accounting agree once sizing is made identical. Measured on
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


# ---------------------------------------------------------------------------------------------
# Fill-time BUY sizing (SPEC D-051 as revised 2026-10-11): an unaffordable next-open BUY is capped
# ---------------------------------------------------------------------------------------------

# Evidence kept from the former D-078 tests. With buffer 0 the engine at 3b00b37 (BUY rejected whole with
# INSUFFICIENT_FUNDS when cash no longer covered the order at the open) rejected BUYs on exactly these fill
# days (the UTC close of the signal bar = the open of the fill bar) of real BTCUSDT 1d (laptop-measured; most
# of them missed by a few cents on a 10 000+ USDT order, they were not dramatic gaps). All are gap-up days
# (open above the previous close). The new engine must reject none of them.
OLD_ENGINE_REJECTION_DAYS = {
    "buy_and_hold": ["2018-01-02", "2018-01-03"],
    "sma100": [
        "2018-04-27",
        "2018-07-24",
        "2018-08-29",
        "2018-08-30",
        "2019-02-26",
        "2019-02-27",
        "2020-01-08",
        "2020-01-11",
        "2020-09-25",
        "2021-09-23",
        "2023-05-27",
        "2024-07-20",
    ],
}


@needs_store
@pytest.mark.parametrize("name", STRATEGIES)
def test_zero_buffer_on_real_data_has_no_fill_rejections_and_caps_instead(name: str) -> None:
    """Buffer 0, next-open: 0 fill rejections on real data, including every day the old engine rejected.

    Not asserted on purpose: the number of "anomaly" resizes on real data (g2_numbers reports 0; SPEC D-082);
    only the invariant (no rejection) and the existence of ordinary ``price_move`` caps are.
    """
    bars = _bars("real")
    res = run(_strategy(name), bars, make_cfg(buffer_bps=Decimal(0)))
    assert res.rejections == ()
    ref = _ref(bars, name)  # default RefParams: buffer 0, BUY capped at fill time
    assert ref.rejected_buys == 0
    # the independent reference caps the same BUYs by the same amounts
    assert [(z.requested_quantity, z.capped_quantity) for z in res.resizes] == [
        (z.requested, z.filled) for z in ref.resizes
    ]

    old_days = set(OLD_ENGINE_REJECTION_DAYS[name])
    # (1) the days the old engine rejected carry no rejection now: `res.rejections == ()` covers them all
    # (2) they really are gap-up days: the open of the fill bar is above the previous close
    by_close = {b.ts.date().isoformat(): i for i, b in enumerate(bars)}
    for day in old_days:
        i = by_close[day]  # bar i closes at `day` 00:00 UTC; the fill is the open of bar i + 1
        assert bars[i + 1].open > bars[i].close, f"{day} is not a gap-up day"
    # (3) the first old rejection precedes any path difference: the new run fills that BUY, capped
    first = min(old_days)
    capped = [z for z in res.resizes if z.ts.date().isoformat() == first]
    filled = [f for f in res.fills if f.fill.ts.date().isoformat() == first and f.fill.side.value == "BUY"]
    assert len(capped) == 1 and len(filled) == 1
    assert capped[0].capped_quantity == filled[0].fill.quantity < capped[0].requested_quantity
    assert capped[0].kind == "price_move"
    if name == "sma100":
        assert any(z.kind == "price_move" for z in res.resizes), "at least one ordinary price_move cap"


def test_vectorbt_helpers_are_typed_loosely_but_return_a_portfolio(vbt_available: None) -> None:
    bars = synthetic_bars(300, seed=3)
    pf: Any = vectorbt_run(bars, _ref(bars, "sma100"), fee=FEE, slippage=SLIP, init_cash=CASH)
    assert len(pf.value()) == len(bars)
