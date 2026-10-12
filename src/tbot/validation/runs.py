"""Engine runs for the Phase 3 harness: markets, fill modes, costs, fixed-parameter and composite runs.

Everything here goes through the public engine API (``run_backtest`` with a ``BacktestConfig``); nothing in
the engine is reimplemented. Two engine generations are supported without touching either: the base engine
(``BacktestConfig.drift_tolerance``, no stop execution) and the engine of the stops/band PR
(``rebalance_band``, ``enforce_stops``). Fields that exist only in one of them are looked up with
``dataclasses.fields`` / ``inspect.signature`` and set only if present, so this module imports,
type-checks and runs on both.

Pooled out-of-sample runs
=========================
A *composite* strategy (``CompositeStrategy``) follows a per-period schedule (period start -> grid key or
``None`` for flat) and, at every decision bar, asks the scheduled child strategy for its target weight. Each
child is called with the **full causal history from the data start**, rebuilt from the market's own series by
timestamp, so a child that replays from the anchor (SPEC 7.1) sees exactly what it would see in a run that
started at the anchor, and the harness never has to ``slice`` a window by index in the engine's frame.

Anchor replay (SPEC 7.1: "folds never restart the strategy; they only slice the replayed series"): the
engine runs from the data start with the FIRST period's choice applied from the anchor (flat if that choice
is ``None``), and the pooled series, its fills and its costs are those from the baseline close of the first
test day on. A position entered before the first test day, and its stop, are carried into it exactly as in a
fixed anchored run, so a candidate that holds one point throughout reproduces ``run_fixed`` bar for bar. A
parameter change at a later boundary is traded through the engine's normal rebalance rule (the 25 % band,
SPEC 7.2 C), which is the SPEC's "charged as a trade". Phase 3 is daily only (D-090): every run uses the 1d
signal feed, and the 1h execution feed under the manual-delay fill model.

S1 and the protective stop (D-092)
==================================
S1 has no intraday stop of its own; it follows its members' daily stopped states. It is run with
``enforce_stops=False`` (the engine never asks it for a stop and produces no stop events). A member that is
stopped out by its daily rule contributes weight 0 through its own target, and the reduction of S1's target
that this causes must not be blocked by the band. Netting rule: at a close where ANY member enters STOPPED,
S1 trades to its full new target (including the other members' moves at that close), ignoring the 25 % band.
``BlendStrategy`` tags the intent on that close and ``StopOutExemptRiskManager`` sends a tagged intent
through a band-0 manager. Every other change of S1's target, a member's ordinary signal exit included, keeps
S0's rebalance rule on the netted position (25 % band; entries from 0 and exits to 0 always trade). Members
must implement :class:`StopOutAware`.
"""

from __future__ import annotations

import dataclasses
import inspect
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any, Protocol, runtime_checkable

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import run_backtest
from tbot.backtest.feed import BacktestFeed
from tbot.backtest.result import BacktestResult
from tbot.backtest.risk_stub import StubRiskManager
from tbot.backtest.slippage import stressed as engine_stressed
from tbot.core.config import CostConfig
from tbot.core.types import (
    Bar,
    BarWindow,
    MarketState,
    PortfolioView,
    RiskDecision,
    RiskManager,
    Strategy,
    SymbolFilters,
    TargetIntent,
    Timeframe,
)
from tbot.execution.simulated import FlatSlippage, SlippageModel
from tbot.strategies.buy_and_hold import BuyAndHold
from tbot.validation.stats import annualised_sharpe, max_drawdown_of_returns

__all__ = [
    "NORMAL_COSTS",
    "STRESS_COSTS",
    "BlendStrategy",
    "CompositeStrategy",
    "CostSpec",
    "FillMode",
    "Market",
    "SeriesRun",
    "StopOutAware",
    "StopOutExemptRiskManager",
    "StrategyFactory",
    "default_filters",
    "engine_composite",
    "engine_supports_stops_and_band",
    "run_buy_and_hold",
    "run_composite",
    "run_fixed",
]

