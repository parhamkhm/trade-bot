"""STUB RiskManager for phase 2 -- NOT the real risk layer.

The real RiskManager (trading states, limits, kill switch, stop validation, vol-target sizing) is a phase 4
deliverable of the risk engineer. This stand-in exists only so the engine can run end to end through the
same ``RiskManager`` interface that live trading uses. It does exactly this and nothing else:

* long-only sizing of a target weight into a MARKET order, using only information at the signal-bar close;
* exchange filters: quantity floored to the step size, ``min_qty`` / ``min_notional`` refusals;
* affordability: a BUY is capped so that notional + fee + slippage (+ optional buffer, default 0) fits
  into cash at the signal close; the broker re-caps it at fill time from the real fill price;
* the SPEC section 7.1 (S0) rebalance rule: entries (flat -> invested) and exits (target 0) ALWAYS trade;
  vol-driven resizing trades only while invested, and only when
  ``|w_target - w_now| >= band * max(w_target, w_now)`` (default band 0.25, ``w_now`` = the weight drifted to
  the signal close). The comparison is done in exact ``Decimal`` arithmetic on the equity-scaled values, so
  the boundary (exactly 25 %) trades and 24.9 % does not, with no float noise. A band of 1.0 never resizes
  while invested (what the buy-and-hold benchmarks need). Protective stops are executed by the engine, not
  here, so they are never subject to the band.

It does NOT check exposure, loss limits, data staleness or trading states.
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
    """Deterministic stand-in implementing the ``RiskManager`` protocol (see module docstring).

    ``client_order_id`` scheme (``{id_prefix}-{seq:06d}``, a per-instance in-memory counter) is
    BACKTEST-ONLY. It has no meaning across process restarts and gives no idempotency guarantee for a
    live OMS, which must generate and persist its own ids (CLAUDE.md section 3.6: "every order carries
    a unique client_order_id; on timeout, query by that ID"). Do not reuse this id scheme in `live/`.
    """

    def __init__(
        self,
        *,
        taker_fee_bps: Decimal,
        slippage_bps: Decimal,
        buy_buffer_bps: Decimal = Decimal(0),
        rebalance_band: float = 0.25,
        id_prefix: str = "bt",
    ) -> None:
        if not 0.0 <= rebalance_band <= 1.0:
            raise ValueError("rebalance_band must be in [0, 1]")
        # Cash needed per unit of notional at the signal close: slippage, fee on the slipped notional and an
        # optional extra buffer (default 0; the broker caps a BUY at fill time anyway) compound
        # multiplicatively (fee is charged on the slipped fill value).
        self._cost_factor = (
            (1 + slippage_bps / _BPS) * (1 + taker_fee_bps / _BPS) * (1 + buy_buffer_bps / _BPS)
        )
        self._band = Decimal(str(rebalance_band))
        self._prefix = id_prefix
        self._seq = 0

    def evaluate(self, intent: TargetIntent, portfolio: PortfolioView, market: MarketState) -> RiskDecision:
        price, flt = market.last_price, market.filters
        equity = portfolio.equity_quote
        weight_now = portfolio.weight(price)
        target = Decimal(str(intent.target_weight))
        held_value = portfolio.base_qty * price
        gap_value = target * equity - held_value
        target_value = target * equity
        if (
            portfolio.base_qty > 0
            and target > 0
            and abs(gap_value) < self._band * max(target_value, held_value)
        ):
            return Approved(intent, None, f"within rebalance band (now {weight_now:.4f})")
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
