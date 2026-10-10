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
