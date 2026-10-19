# EXPERIMENTS — trial log

Every parameter set, variant and rule tried on research data is logged here, even if abandoned.
The row count is the trial count used for Deflated Sharpe and PBO, so omitting a trial is a
scientific error, not a convenience.

| # | date (UTC) | phase | strategy / variant | parameters | data range | timeframe | symbol | result (raw numbers) | notes |
|---|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | — | no experiments yet (phase 0–1) |
| S0 | 2026-10-10 | lock | S0 `sma100_1d` (SMA100 filter, σ̂ vol-target 30 %, fixed 3×ATR20 stop) | fixed in SPEC §7.1, D-063; no search | not yet run | 1d | BTCUSDT | none — registration only, no result computed | locked by Parham before any phase-3 result; lock content first committed in `8736334`; counts as one trial, never selected or tuned; robustness variants (SMA 50/75/150/200, vol target 20 %/40 %) are checks of S0, not extra candidates; first tradable close 2018-06-01 00:00 UTC (151 daily closes for σ̂, the longest S0 window; added 2026-10-19) |
| T1 | 2026-10-19 | 3 | `C1-(10,28,50)` Donchian ensemble | lookbacks (10, 28, 50) (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-06-01 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T2 | 2026-10-19 | 3 | `C1-(20,55,100)` Donchian ensemble | lookbacks (20, 55, 100) (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-06-01 00:00 UTC (SPEC §7.2 B eligibility table); base point; logged before the first run (SPEC §7.2 D) |
| T3 | 2026-10-19 | 3 | `C1-(40,110,200)` Donchian ensemble | lookbacks (40, 110, 200) (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-07-21 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T4 | 2026-10-19 | 3 | `C2-(15,45,90)` time-series momentum | horizons (15, 45, 90) days (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-06-01 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T5 | 2026-10-19 | 3 | `C2-(30,90,180)` time-series momentum | horizons (30, 90, 180) days (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-07-01 00:00 UTC (SPEC §7.2 B eligibility table); base point; logged before the first run (SPEC §7.2 D) |
| T6 | 2026-10-19 | 3 | `C2-(60,180,360)` time-series momentum | horizons (60, 180, 360) days (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-12-28 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T7 | 2026-10-19 | 3 | `C3-1d-(8,16,32)` EWMAC 1d | fast spans (8, 16, 32), slow = 4× fast (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2019-01-20 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T8 | 2026-10-19 | 3 | `C3-1d-(16,32,64)` EWMAC 1d | fast spans (16, 32, 64), slow = 4× fast (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2020-02-08 00:00 UTC (SPEC §7.2 B eligibility table); base point; logged before the first run (SPEC §7.2 D) |
| T9 | 2026-10-19 | 3 | `C4-p10` volatility-compression breakout | pct = 10 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2019-01-21 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T10 | 2026-10-19 | 3 | `C4-p20` volatility-compression breakout | pct = 20 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2019-01-21 00:00 UTC (SPEC §7.2 B eligibility table); base point; logged before the first run (SPEC §7.2 D) |
| T11 | 2026-10-19 | 3 | `C4-p30` volatility-compression breakout | pct = 30 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2019-01-21 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T12 | 2026-10-19 | 3 | `C5-x5` pullback mean-reversion | rsi_entry = 5 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-07-20 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |
| T13 | 2026-10-19 | 3 | `C5-x10` pullback mean-reversion | rsi_entry = 10 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-07-20 00:00 UTC (SPEC §7.2 B eligibility table); base point; logged before the first run (SPEC §7.2 D) |
| T14 | 2026-10-19 | 3 | `C5-x15` pullback mean-reversion | rsi_entry = 15 (SPEC §7.2 C, D-087, D-090) | not yet run (walk-forward 2018-06-01 → 2025-09-30, §7.2 B) | 1d | BTCUSDT (ETH/indices reuse BTC choices) | none — registration only, no result computed | first tradable close 2018-07-20 00:00 UTC (SPEC §7.2 B eligibility table); logged before the first run (SPEC §7.2 D) |

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
