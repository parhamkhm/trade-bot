"""Builders for the depth-at-size slippage curve (SPEC section 7 G3 / D-067).

``DepthCurveSlippage`` itself (``tbot.execution.simulated``) takes its curve points as INPUT. This module
holds the one place that turns a measurement into points.

PROPOSED -- pending Parham's approval (D-067 construction). :func:`curve_from_g0_summary` implements the
PROPOSED construction of the provisional curve, not an approved one. The backtest engine does not use it by
default (``BacktestConfig.slippage_model`` stays ``None`` = flat bps); a harness must opt in explicitly, and
every number produced with it is PROVISIONAL.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from tbot.execution.simulated import CurvePoints, DepthCurveSlippage

__all__ = ["curve_from_g0_summary"]

_ZERO = Decimal(0)
_PCT_TO_BPS = Decimal(100)  # 1 % = 100 bps
_REFERENCE = "vs_touch"  # max_fill reference: measured from the best price consumed (SPEC 7 G3)
_FRACTION_TO_BPS = Decimal(10_000)  # a band given as a fraction of price: 0.001 = 10 bps


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
    statistic: Literal["median", "p10"] = "median",
    equity_normaliser: Decimal | None = None,
) -> DepthCurveSlippage:
    """PROPOSED -- pending Parham's approval (D-067 construction): curve points from the G0 probe summary.

    Reads ``book.max_fill`` and ``book.spread_pct`` of the LBank G0 probe summary JSON (for example
    ``research/reports/g0_lbank_20261011/lbank_probe_20261011T042800Z.summary.json``). ``max_fill[band]`` is
    the LARGEST MARKET ORDER whose volume-weighted fill price stays within ``band`` of the reference, measured
    FROM THE TOUCH (the ``vs_touch`` reference; ``vs_mid`` would include the half spread, which the model adds
    itself). Per side:

    * ``(0, 0)``;
    * ``(quote notional of the largest order within near_band (0.1 %), near_band in bps (10))``;
    * ``(quote notional of the largest order within far_band (0.5 %), far_band in bps (50))``.

    ``statistic`` picks the per-sample distribution statistic across the probe's samples: ``"median"``
    (default; what D-067 / SPEC section 7 G3 specify) or ``"p10"`` (the conservative sensitivity variant: the
    thin-book 10th-percentile sample).

    A BUY consumes the ASK side of the book, a SELL the BID side, so ``buy_points`` come from ``max_fill[..]
    ["buy"]`` and ``sell_points`` from ``["sell"]``. ``half_spread_bps`` = median spread / 2 in bps (the
    summary stores the spread in PERCENT of mid, hence ``* 100 / 2``; it is always the median spread, whatever
    ``statistic`` is).

    The ``book.depth`` section (depth WITHIN a band of mid) is deliberately NOT read: it is a different
    quantity and not the one SPEC section 7 G3 names. Caveat: in the real summary one of the 32 samples at the
    0.5 % band hit the end of the visible book (``truncated_samples``), so that figure is a lower bound.

    Numbers are parsed as exact ``Decimal`` (no float round trip).
    """
    if statistic not in ("median", "p10"):
        raise ValueError(f"statistic must be 'median' or 'p10', got {statistic!r}")
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
    try:
        book = data["book"]
        max_fill = book["max_fill"]
        raw_spread = book["spread_pct"]["median"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"{p}: not a G0 probe summary (need book.max_fill and book.spread_pct.median)"
        ) from exc
    spread_pct = _as_decimal(raw_spread, "book.spread_pct.median", p)
    if spread_pct < _ZERO:
        raise ValueError(f"{p}: book.spread_pct.median must be >= 0, got {spread_pct}")
    bands = [
        (near_band, Decimal(near_band) * _FRACTION_TO_BPS),
        (far_band, Decimal(far_band) * _FRACTION_TO_BPS),
    ]

    def side_points(side: str) -> CurvePoints:
        return (
            (_ZERO, _ZERO),
            *((_max_fill_quote(max_fill, band, side, statistic, p), bps) for band, bps in bands),
        )

    return DepthCurveSlippage(
        buy_points=side_points("buy"),
        sell_points=side_points("sell"),
        half_spread_bps=spread_pct * _PCT_TO_BPS / 2,
        equity_normaliser=equity_normaliser,
    )
