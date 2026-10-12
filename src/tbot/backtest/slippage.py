"""Builders for the depth-at-size slippage curve (SPEC section 7 G3 / D-067).

``DepthCurveSlippage`` itself (``tbot.execution.simulated``) takes its curve points as INPUT. This module
holds the one place that turns a measurement into points.

:func:`curve_from_g0_summary` implements the construction of the provisional D-067 curve DECIDED by Parham on
2026-10-12 (SPEC D-091): the per-notional median for the run, and the adverse per-notional p90 of cost as a
sensitivity that is reported only. The backtest engine does not use it by default
(``BacktestConfig.slippage_model`` stays ``None`` = flat bps); a harness must opt in explicitly, and every
number produced with it is PROVISIONAL.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NamedTuple

from tbot.core.config import CostConfig
from tbot.execution.simulated import CurvePoints, DepthCurveSlippage, FlatSlippage, SlippageModel

__all__ = ["DEFAULT_EQUITY_NORMALISER", "Statistic", "StressedCosts", "curve_from_g0_summary", "stressed"]

_ZERO = Decimal(0)
# SPEC section 7 G3: the curve is read "with equity fixed at 10,000 USDT" (the same fraction of equity costs
# the same bps at any account size). Pass ``equity_normaliser=None`` for the raw order notional instead.
DEFAULT_EQUITY_NORMALISER = Decimal(10_000)
_PCT_TO_BPS = Decimal(100)  # 1 % = 100 bps
_REFERENCE = "vs_touch"  # max_fill reference: measured from the best price consumed (SPEC 7 G3)
_FRACTION_TO_BPS = Decimal(10_000)  # a band given as a fraction of price: 0.001 = 10 bps

Statistic = Literal["median", "adverse_p90_cost"]
# statistic -> (the max_fill size quantile, the spread quantile) it reads from the G0 summary. The p90 of COST
# at a given size is the p10 of the fillable SIZE at a given cost (each sample's cost is monotone in size);
# the probe stores no p90 spread, so the adverse variant uses the more adverse p95 (SPEC D-091).
_QUANTILES: dict[str, tuple[str, str]] = {
    "median": ("median", "median"),
    "adverse_p90_cost": ("p10", "p95"),
}


def _as_decimal(value: object, what: str, path: Path) -> Decimal:
    """Exact Decimal of a JSON number (parsed as Decimal) or of the string the probe stores depth as."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{path}: {what} must be a number, got {value!r}")
    try:
        out = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError(f"{path}: {what} is not a number: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"{path}: {what} must be finite, got {value!r}")
    return out


def _max_fill_quote(max_fill: dict[str, Any], band: str, side: str, statistic: str, path: Path) -> Decimal:
    """Largest market order (quote notional) filling within ``band`` of the touch: one side, one statistic."""
    key = f"quote_{statistic}"
    try:
        raw = max_fill[band][_REFERENCE][side][key]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"{path}: no book.max_fill[{band!r}][{_REFERENCE!r}][{side!r}][{key!r}] in the G0 summary"
        ) from exc
    value = _as_decimal(raw, f"max_fill {band} {_REFERENCE} {side} {key}", path)
    if value <= _ZERO:
        raise ValueError(f"{path}: max_fill {band} {side} {key} must be > 0, got {value}")
    return value


