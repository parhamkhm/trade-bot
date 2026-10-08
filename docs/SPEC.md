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
*Superseded by D-049 (phase order 5a → 6 → 7a → 5b → 7b): the automated order path is allowed only from 7b.
Phase labels are not numerically ordered, so phase 5a replaces the integer with an explicit ordinal enum; until
then no order path exists, so the integer gate cannot be reached.*
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
(no knife-edge optimum); ETH/USDT Sharpe > 0 with the same rules. *For S0* (one pre-registered configuration,
never selected) PBO is not applicable, and the neighbour test is fixed in advance as six one-at-a-time variants —
channel lookbacks N × 0.8 and N × 1.25 (rounded: {16, 44, 80} and {25, 69, 125}, exits ⌊N/2⌋), σ̂ span 20 and
40, vol target 0.15 and 0.25 — each logged as a sensitivity run (not a trial) and each keeping ≥ 70 % of S0's
Sharpe. *For S1* (no parameters of its own) PBO and the neighbour test are not applicable; every member must
have passed them; fees and slippage ×2 (i.e. 70 bps taker fee + 10 bps slippage per
side, D-047) keep the strategy profitable net of costs. Every phase-3 report states annual turnover and the
annual cost drag (fees + slippage, % of equity per year) next to the returns.
Every G3 number is reported twice (D-051): fill at the next bar's open, and fill after the manual delay. The
gating delay is **6 h, fixed**: the fill price is the open of the Binance 1h bar that starts 6 h after the signal
bar's close, adjusted by **adverse** slippage (buys fill higher, sells lower). The slippage model is a
depth-at-size curve: for each side, the median over all recorder order-book snapshots from the G1b start to the
day phase 3 starts (that window is then frozen) of the cost in bps of walking the book for a given notional,
**measured from the best bid/ask**, interpolated piecewise-linearly in notional and extrapolated beyond the
deepest recorded level at the slope of the last two points, evaluated at the trade's own notional with backtest
equity fixed at 10,000 USDT; half the median spread is then added exactly once. It is calibrated once, logged in `research/EXPERIMENTS.md`, and applied
unchanged to the whole 2018–2025 history; other delays are reported as sensitivity only. **The gate
must pass on the manual-delay version**, because phase 7a executes by hand. Every candidate implements
`stop_price()` (D-050) and its report states MinTRL — computed per Bailey & López de Prado on the **pooled OOS
manual-delay daily return series** (skew and kurtosis included) at 95 % confidence, against a benchmark Sharpe
of 0 and, separately, against buy-and-hold's Sharpe over the same period.

**G4 — Risk + regime + holdout.** The regime overlay is kept only if, on the pooled OOS manual-delay series,
its Sharpe is strictly higher than the same strategy without the overlay **and** its maxDD is not worse;
otherwise it is dropped. Every overlay variant tried is a trial in `research/EXPERIMENTS.md`. The keep/drop
decision is made before the gates and is part of the strategy's configuration. The one-shot sealed-holdout run must land inside the
95 % stationary-block-bootstrap band for both Sharpe and maxDD. Run once, logged in `research/HOLDOUT_LOG.md`.
What runs is decided only by the go-live rule in §7.1 (D-048, D-057); the holdout is applied there once, as a
pass/fail gate on a single walk-forward choice.

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

### 7.1 Allocator acceptance rule (phase 4, D-048, amended by D-054)

Fixed before any phase-3 result. Nothing in this section may be tuned after results are seen; every
parameter below is a fixed number.

**S0 — locked default.** One strategy, fully specified here, locked on Parham's approval and never tuned on
phase-3 results. *Proposed (pending Parham):* `donchian_ens_1d`, BTCUSDT, 1d bars (close = 00:00 UTC).

- *Sub-signals.* Three channels with entry lookback N ∈ {20, 55, 100} and exit lookback M = ⌊N/2⌋ ∈ {10, 27, 50}.
  At the close of bar t, sub-signal i becomes **long** if `close_t > max(high_{t−N} … high_{t−1})` (bar t
  excluded), and becomes **flat** if `close_t < min(low_{t−M} … low_{t−1})`; otherwise it keeps its state.
