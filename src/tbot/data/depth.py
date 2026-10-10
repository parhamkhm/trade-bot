"""Shared order-book / trade-window maths (decision D-025).

Used by both ``scripts/tabdeal_probe.py`` (the read-only exchange probe) and
``src/tbot/data/tabdeal_recorder.py`` (the live recorder). This is now the **single**
implementation of this maths -- previously the recorder imported it from the probe script, which
was the wrong dependency direction for a library module (`tbot.data`) to depend on an operational
script (`scripts/`). The probe re-exports these same names from here so its own public API and
tests are unaffected by the move.

``is_saturated`` (decision D-024): Tabdeal's ``/trades`` is a *recent-trades* endpoint -- it
returns the most recent ``limit`` trades, so ``count == limit`` holds on essentially every poll
once enough history exists, and by itself says nothing about whether any trade was actually
missed. It is kept here, and still recorded by the recorder for raw fidelity, but it is **not**
an alert and **not** a gate criterion. The real "are we close to losing trades" signal is
``coverage_ratio = window_span_seconds / poll_interval_seconds`` (computed in
``tbot.data.tabdeal_recorder``, not here, since it needs the poll interval); real data loss is a
trade-id gap, which is detected separately from the stored trade ids, not from this flag.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Literal

__all__ = [
    "best_bid_ask",
    "convert_irt_to_usdt",
    "cumulative_depth",
    "implied_price_from_legs",
    "is_saturated",
    "median_decimal",
    "parse_depth_levels",
    "percentile_decimal",
    "price_basis_bps",
    "spread_bps_and_pct",
    "to_decimal",
]


def to_decimal(value: str | Decimal | int) -> Decimal:
    """Coerce one price/qty-shaped value to ``Decimal`` without ever routing it through ``float``.

    Accepts ``str | Decimal | int``:

    * ``Decimal`` is returned as-is, never re-stringified. ``TabdealClient`` parses response JSON
      with ``json.loads(..., parse_float=Decimal)``, so a bare (unquoted) JSON number already
      arrives here as an exact ``Decimal``.
    * ``str`` is the shape Tabdeal's own docs show for prices/quantities (e.g. ``"61234.5"``) and
      is parsed via ``Decimal(str(value))``.
    * ``int`` goes through ``str()`` first too, which is exact for integers.

    ``float`` is deliberately not in the accepted type -- converting through it can silently
    corrupt a price or quantity's exact decimal representation, which is unacceptable for the
    money-path maths in this module and in ``tbot.execution``/``tbot.data.tabdeal_recorder``.

    Public (decision D-025 follow-up): this is also reused directly by
    ``tbot.data.tabdeal_recorder`` so the recorder does not need its own, second implementation of
    the same str/Decimal coercion.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# Private alias retained only so any external reference written against the old private name
# keeps working; new call sites (in this module and elsewhere) should use ``to_decimal``.
_to_decimal = to_decimal


def parse_depth_levels(raw: Any) -> list[tuple[Decimal, Decimal]]:
    """Parse one side of a ``/depth`` response (``[["price", "qty"], ...]``) into Decimal pairs.

    Skips malformed entries rather than raising -- one bad level must not abort the whole poll.
    M6: this includes entries that parse to a *valid but meaningless* Decimal -- NaN, +-Infinity,
    a non-positive price, or a negative quantity -- not just entries that fail to parse at all.
    ``best_bid_ask``/``spread_bps_and_pct`` have no defined behaviour for such values (NaN
    comparisons are never true, so a NaN price could silently vanish from min()/max(), and a
    negative price would corrupt a comparison), and the 7-day recorder poll loop and the probe
    must survive one malformed level either way.

    Each level's price/qty may be either a ``str`` (Tabdeal's documented shape) or an already-
    exact ``Decimal`` (what a bare JSON number now arrives as, see ``TabdealClient._safe_json``) --
    both are accepted losslessly.
    """
    if not isinstance(raw, list):
        return []
    out: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        try:
            price = to_decimal(level[0])
            qty = to_decimal(level[1])
        except InvalidOperation:
            continue
        if not price.is_finite() or not qty.is_finite():
            continue
        if price <= 0 or qty < 0:
            continue
        out.append((price, qty))
    return out


def best_bid_ask(
    bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]
) -> tuple[Decimal, Decimal] | None:
    """Return ``(best_bid, best_ask)``, or ``None`` if either side of the book is empty."""
    if not bids or not asks:
        return None
    return max(price for price, _ in bids), min(price for price, _ in asks)


def spread_bps_and_pct(best_bid: Decimal, best_ask: Decimal) -> tuple[float, float]:
    """Bid/ask spread of the mid, in basis points and in percent. Raises on a crossed book."""
    if best_ask < best_bid:
        raise ValueError(f"crossed book: ask {best_ask} < bid {best_bid}")
    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid
    ratio = float(spread / mid)
    return ratio * 10_000, ratio * 100


