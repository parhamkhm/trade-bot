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
from typing import Any

from tbot.execution.simulated import CurvePoints, DepthCurveSlippage

__all__ = ["curve_from_g0_summary"]

_ZERO = Decimal(0)
_PCT_TO_BPS = Decimal(100)  # 1 % = 100 bps
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


def _median_quote(depth: dict[str, Any], band: str, side: str, path: Path) -> Decimal:
    try:
        raw = depth[band][side]["quote_median"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"{path}: no book.depth[{band!r}][{side!r}]['quote_median'] in the G0 summary"
        ) from exc
    value = _as_decimal(raw, f"depth {band} {side} quote_median", path)
    if value <= _ZERO:
        raise ValueError(f"{path}: depth {band} {side} quote_median must be > 0, got {value}")
    return value


def curve_from_g0_summary(
    path: Path | str,
    *,
    near_band: str = "0.001",
    far_band: str = "0.005",
    equity_normaliser: Decimal | None = None,
) -> DepthCurveSlippage:
    """PROPOSED -- pending Parham's approval (D-067 construction): curve points from the G0 probe summary.

    Reads ``book.depth`` and ``book.spread_pct`` of the LBank G0 probe summary JSON (for example
    ``research/reports/g0_lbank_20261011/lbank_probe_20261011T042800Z.summary.json``). Per side:

    * ``(0, 0)``;
    * ``(median quote notional within near_band (0.1 %), near_band in bps (10))``;
    * ``(median quote notional within far_band (0.5 %), far_band in bps (50))``.

    A BUY consumes the ASK side of the book, a SELL the BID side, so ``buy_points`` use the ask medians and
    ``sell_points`` the bid medians. ``half_spread_bps`` = median spread / 2 in bps (the summary stores the
    spread in PERCENT of mid, hence ``* 100 / 2``).

    Caveats the caller must know (not hidden):

    * The probe measures depth within the band of MID, not from the touch. At the measured median spread
      (about 0.0012 bps) the two differ by far less than the rounding of the figures, so the points are
      used as given.
    * The 0.1 % and 0.5 % medians are almost the same notional (the book is thin beyond the first 0.1 %), so
      the 10 -> 50 bps segment is nearly vertical and the extrapolation beyond the last point is very steep.
      Orders far below the first point cost almost nothing.

    Numbers are parsed as exact ``Decimal`` (no float round trip).
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
    try:
        book = data["book"]
        depth = book["depth"]
        raw_spread = book["spread_pct"]["median"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{p}: not a G0 probe summary (need book.depth and book.spread_pct.median)") from exc
    spread_pct = _as_decimal(raw_spread, "book.spread_pct.median", p)
    if spread_pct < _ZERO:
        raise ValueError(f"{p}: book.spread_pct.median must be >= 0, got {spread_pct}")
    bands = [
        (near_band, Decimal(near_band) * _FRACTION_TO_BPS),
        (far_band, Decimal(far_band) * _FRACTION_TO_BPS),
    ]

    def side_points(side: str) -> CurvePoints:
        return ((_ZERO, _ZERO), *((_median_quote(depth, band, side, p), bps) for band, bps in bands))

    return DepthCurveSlippage(
        buy_points=side_points("ask"),
        sell_points=side_points("bid"),
        half_spread_bps=spread_pct * _PCT_TO_BPS / 2,
        equity_normaliser=equity_normaliser,
    )
