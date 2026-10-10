# COMPARISON — this project (LBank spot) vs the peer project (LBank perpetual)

Two parts:
- **Part A** is the scoreboard. Both projects fill it **identically**, per reporting date.
- **Part B** is the all-in cost of holding long BTC on each instrument: fees, spread and slippage, and
  funding.

## Part A — Cross-project scoreboard

This template is filled **identically** by both projects: this one (LBank BTC/USDT spot, long/flat) and the peer
project. Both use the **same start date** and the same definitions below, so the numbers are comparable. Fill
one copy per project per reporting date; never edit a past row, append a new dated block instead.

- **Start date (shared):** `YYYY-MM-DD` (00:00 UTC). Both projects agree on it before either fills a number.
- **Reporting date:** `YYYY-MM-DD`
- **Project / strategy id:** `…` (for this project: the S0/S1/candidate id from `docs/SPEC.md` §7.1)
- **Git commit of the code that produced the numbers:** `…`

### Definitions (identical for both projects)

| Metric | Definition |
|---|---|
| Net return | Total return after **all** costs: fees, spread/slippage, funding, borrow. Equity-curve based, not trade-sum based |
| CAGR | `(end_equity / start_equity) ** (365 / days) − 1` |
| Annualised volatility | Std of daily returns × √365 |
| Sharpe (365-day) | Mean daily return / std of daily returns × √365, risk-free = 0 |
| Sortino | Mean daily return / downside deviation (daily returns < 0) × √365 |
| Max drawdown | Largest peak-to-trough fall of the daily equity curve, % |
| Calmar | CAGR / \|max drawdown\| |
| % time in market | Share of days with non-zero exposure |
| Number of trades | Round trips (entry + exit) closed in the period; partial rebalances counted separately in a note |
| Avg measured slippage vs signal price | Mean of (fill price − signal price) / signal price, signed against the trade direction, in bps. Backtest: the model's assumption; paper: simulated; live: **measured** from real fills |
| Benchmark | The same metrics for **BTC buy-and-hold** over the same period, on the same venue's price series |

Daily returns are computed at 00:00 UTC on each project's own venue price. All rows are after costs.

### A1. Backtest — walk-forward out-of-sample only

| Metric | Strategy | BTC buy-and-hold |
|---|---|---|
| Period (OOS folds, first–last date) | | |
| Net return | | |
| CAGR | | |
| Annualised volatility | | |
| Sharpe (365) | | |
| Sortino | | |
| Max drawdown | | |
| Calmar | | |
| % time in market | | 100 % |
| Number of trades | | 1 |
| Avg slippage vs signal (bps, assumed) | | — |
| Fill model | next-bar open / manual delay 6 h (this project reports both, D-051) | |
| Deflated Sharpe / PBO / trial count | | — |
| MinTRL (months) | | — |

### A2. Paper (automatic dry-run, no money)

| Metric | Strategy | BTC buy-and-hold |
|---|---|---|
| Period | | |
| Net return | | |
| CAGR | | |
| Annualised volatility | | |
| Sharpe (365) | | |
| Sortino | | |
| Max drawdown | | |
| Calmar | | |
| % time in market | | 100 % |
| Number of trades | | 1 |
| Avg slippage vs signal (bps, simulated) | | — |
| Signal parity with backtest on the same bars | | — |

### A3. Live

| Metric | Strategy | BTC buy-and-hold |
|---|---|---|
| Period | | |
| Execution | manual (7a) / automated (7b) | |
| Net return | | |
| CAGR | | |
| Annualised volatility | | |
| Sharpe (365) | | |
| Sortino | | |
| Max drawdown | | |
| Calmar | | |
| % time in market | | 100 % |
| Number of trades | | 1 |
| Avg slippage vs signal (bps, **measured**) | | — |
| Total fees paid (actual commission from fills) | | — |

> Paper and live periods shorter than MinTRL do not support choosing between strategies (D-051). They are reported
> for execution quality and parity, not as evidence of edge.

### A4. Instrument differences (must be stated next to any comparison)

| | This project | Peer project |
|---|---|---|
| Venue | LBank | LBank (`…` if it changes) |
| Instrument | BTC/USDT **spot** | BTCUSDT USDT-margined **perpetual** (`…` confirm) |
| Direction | long / flat only | `…` |
| Leverage | none (1×) | 2× (`…` confirm) |
| Funding / borrow | none | `…` (perpetual funding paid/received is included in net return) |
| Fee tier used | spot VIP 0: taker 10 / maker 10 bps per side (D-060) | perpetual VIP 0: taker 6 / maker 2 bps (`…` confirm) |
| Signal timeframe | 1d / 4h | `…` |
| Execution | 7a manual → 7b automated | `…` |
| Benchmark price series | LBank BTCUSDT spot (Binance for backtest) | `…` |