StrategyFactory = Callable[[str], Strategy]
"""key -> a FRESH strategy instance (strategies replay from the anchor and cache; never share them)."""

_DELAY = timedelta(hours=6)


class FillMode(StrEnum):
    NEXT_OPEN = "next_open"
    MANUAL_DELAY = "manual_delay"  # the gating mode: 6 h after the signal close, on 1h bars


@dataclass(frozen=True)
class CostSpec:
    """Fee and slippage per side (SPEC 7.2 F). Every Phase 3 number built from these is PROVISIONAL.

    ``slippage_model`` is the depth-curve model from configuration (SPEC section 7); when it is None the flat
    ``slippage_bps`` placeholder applies and ``is_flat_slippage`` is true. The pre-flight refuses a real run
    with flat slippage.
    """

    fee_bps: Decimal
    slippage_bps: Decimal
    label: str
    slippage_model: SlippageModel | None = None

    @property
    def is_flat_slippage(self) -> bool:
        return self.slippage_model is None or isinstance(self.slippage_model, FlatSlippage)

    def to_cost_config(self) -> CostConfig:
        return CostConfig(
            taker_fee_bps=self.fee_bps,
            maker_fee_bps=self.fee_bps,
            slippage_bps=self.slippage_bps,
            slippage_provisional=True,
        )

    def with_model(self, model: SlippageModel, label: str) -> CostSpec:
        return dataclasses.replace(self, slippage_model=model, label=label)

    def stressed(self, factor: int = 2) -> CostSpec:
        """The cost stress (SPEC 7.2 E, x2) through the engine's ``stressed`` helper, never by hand.

        ``tbot.backtest.slippage.stressed(costs, slippage_model, factor)`` moves fee, slippage bps and the
        slippage model together; the harness only repackages its two outputs. Both are then passed to the
        config by :func:`make_config`. A depth curve is scaled (every cost and the half spread), a flat
        model or no model doubles the flat bps; any other model type raises ``TypeError`` there.
        """
        out = engine_stressed(self.to_cost_config(), self.slippage_model, factor)
        return CostSpec(
            out.costs.taker_fee_bps,
            out.costs.slippage_bps,
            f"x{factor} stress of: {self.label}",
            out.slippage_model,
        )


NORMAL_COSTS = CostSpec(Decimal(10), Decimal(5), "10 bps fee + 5 bps slippage (PROVISIONAL flat slippage)")
STRESS_COSTS = NORMAL_COSTS.stressed()  # 20 bps fee + 10 bps flat slippage (PROVISIONAL)


def default_filters(symbol: str) -> SymbolFilters:
    """LBank-like placeholder rules (price 2, quantity 5, min notional 1 USDT); the harness ignores them."""
    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal(1),
    )


