# Cross-project scoreboard

This template is filled **identically** by both projects: this one (Tabdeal BTC/USDT spot, long/flat) and the peer
project. Both use the **same start date** and the same definitions below, so the numbers are comparable. Fill
one copy per project per reporting date; never edit a past row, append a new dated block instead.

- **Start date (shared):** `YYYY-MM-DD` (00:00 UTC). Both projects agree on it before either fills a number.
- **Reporting date:** `YYYY-MM-DD`
- **Project / strategy id:** `…` (for this project: the S0/S1/candidate id from `docs/SPEC.md` §7.1)
- **Git commit of the code that produced the numbers:** `…`

## Definitions (identical for both projects)

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

## 1. Backtest — walk-forward out-of-sample only

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

## 2. Paper (automatic dry-run, no money)

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

## 3. Live

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

## 4. Instrument differences (must be stated next to any comparison)

| | This project | Peer project |
|---|---|---|
| Venue | Tabdeal (Iran) | `…` |
| Instrument | BTC/USDT **spot** | `…` (spot / perpetual) |
| Direction | long / flat only | `…` |
| Leverage | none (1×) | `…` |
| Funding / borrow | none | `…` (perpetual funding paid/received is included in net return) |
| Fee tier used | taker 35 bps / maker 33 bps per side (D-047) | `…` |
| Signal timeframe | 1d / 4h | `…` |
| Execution | 7a manual → 7b automated | `…` |
| Benchmark price series | Tabdeal BTCUSDT (Binance for backtest) | `…` |

A higher Sharpe on a leveraged or funding-earning perpetual is not the same achievement as on unlevered spot.
Compare drawdown, Calmar and net return after costs side by side, never Sharpe alone.
