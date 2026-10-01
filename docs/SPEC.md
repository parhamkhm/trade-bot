# SPEC — contracts, configuration and pre-registered criteria

Status: **living document, owned by the orchestrator.** Sub-agents never edit this file or
`src/tbot/core/types.py`; they propose changes in their task report.
`CLAUDE.md` is the constitution (what and why); this file is the contract (exactly how).

Last updated: 2026-10-01 · Phase: 0–1 in progress.

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

Key defaults (phase 0): taker/maker fee 20 bps per side, slippage 5 bps per side, `requests_per_second: 5`,
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

1. The default loader returns only bars with `ts < holdout_start`.
2. Loading holdout data requires **both** an explicit `allow_holdout=True` argument **and** the
   environment variable `TBOT_UNSEAL_HOLDOUT=G4`; otherwise the loader raises.
3. Every unsealing appends a line to `research/HOLDOUT_LOG.md` (timestamp, caller, reason).
4. The holdout may be used **once**, at gate G4, after Parham's explicit written OK.

### 5.3 Data-quality report

`scripts/download_binance.py --report` writes `research/reports/data_quality_<date>.{json,md}` containing,
per symbol/timeframe: row count, coverage per month, missing bars with timestamps (and a classification,
`unknown` until investigated), duplicate timestamps, out-of-order timestamps, zero-volume bars,
bars where `high == low`, and |return| > 20 % outliers (1h) / > 40 % (1d).
"No unexplained gaps" means every missing bar is classified with a reason.

### 5.4 Tabdeal recorder

Tabdeal has no kline endpoint, so 1h candles are built from polled public `/trades`.

- SQLite at `data/tabdeal/trades.sqlite`:
  `trades(trade_id INTEGER PRIMARY KEY, ts_ms INTEGER, price TEXT, qty TEXT, is_buyer_maker INTEGER, recorded_ts_ms INTEGER)`
  (price/qty stored as TEXT to keep exact decimals),
  `poll_log(poll_ts_ms, first_id, last_id, n_trades, saturated INTEGER, http_status, latency_ms)`,
  `gaps(detected_ts_ms, from_id, to_id, reason)`.
- Dedupe by `trade_id` (primary key, `INSERT OR IGNORE`).
- `saturated = 1` when the response length equals the requested `limit` — a sign the polling interval is
  too long; this is both logged and alerted.
- Candles are written to `data/parquet/klines/source=tabdeal/symbol=BTCUSDT/timeframe=1h/...` with the same
  schema plus `complete BOOL` (false when a gap or saturation overlapped the bar) and `n_trades`.
  Resampling uses `label='right', closed='right'`; an hour with no trades produces **no bar** and a gap row —
  never a forward-filled bar.
- A candle for hour H is written only after `H_end + grace` (grace default 60 s) has passed.

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

**G1 — Data.** Binance 1h/4h/1d for BTCUSDT and ETHUSDT from 2018-01-01 to `holdout_start`:
every file checksum-verified, zero duplicate timestamps, zero out-of-order timestamps, and every missing
bar classified (no `unknown` left); 4h/1d values reconcile with 1h resampling within 1e-9.
Tabdeal recorder: ≥ 7 consecutive days with ≥ 99 % of hours complete, zero saturated polls, and the
Binance-vs-Tabdeal BTCUSDT basis measured (median and p95, in bps).

**G2 — Engine.** Truncation test passes exactly; a hand-computed 5-bar example matches the engine to the
cent; buy-and-hold and SMA-filter equity curves match an independent vectorbt run to ≤ 0.1 % final equity;
fees and slippage appear in the ledger and reduce returns by the expected amount.

**G3 — Strategy (out-of-sample, after costs).** `maxDD ≤ 0.60 × maxDD(buy-and-hold)`,
`Sharpe ≥ 0.80 × Sharpe(buy-and-hold)`, `PBO < 0.30`, Deflated Sharpe > 0 at the 95 % level using the trial
count from `research/EXPERIMENTS.md`; neighbouring parameter sets (±1 grid step) keep ≥ 70 % of the Sharpe
(no knife-edge optimum); ETH/USDT Sharpe > 0 with the same rules; fees and slippage ×2 keep the strategy
profitable net of costs.