class Market:
    """One instrument's bars by timeframe plus cached feeds. The daily series is required."""

    def __init__(
        self,
        symbol: str,
        bars: Mapping[Timeframe, Sequence[Bar]],
        *,
        filters: SymbolFilters | None = None,
        periods_per_year: float = 365.0,
        base_asset: str = "BTC",
    ) -> None:
        if Timeframe.D1 not in bars or not bars[Timeframe.D1]:
            raise ValueError("a market needs daily bars")
        self.symbol = symbol
        self.filters = filters or default_filters(symbol)
        self.periods_per_year = periods_per_year
        self.base_asset = base_asset
        self._bars: dict[Timeframe, tuple[Bar, ...]] = {}
        self._ts: dict[Timeframe, list[datetime]] = {}
        for tf, seq in bars.items():
            tup = tuple(seq)
            if any(b.symbol != symbol or b.timeframe != tf for b in tup):
                raise ValueError(f"foreign bar in {symbol}/{tf}")
            ts = [b.ts for b in tup]
            if any(b <= a for a, b in pairwise(ts)):
                raise ValueError(f"{symbol}/{tf} bars must have strictly increasing close times")
            self._bars[tf] = tup
            self._ts[tf] = ts
        self._feeds: dict[tuple[Timeframe, datetime | None, datetime | None], BacktestFeed] = {}

    def has(self, tf: Timeframe) -> bool:
        return tf in self._bars and bool(self._bars[tf])

    def series(self, tf: Timeframe) -> tuple[Bar, ...]:
        return self._bars[tf]

    def closes(self, tf: Timeframe) -> list[datetime]:
        return self._ts[tf]

    @property
    def daily_closes(self) -> list[datetime]:
        return self._ts[Timeframe.D1]

    @property
    def last_close(self) -> datetime:
        return self._ts[Timeframe.D1][-1]

    @property
    def anchor(self) -> datetime:
        """The earliest bar close of any series: where an anchored replay starts (SPEC 7.1)."""
        return min(ts[0] for ts in self._ts.values() if ts)

    def feed(self, tf: Timeframe, lo: datetime | None = None, hi: datetime | None = None) -> BacktestFeed:
        """Bars with ``lo <= ts <= hi`` (cached; the feed is re-iterable)."""
        key = (tf, lo, hi)
        if key not in self._feeds:
            ts = self._ts[tf]
            i = 0 if lo is None else bisect_left(ts, lo)
            j = len(ts) if hi is None else bisect_right(ts, hi)
            self._feeds[key] = BacktestFeed(list(self._bars[tf][i:j]))
        return self._feeds[key]

    def window_to(self, tf: Timeframe, ts: datetime) -> BarWindow | None:
        """All closed bars of ``tf`` with close time <= ``ts`` (None if none). Causal by construction."""
        k = bisect_right(self._ts[tf], ts)
        if k == 0:
            return None
        return _window(self.symbol, tf, self._bars[tf][:k])

    def truncated(self, end: datetime) -> Market:
        """The same market with every bar closing after ``end`` removed."""
        return Market(
            self.symbol,
            {tf: [b for b in bars if b.ts <= end] for tf, bars in self._bars.items()},
            filters=self.filters,
            periods_per_year=self.periods_per_year,
            base_asset=self.base_asset,
        )


def _window(symbol: str, tf: Timeframe, bars: tuple[Bar, ...]) -> BarWindow:
    """``BarWindow`` without the O(n) revalidation (the bars come from a validated, ordered series)."""
    window = object.__new__(BarWindow)
    object.__setattr__(window, "symbol", symbol)
    object.__setattr__(window, "timeframe", tf)
    object.__setattr__(window, "bars", bars)
    return window


# -- engine compatibility ----------------------------------------------------------------------


def _config_fields() -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(BacktestConfig))


def engine_supports_stops_and_band() -> bool:
    """True iff the engine has the SPEC 7.1 stop execution and the 25 % relative band (the stops/band PR)."""
    fields = _config_fields()
    return "enforce_stops" in fields and "rebalance_band" in fields


def _benchmark_overrides() -> dict[str, Any]:
    """Config fields that make a buy-and-hold run trade exactly once: no stops, no rebalancing."""
    fields = _config_fields()
    out: dict[str, Any] = {}
    if "enforce_stops" in fields:
        out["enforce_stops"] = False
    if "rebalance_band" in fields:
        out["rebalance_band"] = 1.0
    elif "drift_tolerance" in fields:
        out["drift_tolerance"] = 0.99
    return out


def make_config(
    market: Market,
    timeframe: Timeframe,
    fill_mode: FillMode,
    costs: CostSpec,
    *,
    benchmark: bool = False,
    enforce_stops: bool = True,
) -> BacktestConfig:
    """The engine configuration of one run. ``enforce_stops=False`` is S1 only (D-092)."""
    delayed = fill_mode is FillMode.MANUAL_DELAY
    cfg = BacktestConfig(
        symbol=market.symbol,
        timeframe=timeframe,
        filters=market.filters,
        costs=costs.to_cost_config(),
        slippage_model=costs.slippage_model,
        base_asset=market.base_asset,
        fill_delay=_DELAY if delayed else timedelta(0),
        exec_timeframe=Timeframe.H1 if delayed else None,
    )
    if "enforce_stops" in _config_fields():
        cfg = dataclasses.replace(cfg, enforce_stops=enforce_stops)
    if benchmark:
        cfg = dataclasses.replace(cfg, **_benchmark_overrides())
    return cfg


