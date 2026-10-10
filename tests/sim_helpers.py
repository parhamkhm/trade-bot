"""Shared synthetic-data helpers for the portfolio / execution / backtest tests (no real data)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tbot.core.types import Bar, SymbolFilters, Timeframe

SYMBOL = "BTCUSDT"
T_START = datetime(2024, 1, 1, tzinfo=UTC)  # open of the first bar


def lbank_like_filters() -> SymbolFilters:
    """LBank-like BTCUSDT rules: price precision 2, quantity precision 5, min notional 1 USDT."""
    return SymbolFilters(
        symbol=SYMBOL,
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal(1),
    )


def make_bar(
    i: int,
    open_: str,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    tf: Timeframe = Timeframe.D1,
    start: datetime = T_START,
) -> Bar:
    """Bar number i (0-based): opens at start + i*tf and closes one timeframe later."""
    o, c = Decimal(open_), Decimal(close)
    hi = Decimal(high) if high is not None else max(o, c)
    lo = Decimal(low) if low is not None else min(o, c)
    return Bar(
        symbol=SYMBOL,
        timeframe=tf,
        ts=start + (i + 1) * tf.delta,
        open=o,
        high=hi,
        low=lo,
        close=c,
        volume=Decimal(10),
    )


def bars_from_closes(
    closes: Sequence[float], *, tf: Timeframe = Timeframe.D1, start: datetime = T_START
) -> list[Bar]:
    """Synthetic bars whose open is the previous close (first open = first close)."""
    out: list[Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(make_bar(i, f"{prev:.2f}", f"{c:.2f}", tf=tf, start=start))
        prev = c
    return out


def hour_bars_for_day(day_bar: Bar, opens: Sequence[str]) -> list[Bar]:
    """24 hourly bars covering day_bar (opens given; close = next open, last close = the day close)."""
    assert len(opens) == 24
    out: list[Bar] = []
    for h in range(24):
        o = Decimal(opens[h])
        c = Decimal(opens[h + 1]) if h < 23 else day_bar.close
        out.append(
            Bar(
                symbol=day_bar.symbol,
                timeframe=Timeframe.H1,
                ts=day_bar.open_ts + timedelta(hours=h + 1),
                open=o,
                high=max(o, c),
                low=min(o, c),
                close=c,
                volume=Decimal(1),
            )
        )
    return out