def cumulative_depth(
    levels: list[tuple[Decimal, Decimal]],
    mid: Decimal,
    pct_threshold: Decimal,
    *,
    side: Literal["bid", "ask"],
) -> tuple[Decimal, Decimal]:
    """Sum ``(base_qty, quote_notional)`` for one side of the book within ``pct_threshold`` of mid.

    ``side`` is ``"bid"`` (price <= mid) or ``"ask"`` (price >= mid). ``pct_threshold`` is a
    fraction, e.g. ``Decimal("0.001")`` for 0.1%.

    Raises:
        ValueError: if ``side`` is anything other than ``"bid"`` or ``"ask"`` (M14) -- mypy
            --strict catches this at type-check time via the ``Literal``, but a value smuggled in
            through ``Any``-typed JSON must still fail loudly rather than silently miscount.
    """
    if side not in ("bid", "ask"):
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
    bound = mid * pct_threshold
    base_total = Decimal("0")
    quote_total = Decimal("0")
    for price, qty in levels:
        distance = (mid - price) if side == "bid" else (price - mid)
        if distance < 0:
            continue
        if distance <= bound:
            base_total += qty
            quote_total += qty * price
    return base_total, quote_total


def is_saturated(requested_limit: int, returned_count: int) -> bool:
    """True when the response length is greater than or equal to the requested limit.

    Kept for raw fidelity only (D-024) -- on a *recent-trades* endpoint this is true on
    essentially every poll once enough history exists and is not, by itself, evidence of a
    missed trade. See the module docstring for the metrics that actually matter operationally.
    """
    return requested_limit > 0 and returned_count >= requested_limit


# ---------------------------------------------------------------------------------
# IRT cross-rate maths (Parham's request, G0 depth-sampling extension): BTCIRT and USDTIRT are
# measured for comparison only -- the traded pair stays BTCUSDT (CLAUDE.md section 2) -- but
# sizing an IRT-denominated book in USDT, and checking the implied BTC/USDT cross against the
# directly-quoted BTCUSDT price, both need a few more pure Decimal helpers. None of these ever
# raise on a non-positive divisor: a momentarily bad or missing USDTIRT mid must degrade one
# round's cross maths to ``None``, never abort an hours-long sampling run.
# ---------------------------------------------------------------------------------


def convert_irt_to_usdt(amount_irt: Decimal, usdt_irt_mid: Decimal) -> Decimal | None:
    """Convert an IRT-denominated amount (e.g. a BTCIRT book's quote-side notional) to USDT
    using a same-round USDTIRT mid price. ``None`` (never raises) when ``usdt_irt_mid`` is
    non-positive -- a zero or negative mid is not a valid exchange rate."""
    if usdt_irt_mid <= 0:
        return None
    return amount_irt / usdt_irt_mid


def implied_price_from_legs(leg_irt_mid: Decimal, usdt_irt_mid: Decimal) -> Decimal | None:
    """Implied USDT price of an IRT-quoted leg, e.g. ``implied_price_from_legs(btcirt_mid,
    usdtirt_mid)`` for the implied BTC/USDT cross rate. ``None`` (never raises) when
    ``usdt_irt_mid`` is non-positive."""
    if usdt_irt_mid <= 0:
        return None
    return leg_irt_mid / usdt_irt_mid


def price_basis_bps(observed: Decimal, reference: Decimal) -> float | None:
    """``(observed - reference) / reference`` in basis points. ``None`` (never raises) when
    ``reference`` is non-positive. Mirrors :func:`spread_bps_and_pct`'s float-at-the-last-step
    convention -- the comparison itself is exact Decimal, only the reported ratio is a float."""
    if reference <= 0:
        return None
    return float((observed - reference) / reference) * 10_000


def median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    """Exact Decimal median -- never routes through float. ``None`` for an empty sequence.

    An odd-length sequence returns its middle element unchanged (no arithmetic at all); an
    even-length one averages the two middle elements, which is exact because dividing an exact
    Decimal sum by 2 never loses precision.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def percentile_decimal(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    """Nearest-rank percentile (no interpolation) -- exact Decimal in, exact Decimal out.

    ``fraction`` is in ``[0, 1]`` (e.g. ``Decimal("0.10")`` for p10). Nearest-rank means every
    returned value is one of the inputs themselves, never an interpolated point between two of
    them -- appropriate here since "p10 of the fillable size across samples" means a specific,
    real, bad-but-common snapshot, not a synthetic blend of two different snapshots.
    ``None`` for an empty sequence.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = 1 if fraction <= 0 else min(n, int((fraction * n).to_integral_value(rounding=ROUND_CEILING)))
    return ordered[rank - 1]