def make_stub_risk(
    cfg: BacktestConfig, *, rebalance_band: float | None = None, id_prefix: str | None = None
) -> StubRiskManager:
    """The engine's default ``StubRiskManager`` for ``cfg`` (built the way the engine builds it).

    ``rebalance_band`` overrides the config's band (0.0 always trades a non-zero gap) and ``id_prefix`` the
    client-order-id prefix, which two managers sharing one engine run must make distinct.
    """
    params = inspect.signature(StubRiskManager.__init__).parameters
    kwargs: dict[str, Any] = {
        "taker_fee_bps": cfg.costs.taker_fee_bps,
        "slippage_bps": cfg.costs.slippage_bps,
        "buy_buffer_bps": cfg.buy_buffer_bps,
    }
    if "rebalance_band" in params:
        kwargs["rebalance_band"] = (
            rebalance_band if rebalance_band is not None else getattr(cfg, "rebalance_band", 0.25)
        )
    elif "drift_tolerance" in params:
        kwargs["drift_tolerance"] = getattr(cfg, "drift_tolerance", 0.02)
    if id_prefix is not None and "id_prefix" in params:
        kwargs["id_prefix"] = id_prefix
    return StubRiskManager(**kwargs)


# -- results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesRun:
    """A daily equity series (equity at the 00:00 closes) with the cost bookkeeping of the run."""

    ts: tuple[datetime, ...]  # daily close times; ts[0] is the baseline of the first return
    equity: tuple[float, ...]
    periods_per_year: float
    n_fills: int
    fees_paid: float
    slippage_cost: float
    traded_notional: float
    n_stop_events: int

    @property
    def return_ts(self) -> tuple[datetime, ...]:
        return self.ts[1:]

    @property
    def returns(self) -> tuple[float, ...]:
        e = self.equity
        return tuple(e[i] / e[i - 1] - 1.0 if e[i - 1] > 0 else 0.0 for i in range(1, len(e)))

    def returns_between(self, lo: datetime, hi: datetime) -> list[float]:
        """Returns whose close time lies in ``[lo, hi)``."""
        rts, rets = self.return_ts, self.returns
        i, j = bisect_left(rts, lo), bisect_left(rts, hi)
        return list(rets[i:j])

    @property
    def sharpe(self) -> float | None:
        return annualised_sharpe(self.returns, self.periods_per_year)

    @property
    def max_drawdown(self) -> float:
        return max_drawdown_of_returns(self.returns)

    @property
    def years(self) -> float:
        return len(self.returns) / self.periods_per_year

    @property
    def mean_equity(self) -> float:
        return sum(self.equity) / len(self.equity) if self.equity else 0.0

    @property
    def turnover_sides_per_year(self) -> float | None:
        return self.n_fills / self.years if self.years > 0 else None

    @property
    def turnover_notional_per_year(self) -> float | None:
        """Traded notional as a multiple of the mean equity, per year."""
        if self.years <= 0 or self.mean_equity <= 0:
            return None
        return self.traded_notional / self.mean_equity / self.years

    @property
    def cost_drag_annual(self) -> float | None:
        """(fees + slippage) / mean equity / years -- the same definition as ``backtest.metrics``."""
        if self.years <= 0 or self.mean_equity <= 0:
            return None
        return (self.fees_paid + self.slippage_cost) / self.mean_equity / self.years


def _is_daily_close(ts: datetime) -> bool:
    return ts.hour == 0 and ts.minute == 0 and ts.second == 0