A higher Sharpe on a leveraged or funding-earning perpetual is not the same achievement as on unlevered spot.
Compare drawdown, Calmar and net return after costs side by side, never Sharpe alone.

---

## Part B — All-in cost: LBank spot vs LBank perpetual

All-in cost comparison between the two ways of holding long BTC on LBank. This project trades **spot,
unleveraged, long/flat** (CLAUDE.md §2, SPEC D-061). The peer project trades the **USDT-margined BTCUSDT
perpetual at 2× leverage**. Every number here is a cost per year as a % of equity, so the two can be compared
like for like. Fees are verified; spread, slippage and LBank funding are still being measured (see
*Pending*).

### B1. Fee schedules (verified from Parham's LBank account, VIP 0, 2026-10-10)

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

### B2. Funding: the perpetual's cost that spot does not have

A long perpetual pays the funding rate every 8 h on its notional whenever the rate is positive, and receives
it when the rate is negative. The figures below are the **signed sum**, so periods where longs were paid are
already netted in.

Stand-in: Binance BTCUSDT USD-M funding, 2020-01-01 → 2025-10-01 (pre-holdout, 6,300 events), from
`research/funding_vs_spot_fees.py` → `research/reports/funding_vs_spot_fees_20261010.md`.

| year | funding paid by an always-long perpetual, % of notional | while the SMA100 proxy is long, % | proxy in market |
|---|---|---|---|
| 2020 | 17.2 | 18.2 | 82 % |
| 2021 | 30.6 | 28.9 | 69 % |
| 2022 | 4.2 | 0.6 | 7 % |
| 2023 | 7.9 | 7.2 | 76 % |
| 2024 | 12.0 | 11.0 | 76 % |
| 2025 (Jan–Sep) | 4.0 | 3.0 | 69 % |
| **per year, whole sample** | **13.2** | **12.0** | 63 % |

Longs paid in about 88 % of all 8 h periods. Funding is highest in bull trends, which is exactly when a
trend follower is long. That is why holding only while the proxy is long saves very little versus always long.

### B3. All-in cost per year at the SMA100 proxy's turnover

SMA100 proxy: close > SMA100 on 1d bars, long/flat, weight 1.0, no stop and no vol sizing (S0's signal
only, not S0). It made **14.8 sides a year**. Slippage
uses the provisional 5 bps per side until the LBank G0 probe measures it.

| cost line, % of equity per year | spot 1× (this project) | perpetual 1× | perpetual 2× * |
|---|---|---|---|
| fees (taker) | 1.48 | 0.89 | 1.78 |
| spread + slippage (provisional 5 bps) | 0.74 | 0.74 * | 1.48 * |
| funding while long (Binance stand-in) | 0.00 | 11.97 | 23.94 |
| **total** | **2.22** | **13.60** | **27.20** |

\* The perpetual has its own order book; its spread is not measured yet. The 2× column assumes the proxy's
turnover and exposure. It is **not** the peer project's actual cost, which depends on its own strategy.

The perpetual's fee saving (0.6 %/yr as taker, 1.2 %/yr as maker) is about a tenth of its funding bill.
The two break even only at **150–300 full-equity sides a year**, 10–20× the proxy's turnover. Leverage doubles every line.
It also adds liquidation and auto-deleveraging risk, which spot does not have and which this table does not
price. With volatility sizing (weight ≈ 0.4–0.5), funding scales with the average weight while rebalancing adds
some turnover, but even 10× the proxy's turnover stays below breakeven, so the ranking does not change.

### B4. Pending (fill in as measurements arrive)

- **LBank funding:** recorded from **2026-10-10 13:39:59 UTC** by the LBank recorder (`funding` table, every
  60 s). Once the LBank and Binance series overlap, report their correlation and mean difference here. The
  funding-overlay thresholds are defined on Binance history only and are never tuned on LBank data.
- **Spot spread and slippage:** from the LBank G0 probe (order-book snapshots from the Turkey server).
  Replaces the 5 bps placeholder.
- **Perpetual spread:** measured only if the peer comparison needs it. This project does not record the
  perpetual order book.