- *Anchored replay (the state has memory).* "Keeps its state" is hysteresis: the sub-signal state depends on the
  whole history, not on a fixed window. So the state is defined by **replay from a fixed anchor**: every
  sub-signal starts flat at `history_start` (2018-01-01 00:00 UTC; close #1 is the first bar opening there, which closes at 2018-01-02 00:00 UTC — a bar's `ts` is its close) and is updated on every
  Binance 1d bar from the first bar on which its channel can be evaluated (bar N+1). The backtest, **every**
  walk-forward fold (folds never restart the strategy; they only slice the replayed series) and the paper/live
  runner all replay every Binance 1d bar from that anchor up to bar t (≈ 3,000 bars; cheap). The same rule
  applies to **every** stateful strategy. In walk-forward with per-fold parameters, each fold's parameter set is
  replayed from the anchor; the jump from the previous fold's position to the new fold's position at the
  boundary is charged as a trade (fees + slippage). This — not
  `warmup_bars` — is what makes backtest and live agree on the same bars (G5).
- *Raw weight* `w_raw = (number of long sub-signals) / 3`.
- *Volatility (finite kernel, start-independent).* `r_t = ln(close_t / close_{t−1})`. With α = 2/31 (span 30) and
  K = 150: `σ̂_t = sqrt( Σ_{k=0}^{K−1} (1−α)^k · r_{t−k}² / Σ_{k=0}^{K−1} (1−α)^k ) × sqrt(365)` — the last 150
  returns up to and including t, exponentially weighted and renormalised. Because the kernel is finite, σ̂_t
  depends only on the last 151 closes, so the §6 warm-up test holds exactly for σ̂. `warmup_bars = 151` is the
  minimum before any trade: the weight is 0 until close #151 counted from `history_start` (close #1), i.e. the
  first tradable close is 2018-06-01 00:00 UTC. Sub-signal states are not covered by `warmup_bars`; they come
  from the anchored replay above.
- *Target.* `w_target = min(1, w_raw × min(1, 0.20 / σ̂_t))`.
- *Rebalance rule.* Let `w_now` be the actual (drifted) weight at the close of t.
  1. If the number of long sub-signals changed at t, trade to `w_target` (always; this includes every exit to 0
     and every new entry).
  2. Otherwise, **only if `w_now > 0`**, trade if `|w_target − w_now| ≥ 0.25 × max(w_target, w_now)` (25 %
     relative band; vol-driven resizing only). Rule 2 never opens a position from flat: after an execution-only
     stop-out (manual-delay 1h model or a real Tabdeal fill) while the signal state is still long, or when the
     live runner starts mid-trend, the position stays flat until rule 1 fires (the next change in the number of
     long sub-signals).
  3. A RiskManager-forced reduction or exit, and a stop-out, are never blocked by the band.
- *Protective stop.* `stop_price` = `min(low_{t−M} … low_{t−1})` of the **longest-lookback long** sub-signal (the
  lowest active exit level, i.e. where the whole position would go flat anyway). It raises `ValueError` when no
  sub-signal is long (there is no position to protect). Backtest stop model: the stop computed at the close of t
  triggers on bar t+1 if `low_{t+1} ≤ stop`, filling at `min(open_{t+1}, stop)` minus slippage. After a
  stop-out **all** sub-signals reset to flat and each needs a fresh breakout to re-enter. The stop-out (and so the
  reset) is defined **only** by this **daily** Binance-bar rule in every variant — the manual-delay 1h stop model
  below and real Tabdeal fills affect execution and P&L only, never the state — so the replayed state is the
  same in backtest, paper and live even when the Binance–Tabdeal basis makes the hand-placed stop fill
  differently; a real fill that disagrees is logged as an execution difference (G6a), not fed back into the
  signal.
  *Under the manual-delay variant (D-051)* the stop is only active once the position exists: from the delayed
  fill hour it triggers on the first 1h bar h after that point with `low_h ≤ stop`, filling at
  `min(open_h, stop)` with the adverse depth-at-size slippage; a new stop level computed at the close of t
  becomes effective 6 h after that close (the time Parham needs to move the hand-placed stop).
- *Fills.* Signal at the close of t, fill at the open of t+1 (and the manual-delay variant, D-051).

These are textbook parameters with no search, so S0 counts as **one trial**. At BTC's usual σ̂ (0.6–0.9) the
target is ≈ 0.22–0.33 of equity when fully long: S0 is deliberately a low-exposure, low-turnover strategy.

**S1 — equal-weight blend.** Equal-weight average of the target weights of every strategy that passed G3 (S0
included if it passed), netted into one position, with S0's rebalance rule (relative 25 % band; transitions
always traded). When S1 is compared with a candidate C, S1 is computed **without C** (leave-one-out). S1's trial
count for Deflated Sharpe is the sum of its members' trial counts.