def curve_from_g0_summary(
    path: Path | str,
    *,
    near_band: str = "0.001",
    far_band: str = "0.005",
    statistic: Statistic = "median",
    equity_normaliser: Decimal | None = DEFAULT_EQUITY_NORMALISER,
) -> DepthCurveSlippage:
    """The D-067 provisional curve as decided in SPEC D-091: curve points from the G0 probe summary.

    Reads ``book.max_fill`` and ``book.spread_pct`` of the LBank G0 probe summary JSON (for example
    ``research/reports/g0_lbank_20261011/lbank_probe_20261011T042800Z.summary.json``). ``max_fill[band]`` is
    the LARGEST MARKET ORDER whose volume-weighted fill price stays within ``band`` of the reference, measured
    FROM THE TOUCH (the ``vs_touch`` reference; ``vs_mid`` would include the half spread, which the model adds
    itself). Per side:

    * ``(0, 0)``;
    * ``(quote notional of the largest order within near_band (0.1 %), near_band in bps (10))``;
    * ``(quote notional of the largest order within far_band (0.5 %), far_band in bps (50))``.

    ``statistic`` (SPEC D-091):

    * ``"median"`` (default; the run): the per-notional MEDIAN of cost, the SPEC section 7 G3 statistic. Each
      sample's cost rises monotonically with notional, so the median fillable size at 10 (50) bps is exactly
      the notional at which the median cost is 10 (50) bps. Reads ``quote_median`` and the median spread.
    * ``"adverse_p90_cost"`` (sensitivity, reported only, never gating): the per-notional p90 of COST, the
      thin-book tail. By the same argument its points are the p10 of the fillable SIZE (``quote_p10``); the
      half spread uses the p95 spread (no p90 is stored; p95 is the more adverse).

    A BUY consumes the ASK side of the book, a SELL the BID side, so ``buy_points`` come from ``max_fill[..]
    ["buy"]`` and ``sell_points`` from ``["sell"]``. ``half_spread_bps`` = median spread / 2 in bps (the
    summary stores the spread in PERCENT of mid, hence ``* 100 / 2``; median or p95 per ``statistic``).

    ``equity_normaliser`` defaults to 10,000 USDT (SPEC section 7 G3: equity fixed at 10,000 USDT); it is
    overridable, and ``None`` disables the rescaling (raw order notional).

    The ``book.depth`` section (depth WITHIN a band of mid) is deliberately NOT read: it is a different
    quantity and not the one SPEC section 7 G3 names. Caveat: in the real summary one of the 32 samples at the
    0.5 % band hit the end of the visible book (``truncated_samples``), so that figure is a lower bound.

    Numbers are parsed as exact ``Decimal`` (no float round trip).
    """
    if statistic not in _QUANTILES:
        raise ValueError(f"statistic must be one of {sorted(_QUANTILES)}, got {statistic!r}")
    size_q, spread_q = _QUANTILES[statistic]
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
    try:
        book = data["book"]
        max_fill = book["max_fill"]
        raw_spread = book["spread_pct"][spread_q]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"{p}: not a G0 probe summary (need book.max_fill and book.spread_pct.{spread_q})"
        ) from exc
    spread_pct = _as_decimal(raw_spread, f"book.spread_pct.{spread_q}", p)
    if spread_pct < _ZERO:
        raise ValueError(f"{p}: book.spread_pct.{spread_q} must be >= 0, got {spread_pct}")
    bands = [
        (near_band, Decimal(near_band) * _FRACTION_TO_BPS),
        (far_band, Decimal(far_band) * _FRACTION_TO_BPS),
    ]

    def side_points(side: str) -> CurvePoints:
        return (
            (_ZERO, _ZERO),
            *((_max_fill_quote(max_fill, band, side, size_q, p), bps) for band, bps in bands),
        )

    return DepthCurveSlippage(
        buy_points=side_points("buy"),
        sell_points=side_points("sell"),
        half_spread_bps=spread_pct * _PCT_TO_BPS / 2,
        equity_normaliser=equity_normaliser,
    )


class StressedCosts(NamedTuple):
    """Result of :func:`stressed`: pass ``costs`` as the config's costs and ``slippage_model`` as its hook."""

    costs: CostConfig
    slippage_model: SlippageModel | None


def stressed(
    costs: CostConfig, slippage_model: SlippageModel | None, factor: int | Decimal = 2
) -> StressedCosts:
    """The cost-stress run (default x2) in ONE place: fees AND slippage move together.

    Returns ``costs`` with taker fee, maker fee and ``slippage_bps`` all multiplied by ``factor``, and the
    slippage hook to plug into :class:`BacktestConfig`:

    * a :class:`DepthCurveSlippage` -> ``slippage_model.scaled(factor)`` (every curve cost and the half
      spread; the equity normaliser geometry is kept);
    * a :class:`FlatSlippage` -> the same flat model with ``bps * factor``;
    * ``None`` (no hook, the flat ``costs.slippage_bps`` applies) -> ``None``, the doubled
      ``costs.slippage_bps`` is then the doubled flat slippage.

    ``costs.slippage_bps`` is doubled in every case: with a curve it is only the risk stub's sizing
    estimate, but keeping it consistent means a stress run can never double one and not the other. Any other
    model type raises ``TypeError`` (its scaling is unknown), rather than silently running unstressed.
    """
    f = Decimal(factor)
    if not f.is_finite() or f < 0:
        raise ValueError(f"stress factor must be a finite number >= 0, got {factor!r}")
    new_costs = costs.model_copy(
        update={
            "taker_fee_bps": costs.taker_fee_bps * f,
            "maker_fee_bps": costs.maker_fee_bps * f,
            "slippage_bps": costs.slippage_bps * f,
        }
    )
    model: SlippageModel | None
    if slippage_model is None:
        model = None
    elif isinstance(slippage_model, DepthCurveSlippage):
        model = slippage_model.scaled(f)
    elif isinstance(slippage_model, FlatSlippage):
        model = FlatSlippage(bps=slippage_model.bps * f)
    else:
        raise TypeError(
            f"cannot stress a {type(slippage_model).__name__}: only depth-curve and flat models scale"
        )
    return StressedCosts(new_costs, model)
