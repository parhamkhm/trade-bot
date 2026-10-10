"""STUB RiskManager for phase 2 -- NOT the real risk layer.

The real RiskManager (trading states, limits, kill switch, stop validation, vol-target sizing) is a phase 4
deliverable of the risk engineer. This stand-in exists only so the engine can run end to end through the
same ``RiskManager`` interface that live trading uses. It does exactly this and nothing else:

* long-only sizing of a target weight into a MARKET order, using only information at the signal-bar close;
* exchange filters: quantity floored to the step size, ``min_qty`` / ``min_notional`` refusals;
* affordability: a BUY is capped so that notional + fee + slippage + buffer fits into cash;
* a drift tolerance so a constant target (e.g. buy-and-hold at weight 1.0) does not re-trade every bar
  because of price drift. Entries from flat and exits to zero are never blocked by it. The real
  rebalance-band rule (SPEC section 7.1, S0) needs the strategy state and belongs to the phase 3/4 allocator.

It does NOT check stops, exposure, loss limits, data staleness or trading states.
"""

from __future__ import annotations

from decimal import Decimal

from tbot.core.types import (
    Approved,
    MarketState,
    OrderRequest,
    OrderType,
    PortfolioView,
    RefusalReason,
    Refused,
    RiskDecision,
    Side,
    TargetIntent,
)

__all__ = ["StubRiskManager"]

_BPS = Decimal(10_000)
_ZERO = Decimal(0)


class StubRiskManager:
    """Deterministic stand-in implementing the ``RiskManager`` protocol (see module docstring)."""

    def __init__(
        self,
        *,
        taker_fee_bps: Decimal,
        slippage_bps: Decimal,
        buy_buffer_bps: Decimal = Decimal(10),
        drift_tolerance: float = 0.02,
        id_prefix: str = "bt",
    ) -> None:
        # Cash needed per unit of notional at the signal close: slippage, fee on the slipped notional and a
        # safety buffer compound multiplicatively (fee is charged on the slipped fill value).
        self._cost_factor = (
            (1 + slippage_bps / _BPS) * (1 + taker_fee_bps / _BPS) * (1 + buy_buffer_bps / _BPS)
        )
        self._tolerance = drift_tolerance
        self._prefix = id_prefix
        self._seq = 0

    def evaluate(self, intent: TargetIntent, portfolio: PortfolioView, market: MarketState) -> RiskDecision:
        price, flt = market.last_price, market.filters
        equity = portfolio.equity_quote
        weight_now = portfolio.weight(price)
        target = Decimal(str(intent.target_weight))
        held_value = portfolio.base_qty * price
        gap_value = target * equity - held_value
        drift = abs(intent.target_weight - weight_now)
        if portfolio.base_qty > 0 and intent.target_weight > 0 and drift < self._tolerance:
            return Approved(intent, None, f"within drift tolerance (now {weight_now:.4f})")
        if gap_value > 0:
            side = Side.BUY
            budget = min(gap_value, portfolio.cash_quote / self._cost_factor)
            qty = flt.floor_qty(budget / price)
        elif gap_value < 0:
            side = Side.SELL
            qty = (
                portfolio.base_qty
                if intent.target_weight == 0
                else flt.floor_qty(min(-gap_value / price, portfolio.base_qty))
            )
        else:
            return Approved(intent, None, "target already met")
        return self._finish(intent, market, side, qty)

    def _finish(self, intent: TargetIntent, market: MarketState, side: Side, qty: Decimal) -> RiskDecision:
        flt, price = market.filters, market.last_price
        if qty <= _ZERO:
            return Refused(intent, RefusalReason.ZERO_QUANTITY, f"{side.value} sizes to zero after flooring")
        if qty < flt.min_qty:
            return Refused(intent, RefusalReason.MIN_QUANTITY, f"{qty} < min_qty {flt.min_qty}")
        if not flt.passes_notional(price, qty):
            detail = f"notional {price * qty} < min_notional {flt.min_notional}"
            return Refused(intent, RefusalReason.MIN_NOTIONAL, detail)
        self._seq += 1
        order = OrderRequest(
            client_order_id=f"{self._prefix}-{self._seq:06d}",
            symbol=market.symbol,
            side=side,
            type=OrderType.MARKET,
            quantity=qty,
            created_ts=intent.ts,
        )
        return Approved(intent, order, f"{side.value} {qty}")
