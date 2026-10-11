"""Independent reference simulators for the G2 cross-check.

``reference_orders`` re-implements, from the SPEC / module documentation and without importing anything from
``tbot.backtest`` or ``tbot.execution``, what a next-open, long-only, all-or-nothing backtest does:

    decision at close(t)  ->  order sized at close(t)  ->  fill at open(t+1) with adverse slippage,
    fee charged on the slipped notional, price rounded adversely to the tick, quantity floored to the step.
    A BUY that cash no longer affords at the real fill price is capped to
    ``floor_to_step(cash / (fill_price * (1 + fee)))`` (SPEC D-051 as revised 2026-10-11) and logged as a
    resize; it is rejected only if the capped order falls below the minimum notional.

It is a plain Decimal loop, so the engine can be compared with it EXACTLY, and its order list can be fed to
vectorbt as the independent portfolio accounting (``vectorbt_run``) for a float-precision comparison.
``pandas_signals`` builds the 0/1 decision series for the two baselines with pandas only.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from tbot.core.types import Bar

BPS = Decimal(10_000)

__all__ = [
    "RefFill",
    "RefParams",
    "RefResize",
    "RefResult",
    "pandas_signals",
    "reference_orders",
    "vectorbt_naive_all_in",
    "vectorbt_run",
]


@dataclass(frozen=True)
class RefParams:
    fee_bps: Decimal = Decimal(10)
    slip_bps: Decimal = Decimal(5)
    # Default 0 = the engine's default (the sizing divisor already carries slippage and fee, and the BUY is
    # capped at fill time). The literal pre-2026-10-11 configuration passes Decimal(10) explicitly.
    buffer_bps: Decimal = Decimal(0)
    tick: Decimal = Decimal("0.01")
    step: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal(1)
    # SPEC 7.1 S0 relative rebalance band (engine default 0.25): while invested, a resize trades only if
    # |target - now| >= band * max(target, now). Entries from flat and exits to zero always trade. This
    # reference is STOPLESS by construction (no protective stop), so it is compared with engine runs made
    # with enforce_stops=False.
    rebalance_band: Decimal = Decimal("0.25")
    initial_cash: Decimal = Decimal(10_000)
    # "close": the engine's rule, sized at close(t) (plus the optional buffer; the BUY is then capped at fill
    # time). "oracle_open": ablation only, sized with
    # knowledge of the fill price at open(t+1) and no buffer, i.e. spend (almost) all cash like a naive
    # all-in vectorbt run does; it is not causal and is used to attribute the buffer effect, never as a rule.
    sizing: str = "close"


@dataclass(frozen=True)
class RefFill:
    row: int  # index of the bar whose OPEN the order fills at
    side: str  # "BUY" | "SELL"
    price: Decimal
    quantity: Decimal
    fee: Decimal


@dataclass(frozen=True)
class RefResize:
    row: int  # index of the bar whose OPEN the capped BUY fills at
    requested: Decimal
    filled: Decimal


@dataclass(frozen=True)
class RefResult:
    fills: list[RefFill]
    equity: list[Decimal]  # marked at each bar close
    cash: list[Decimal]
    quantity: list[Decimal]
    rejected_buys: int
    resizes: list[RefResize]


def pandas_signals(bars: Sequence[Bar], kind: str, n: int = 100) -> pd.Series:
    """0/1 target weight decided at each bar close (NaN while warming up). Uses only pandas rolling means."""
    idx = pd.DatetimeIndex([b.ts for b in bars])
    close = pd.Series([float(b.close) for b in bars], index=idx)
    if kind == "buy_and_hold":
        return pd.Series(1.0, index=idx)
    sma = close.rolling(n).mean()
    out = (close > sma).astype(float)
    out[sma.isna()] = np.nan
    return out


def _floor(value: Decimal, unit: Decimal) -> Decimal:
    return (value / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def _ceil(value: Decimal, unit: Decimal) -> Decimal:
    return (value / unit).to_integral_value(rounding=ROUND_CEILING) * unit


def reference_orders(bars: Sequence[Bar], weights: pd.Series, params: RefParams | None = None) -> RefResult:
    """Walk the bars once; ``weights.iloc[t]`` is the target decided at close(t) (NaN = still warming up)."""
    p = params or RefParams()
    slip, fee = p.slip_bps / BPS, p.fee_bps / BPS
    cost_factor = (1 + slip) * (1 + fee) * (1 + p.buffer_bps / BPS)
    cash, qty = p.initial_cash, Decimal(0)
    pending: tuple[str, Decimal] | None = None
    fills: list[RefFill] = []
    equity: list[Decimal] = []
    cash_path: list[Decimal] = []
    qty_path: list[Decimal] = []
    rejected = 0
    resizes: list[RefResize] = []
    for t, bar in enumerate(bars):
        if pending is not None:
            side, q = pending
            pending = None
            if side == "BUY":
                price = _ceil(bar.open * (1 + slip), p.tick)
                requested = q
                if price * q * (1 + fee) > cash:  # price moved against the close-sized order: cap it
                    q = _floor(cash / (price * (1 + fee)), p.step)
                if q > 0 and price * q >= p.min_notional:
                    if q < requested:
                        resizes.append(RefResize(t, requested, q))
                    notional = price * q
                    fee_amt = notional * fee
                    cash -= notional + fee_amt
                    qty += q
                    fills.append(RefFill(t, side, price, q, fee_amt))
                else:
                    rejected += 1
            else:
                price = _floor(bar.open * (1 - slip), p.tick)
                notional = price * q
                fee_amt = notional * fee
                cash += notional - fee_amt
                qty -= q
                fills.append(RefFill(t, side, price, q, fee_amt))
        eq = cash + qty * bar.close
        equity.append(eq)
        cash_path.append(cash)
        qty_path.append(qty)
        w = weights.iloc[t]
        if np.isnan(w):
            continue
        target = Decimal(str(float(w)))
        held = qty * bar.close
        weight_now = held / eq
        if qty > 0 and target > 0 and abs(target - weight_now) < p.rebalance_band * max(target, weight_now):
            continue
        gap = target * eq - held
        if gap > 0:
            if p.sizing == "oracle_open" and t + 1 < len(bars):
                fill_px = _ceil(bars[t + 1].open * (1 + slip), p.tick)
                q = _floor(min(gap / bar.close, cash / (fill_px * (1 + fee))), p.step)
            else:
                q = _floor(min(gap, cash / cost_factor) / bar.close, p.step)
            if q > 0 and bar.close * q >= p.min_notional:
                pending = ("BUY", q)
        elif gap < 0:
            q = qty if target == 0 else _floor(min(-gap / bar.close, qty), p.step)
            if q > 0 and bar.close * q >= p.min_notional:
                pending = ("SELL", q)
    return RefResult(fills, equity, cash_path, qty_path, rejected, resizes)


def _series(bars: Sequence[Bar], field: str) -> pd.Series:
    idx = pd.DatetimeIndex([b.ts for b in bars])
    return pd.Series([float(getattr(b, field)) for b in bars], index=idx)


def vectorbt_run(
    bars: Sequence[Bar],
    ref: RefResult,
    *,
    fee: float,
    slippage: float,
    init_cash: float,
) -> Any:
    """Feed the reference order list to vectorbt (independent accounting): fills at the OPEN of each row.

    vectorbt does the cash, position, fee and slippage arithmetic; only the order amounts come from the
    reference. vectorbt is not a project dependency, so it is imported lazily and its (numba / pandas)
    import-time warnings are silenced: the repo runs pytest with ``filterwarnings = error``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import vectorbt as vbt  # type: ignore[import-not-found,unused-ignore]

        size = pd.Series(np.nan, index=pd.DatetimeIndex([b.ts for b in bars]))
        for f in ref.fills:
            size.iloc[f.row] = float(f.quantity) if f.side == "BUY" else -float(f.quantity)
        return vbt.Portfolio.from_orders(
            close=_series(bars, "close"),
            price=_series(bars, "open"),
            size=size,
            size_type="amount",
            fees=fee,
            slippage=slippage,
            init_cash=init_cash,
            direction="longonly",
            freq="1D",
        )


def vectorbt_naive_all_in(
    bars: Sequence[Bar],
    weights: pd.Series,
    *,
    fee: float,
    slippage: float,
    init_cash: float,
) -> Any:
    """The textbook vectorbt baseline: ``targetpercent`` orders at the next OPEN, no buffer, no step rounding.

    ``weights.iloc[t]`` is the 0/1 decision at close(t) (NaN = warming up, treated as flat). The order for
    row t+1 is placed only when the decision changes, so the position is not rebalanced in between.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import vectorbt as vbt  # type: ignore[import-not-found,unused-ignore]

        target = weights.fillna(0.0).shift(1)  # decided at close(t-1), executed at open(t)
        previous = target.shift(1).fillna(0.0)
        size = pd.Series(np.nan, index=weights.index)
        changed = (target != previous) & target.notna()
        size[changed] = target[changed]
        return vbt.Portfolio.from_orders(
            close=_series(bars, "close"),
            price=_series(bars, "open"),
            size=size,
            size_type="targetpercent",
            fees=fee,
            slippage=slippage,
            init_cash=init_cash,
            direction="longonly",
            freq="1D",
        )
