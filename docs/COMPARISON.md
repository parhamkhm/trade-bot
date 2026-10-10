# COMPARISON — LBank spot (this project) vs LBank perpetual (peer project)

All-in cost comparison between the two ways of holding long BTC on LBank. This project trades **spot,
unleveraged, long/flat** (CLAUDE.md §2, SPEC D-061). The peer project trades the **USDT-margined BTCUSDT
perpetual at 2× leverage**. Every number here is a cost per year as a % of equity, so the two can be compared
like for like. Fees are verified; spread, slippage and LBank funding are still being measured (see
*Pending*).

## 1. Fee schedules (verified from Parham's LBank account, VIP 0, 2026-10-10)

Per side, in % of notional.

| tier | spot taker | spot maker | perpetual taker | perpetual maker |
|---|---|---|---|---|
| **VIP 0 (ours)** | **0.10** | **0.10** | **0.06** | **0.02** |
| VIP 1 | 0.08 | 0.08 | 0.06 | 0.019 |
| VIP 2 | 0.07 | 0.065 | 0.04 | 0.016 |
| VIP 3 | 0.06 | 0.05 | 0.0375 | 0.014 |
| VIP 4 | 0.05 | 0.04 | 0.035 | 0.012 |
| VIP 5 | 0.04 | 0.03 | 0.032 | 0.01 |
| VIP 6 | 0.035 | 0.02 | 0.03 | 0.008 |
| SVIP | 0.03 | 0.00 | 0.02 | 0.00 |

Backtests use spot VIP 0 taker, 10 bps per side (SPEC D-060). The G3 ×2 stress uses 20 bps.

## 2. Funding: the perpetual's cost that spot does not have

A long perpetual pays the funding rate every 8 h on its notional whenever the rate is positive, and receives
it when the rate is negative. The figures below are the **signed sum**, so periods where longs were paid are
already netted in.

Stand-in: Binance BTCUSDT USD-M funding, 2020-01-01 → 2025-10-01 (pre-holdout, 6,300 events), from
`research/funding_vs_spot_fees.py` → `research/reports/funding_vs_spot_fees_20261010.md`.

| year | funding paid by an always-long perpetual, % of notional | while the S0 proxy is long, % | S0 proxy in market |
|---|---|---|---|
| 2020 | 17.2 | 18.2 | 82 % |
| 2021 | 30.6 | 28.9 | 69 % |
| 2022 | 4.2 | 0.6 | 7 % |
| 2023 | 7.9 | 7.2 | 76 % |
| 2024 | 12.0 | 11.0 | 76 % |
| 2025 (Jan–Sep) | 4.0 | 3.0 | 69 % |
| **per year, whole sample** | **13.2** | **12.0** | 63 % |

Longs paid in about 88 % of all 8 h periods. Funding is highest in bull trends, which is exactly when a
trend follower is long. That is why holding only while S0 is long saves very little versus always long.

## 3. All-in cost per year at S0's turnover

S0 proxy: close > SMA100 on 1d bars, long/flat, weight 1.0, no stop. It made **14.8 sides a year**. Slippage
uses the provisional 5 bps per side until the LBank G0 probe measures it.

| cost line, % of equity per year | spot 1× (this project) | perpetual 1× | perpetual 2× (peer) |
|---|---|---|---|
| fees (taker) | 1.48 | 0.89 | 1.78 |
| spread + slippage (provisional 5 bps) | 0.74 | 0.74 * | 1.48 * |
| funding while long (Binance stand-in) | 0.00 | 11.97 | 23.94 |
| **total** | **2.22** | **13.60** | **27.20** |

\* The perpetual has its own order book; its spread is not measured yet.

The perpetual's fee saving (0.6 %/yr as taker, 1.2 %/yr as maker) is about a tenth of its funding bill.
The two break even only at **150–300 sides a year**, 10–20× S0's turnover. Leverage doubles every line.
It also adds liquidation and auto-deleveraging risk, which spot does not have and which this table does not
price. Volatility targeting (weight ≈ 0.4–0.5) scales every line by the same factor, so the ranking does not
change.

## 4. Pending (fill in as measurements arrive)

- **LBank funding:** recorded from **2026-10-10 13:39:59 UTC** by the LBank recorder (`funding` table, every
  60 s). Once the LBank and Binance series overlap, report their correlation and mean difference here. The
  funding-overlay thresholds are defined on Binance history only and are never tuned on LBank data.
- **Spot spread and slippage:** from the LBank G0 probe (order-book snapshots from the Turkey server).
  Replaces the 5 bps placeholder.
- **Perpetual spread:** measured only if the peer comparison needs it. This project does not record the
  perpetual order book.
