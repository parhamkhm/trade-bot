# SPEC — contracts, configuration and pre-registered criteria

Status: **living document, owned by the orchestrator.** Sub-agents never edit this file or
`src/tbot/core/types.py`; they propose changes in their task report.
`CLAUDE.md` is the constitution (what and why); this file is the contract (exactly how).

Last updated: 2026-10-04 · Phase: 0–1 in progress.

---

## 1. Scope of this document

1. Core contracts, as implemented in `src/tbot/core/types.py`.
2. Configuration schema (`src/tbot/core/config.py`, `config/*.yaml`, `.env`).
3. Data contracts: Parquet store, dataset metadata, sealed holdout, quality report, Tabdeal recorder.
4. Directory layout and module ownership.
5. Testing and validation requirements.
6. Pre-registered gate criteria (G0–G6) — fixed **before** results are seen.
7. Decisions log.
8. Open questions.

---

## 2. Core contracts

All contracts live in `src/tbot/core/types.py` and are immutable frozen dataclasses with
validation in `__post_init__`. Invalid objects cannot be constructed.

### 2.1 Universal rules

| Rule | Enforcement |
|---|---|
| Every timestamp is timezone-aware UTC | `ensure_utc()` is called in every dataclass holding a `datetime`; naive or non-zero-offset values raise |
| A bar's `ts` is its **close** time | `Bar.open_ts` derives the open time; no code stores open-time-indexed bars |
| Money and quantities are `Decimal` in execution/portfolio code | `_dec()` raises `TypeError` on `float` |
| Weights/exposures are `float` in `[0, 1]` | `_unit_interval()` |
| Long or flat only | `TargetIntent.target_weight ∈ [0, 1]`; no type can express a short or leverage |
| No wall-clock reads in `core/`, `risk/`, `execution/` | `Clock` protocol is injected; ruff rule `DTZ` flags naive `datetime` usage |

### 2.2 Types

| Type | Purpose | Key invariants |
|---|---|---|
| `Timeframe` | `1h`, `4h`, `1d` | `delta`, `periods_per_year` = 8760 / 2190 / 365 (365-day year) |
| `Bar` | one closed OHLCV bar | prices > 0, volume ≥ 0, `high ≥ max(o,c,l)`, `low ≤ min(o,c,h)` |
| `BarWindow` | ordered closed bars up to `as_of` | non-empty, one symbol+timeframe, strictly increasing `ts`; `tail(n)` keeps the suffix |
| `SymbolFilters` | exchange rules | `round_price` (down to tick), `floor_qty` (down to step), `passes_notional` |
| `MarketState` | tradable snapshot | book may be absent; crossed book rejected; `spread_bps`, `age_seconds(now)` |
| `TargetIntent` | strategy output | weight ∈ [0,1]; `scaled(m)` applies the regime multiplier |
| `RegimeState` | overlay output | `exposure ∈ [0,1]`, never a strategy switch |
| `PortfolioView` | read-only portfolio snapshot | `weight(price)`; zero equity → weight 0 |
| `OrderRequest` | approved order | non-empty `client_order_id`, qty > 0, LIMIT⇔`limit_price` |
| `OrderAck`, `OrderStatus`, `Fill` | broker responses | `filled_quantity ≤ quantity`; `OrderState.is_open` / `.is_terminal` |
| `Approved` / `Refused` | risk decision (`RiskDecision`) | `Approved.order is None` means "already at target, do nothing"; `Refused.reason` is a `RefusalReason` enum |
| `TradingState` | kill switch | `ACTIVE` / `REDUCING` / `HALTED` |
| `RefusalReason` | machine-readable refusals | logged on every refusal; see enum for the full list |

### 2.3 Protocols (the only seams between backtest and live)

`Clock`, `Strategy`, `RegimeModel`, `RiskManager`, `Broker`, `DataFeed`.
Backtest and live **must** share `Strategy`, `RegimeModel`, `RiskManager` and the portfolio code;
only `DataFeed`, `Broker` and `Clock` implementations differ.

### 2.4 Flow and timing

```
closed bar t  →  RegimeModel.update(window)  →  Strategy.on_bar(window)  →  TargetIntent
              →  intent.scaled(regime.exposure)  →  RiskManager.evaluate(intent, portfolio, market)
              →  Approved(OrderRequest|None) | Refused(reason)  →  OMS  →  Broker
fill happens at the OPEN of bar t+1 plus slippage — never at the close of bar t.
```

### 2.5 Improvements over the CLAUDE.md §5 draft (all additive)

1. `Clock` protocol — removes `datetime.now()` from the deterministic path (backtest/live parity).
2. `SymbolFilters` as a first-class type with rounding helpers, so the backtest applies exactly the
   same tick/step/min-notional rules the live broker will.
3. `RefusalReason` enum instead of free-text — refusals become countable and alertable.
4. `OrderState`, `OrderStatus`, `OrderAck`, `Fill` fully specified (the draft only named them).
5. `Approved.order = None` for "no action needed" — avoids a third decision type.
6. `TargetIntent.scaled()` — the one sanctioned way to apply the regime multiplier.
7. `BarWindow.as_of` — an explicit causality anchor every validation test can assert against.