def series_from_result(
    result: BacktestResult,
    *,
    periods_per_year: float,
    daily_only: bool,
    since: datetime | None = None,
) -> SeriesRun:
    """The daily equity series of ``result``; with ``since`` only the part from that close on.

    ``since`` slices an anchored replay: the equity points with ``ts >= since`` (``ts[0]`` is the baseline of
    the first return) and the cost bookkeeping of the fills and stop events at or after ``since`` (a fill at
    exactly ``since`` is the first return's). Without ``since`` the totals are the engine's own.
    """
    points = [p for p in result.equity if not daily_only or _is_daily_close(p.ts)]
    if since is None:
        return SeriesRun(
            ts=tuple(p.ts for p in points),
            equity=tuple(float(p.equity) for p in points),
            periods_per_year=periods_per_year,
            n_fills=len(result.fills),
            fees_paid=float(result.fees_paid),
            slippage_cost=float(result.slippage_cost),
            traded_notional=float(result.traded_notional),
            n_stop_events=len(getattr(result, "stop_events", ())),
        )
    points = [p for p in points if p.ts >= since]
    fills = [f for f in result.fills if f.fill.ts >= since]
    return SeriesRun(
        ts=tuple(p.ts for p in points),
        equity=tuple(float(p.equity) for p in points),
        periods_per_year=periods_per_year,
        n_fills=len(fills),
        fees_paid=float(sum((f.fill.fee for f in fills), Decimal(0))),
        slippage_cost=float(sum((f.slippage_cost for f in fills), Decimal(0))),
        traded_notional=float(sum((f.fill.notional for f in fills), Decimal(0))),
        n_stop_events=sum(1 for e in getattr(result, "stop_events", ()) if e.ts >= since),
    )


# -- composite strategy ------------------------------------------------------------------------


@runtime_checkable
class StopOutAware(Protocol):
    """A strategy that can say whether its own daily protective stop was hit on the last bar of a window.

    ``stopped_out_on(window)`` is True iff, at the close of ``window``'s last bar, the strategy went from LONG
    to STOPPED by its daily rule on THAT bar (the bar's low reached the stop). It must use only bars up to and
    including the last one (the same causality as ``on_bar``) and must also report a stop-out that is released
    to FLAT on the same bar. The S1 blend needs it from its members (D-092).
    """

    def stopped_out_on(self, window: BarWindow) -> bool: ...


class CompositeStrategy:
    """Follows a schedule of ``(start, key | None)``: flat before the first start and while the key is None.

    Every bar of the daily feed is a decision bar. The scheduled child is asked with the full daily history
    up to the bar's close (``Market.window_to``).
    """

    warmup_bars = 1

    def __init__(
        self,
        *,
        strategy_id: str,
        market: Market,
        schedule: Sequence[tuple[datetime, str | None]],
        factory: StrategyFactory,
    ) -> None:
        if not schedule or any(b[0] <= a[0] for a, b in pairwise(schedule)):
            raise ValueError("schedule must be non-empty with strictly increasing starts")
        self.id = strategy_id
        self._market = market
        self._starts = [s for s, _ in schedule]
        self._keys = [k for _, k in schedule]
        self._factory = factory
        self._children: dict[str, Strategy] = {}

    # -- schedule helpers --
    def key_at(self, ts: datetime) -> str | None:
        i = bisect_right(self._starts, ts) - 1
        return None if i < 0 else self._keys[i]

    def _child(self, key: str) -> Strategy:
        if key not in self._children:
            self._children[key] = self._factory(key)
        return self._children[key]

    # -- Strategy protocol --
    def on_bar(self, window: BarWindow) -> TargetIntent:
        ts = window.as_of
        key = self.key_at(ts)
        if key is None:
            return TargetIntent(self.id, ts, 0.0, "flat: no scheduled point")
        child_window = self._market.window_to(Timeframe.D1, ts)
        child = self._child(key)
        if child_window is None or len(child_window) < child.warmup_bars:
            weight = 0.0
        else:
            weight = child.on_bar(child_window).target_weight
        return TargetIntent(self.id, ts, weight, f"{key}")

    def stopped_out(self, window: BarWindow) -> bool:
        """True iff the scheduled child was stopped out by its daily rule on the last bar of ``window``."""
        ts = window.as_of
        key = self.key_at(ts)
        if key is None:
            return False
        child = self._child(key)
        if not isinstance(child, StopOutAware):
            raise TypeError(f"{key}: the strategy has no stopped_out_on(window); S1 needs it (D-092)")
        child_window = self._market.window_to(Timeframe.D1, ts)
        if child_window is None:
            return False
        return child.stopped_out_on(child_window)

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        key = self.key_at(window.as_of)
        if key is None:
            raise ValueError("stop requested while no point is scheduled")
        child_window = self._market.window_to(Timeframe.D1, window.as_of)
        if child_window is None:
            raise ValueError(f"no daily history at {window.as_of}")
        return self._child(key).stop_price(child_window, entry_price)