**G4 — Risk + regime + holdout.** The regime overlay is kept only if it improves OOS Sharpe, or cuts maxDD
materially at equal Sharpe; otherwise it is dropped. The one-shot sealed-holdout run must land inside the
95 % stationary-block-bootstrap band for both Sharpe and maxDD. Run once, logged in `research/HOLDOUT_LOG.md`.

**G5 — Paper (8–12 weeks).** Zero unresolved state mismatches; for every bar, the live signal equals the
backtest signal computed on the same bars (100 % match); no unhandled exception; reconciliation clean after
each restart; alerting verified by fault injection.

**G6 — Small live (3 months).** Realised Sharpe and drawdown inside the expected band; automatic halt if
drawdown exceeds the 95th percentile of the bootstrap distribution. Capital scales only by a written rule.

---

## 8. Decisions log

| ID | Decision | Rationale |
|---|---|---|
| D-001 | Contracts implemented as frozen, self-validating dataclasses in `core/types.py` | invalid states unrepresentable; cheap to assert in tests |
| D-002 | `Bar.ts` is the **close** time; `open_ts` is derived | one convention everywhere kills a whole class of look-ahead bugs |
| D-003 | `Decimal` in execution/portfolio, `float` only in research/indicators | exact money math; `_dec()` rejects floats at runtime |
| D-004 | Annualisation 365 / 2190 / 8760 via `Timeframe.periods_per_year` | crypto trades every day (CLAUDE.md §3.4) |
| D-005 | Config = YAML (behaviour) + env `TBOT_*` (secrets); live orders need switch **and** phase ≥ 6 **and** mode live | one auditable gate for real money |
| D-006 | Added dependencies `pydantic-settings` and `PyYAML` beyond CLAUDE.md §7 | pydantic v2 moved settings into a separate package; YAML is the config format. Both are small and widely used. Flagged for Parham's approval |
| D-007 | `mypy` strict on `core/risk/execution`; tests may omit return annotations but their bodies are still checked | keeps test code readable without weakening production typing |
| D-008 | Sealed holdout is the **fixed date** `2025-10-01T00:00:00Z`, not a rolling 12-month window | a rolling window would silently reveal a new month every month and destroy the one-shot property |
| D-009 | Holdout is double-locked: `allow_holdout=True` **and** `TBOT_UNSEAL_HOLDOUT=G4`, every unsealing logged | accidental access must be impossible, not merely discouraged |
| D-010 | `round_price` and `floor_qty` both round **down** | never round up into funds we do not have or a price we did not intend |
| D-011 | Parquet stores OHLCV as `decimal128(38,12)`; research casts to float on load | exact storage, fast research |
| D-012 | ruff rule set includes `DTZ` (naive datetime) and `ANN` (missing annotations) | automated enforcement of the UTC rule |
| D-013 | `Clock` protocol injected; no `datetime.now()` in `core/`, `risk/`, `execution/` | deterministic backtests and testable live code |
| D-014 | `scripts/` is a Python package so `mypy` and `ruff` cover operational scripts too | the probe and recorder are production code |
| D-015 | Tabdeal trade prices/quantities stored as TEXT in SQLite | SQLite has no decimal type; TEXT round-trips exactly |

---

## 9. Open questions (need Parham or the probe to answer)

1. **Tabdeal fee tier** for the real account (maker/taker). Default 20 bps/side until measured — the probe's
   `account` response or the fee schedule should settle it.
2. **Real `exchangeInfo` filters** for BTCUSDT/ETHUSDT (tick, step, min-notional) — unknown until G0.
3. **Rate limits** (undocumented). Start at 5 req/s; the probe reports observed headers and any 429.
4. **Does any endpoint require `tabdealSymbol=BTC_USDT`** instead of `symbol=BTCUSDT`?
5. **Server static IP** for the API-key whitelist.
6. **`/trades` behaviour**: default and maximum `limit`, whether the window is id- or time-based — determines
   the recorder's polling interval.
7. Whether CI should run on GitHub-hosted runners for a private repo (minutes cost) or a self-hosted runner.
