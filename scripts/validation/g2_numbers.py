"""Print every number quoted in docs/reports/g2-phase2-validation.md (laptop-measured, reproducible).

Run from the repository root::

    uv run --with vectorbt python -m scripts.validation.g2_numbers

Without ``--with vectorbt`` the vectorbt section is reported as unavailable and everything else still runs.
Data: Binance BTCUSDT 1d / 1h from the local Parquet store, bars closing before 2025-10-01 only (the loader
used here cannot return the sealed holdout), plus seeded synthetic series where a section says so.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from tests.validation import test_hand_computed as hand
from tests.validation import test_metrics_independent as metrics_check
from tests.validation import test_truncation as trunc
from tests.validation import test_vectorbt_crosscheck as xcheck
from tests.validation.data import find_parquet_root, real_bars
from tests.validation.harness import Peeker, compare_prefix, make_cfg, run
from tests.validation.reference import (
    RefParams,
    pandas_signals,
    reference_orders,
    vectorbt_naive_all_in,
    vectorbt_run,
)

from tbot.backtest.metrics import compute_metrics
from tbot.core.types import BarWindow, Timeframe
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.strategies.sma_filter import SmaFilter

HAVE_STORE = find_parquet_root() is not None


def section(title: str) -> None:
    print(f"\n=== {title}")


def data_section() -> None:
    section("DATA (Binance BTCUSDT 1d, before the sealed holdout)")
    if not HAVE_STORE:
        print("local Parquet store absent: real-data sections are skipped, synthetic ones still run")
        return
    start, end = datetime(2018, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC)
    bars = real_bars("BTCUSDT", Timeframe.D1, start, end)
    print(f"bars={len(bars)} first_close_ts={bars[0].ts:%Y-%m-%d} last_close_ts={bars[-1].ts:%Y-%m-%d}")
    gaps = [
        float((b.open / a.close - 1) * 10_000)
        for a, b in pairwise(bars)
        if abs(b.open / a.close - 1) > Decimal("0.001")
    ]
    largest = max(map(abs, gaps))
    print(f"days with |open gap| above the 10 bps buy buffer: {len(gaps)} (largest {largest:.0f} bps)")


def _sources() -> list[str]:
    return ["real", "synthetic"] if HAVE_STORE else ["synthetic"]


def truncation_section() -> None:
    section("TRUNCATION (engine run on data[:N] vs data[:N+k]; 'differing' must be 0 for honest strategies)")
    cfg = make_cfg()
    for source in _sources():
        bars = trunc._daily(source)
        for name, factory in trunc.HONEST.items():
            total = bad = 0
            for n in trunc.CUTS_NEXT_OPEN[source]:
                for k in trunc.KS_NEXT_OPEN:
                    diffs = compare_prefix(run(factory(), bars[:n], cfg), run(factory(), bars[: n + k], cfg))
                    total += 1
                    bad += bool(diffs)
            print(f"next-open {source:9s} {name:13s} pairs={total:3d} differing={bad}")
        days, hours = trunc._delay_data(source)
        dcfg = make_cfg(delay_hours=6)
        for name in ("buy_and_hold", "sma20"):
            for view in ("trimmed", "full"):
                total = bad = 0
                for n in trunc.CUTS_DELAY:
                    for k in trunc.KS_DELAY:
                        ld, sd = days[: n + k], days[:n]
                        lr = run(trunc.HONEST[name](), ld, dcfg, trunc._hours_until(hours, ld))
                        sx = trunc._hours_until(hours, sd) if view == "trimmed" else hours
                        sr = run(trunc.HONEST[name](), sd, dcfg, sx)
                        total += 1
                        bad += bool(compare_prefix(sr, lr))
                print(f"6h-delay  {source:9s} {name:13s} exec={view:7s} pairs={total:3d} differing={bad}")
        cuts = range(300, 360) if source == "real" else range(130, 190)
        flagged = expected = 0
        for n in cuts:
            lr = run(Peeker(bars[: n + 2]), bars[: n + 2], cfg)
            sr = run(Peeker(bars[:n]), bars[:n], cfg)
            weight = next(d.target_weight for d in lr.decisions if d.ts == sr.equity[-1].ts)
            flagged += bool(compare_prefix(sr, lr))
            expected += weight != 0.0
        print(
            f"cheater next-bar peek    {source:9s} pairs={len(cuts)} flagged={flagged} "
            f"expected_to_differ={expected}"
        )
        n = trunc.CUTS_NEXT_OPEN[source][1]
        cheat = trunc.FullSampleMeanCheater
        sr = run(cheat(bars[:n]), bars[:n], cfg)
        lr = run(cheat(bars[: n + 40]), bars[: n + 40], cfg)
        print(f"cheater full-sample mean {source:9s} flagged={bool(compare_prefix(sr, lr))}")


def warmup_section() -> None:
    section("WARM-UP (SMA: max diff across start offsets, and max relative diff vs pandas rolling mean)")
    for source in _sources():
        bars = trunc._daily(source)
        closes = pd.Series([float(b.close) for b in bars])
        for n in (20, 100, 200):
            strat = SmaFilter(n)
            pandas_sma = closes.rolling(n).mean()
            worst_rel = worst_abs = 0.0
            for off in (0, 1, 7, 33, 100, 251):
                sub = bars[off:]
                for t in range(n - 1, len(sub), 13):
                    value = strat.sma(BarWindow.of(sub[: t + 1]))
                    ref = strat.sma(BarWindow.of(bars[off + t - n + 1 : off + t + 1]))
                    worst_abs = max(worst_abs, abs(value - ref))
                    worst_rel = max(
                        worst_rel, abs(value - pandas_sma.iloc[off + t]) / pandas_sma.iloc[off + t]
                    )
            print(
                f"{source:9s} SMA{n:<4d} max|diff across offsets|={worst_abs:.2e} "
                f"max rel vs pandas={worst_rel:.2e}"
            )


def hand_section() -> None:
    section("HAND-COMPUTED 5-BAR EXAMPLE (10 bps fee, 5 bps slippage, 10 bps buy buffer)")
    bars = hand.five_bars()
    res = run(hand.Scripted([1.0, 1.0, 0.0, 0.0, 0.0], bars), bars, make_cfg())
    for r in res.fills:
        f = r.fill
        print(f"{f.side.value} qty={f.quantity} price={f.price} fee={f.fee}")
    print("equity:", [str(p.equity) for p in res.equity])
    print(f"fees={res.fees_paid} slippage={res.slippage_cost} final={res.equity[-1].equity}")


def _vectorbt_importable() -> bool:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import vectorbt  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
        except Exception:
            return False
    return True


def vectorbt_section() -> None:
    section("VECTORBT CROSS-CHECK (BTCUSDT 1d, fee 10 bps, slip 5 bps, next-open fills)")
    source = "real" if HAVE_STORE else "synthetic"
    bars = xcheck._bars(source)
    print(f"source={source} bars={len(bars)}")
    have_vbt = _vectorbt_importable()
    if not have_vbt:
        print("vectorbt unavailable: only the Decimal-reference layer is reported")
    for name in xcheck.STRATEGIES:
        res = run(xcheck._strategy(name), bars, make_cfg())
        sig = pandas_signals(bars, xcheck._kind(name), 100)
        ref = reference_orders(bars, sig)
        eng = [(f.fill.side.value, f.fill.price, f.fill.quantity, f.fill.fee) for f in res.fills]
        same = eng == [(f.side, f.price, f.quantity, f.fee) for f in ref.fills]
        same_eq = [p.equity for p in res.equity] == ref.equity
        final = float(res.equity[-1].equity)
        print(f"[{name}] fills={len(res.fills)} fees={float(res.fees_paid):.5f} final={final:.4f}")
        print(f"  A: Decimal reference identical fills={same} identical equity curve={same_eq}")
        res0 = run(xcheck._strategy(name), bars, make_cfg(buffer_bps=Decimal(0)))
        print(
            f"  buffer=0: final={float(res0.equity[-1].equity):.2f} "
            f"INSUFFICIENT_FUNDS rejections={len(res0.rejections)}"
        )
        if not have_vbt:
            continue
        kw = {"fee": xcheck.FEE, "slippage": xcheck.SLIP, "init_cash": xcheck.CASH}
        pf = vectorbt_run(bars, ref, **kw)
        v, e = pf.value().to_numpy(), xcheck._engine_equity(res)
        print(
            f"  B: vbt(same orders) final={v[-1]:.4f} rel={v[-1] / e[-1] - 1:+.2e} orders={pf.orders.count()}"
        )
        print(
            f"     max abs equity diff={np.abs(v - e).max():.4f} USD, "
            f"max rel={(np.abs(v - e) / e).max():.2e}, fees vbt={float(pf.orders.fees.sum()):.5f}"
        )
        naive = vectorbt_naive_all_in(bars, sig, **kw)
        nv = float(naive.value().iloc[-1])
        pred = xcheck._predicted_buffer_shortfall(res, bars[-1].close)
        print(
            f"  C: naive all-in vbt final={nv:.4f} engine/naive-1={e[-1] / nv - 1:+.4%} "
            f"predicted by the buffer={pred:+.4%} orders naive={naive.orders.count()}"
        )
        oracle = reference_orders(
            bars, sig, replace(RefParams(), sizing="oracle_open", buffer_bps=Decimal(0))
        )
        orc = vectorbt_run(bars, oracle, **kw)
        abl = float(orc.value().iloc[-1]) / nv - 1
        print(f"     ablation (no buffer, fill price known) vs naive: {abl:+.2e}")


def metrics_section() -> None:
    section("INDEPENDENT METRICS (365-day annualisation)")
    bars = metrics_check._daily_real() if HAVE_STORE else xcheck._bars("synthetic")
    for name, strat in (("buy_and_hold", BuyAndHold()), ("sma100", SmaFilter(100))):
        res = run(strat, bars, make_cfg())
        m = compute_metrics(res)
        mine = metrics_check.independent(metrics_check.window_equity(res), 365.0)
        print(f"[{name}] module: sharpe={m.sharpe:.9f} maxdd={m.max_drawdown:.9f} cagr={m.cagr:.9f}")
        print(
            f"  mine:   sharpe={mine['sharpe']:.9f} maxdd={mine['max_drawdown']:.9f} cagr={mine['cagr']:.9f}"
        )
        print(f"  turnover sides/yr={m.turnover_sides_per_year:.2f} cost drag/yr={m.cost_drag_annual:.4%}")


def cost_section() -> None:
    section("COST IDENTITY AND x2 STRESS (SMA100, BTCUSDT 1d)")
    if not HAVE_STORE:
        print("store absent")
        return
    bars = metrics_check._daily_real()
    for label, fee, slip in (("base 10+5 bps", 10, 5), ("x2 stress 20+10 bps", 20, 10)):
        res = run(SmaFilter(100), bars, make_cfg(fee_bps=Decimal(fee), slip_bps=Decimal(slip)))
        frictionless = hand._frictionless_equity(res, bars)
        residual = frictionless - res.fees_paid - res.slippage_cost - res.equity[-1].equity
        print(
            f"{label}: final={float(res.equity[-1].equity):.2f} fees={float(res.fees_paid):.2f} "
            f"slippage={float(res.slippage_cost):.2f} frictionless={float(frictionless):.2f} "
            f"identity residual={residual}"
        )


if __name__ == "__main__":
    data_section()
    truncation_section()
    warmup_section()
    hand_section()
    vectorbt_section()
    metrics_section()
    cost_section()