---

## 3. Directory layout

As in `CLAUDE.md` §4. Current state (phase 0–1):

```
config/default.yaml          # behaviour, no secrets
docs/SPEC.md                 # this file
docs/reports/                # Persian reports for Parham
src/tbot/core/               # types.py (contracts), config.py       [orchestrator-owned]
src/tbot/data/               # binance_loader, store, quality, tabdeal_recorder, candles
src/tbot/{indicators,strategies,regime,risk,execution,portfolio,backtest,validation,live,monitoring}/
scripts/                     # tabdeal_probe.py, download_binance.py, record_tabdeal.py
tests/                       # mirrors src/tbot
deploy/                      # Dockerfile, docker-compose.yml, systemd, SERVER_SETUP.md
research/                    # EXPERIMENTS.md (trial log), notebooks, reports/
```

Ownership: `core/types.py`, `core/config.py`, `docs/SPEC.md` → orchestrator only.

---

## 4. Configuration

Two layers, no overlap:

1. **`config/<name>.yaml`** (committed, no secrets) — validated by `Config` (`extra="forbid"`, frozen).
   Sections: `runtime`, `costs`, `data`, `exchange`, `risk`.
2. **Environment / `.env`** (never committed), prefix `TBOT_` — validated by `Secrets`:
   `TBOT_TABDEAL_API_KEY`, `TBOT_TABDEAL_API_SECRET`, `TBOT_TELEGRAM_BOT_TOKEN`,
   `TBOT_TELEGRAM_CHAT_ID`, `TBOT_LIVE_TRADING`, `TBOT_CONFIG`.

Secrets are `SecretStr`; they must never appear in logs, tracebacks, reports or Telegram messages.

**Live-order gate (single authority).** `Config.live_orders_enabled(secrets)` returns `True` only when
all three hold: `TBOT_LIVE_TRADING=true` **and** `runtime.phase >= 6` **and** `runtime.mode == "live"`.
Loading a config with `mode: live` and `phase < 6` is a validation error. No other code may decide this.

Key defaults: taker fee 35 bps, maker fee 33 bps per side (Tabdeal tier 1, D-047), slippage 5 bps per side, `requests_per_second: 5`,
`recv_window_ms: 5000`, `max_data_age_seconds: 900`, `annual_vol_target: 0.20`, `max_exposure: 1.0`.

---

## 5. Data contracts

### 5.1 Binance research store

Source: `https://data.binance.vision` monthly kline ZIPs (daily files only for the current partial month),
each verified against its published `.CHECKSUM` (SHA-256). Raw ZIPs are kept under `data/raw/` and are
never committed.

Layout (Hive-partitioned Parquet):

```
data/parquet/klines/source=binance/symbol=BTCUSDT/timeframe=1h/year=2021/part-0001.parquet
```

Schema (pyarrow):

| column | type | meaning |
|---|---|---|
| `ts` | `timestamp[us, tz=UTC]` | **close** time of the bar (Binance `close_time + 1ms`, normalised to the exact close instant) |
| `open`, `high`, `low`, `close` | `decimal128(38, 12)` | exact decimal values from the CSV, never parsed through `float` |
| `volume`, `quote_volume` | `decimal128(38, 12)` | base / quote volume |
| `trades` | `int64` | number of trades |

Rationale for `decimal128`: the CSV values are exact decimals; `float` parsing would silently change the
last digits of money values. Research code casts to `float64` on load (`as_float=True`); the backtest
builds `Bar` objects with exact `Decimal`s.

Each `symbol/timeframe` directory carries a sidecar `_dataset.json`:

```json
{"source": "binance", "symbol": "BTCUSDT", "timeframe": "1h", "rows": 70128,
 "first_ts": "2018-01-01T01:00:00Z", "last_ts": "2026-09-30T23:00:00Z",
 "holdout_start": "2025-10-01T00:00:00Z", "files": [{"name": "...zip", "sha256": "...", "verified": true}],
 "gaps": [{"from": "...", "to": "...", "missing_bars": 3, "classification": "exchange_outage"}],
 "downloaded_at": "2026-10-01T12:00:00Z", "tool_version": "0.1.0"}
```

### 5.2 Sealed holdout (double lock)

`holdout_start = 2025-10-01T00:00:00Z` — a **fixed calendar date**, not a rolling "last 12 months"
(decision D-008). Rules:

0. `holdout_start` is a **canonical constant in code** (`2025-10-01T00:00:00Z`), not merely a config field.
   The loader compares `DataConfig.holdout_start` against that constant and refuses to read (logging a
   `refused: holdout_start overridden` row) when they differ and both locks are not engaged. A YAML edit must
   never be able to unseal the holdout silently (decision D-026).
1. The default loader returns only bars with `ts < holdout_start`.
2. Loading holdout data requires **both** an explicit `allow_holdout=True` argument **and** the
   environment variable `TBOT_UNSEAL_HOLDOUT=G4`; otherwise the loader raises.
