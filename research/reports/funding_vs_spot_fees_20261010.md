# Funding vs spot fees: BTC long perpetual vs spot (Binance funding, pre-holdout)

Sample {'start': '2020-01-01T00:00:00+00:00', 'end_exclusive': '2025-10-01T00:00:00+00:00', 'years': 5.753}; 6300 funding events. S0 proxy: close > SMA100 (1d), long/flat, weight 1.0, no stop.

- Funding paid by an always-long perpetual: **13.17 % / year**
- Funding paid only while S0 is long: **11.97 % / year** (time in market 62.7 %, 14.8 sides / year)

| perpetual leg | saving / side (bps) | fee saving % / yr | net extra cost of perp % / yr | breakeven sides / yr |
|---|---|---|---|---|
| perp_taker | 4.0 | 0.59 | 11.38 | 299 |
| perp_maker | 8.0 | 1.18 | 10.79 | 150 |

| year | funding always long % | funding while S0 long % | S0 in market % | S0 sides | 8 h periods > 0 % |
|---|---|---|---|---|---|
| 2020 | 17.24 | 18.17 | 81.7 | 15 | 85.7 |
| 2021 | 30.61 | 28.94 | 68.8 | 13 | 92.7 |
| 2022 | 4.16 | 0.56 | 7.1 | 8 | 77.9 |
| 2023 | 7.87 | 7.16 | 75.6 | 13 | 89.9 |
| 2024 | 11.96 | 11.03 | 76.0 | 18 | 91.6 |
| 2025 (partial) | 3.95 | 3.01 | 68.5 | 18 | 88.3 |
