"""Engine-run harness and the truncation comparison used by the G2 validation tests.

``Snapshot`` is everything observable about a run up to a cut-off bar (positions, weights, equity, fills,
decisions). ``compare_prefix`` checks a short run (data[:N]) against a longer run (data[:N+k]) on the bars
and events that both runs can legitimately see. The cheating strategy below is the harness's own proof of
sensitivity: if ``compare_prefix`` did not flag it, a pass on the honest strategies would mean nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.result import BacktestResult
from tbot.core.config import CostConfig
from tbot.core.types import Bar, BarWindow, Strategy, SymbolFilters, TargetIntent, Timeframe
from tests.sim_helpers import SYMBOL, lbank_like_filters

__all__ = [
    "BPS_FEE",
    "BPS_SLIP",
    "Peeker",
    "Snapshot",
    "compare_prefix",
    "make_cfg",
    "run",
    "snapshot",
]

BPS_FEE = Decimal(10)  # D-060: LBank VIP-0 taker 0.10 % per side
BPS_SLIP = Decimal(5)  # provisional flat slippage, 0.05 % per side


def make_cfg(
    *,
    timeframe: Timeframe = Timeframe.D1,
    fee_bps: Decimal = BPS_FEE,
    slip_bps: Decimal = BPS_SLIP,
    delay_hours: int = 0,
    buffer_bps: Decimal = Decimal(10),
    initial_cash: Decimal = Decimal(10_000),
    filters: SymbolFilters | None = None,
) -> BacktestConfig:
    return BacktestConfig(
        symbol=SYMBOL,
        timeframe=timeframe,
        filters=filters or lbank_like_filters(),
        costs=CostConfig(taker_fee_bps=fee_bps, maker_fee_bps=Decimal("10"), slippage_bps=slip_bps),
        initial_cash=initial_cash,
        buy_buffer_bps=buffer_bps,
        fill_delay=timedelta(hours=delay_hours) if delay_hours else timedelta(0),
        exec_timeframe=Timeframe.H1 if delay_hours else None,
    )


def run(
    strategy: Strategy,
    bars: list[Bar],
    cfg: BacktestConfig,
    exec_bars: list[Bar] | None = None,
) -> BacktestResult:
    exec_feed = BacktestFeed(exec_bars) if exec_bars is not None else None
    return run_backtest(BacktestFeed(bars), strategy, cfg, exec_feed=exec_feed)


@dataclass(frozen=True)
class Snapshot:
    closes: tuple[datetime, ...]
    positions: tuple[Decimal, ...]
    weights: tuple[float, ...]
    equity: tuple[Decimal, ...]
    cash: tuple[Decimal, ...]
    fills: tuple[tuple[object, ...], ...]
    decisions: tuple[tuple[object, ...], ...]
    resizes: tuple[tuple[object, ...], ...]
    rejections: tuple[tuple[object, ...], ...]


def snapshot(res: BacktestResult, upto: datetime) -> Snapshot:
    """The part of a run visible at the close ``upto`` of the last signal bar.

    Equity points and decisions are stamped at a bar close, so they are included when ``ts <= upto``. Fills,
    resizes and rejections are stamped at the fill time. A fill at exactly ``upto`` is the open of the NEXT
    bar (open_ts(N+1) == close(N)): a longer run has it, a run cut at N never can, so it is excluded
    (strictly ``ts < upto``). Its effect on the books shows up from bar N+1 on, outside the horizon.
    """
    idx = [i for i, p in enumerate(res.equity) if p.ts <= upto]
    eq = [res.equity[i] for i in idx]
    return Snapshot(
        closes=tuple(p.ts for p in eq),
        positions=tuple(p.quantity for p in eq),
        weights=tuple(p.weight for p in eq),
        equity=tuple(p.equity for p in eq),
        cash=tuple(p.cash for p in eq),
        fills=tuple(
            (r.fill.client_order_id, r.fill.side, r.fill.ts, r.fill.price, r.fill.quantity, r.fill.fee)
            for r in res.fills
            if r.fill.ts < upto
        ),
        decisions=tuple(
            (d.ts, d.target_weight, d.outcome, d.client_order_id) for d in res.decisions if d.ts <= upto
        ),
        resizes=tuple((z.ts, z.requested_quantity, z.filled_quantity) for z in res.resizes if z.ts < upto),
        rejections=tuple((j.ts, j.reason) for j in res.rejections if j.ts < upto),
    )


def compare_prefix(short: BacktestResult, long: BacktestResult) -> list[str]:
    """Differences between a short run and the longer run, over the short run's own horizon. Empty = equal."""
    upto = short.equity[-1].ts
    a, b = snapshot(short, upto), snapshot(long, upto)
    diffs = []
    names = (
        "closes",
        "positions",
        "weights",
        "equity",
        "cash",
        "fills",
        "decisions",
        "resizes",
        "rejections",
    )
    for name in names:
        if getattr(a, name) != getattr(b, name):
            diffs.append(name)
    return diffs


class Peeker:
    """Deliberately cheating strategy: goes long when the NEXT bar's close is above this bar's close.

    It reads bar t+1 from the full array it was constructed with, i.e. exactly the look-ahead a buggy
    indicator (``shift(-1)``) would introduce. Built on ``data[:N]`` it cannot see past N, so its
    decisions near the cut differ from a run on ``data[:N+k]``: the truncation test must flag it.
    """

    warmup_bars = 1

    def __init__(self, data: list[Bar]) -> None:
        self.id = "peeker"
        self._close_by_ts = {b.ts: b.close for b in data}
        self._ts = [b.ts for b in data]
        self._next = dict(zip(self._ts, self._ts[1:], strict=False))

    def on_bar(self, window: BarWindow) -> TargetIntent:
        nxt = self._next.get(window.as_of)
        peek_close = self._close_by_ts[nxt] if nxt is not None else window.last.close
        weight = 1.0 if peek_close > window.last.close else 0.0
        return TargetIntent(self.id, window.as_of, weight, "peeks at close(t+1)")

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price / 2