**S2 — champion/challenger (from phase 7b only).** A challenger replaces the running strategy only if all hold
over the evaluation window = the **paper and live days after the challenger's lock date** (historical
walk-forward days all predate the lock and do not count), minimum 365 days:
(a) its DSR exceeds the champion's by **≥ 0.10** in DSR probability units (fixed, not a default), every variant
counted. Accepted consequence: once the champion's DSR is ≥ 0.90, no challenger can replace it — a strong
champion is never swapped out on paper/live evidence alone;
(b) it tripped no risk tripwire (REDUCING/HALTED, stop breach, drawdown halt) in the window;
(c) Sharpe > 0 after costs in at least 2 of 3 regime buckets — terciles of `σ̂` (the S0 estimator) with the two
cut-points computed **once** on the 2018-01-01 … holdout_start research data and frozen.

**S3 — regime-conditional selection.** Ablation only: reported, never deployed in v1. The regime overlay remains
an exposure scaler and must beat "no overlay" out of sample (G4).

**Deflated Sharpe (one definition for the whole project).** DSR is Bailey & López de Prado's deflated Sharpe
ratio expressed as a probability, `DSR = PSR(SR*) ∈ [0, 1]`. Inputs, all fixed:
- the return series is the **pooled OOS manual-delay daily return series** of the strategy being judged;
- Sharpe inside PSR and SR* is the **daily, non-annualised** Sharpe; T = the number of OOS days; PSR uses the
  skew and kurtosis of that series;
- SR* = the expected maximum of N independent daily Sharpes, where for **any candidate other than S0**, N = all
  non-S0 trials logged in `research/EXPERIMENTS.md` up to that candidate's lock date, **across all strategy
  types** (no "family" partitioning), and V[SR] = the variance of the daily Sharpe across exactly those trials,
  each computed on its own pooled OOS manual-delay series. Only S0 — locked before any result — uses N = 1, so
  SR* = 0 and DSR = PSR(0).
"Deflated Sharpe > 0 at the 95 % level" (G3) means **DSR ≥ 0.95**; every DSR threshold and margin in this
document is in these probability units.

**Go-live rule.**
- *Metric.* Annualised Sharpe (365, after all costs, **manual-delay fills**) of the **pooled** out-of-sample
  walk-forward return series (all folds concatenated).
- *Walk-forward gates* = G3 plus the **non-holdout** part of G4 (the regime-overlay decision). The holdout is
  never used to qualify or rank anything.
- *A candidate C qualifies* only if C passes the walk-forward gates, C's pooled OOS Sharpe is higher than both
  S0's and S1's (S1 leave-one-out without C), C's annualised manual-delay Sharpe beats S0's in at least half of
  the folds, and C's DSR ≥ 0.95 with
  every variant counted. S1 with no member other than C is not a contender.
- *The single choice X* is made on walk-forward results only: the qualifying C with the highest pooled OOS Sharpe;
  if none qualifies, the baseline (S0 or S1) with the higher pooled OOS Sharpe **among those that pass the
  walk-forward gates**; if neither passes, X = none and nothing goes live — the no-edge outcome of CLAUDE.md §1.
- *Holdout band (fixed, computed before unsealing).* Source series: X's pooled OOS manual-delay daily returns.
  Stationary bootstrap (Politis–Romano) with mean block length 20 days, path length = the number of days in the
  holdout, 10,000 resamples, seed 20261008. From the resampled paths, the 2.5th percentile of annualised Sharpe
  and the 97.5th percentile of maxDD. Both numbers, with the parameters above and the code commit, are written to
  `research/HOLDOUT_LOG.md` **before** the holdout is unsealed.
- *Holdout gate.* The holdout is unsealed **once**, for X only. X passes if its holdout Sharpe ≥ the 2.5th
  percentile **and** its holdout maxDD ≤ the 97.5th percentile (one-sided, adverse tails only: an unusually good
  holdout is reported but never fails). If X passes, X runs. If it does not, **nothing goes live** — there is no
  fallback to a second-best strategy, because choosing one after seeing the holdout would turn the sealed year
  into a selection set.
- *After G4: live bar store.* Live replay and the G5 comparison need Binance bars from `holdout_start` onward.
  After the single unsealing, those bars are ingested into a **separate live bar store** (`data/live_bars/`) used
  only by the paper/live runner and G5; the research loader never reads it, and the step is logged in
  `research/HOLDOUT_LOG.md`.