3. Every unsealing appends a line to `research/HOLDOUT_LOG.md` (timestamp, caller, reason).
4. The holdout may be used **once**, at gate G4, after Parham's explicit written OK.

### 5.3 Data-quality report

`scripts/download_binance.py --report` writes `research/reports/data_quality_<date>_<symbol>_<timeframe>.{json,md}`
(one report per series — decision D-027) containing,
per symbol/timeframe: row count, coverage per month, missing bars with timestamps (and a classification,
`unknown` until investigated), duplicate timestamps, out-of-order timestamps, zero-volume bars,
bars where `high == low`, and |return| > 20 % outliers (1h) / > 40 % (1d).
"No unexplained gaps" means every missing bar is classified with a reason.

### 5.4 Tabdeal recorder

Tabdeal has no kline endpoint, so 1h candles are built from polled public `/trades`.

- SQLite at `data/tabdeal/trades.sqlite`:
  `trades(trade_id INTEGER PRIMARY KEY, ts_ms INTEGER, price TEXT, qty TEXT, is_buyer_maker INTEGER, recorded_ts_ms INTEGER)`
  (price/qty stored as TEXT to keep exact decimals),
  `poll_log(poll_ts_ms, first_id, last_id, n_trades, n_items_received, saturated INTEGER, http_status,
  latency_ms, window_span_seconds, coverage_ratio)` — `n_items_received` separates "the exchange returned
  nothing" from "we received items and parsed none of them",
  `gaps(detected_ts_ms, from_id, to_id, reason, hour_close_ms)` — `hour_close_ms` identifies WHICH hour an
  empty-hour gap refers to, which G1b needs,
  `sweep_cursor` (how far the candle sweep has walked, so a trailing empty hour is not re-gapped on every
  poll — see D-029), `recorder_state(symbol, last_verified_poll_ts_ms)` (the candle sweep never passes the
  last poll that proved continuity or recorded a gap — D-038) and `meta(key, value)` (the symbol the database
  belongs to; a different symbol is refused — D-038).
  The database carries `PRAGMA user_version` and migrates an older file in place by adding missing columns
  (D-035); it never silently runs against a schema it does not understand.
- Dedupe by `trade_id` (primary key, `INSERT OR IGNORE`). All writes of one poll (trades, gap rows, poll log)
  commit in a single `BEGIN IMMEDIATE` transaction (D-038).
- **Trade ids are global across markets** (measured from the Turkey server, 2026-10-04 — D-037). Continuity is
  therefore proven by **window overlap**: a poll is continuous iff its lowest returned id is ≤ the last stored
  id. Otherwise a `window_no_overlap` gap is recorded and every hour it touches is `complete=False` (an
  already-written candle is rewritten). Per-symbol id contiguity is meaningless and never checked.
- **Saturation and data-loss risk (corrected, decision D-024 — provisional: observed from Parham's laptop, to be
  confirmed by the server probe).** Tabdeal's `/trades` appears to be a *recent trades*
  endpoint: it returns the most recent `limit` trades, so `count == limit` holds on essentially every poll
  and carries no information. `saturated` is still recorded for raw fidelity, but it is **not** an alert and
  **not** a gate criterion. The operational metrics are:
  * `coverage_ratio = window_span_seconds / poll_interval_seconds` — how much margin the returned window
    gives us. A poll is *at risk* when `coverage_ratio < 3`; that is what gets logged as a warning.
  * actual data loss — the returned window does not overlap what is stored (D-037). This writes a `gaps` row.
  Measured from the Turkey server (2026-10-04): `limit=1000` is accepted and covers ~29 h of BTCUSDT trades, so
  coverage is not the binding risk; the overlap rule is.
  `poll_log` therefore also stores `window_span_seconds` and `coverage_ratio`.
- Candles are written to `data/parquet/klines/source=tabdeal/symbol=BTCUSDT/timeframe=1h/...` with the same
  schema plus `complete BOOL` and `n_trades`. `complete=False` is driven by a `window_no_overlap` gap or an
  empty-hour gap overlapping the bar — never by the uninformative `saturated` flag (D-024).
  Resampling uses `label='right', closed='right'`; an hour with no trades produces **no bar** and a gap row —
  never a forward-filled bar.
- A candle for hour H is written only when **all** hold (D-041): `H_end + grace` (grace default 60 s) has passed
  on the clock capped at the last verified poll; and either a stored trade exists with `ts > H_end` (the feed
  has moved past the hour) or the last verified poll is ≥ `H_end + quiet_hour_timeout` (default 7200 s). The
  sweep stops at the first hour that fails, and never runs before any poll has been verified. An HTTP-200
  empty list after data exists is not a verified poll. Trades are rejected only as implausible
  (`ts < 1.5e12` ms or more than 5 min in the future), never for being old.
- Schema v5 (D-043): indexes on `gaps(hour_close_ms)` and `gaps(reason, rewritten)`; gap reason `late_trades` for a
  trade that arrives for an already-sealed hour (the hour is rewritten `complete=False`).
