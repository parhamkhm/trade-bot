"""Tests for src/tbot/data/depth.py: the shared order-book / trade-window maths (decision D-025).

Pure functions only -- no network, no fixtures beyond plain Python literals. These are the single
implementation behind both ``scripts/tabdeal_probe.py`` (re-exported unchanged) and
``src/tbot/data/tabdeal_recorder.py`` (imported directly); ``tests/scripts/test_tabdeal_probe.py``
keeps exercising the probe's own behaviour through its re-exported names.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest

from tbot.data import depth

# ---------------------------------------------------------------------------------
# to_decimal (public helper, promoted from the former private ``_to_decimal`` so
# ``tbot.data.tabdeal_recorder`` can reuse it instead of duplicating the coercion)
# ---------------------------------------------------------------------------------


def test_to_decimal_is_exported_publicly() -> None:
    assert "to_decimal" in depth.__all__


def test_to_decimal_parses_str_exactly() -> None:
    assert depth.to_decimal("61234.56789012345") == Decimal("61234.56789012345")
    assert str(depth.to_decimal("61234.56789012345")) == "61234.56789012345"


def test_to_decimal_returns_decimal_input_as_is() -> None:
    value = Decimal("61234.56789012345")
    assert depth.to_decimal(value) is value


def test_to_decimal_accepts_int() -> None:
    assert depth.to_decimal(5) == Decimal("5")


# ---------------------------------------------------------------------------------
# parse_depth_levels
# ---------------------------------------------------------------------------------


def test_parse_depth_levels_parses_well_formed_pairs() -> None:
    assert depth.parse_depth_levels([["100.5", "1.25"], ["99", "2"]]) == [
        (Decimal("100.5"), Decimal("1.25")),
        (Decimal("99"), Decimal("2")),
    ]


def test_parse_depth_levels_skips_malformed_entries_without_raising() -> None:
    raw = [["100", "1"], "not-a-pair", ["only-one-element"], ["bad", "qty"], ["50", "0.5"]]
    assert depth.parse_depth_levels(raw) == [(Decimal("100"), Decimal("1")), (Decimal("50"), Decimal("0.5"))]


def test_parse_depth_levels_non_list_input_returns_empty() -> None:
    assert depth.parse_depth_levels(None) == []
    assert depth.parse_depth_levels("nope") == []


def test_parse_depth_levels_accepts_decimal_inputs_losslessly() -> None:
    """After the client-side JSON-parsing fix (``json.loads(..., parse_float=Decimal)`` in
    ``TabdealClient._safe_json``), a level's price/qty may already arrive as an exact ``Decimal``
    rather than a ``str``. Mixed Decimal/str levels in the same list must both parse exactly --
    full precision preserved, not approximately."""
    raw = [
        [Decimal("61234.56789012345"), Decimal("0.1")],
        ["50", "0.5"],
    ]
    assert depth.parse_depth_levels(raw) == [
        (Decimal("61234.56789012345"), Decimal("0.1")),
        (Decimal("50"), Decimal("0.5")),
    ]
    # Exact string round-trip, not pytest.approx -- a float detour would corrupt this value.
    parsed_price, parsed_qty = depth.parse_depth_levels(raw)[0]
    assert str(parsed_price) == "61234.56789012345"
    assert str(parsed_qty) == "0.1"


# ---------------------------------------------------------------------------------
# M6: NaN / Infinity / non-positive price / negative qty must be skipped, not raise
# ---------------------------------------------------------------------------------


def test_parse_depth_levels_skips_nan_price_and_qty() -> None:
    raw = [["NaN", "1"], ["100", "NaN"], ["50", "0.5"]]
    assert depth.parse_depth_levels(raw) == [(Decimal("50"), Decimal("0.5"))]


def test_parse_depth_levels_skips_infinite_price() -> None:
    raw = [["Infinity", "1"], ["-Infinity", "1"], ["50", "0.5"]]
    assert depth.parse_depth_levels(raw) == [(Decimal("50"), Decimal("0.5"))]


def test_parse_depth_levels_skips_non_positive_price() -> None:
    raw = [["0", "1"], ["-10", "1"], ["50", "0.5"]]
    assert depth.parse_depth_levels(raw) == [(Decimal("50"), Decimal("0.5"))]


def test_parse_depth_levels_skips_negative_qty() -> None:
    raw = [["100", "-1"], ["50", "0.5"]]
    assert depth.parse_depth_levels(raw) == [(Decimal("50"), Decimal("0.5"))]


def test_parse_depth_levels_malformed_values_never_reach_best_bid_ask_uncaught() -> None:
    """Regression for M6: a malformed level used to make best_bid_ask/spread raise inside the
    7-day recorder poll loop (and abort the probe before any report was written). It must now
    just be dropped and the rest of the book used normally."""
    bids = depth.parse_depth_levels([["NaN", "1"], ["100", "1"]])
    asks = depth.parse_depth_levels([["-5", "1"], ["101", "1"]])
    best = depth.best_bid_ask(bids, asks)
    assert best == (Decimal("100"), Decimal("101"))
    bps, pct = depth.spread_bps_and_pct(*best)
    assert bps > 0
    assert pct > 0


# ---------------------------------------------------------------------------------
# best_bid_ask / spread_bps_and_pct
# ---------------------------------------------------------------------------------


def test_best_bid_ask_picks_the_extremes_of_each_side() -> None:
    bids = [(Decimal("100"), Decimal("1")), (Decimal("99.6"), Decimal("2"))]
    asks = [(Decimal("101"), Decimal("1")), (Decimal("101.4"), Decimal("2"))]
    assert depth.best_bid_ask(bids, asks) == (Decimal("100"), Decimal("101"))


@pytest.mark.parametrize(
    "bids,asks",
    [
        ([], [(Decimal("101"), Decimal("1"))]),
        ([(Decimal("100"), Decimal("1"))], []),
    ],
)
def test_best_bid_ask_none_when_either_side_is_empty(
    bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]
) -> None:
    assert depth.best_bid_ask(bids, asks) is None


def test_spread_bps_and_pct_hand_computed() -> None:
    # mid = 100.5; spread = 1 -> bps = 1/100.5 * 10000, pct = 1/100.5 * 100
    bps, pct = depth.spread_bps_and_pct(Decimal("100"), Decimal("101"))
    assert bps == pytest.approx(99.502487562189, rel=1e-9)
    assert pct == pytest.approx(0.99502487562189, rel=1e-9)


def test_spread_bps_and_pct_raises_on_a_crossed_book() -> None:
    with pytest.raises(ValueError, match="crossed book"):
        depth.spread_bps_and_pct(Decimal("101"), Decimal("100"))


# ---------------------------------------------------------------------------------
# cumulative_depth -- hand-computed against a fixed fixture book
# ---------------------------------------------------------------------------------

# bids [[100,1],[99.6,2],[90,5]], asks [[101,1],[101.4,2],[110,5]]; mid = 100.5.
# 0.1% bound  = 0.1005 -> nothing within it on either side -> depth 0 / 0
# 0.5% bound  = 0.5025 -> bid@100 (dist .5) + ask@101 (dist .5) only -> 1 / 1
# 1%   bound  = 1.005  -> + bid@99.6 (dist .9, qty 2) + ask@101.4 (dist .9, qty 2) -> 3 / 3
_BIDS = [(Decimal("100"), Decimal("1")), (Decimal("99.6"), Decimal("2")), (Decimal("90"), Decimal("5"))]
_ASKS = [(Decimal("101"), Decimal("1")), (Decimal("101.4"), Decimal("2")), (Decimal("110"), Decimal("5"))]
_MID = Decimal("100.5")


@pytest.mark.parametrize(
    "threshold,side,expected_base",
    [
        (Decimal("0.001"), "bid", Decimal("0")),
        (Decimal("0.001"), "ask", Decimal("0")),
        (Decimal("0.005"), "bid", Decimal("1")),
        (Decimal("0.005"), "ask", Decimal("1")),
        (Decimal("0.01"), "bid", Decimal("3")),
        (Decimal("0.01"), "ask", Decimal("3")),
    ],
)
def test_cumulative_depth_hand_computed(
    threshold: Decimal, side: Literal["bid", "ask"], expected_base: Decimal
) -> None:
    levels = _BIDS if side == "bid" else _ASKS
    base_qty, _quote_notional = depth.cumulative_depth(levels, _MID, threshold, side=side)
    assert base_qty == expected_base


def test_cumulative_depth_quote_notional_is_base_times_price() -> None:
    # At the 1% threshold, bid side includes (100, 1) and (99.6, 2):
    # quote_notional = 100*1 + 99.6*2 = 299.2
    _base_qty, quote_notional = depth.cumulative_depth(_BIDS, _MID, Decimal("0.01"), side="bid")
    assert quote_notional == Decimal("299.2")


def test_cumulative_depth_rejects_invalid_side() -> None:
    """M14: ``side`` is typed Literal["bid", "ask"] for mypy, but a value smuggled in through
    Any-typed JSON must still raise, not silently miscount, at runtime."""
    with pytest.raises(ValueError, match="side must be 'bid' or 'ask'"):
        depth.cumulative_depth(_BIDS, _MID, Decimal("0.01"), side="buy")  # type: ignore[arg-type]


def test_cumulative_depth_skips_levels_on_the_wrong_side_of_mid() -> None:
    """A "bid" level priced above mid (or an "ask" below mid) must never be counted."""
    levels = [(Decimal("200"), Decimal("99"))]  # absurdly far above mid
    base_qty, quote_notional = depth.cumulative_depth(levels, _MID, Decimal("1"), side="bid")
    assert base_qty == Decimal("0")
    assert quote_notional == Decimal("0")


# ---------------------------------------------------------------------------------
# is_saturated
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,returned,expected",
    [
        (500, 500, True),
        (500, 499, False),
        (500, 0, False),
        (0, 0, False),  # a zero limit is never meaningfully "saturated"
    ],
)
def test_is_saturated(requested: int, returned: int, expected: bool) -> None:
    assert depth.is_saturated(requested, returned) is expected


def test_is_saturated_docstring_matches_implementation_ge_semantics() -> None:
    """M20: the docstring used to say "equals the requested limit" while the code used >=.
    returned > requested cannot happen against a real exchange, but the documented contract and
    the actual comparison operator must agree regardless."""
    assert "greater than or equal to" in (depth.is_saturated.__doc__ or "")
    assert depth.is_saturated(500, 501) is True