**Switching.** A switch applies to new entries only: an open position is managed to its exit by the strategy
that opened it (under S1 the netted position is treated as one position opened by S1), then the new strategy
takes over. The new choice is kept at least 3 months, and every switch needs Parham's written approval in
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
| D-054 | Amends D-048/D-051 after review: S0 fully specified (M = ⌊N/2⌋, σ̂ definition, warm-up, transitions always traded, a 25 % *relative* band only for vol-driven resizing, stop model and post-stop reset); go-live metric = pooled OOS Sharpe after costs with manual-delay fills; S0/S1 run only if they themselves pass G3 and G4, else no-go; S1 leave-one-out with summed trials; S2 margin, window and frozen vol-tercile cut-points fixed; gating manual delay fixed at 6 h with a calibrated depth-at-size slippage curve; MinTRL inputs fixed | T6 review M-4/M-5: with an absolute 0.25 band and vol targeting at ~0.22–0.33 weight, the proposed S0 could never exit; unnamed metrics and "otherwise S0 runs" left choices open after results and could deploy a strategy that failed G3 |
| D-055 | Amends D-052 after review: the guardrail's scope is any path with a `src`, `scripts`, `deploy` or `config` segment (worktrees and other checkouts included); the protected set is `.claude/settings.json`, `.claude/settings.local.json`, the whole `.claude/hooks/` directory, `tests/test_order_code_audit.py` and `.github/workflows/ci.yml`, never unlocked by `TBOT_ALLOW_ORDER_CODE`; the CI audit can never be skipped; verb literals, endpoint literals and SDK-style order methods are matched; read-only order queries for 7a reconciliation will need a reviewed allow-list that Parham edits by hand | T6 reviews M-1..M-3 and round-3 m-1/m-2: path, case and worktree bypasses, unprotected disarm files, and a realistic config-held endpoint slip |
| D-056 | Amends D-054: σ̂ is a finite 150-return exponential kernel (start-independent; `warmup_bars = 151`); one DSR definition (probability units, G3 threshold DSR ≥ 0.95); go-live picks the best qualifying candidate, which must itself pass G3/G4; baseline fallback order fixed; the single holdout unsealing evaluates S0, S1 and all qualifying candidates together and only gates the walk-forward choice; S2 window = post-lock paper/live days; manual-delay stop model, adverse depth-at-size slippage calibration window and MinTRL input series fixed | T6 round-3 review MAJOR-1/2 and m-4..m-6: an infinite-memory EWMA broke the warm-up test and live/backtest parity; several go-live choices were still open after results |
| D-057 | Amends D-054/D-056: S0's sub-signal state is defined by anchored replay from `history_start` (all flat at close #1; backtest, every walk-forward fold and the live runner replay every Binance 1d bar) — `warmup_bars = 151` is only the minimum before trading (first tradable close 2018-06-01); the stop-out reset follows the Binance-bar rule only; the manual-delay stop fills at `min(open_h, stop)` with adverse slippage; DSR inputs fixed (pooled OOS manual-delay daily series, daily non-annualised Sharpe, N and V[SR] from the logged trials, SR* = 0 for N = 1); the holdout is unsealed once for a single walk-forward choice X and only gates it — no fallback after a holdout failure; G4's summary now points to §7.1; slippage measured from the best quote. D-055's "endpoint literals are matched" is restored by the round-4 guardrail fix | T6 third review MAJOR-A/B, m-a, m-b, m-d and NITs: hysteresis made the Donchian state start-dependent beyond any fixed warm-up; qualifying on G4 while also gating on the holdout was circular |
| D-058 | Amends D-056/D-057 after the fourth T6 review: DSR trial count for any non-S0 candidate = all non-S0 trials up to its lock date across all strategy types (no family partitioning); G3 for S0 = six fixed one-at-a-time neighbour variants (sensitivity runs, not trials), PBO N/A for S0 and S1; holdout band fully specified (stationary bootstrap of X's pooled OOS manual-delay returns, block 20, 10,000 paths, seed 20261008, adverse-tail percentiles) and logged before unsealing; post-G4 live bar store outside the research loader; the daily Binance rule alone drives S0's state, rule 2 never re-enters from flat; anchored replay and charged fold-boundary trades for every stateful strategy; overlay keep rule numeric and overlay variants counted as trials; fold metric named | T6 fourth review MAJOR-S1/S2/S3 and m-1..m-4: each left a choice open after results (trial partitioning, an S0 that could never pass G3, a holdout band whose parameters could be picked after unsealing) |

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