- Schema v4: `gaps.rewritten` — a `window_no_overlap` gap's rewrite of already-written candles is retried by
  every sweep until it succeeds (D-041). The process exits non-zero after 20 consecutive failed cycles so
  Docker restarts it.

---

## 6. Testing and validation requirements

Definition of Done for every task: `uv run pytest`, `uv run ruff check .`, `uv run mypy` all green
(`mypy` is strict on `tbot.core.*`, `tbot.risk.*`, `tbot.execution.*`), new code has tests, no secrets.

Required test kinds (phase-dependent):

- **Truncation test** (`validation/truncation.py`, phase 2): positions on `data[:N]` and `data[:N+k]` are
  bit-identical for all bars ≤ N.
- **Warm-up stability**: indicator values differ by ≤ 1e-9 after `warmup_bars` regardless of start index.
- **Property tests** (hypothesis) for rounding/risk invariants: never exceed 100 % exposure, never order in
  `HALTED`, never increase position in `REDUCING`, `floor_qty` never rounds up.
- **Network tests are mocked** (`respx`). No test may reach a real exchange; no test may send an order.

---

## 7. Pre-registered acceptance criteria (fixed before results)

These refine `CLAUDE.md` §9 into measurable checks. They may be tightened, never relaxed after seeing data.

**G0 — Foundations.** From the Turkey server: `ping` and `time` succeed; |server clock − local NTP clock| < 1 s;
`exchangeInfo` lists `BTCUSDT` with status `TRADING` and readable tick/step/min-notional filters;
median bid/ask spread over ≥ 30 samples spread across ≥ 6 h is < 0.20 %; a signed read endpoint
(`account`) answers 200 with the read-only key (key has no withdrawal permission and is IP-whitelisted).

**G1a — Research data (Binance).** 1h/4h/1d for BTCUSDT and ETHUSDT from 2018-01-01 to `holdout_start`:
every file checksum-verified, zero duplicate timestamps, zero out-of-order timestamps, and every missing
bar classified (no `unknown` left); 4h/1d values reconcile with 1h resampling within 1e-9; the timestamp
unit of every file is detected and normalised (see D-017). Source anomalies are handled per D-036.
*Pending Parham (D-040), not yet in force:* how to treat reconciliation mismatches that sit in Binance's own
files (see the phase 0–1 report). Until he decides, the criterion above applies unchanged.

**G1b — Live data (Tabdeal).** The recorder has run ≥ 7 consecutive days with ≥ 99 % of hours complete,
zero trade-id gaps (*proposed wording, pending Parham — D-037:* zero `window_no_overlap` gaps, because per-symbol
ids are never contiguous), and `coverage_ratio ≥ 3` on every poll (D-024), and the Binance-vs-Tabdeal BTCUSDT basis
is measured (median and p95, in bps) over that window. G1a and G1b are decided separately: phase 2 may start once G1a passes (decision D-016).

**G2 — Engine.** Truncation test passes exactly; a hand-computed 5-bar example matches the engine to the
cent; buy-and-hold and SMA-filter equity curves match an independent vectorbt run to ≤ 0.1 % final equity;
fees and slippage appear in the ledger and reduce returns by the expected amount.

**G3 — Strategy (out-of-sample, after costs).** `maxDD ≤ 0.60 × maxDD(buy-and-hold)`,
`Sharpe ≥ 0.80 × Sharpe(buy-and-hold)`, `PBO < 0.30`, Deflated Sharpe > 0 at the 95 % level using the trial
count from `research/EXPERIMENTS.md`; neighbouring parameter sets (±1 grid step) keep ≥ 70 % of the Sharpe
(no knife-edge optimum); ETH/USDT Sharpe > 0 with the same rules; fees and slippage ×2 (i.e. 70 bps taker fee + 10 bps slippage per
side, D-047) keep the strategy profitable net of costs. Every phase-3 report states annual turnover and the
annual cost drag (fees + slippage, % of equity per year) next to the returns.
Every G3 number is reported twice (D-051): fill at the next bar's open, and fill after the manual delay (default
6 h, slippage from recorded order-book snapshots). **The gate must pass on the manual-delay version**, because
phase 7a executes by hand. Every candidate implements `stop_price()` (D-050), and its report states MinTRL.

**G4 — Risk + regime + holdout.** The regime overlay is kept only if it improves OOS Sharpe, or cuts maxDD
materially at equal Sharpe; otherwise it is dropped. The one-shot sealed-holdout run must land inside the
95 % stationary-block-bootstrap band for both Sharpe and maxDD. Run once, logged in `research/HOLDOUT_LOG.md`.
The allocator acceptance rule (D-048) decides what runs: S0 unless a candidate beats both S0 and S1.

**G5 — Paper (8–12 weeks).** Zero unresolved state mismatches; for every bar, the live signal equals the
backtest signal computed on the same bars (100 % match); no unhandled exception; reconciliation clean after
each restart; alerting verified by fault injection.

