# EXPERIMENTS — trial log

Every parameter set, variant and rule tried on research data is logged here, even if abandoned.
The row count is the trial count used for Deflated Sharpe and PBO, so omitting a trial is a
scientific error, not a convenience.

| # | date (UTC) | phase | strategy / variant | parameters | data range | timeframe | symbol | result (raw numbers) | notes |
|---|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | — | no experiments yet (phase 0–1) |

Non-trial computations (no return / Sharpe / drawdown computed, so not counted):

- 2026-10-10 — `research/funding_vs_spot_fees.py`: SMA100 (1d) long/flat **turnover and time in market only**,
  BTCUSDT 2020-01-01 → 2025-10-01, to cost the perpetual-vs-spot question (SPEC D-061). 14.8 sides/year,
  62.7 % in the market.
  - **Disclosure: statistics viewed** (pre-holdout only):
    - the proxy's time in market per calendar year (2020 81.7 %, 2021 68.8 %, 2022 7.1 %, 2023 75.6 %,
      2024 76.0 %, 2025 Jan–Sep 68.5 %) and its trades per year;
    - Binance BTCUSDT funding sums per year and overall (13.17 %/yr always long, 11.97 %/yr while the proxy
      was long);
    - the share of 8 h periods with positive funding (87.65 % overall, per-year 77.9–92.7 %).
  - **Effect on S0:** none. S0's form (SMA100 + vol target + ATR stop) was proposed in Parham's own pivot
    message and in the plan (`plan/lbank-pivot`, commit 358d1f7) before this computation, and it has no
    parameter search.
  - **Effect on the funding overlay (D-066):** these aggregates are exploratory statistics on the same Binance
    funding history the overlay thresholds will be set on. They are recorded so the overlay's design is judged
    knowing what was already seen. The overlay's thresholds must come from a pre-registered rule, not from
    these numbers.