class BlendStrategy:
    """Equal-weight mean of member strategies' targets (S1), with no protective stop of its own (D-092).

    S1 follows each member's daily state through the member's target weight: a member that is STOPPED
    contributes 0. On the bar a member is stopped out by its daily rule, the blend records the fact
    (``stop_out_at``) for the :class:`StopOutExemptRiskManager`. The tag at close ``t`` is computed from the
    members' windows up to ``t`` only.
    """

    warmup_bars = 1

    def __init__(self, strategy_id: str, members: Sequence[CompositeStrategy]) -> None:
        if not members:
            raise ValueError("a blend needs at least one member")
        self.id = strategy_id
        self._members = list(members)
        self._tag: tuple[datetime, bool] | None = None

    def on_bar(self, window: BarWindow) -> TargetIntent:
        ts = window.as_of
        weights = [m.on_bar(window).target_weight for m in self._members]
        stopped = any(m.stopped_out(window) for m in self._members)
        self._tag = (ts, stopped)
        reason = f"mean of {len(weights)}" + ("; a member was stopped out" if stopped else "")
        return TargetIntent(self.id, ts, sum(weights) / len(weights), reason)

    def stop_out_at(self, ts: datetime) -> bool:
        """True iff the intent just produced for close ``ts`` was tagged: a member stopped out on that bar."""
        return self._tag is not None and self._tag[0] == ts and self._tag[1]

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:
        raise NotImplementedError(
            "S1 has no intraday stop of its own; it follows its members' daily stopped states (D-092)"
        )


class StopOutExemptRiskManager:
    """Trades S1 to its full new target at a close where any member entered STOPPED (D-092 netting rule).

    ``banded`` is the engine's normal manager (25 % band); ``unbanded`` the same manager with band 0 and a
    different order-id prefix. An intent goes to ``unbanded`` iff ``is_stop_out(intent.ts)``, whatever its
    direction (other members' moves at that close are netted into the same trade). Every other intent,
    including a member's ordinary signal exit, takes the normal banded path.
    """

    def __init__(
        self, banded: RiskManager, unbanded: RiskManager, is_stop_out: Callable[[datetime], bool]
    ) -> None:
        self._banded = banded
        self._unbanded = unbanded
        self._is_stop_out = is_stop_out

    def evaluate(self, intent: TargetIntent, portfolio: PortfolioView, market: MarketState) -> RiskDecision:
        if self._is_stop_out(intent.ts):
            return self._unbanded.evaluate(intent, portfolio, market)
        return self._banded.evaluate(intent, portfolio, market)


# -- runners -----------------------------------------------------------------------------------


def _exec_feed(market: Market, lo: datetime, hi: datetime) -> BacktestFeed:
    if not market.has(Timeframe.H1):
        raise ValueError(f"{market.symbol}: the manual-delay model needs 1h execution bars")
    return market.feed(Timeframe.H1, lo, hi)


def run_fixed(
    market: Market,
    strategy: Strategy,
    timeframe: Timeframe,
    fill_mode: FillMode,
    costs: CostSpec,
    *,
    end: datetime | None = None,
) -> SeriesRun:
    """One fixed-parameter run over the full history (from the first bar to the last daily close).

    The daily equity series is read at the 00:00 closes.
    """
    last = end or market.last_close
    first = market.series(timeframe)[0].ts
    cfg = make_config(market, timeframe, fill_mode, costs)
    exec_feed = _exec_feed(market, first, last) if fill_mode is FillMode.MANUAL_DELAY else None
    result = run_backtest(market.feed(timeframe, None, last), strategy, cfg, exec_feed=exec_feed)
    return series_from_result(result, periods_per_year=market.periods_per_year, daily_only=True)