**G6a — Small live, manual execution (phase 7a, 3 months).** Parham places every order by hand from the bot's
signal and logs each fill (price, qty, time). Realised results inside the expected band of the **manual-delay**
backtest; automatic halt (signals stop, Parham alerted) if drawdown exceeds the 95th percentile of the bootstrap
distribution. Measured slippage vs signal price is reported per trade. Three months is shorter than MinTRL for
any plausible strategy: G6a tests execution and parity, not edge, and cannot be used to switch strategies.

**G6b — Automated live (phase 7b, 3 months).** Same band and halt rule with automated execution (after 5b);
capital scales only by a written rule.

### 7.1 Allocator acceptance rule (phase 4, D-048)

Fixed before any phase-3 result.

- **S0 — locked default.** One strategy, fully specified below, locked on Parham's approval and never tuned on
  phase-3 results. *Proposed (pending Parham):* `donchian_ens_1d` on BTCUSDT daily bars —
  - three Donchian sub-signals with entry lookbacks N ∈ {20, 55, 100} days: long when the close exceeds the
    highest high of the previous N days, flat when the close falls below the lowest low of the previous N/2 days;
  - raw weight = mean of the three sub-signals (0, ⅓, ⅔ or 1);
  - volatility targeting: weight × min(1, 0.20 / σ̂), σ̂ = EWMA (span 30 days) annualised realised vol (365);
  - rebalance only when |target − current| ≥ 0.25 (turnover control: a round trip costs ≈ 0.8 %, D-047);
  - `stop_price` = the exit level of the longest active sub-signal (the lowest of the active N/2-day lows) — i.e.
    the price at which the whole position would be closed anyway.
  These are textbook parameters (no search), so S0 counts as one trial.
- **S1 — equal-weight blend.** Equal-weight average of the target weights of all strategies that passed G3,
  netted into one position, same 0.25 rebalance threshold.
- **S2 — champion/challenger.** A challenger replaces the running strategy only if (a) its trailing Deflated
  Sharpe beats the champion's by a margin fixed before the comparison (default ΔDSR ≥ 0.10, every variant
  counted as a trial), (b) it tripped no risk tripwire (REDUCING/HALTED, stop breach, drawdown halt) in the
  window, and (c) it holds — Sharpe > 0 after costs — in at least 2 of 3 regime buckets (realised-vol terciles).
- **S3 — regime-conditional selection.** Ablation only: reported, never deployed in v1. The regime overlay
  remains an exposure scaler and must beat "no overlay" out of sample (G4).
- **Go-live rule.** A candidate other than S0 goes live only if its walk-forward folds beat **both** S0 and S1
  after costs (manual-delay fills) and its Deflated Sharpe stays > 0 with every variant counted. Otherwise S0 (or
  S1, if S1 beats S0 under the same test) runs.
- **Switching.** A switch applies to new entries only (open positions finish under the rules they were opened
  with), the new choice is kept at least 3 months, and every switch needs Parham's written approval in
  `docs/reports/`.

---

## 8. Decisions log