def engine_composite(
    market: Market,
    strategy: CompositeStrategy | BlendStrategy,
    fill_mode: FillMode,
    costs: CostSpec,
    *,
    baseline: datetime,
    end: datetime,
    benchmark: bool = False,
    from_anchor: bool = True,
) -> BacktestResult:
    """The engine run behind :func:`run_composite`, returned whole (decisions, fills, stop events).

    ``from_anchor`` (default) replays the engine from the data start, as SPEC 7.1 requires ("folds never
    restart the strategy; they only slice the replayed series"): the position, the stop and the re-arm state
    entering the first test day are those of a run that started at the anchor. ``from_anchor=False`` starts
    the engine at ``baseline`` instead (the buy-and-hold benchmark, which by definition is entered on the
    first test day).

    A :class:`BlendStrategy` (S1) runs with ``enforce_stops=False`` and the stop-out exemption (D-092).
    """
    is_blend = isinstance(strategy, BlendStrategy)
    cfg = make_config(market, Timeframe.D1, fill_mode, costs, benchmark=benchmark, enforce_stops=not is_blend)
    feed_lo = None if from_anchor else baseline
    exec_lo = market.series(Timeframe.D1)[0].ts if from_anchor else baseline
    exec_feed = _exec_feed(market, exec_lo, end) if fill_mode is FillMode.MANUAL_DELAY else None
    risk: RiskManager | None = None
    if isinstance(strategy, BlendStrategy):
        risk = StopOutExemptRiskManager(
            make_stub_risk(cfg),
            make_stub_risk(cfg, rebalance_band=0.0, id_prefix="so"),
            strategy.stop_out_at,
        )
    feed = market.feed(Timeframe.D1, feed_lo, end)
    return run_backtest(feed, strategy, cfg, risk=risk, exec_feed=exec_feed)


def run_composite(
    market: Market,
    strategy: CompositeStrategy | BlendStrategy,
    fill_mode: FillMode,
    costs: CostSpec,
    *,
    baseline: datetime,
    end: datetime,
    benchmark: bool = False,
    from_anchor: bool = True,
) -> SeriesRun:
    """A pooled run reported on ``[baseline, end]`` (``baseline``: the daily close before the first test).

    See :func:`engine_composite` for ``from_anchor`` and the S1 handling; only the series from ``baseline`` on
    is returned.
    """
    result = engine_composite(
        market,
        strategy,
        fill_mode,
        costs,
        baseline=baseline,
        end=end,
        benchmark=benchmark,
        from_anchor=from_anchor,
    )
    return series_from_result(
        result, periods_per_year=market.periods_per_year, daily_only=True, since=baseline
    )


def run_buy_and_hold(
    market: Market,
    fill_mode: FillMode,
    costs: CostSpec,
    *,
    first_test_close: datetime,
    end: datetime,
) -> SeriesRun:
    """Weight 1 held over the pooled test days: entered once at the first decision, never rebalanced, no stop.

    Same fee and slippage as the strategy runs (SPEC 7.2 E). The decision is taken at the first test close
    (``first_test_close``) like the strategies' first decision, and fills per ``fill_mode``.
    """
    closes = market.daily_closes
    idx = bisect_left(closes, first_test_close)
    if idx == 0:
        raise ValueError("need a daily close before the first test close as the return baseline")
    baseline = closes[idx - 1]
    bh = BuyAndHold(weight=1.0, warmup_bars=1)
    composite = CompositeStrategy(
        strategy_id="buy_and_hold",
        market=market,
        schedule=[(first_test_close, "BH")],
        factory=lambda _key: bh,
    )
    return run_composite(
        market,
        composite,
        fill_mode,
        costs,
        baseline=baseline,
        end=end,
        benchmark=True,
        from_anchor=False,
    )