| ID | Decision | Rationale |
|---|---|---|
| D-001 | Contracts implemented as frozen, self-validating dataclasses in `core/types.py` | invalid states unrepresentable; cheap to assert in tests |
| D-002 | `Bar.ts` is the **close** time; `open_ts` is derived | one convention everywhere kills a whole class of look-ahead bugs |
| D-003 | `Decimal` in execution/portfolio, `float` only in research/indicators | exact money math; `_dec()` rejects floats at runtime |
| D-004 | Annualisation 365 / 2190 / 8760 via `Timeframe.periods_per_year` | crypto trades every day (CLAUDE.md §3.4) |
| D-005 | Config = YAML (behaviour) + env `TBOT_*` (secrets); live orders need switch **and** phase ≥ 6 **and** mode live | one auditable gate for real money |
| D-006 | Added dependencies `pydantic-settings` and `PyYAML` beyond CLAUDE.md §7 | pydantic v2 moved settings into a separate package; YAML is the config format. **Approved by Parham 2026-10-01** |
| D-007 | `mypy` strict on `core/risk/execution`; tests may omit return annotations but their bodies are still checked | keeps test code readable without weakening production typing |
| D-008 | Sealed holdout is the **fixed date** `2025-10-01T00:00:00Z`, not a rolling 12-month window | a rolling window would silently reveal a new month every month and destroy the one-shot property. **Approved by Parham 2026-10-01** |
| D-009 | Holdout is double-locked: `allow_holdout=True` **and** `TBOT_UNSEAL_HOLDOUT=G4`, every unsealing logged | accidental access must be impossible, not merely discouraged |
| D-010 | `round_price` and `floor_qty` both round **down** | never round up into funds we do not have or a price we did not intend |
| D-011 | Parquet stores OHLCV as `decimal128(38,12)`; research casts to float on load | exact storage, fast research |
| D-012 | ruff rule set includes `DTZ` (naive datetime) and `ANN` (missing annotations) | automated enforcement of the UTC rule |
| D-013 | `Clock` protocol injected; no `datetime.now()` in `core/`, `risk/`, `execution/` | deterministic backtests and testable live code |
| D-014 | `scripts/` is a Python package so `mypy` and `ruff` cover operational scripts too | the probe and recorder are production code |
| D-015 | Tabdeal trade prices/quantities stored as TEXT in SQLite | SQLite has no decimal type; TEXT round-trips exactly |
| D-016 | Gate G1 split into G1a (Binance research data) and G1b (Tabdeal recorder + basis, ≥ 7 days) | the basis measurement needs a week of live recording; phase 2 must not wait for it. **Approved by Parham 2026-10-01** |
| D-017 | data.binance.vision spot files switched kline timestamps from ms to µs on 2025-01-01; the loader detects the unit per file by magnitude and normalises to UTC | mixing units silently shifts every bar of the recent history by orders of magnitude |
| D-018 | Fee default stays 20 bps per side until Parham supplies the real Tabdeal fee tier | conservative placeholder; the real tier only improves results |
| D-019 | The recorder also stores a periodic order-book snapshot (default every 60 s) | phase 3 needs a measured Tabdeal execution-cost model, not a guessed slippage number |
| D-020 | The |return| outlier threshold in the quality report is 20 % for 1h and 4h, 40 % for 1d | the 4h threshold was unspecified; 20 % is the more sensitive choice. These thresholds flag bars for inspection in the report — they are not a gate criterion and never drop data |
| D-021 | Only **complete monthly** Binance files are ingested; the current partial month (daily files) is not | research data stops at `holdout_start` = 2025-10-01, so the current month is irrelevant to research. Revisit only if live-vs-backtest comparison needs recent Binance bars |
| D-022 | Binance bar close time is derived as `open_time + timeframe.delta`, not read from the file's `close_time` column | the file's `close_time` carries a unit-dependent epsilon (-1 ms before 2025, -1 µs after); deriving it removes that trap |
| D-023 | `.gitignore` no longer blanket-ignores `*.csv` / `*.zip` repo-wide | the blanket rules silently hid legitimate test fixtures; bulk data is excluded by the anchored `/data/` rule instead |
| D-024 | "Saturated" (`count == limit`) is recorded but demoted: the real metrics are `coverage_ratio = window_span / poll_interval` (< 3 = at risk) and actual trade-id gaps | `/trades` behaved as a *recent trades* endpoint (most recent `limit` trades, so `count == limit` on every call), which would have made G1b unsatisfiable and every candle incomplete. **Provenance:** observed from Parham's Windows laptop (his local network), **not** from the Turkey server, with ad-hoc read-only calls to the public `/trades` endpoint during T1/T3 development (2026-10-01/02); no raw output was kept in the repo. **Status: provisional — to be confirmed by the server probe (G0).** The probe has not been run on the server yet. The demotion is safe either way: if the server probe shows a different behaviour, `saturated` is still recorded and can be re-promoted |
| D-025 | Shared order-book maths lives in `src/tbot/data/depth.py`, imported by both the probe script and the recorder | a library module importing from `scripts/` is the wrong dependency direction; one implementation keeps the phase-3 cost model consistent |
| D-026 | The holdout boundary is a **code constant**, and any config that disagrees with it is refused and logged | review found that a one-line YAML edit (or `--config my.yaml`) unsealed 12 months of sealed data with no lock, no log and no error — the seal must not be a configuration value |
| D-027 | The data-quality report is written per symbol/timeframe, not one file per date | one report per series is more useful than a merged one; SPEC updated to match the implementation rather than the reverse |
| D-028 | Any public read path that bypasses the holdout filter (`store.read_symbol_timeframe`, `candles.read_candles`) is made private or requires an explicit `holdout_start` argument | a friendly, unguarded second read path is what a future phase-2 author would reach for by accident |
| D-029 | The recorder keeps a `sweep_cursor` table in addition to the four documented tables | without it, a trailing empty hour is re-walked on every poll, writing ~2,160 duplicate gap rows per hour and degrading the overlap scan quadratically |
| D-030 | `DataConfig.holdout_start` is **documentation only**; the enforced boundary is `CANONICAL_HOLDOUT_START` in `binance_loader.py`, and a config that disagrees is refused | keeps one source of truth after D-026 while leaving the value visible where a reader looks for it |
| D-031 | Tabdeal response bodies are parsed with `parse_float=Decimal` | httpx's `.json()` turns JSON numbers into floats, rounding money before any `Decimal` conversion can preserve it |
| D-032 | Binance ingestion **stops at** `CANONICAL_HOLDOUT_START`; the sealed year is downloaded once, at G4 | previously the whole holdout year was written to `data/`, leaving a stray `pd.read_parquet` as the last way to see sealed bars. Not downloading it is a stronger seal than guarding the reader |
| D-033 | Secret redaction is **value-based**: the actual secret strings are registered and scrubbed from any rendered output, with the name/prefix patterns kept only as a second line of defence | a real Tabdeal key is a bare alphanumeric string, so pattern matching on `sk-`-style prefixes or `key=` shapes never catches it in a traceback |
| D-034 | stdlib `logging` is routed through the structlog pipeline (`ProcessorFormatter`), plus a scrubbing `sys.excepthook` | silencing httpx was a point fix for one known leaker; phase 5 adds python-telegram-bot, which logs bot-token URLs |
| D-035 | The recorder database carries `PRAGMA user_version` and an explicit migration path | `CREATE TABLE IF NOT EXISTS` silently skips new columns, which turned an existing database into a 5-second crash loop that the healthcheck could not see |
| D-036 | Binance source anomalies are classified per row instead of aborting the file: `misaligned` (off-grid open), `empty_irregular` (irregular close, 0 trades) and `long` (closes after its label) are dropped; `short` (on-grid, ends early, has trades) is stored at the normal label and flagged. Anomalies persist in `_dataset.json`; a gap overlapping one is `exchange_outage`. File-level errors still abort | the real 2018-01 file aborted ingestion. A full scan (175,188 rows) found 60 irregular-duration rows and a 42-hour off-grid run (2018-02-09..11). Dropping or labelling at-or-after the real end is causal; nothing is filled or interpolated |
| D-037 | Recorder continuity is proven by window overlap (lowest returned id ≤ last stored id), not by `first_id == last_stored_id + 1`; gap reason `window_no_overlap` | measured from the Turkey server on 2026-10-04 (curl to public `/trades`, limit 1000): BTCUSDT and ETHUSDT ids interleave in one global id space (median step 84 between consecutive BTCUSDT trades). The old rule would fire on every poll. **G1b wording change pending Parham** |
| D-038 | Recorder hardening after review: one SQLite transaction per poll; the candle sweep never passes the last verified poll (`recorder_state`); a `meta` table pins the database to one symbol; recorder-level exponential backoff (cap 300 s) after 5 consecutive errors; the heartbeat carries `last_new_trade_ts_ms` and the healthcheck fails after 7200 s without a new trade | review MAJOR-1..3 and m3/m4/m6. 7200 s because the measured max gap between BTCUSDT trades on Tabdeal was 2120 s |
| D-039 | **Proposed, pending Parham:** G1b's Binance-vs-Tabdeal basis needs Binance bars after `holdout_start`, which D-032 forbids downloading. Proposal: a separate live-comparison fetch (Binance REST, only the G1b window) into `data/live_compare/`, unreadable by the research loader and never used for strategy evaluation | the seal and G1b otherwise contradict each other (review m10) |
| D-040 | **Open, pending Parham:** G1a reconciliation fails on exactly 3 timestamps (2021-01-21, 2021-04-23, 2022-04-13), volume only, identical in BTC and ETH and in both 4h and 1d — Binance's own files disagree; OHLC reconciles everywhere outside outages | §7 says criteria may never be relaxed after seeing data, so this is Parham's call, not the orchestrator's |
| D-041 | Recorder review round 2: the candle sweep needs evidence the feed has passed the hour (a later trade, or a 2 h quiet timeout after a verified poll); `[]` after data is not verified; the 24 h age filter is replaced by a plausibility check; schema v4 self-healing rewrites; fatal exit after 20 failed cycles | round-2 MAJOR-A (age filter rejected ~17 % of every 29 h window), MAJOR-B (`[]` sealed hours with missing trades) and m-C (stale/cached responses, clock skew). Cost: a genuinely silent hour seals up to 2 h late |
| D-042 | Amends D-036: gap auto-classification is evidence-scoped — `exchange_outage` only when a *dropped* anomaly overlaps the missing-bar window `(from, to − Δ]`; `exchange_outage_after_short_bar` when a stored short bar's label equals the gap start; `exchange_wide_outage` when the identical window is missing in the other symbol from the same source. Every gap records `classified_by` (anomaly_overlap / after_short_bar / cross_symbol / manual); no rule overwrites a non-`unknown` classification. A raw close later than its label (beyond the 1-unit epsilon) is `long` and dropped | round-2 m-J: the first rule was broad enough to explain away unrelated gaps. Cross-symbol corroboration is evidence, not a guess: an exchange-wide stop hits every market at once |
| D-043 | Recorder review round 3: a trade arriving for an already-sealed hour records a `late_trades` gap and rewrites (or first writes) that hour's candle `complete=False`; an unparsable item's gap is anchored to its own hour and recorded once per (reason, hour); schema v5 adds indexes on `gaps(hour_close_ms)` and `gaps(reason, rewritten)`; backoff also counts consecutive cycle exceptions; the heartbeat carries a redacted `last_cycle_error`; `transaction()` refuses nesting; the plausibility clock is read after the HTTP response | round-3 MINOR-1..3, NIT-2..4: a feed frozen > 2 h could seal an hour with missing trades; one bad item marked ~29 h incomplete |
| D-044 | A Binance gap classification loaded without `classified_by` is `legacy` and is re-checked against the D-042 rules on every ingest (falls back to `unknown`); only an explicit `classified_by: manual` is never revisited. Close-time classification uses exact integer arithmetic in the file's own unit | round-3 MINOR-4 and NIT-1 |
| D-045 | Tabdeal `exchangeInfo` is a bare JSON list of markets (1047 on 2026-10-05, measured from the Turkey server), not Binance's `{"symbols": [...]}`; the probe accepts both. BTCUSDT filters: tick 0.01, step 0.000001, min notional 1 USDT, market max qty 8.84 BTC | the first server probe reported BTCUSDT "not found" because of the Binance-shaped parser |
| D-046 | The G0 probe also samples BTCIRT and USDTIRT depth each round, for comparison only (the traded pair stays BTCUSDT), reports fillable BUY/SELL size within 0.1 % / 0.5 % of mid (median and p10 across samples) and the implied BTC/USDT basis (BTCIRT/USDTIRT vs BTCUSDT) | Parham's request: execution capacity and the IRT market as context for G0 |
| D-047 | **Fees (supersedes D-018):** Tabdeal tier 1 (30-day volume < 1,000 USDT) taker 35 bps, maker 33 bps per side (tier 2: 35/31, tier 3: 33/28, tier 4: 31/26). Backtests assume taker unless a strategy explicitly uses limit orders; the G3 ×2 stress means 70 bps per side. Phase-3 reports must show turnover and annual cost drag. Whether USDT markets use the same table is still to verify | Parham's fee-table review (2026-10-06). A pre-registration update made **before any strategy result** exists — it tightens, never loosens: a round trip now costs ≈ 0.8 % (2 × 35 bps fee + spread/slippage), which leaves only slow, low-turnover variants realistic |
| D-048 | Allocator acceptance rule S0/S1/S2/S3 and the go-live rule (§7.1). S0's definition is **proposed, pending Parham's lock** | Parham, design additions from the peer project (2026-10-06). Locking a default before results is the only defence against picking the best of many backtests |
| D-049 | Phase order 0 → 1 → 2 → 3 → 4 → 5a (signal infra, no order code) → 6 paper → 7a small live with manual execution → 5b order code → 7b automated live | Parham (2026-10-06): order code is the riskiest code in the project; it is written only after a manual live period has proven the signals. The `runtime.phase` integer and the `mode: live` check in `core/config.py` are re-mapped to these labels in phase 5a (no order path exists before then) |
| D-050 | `Strategy.stop_price(window, entry_price) -> Decimal` is mandatory; registration fails without it; the RiskManager (phase 4) refuses a long whose stop is missing, non-positive or not below the price | Parham (2026-10-06). The stop rule is part of the strategy: its parameters are fixed in advance and every variant counts as a trial. In 7a the stop is placed by hand; exchange-side stops arrive with 5b (`docs/vendor/tabdeal-api-notes.md` §3) |
| D-051 | From phase 3 every backtest reports two fill models — next-bar open, and a configurable manual delay (default 6 h) with slippage from recorded order-book snapshots — and validation reports state Minimum Track Record Length per candidate; paper/live records shorter than MinTRL may not be used to choose between strategies | Parham (2026-10-06): phase 7a fills are manual and late; a strategy that only works with instant fills must not pass |
| D-052 | Order-code guardrail: a `PreToolUse` hook blocks edits under `src/` and `scripts/` introducing order/cancel/OCO/margin/withdrawal/`userDataStream` endpoints or exchange `POST`/`DELETE`; unlock only by Parham via `TBOT_ALLOW_ORDER_CODE=1`; the hook protects itself and `.claude/settings.json`; a pytest audit + CI step scan the same patterns | Parham (2026-10-06). The hook cannot see writes made through a shell, so the audit test and CI are the binding check |
| D-053 | PR #2 review round: the late-trade / sealed-hour cursor lives in SQLite only (`sweep_cursor`, advanced after each written candle and reconciled with Parquet once per sweep, outside the poll transaction) — no Parquet access on the trade-ingestion path. Every downstream step (candle sweep, post-commit candle rewrites, order-book snapshot, heartbeat write) is isolated: a failure is logged at error level and counted in the heartbeat's `consecutive_cycle_exceptions` (healthcheck fails at 3), never via backoff or process exit. An unparsable item's gap anchors to its own plausible time, else its parsed neighbours' hour span | CLAUDE.md §3.8 (raw data first). Review M-1: one unreadable Parquet part stopped all trade recording, and Tabdeal keeps only ~29 h of history |

---

## 9. Open questions (need Parham or the probe to answer)

1. ~~Tabdeal fee tier~~ — answered by Parham (D-047): tier 1, taker 35 / maker 33 bps. Still open: whether
   USDT-quoted markets use the same table.
2. **Real `exchangeInfo` filters** for BTCUSDT/ETHUSDT (tick, step, min-notional) — unknown until G0.
3. **Rate limits** (undocumented). Start at 5 req/s; the probe reports observed headers and any 429.
4. **Does any endpoint require `tabdealSymbol=BTC_USDT`** instead of `symbol=BTCUSDT`?
5. **Server static IP** for the API-key whitelist.
6. **`/trades` behaviour**: default and maximum `limit`, whether the window is id- or time-based — determines
   the recorder's polling interval.
   *Answered from the Turkey server (2026-10-04, curl):* recent-trades window; `limit=1000` accepted (~29 h of
   BTCUSDT); `time` in integer ms; ids global across markets (D-037). The full probe run will re-confirm.
7. Whether CI should run on GitHub-hosted runners for a private repo (minutes cost) or a self-hosted runner.
