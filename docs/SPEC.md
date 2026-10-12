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
   `TBOT_TABDEAL_API_KEY`, `TBOT_TABDEAL_API_SECRET` (legacy; the LBank key names are defined when the private probe is added, D-073), `TBOT_TELEGRAM_BOT_TOKEN`,
   `TBOT_TELEGRAM_CHAT_ID`, `TBOT_LIVE_TRADING`, `TBOT_CONFIG`.

Secrets are `SecretStr`; they must never appear in logs, tracebacks, reports or Telegram messages.

**Live-order gate (single authority).** `Config.live_orders_enabled(secrets)` returns `True` only when
all three hold: `TBOT_LIVE_TRADING=true` **and** `runtime.phase >= 6` **and** `runtime.mode == "live"`.
*Superseded by D-049 (phase order 5a → 6 → 7a → 5b → 7b): the automated order path is allowed only from 7b.
Phase labels are not numerically ordered, so phase 5a replaces the integer with an explicit ordinal enum; until
then no order path exists, so the integer gate cannot be reached.*
Loading a config with `mode: live` and `phase < 6` is a validation error. No other code may decide this.

Key defaults: taker fee 10 bps, maker fee 10 bps per side (LBank spot VIP 0, verified, D-060), slippage 5 bps per side
(provisional until the LBank G0 probe measures spread and slippage, D-060), `requests_per_second: 5`,
`recv_window_ms: 5000`, `max_data_age_seconds: 900`, `annual_vol_target: 0.30`, `max_exposure: 1.0`.

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

**G0 — Foundations (LBank, D-073).** Measured **from the Turkey server**, public endpoints only, ≥ 30 samples
spread across ≥ 6 h. Every number carries where and when it was measured.
- *Pass conditions:*
  - every spot v2 endpoint (`timestamp`, `accuracy`, `depth`, `supplement/trades`, `kline`, `ticker/24hr`) and
    the perpetual `marketData` answer 200. *Interpretation, fixed 2026-10-11 before any G0 data:* each endpoint
    returns HTTP 200 with a valid payload in **≥ 95 %** of its sampled requests (a timeout or transport error
    counts as a failure; timeout 20 s for `supplement/trades`, 10 s for the rest), and **no** 429/403/418 occurs
    at all. A ban-type response stops the run at once (no further requests) and G0 fails;
  - median |LBank server time − local NTP clock| < 1 s, and the offset measured at the minimum round trip
    is also < 1 s. When the median ± the median half round trip straddles 1 s, the result is *inconclusive*
    and counts as not passed;
  - *Dedicated clock re-test (D-079, registered 2026-10-11 after the sampling run's clock item was inconclusive,
    BEFORE the re-test code or any re-test data existed).* Run **once**, from the Turkey server, public
    `timestamp.do` only: **120 calls spaced 30 s apart over 1 hour**, one request in flight, local time read
    from the NTP-synced host clock immediately before and after each call. For each call: RTT = t_after −
    t_before, offset = server_ms − (t_before + t_after)/2. Select the **fastest 10 % by RTT** (12 calls);
    `offset` = the median of their offsets, `min_rtt` = the smallest RTT of all 120. **Pass iff
    |offset| + min_rtt / 2 < 500 ms.** Any non-200 or ban-type response is recorded; a 429/403/418 stops the
    run and it counts as failed; fewer than 108 valid calls (90 %) also counts as failed. **Pass ⇒ the G0
    clock item passes and G0 (public part) passes; fail ⇒ G0 stays failed.** The threshold is stricter than
    the 1 s above because the method removes most of the latency uncertainty; it must not be relaxed.
    *Operational meaning, fixed before the run (2026-10-11):* a valid call is HTTP 200 with a success payload
    and a millisecond server time; `min_rtt` and the fastest 10 % (`ceil(calls/10)` = 12) are taken over valid
    calls only, ties by call index (both choices can only make passing harder); a slot missed by more than
    min(5 s, spacing/2) is skipped, never made up, and counts as not valid;
  - **G0 result (final, Parham 2026-10-11): FAIL on the clock item, PASS on every other item.** Sampling run
    2026-10-11 04:28–10:40 UTC: endpoints 100 % valid, no ban response; `accuracy` 32/32; median spread one tick;
    clock inconclusive. Re-test (D-079, rule in commit 6dd5b07) 16:17:45–17:17:18 UTC: |offset| + min_rtt/2 =
    929.9 ms ≥ 500 ms ⇒ FAIL (120/120 valid calls; min RTT 1090 ms). G0 is never to be relabelled a pass. Its
    scope is clarified in D-083 (post-hoc); evidence in `docs/reports/g0-lbank-report.md`,
    `research/reports/g0_lbank_20261011/` and `research/reports/g0_lbank_clock_20261011/`;
  - `accuracy` lists `btc_usdt` with readable price/quantity precision, minimum quantity and minimum order value;
  - median best bid/ask spread < 0.20 %.
- *Reported, not gating:*
  - p50 and p95 latency per endpoint;
  - kline history depth for 1d, 4h, 1h and 1m;
  - depth within 0.1 %, 0.5 % and 1 % of mid (median and p10 per side);
  - the largest market order that fills within 0.1 % and 0.5 % at the median sample. This feeds the
    **temporary** depth-at-size curve (D-067).
- *Rate limits are probed conservatively.* The request rate rises step by step from the recorder's normal rate,
  and probing **stops at the first 429, 418, 403 or other ban-type response**. Headers and codes are recorded.
  The Turkey server is our only IP, so a ban must never be risked.
- *Private part* (a signed read of balances and the user's fee rate, with a read-only key): **postponed** until
  Parham creates a read-only LBank key from an account in his own name (no trading, no withdrawal,
  IP-whitelisted). It must pass before phase 7a. Until then no private call is made. The verified VIP 0 spot fee
  (D-060) stands.

*Legacy:* the Tabdeal G0 (public part passed 2026-10-08, `docs/reports/g0-report.md`) is historical.

**G1a — Research data (Binance).** 1h/4h/1d for BTCUSDT and ETHUSDT from 2018-01-01 to `holdout_start`:
every file checksum-verified, zero duplicate timestamps, zero out-of-order timestamps, and every missing
bar classified (no `unknown` left); 4h/1d values reconcile with 1h resampling within 1e-9; the timestamp
unit of every file is detected and normalised (see D-017). Source anomalies are handled per D-036.
*Pending Parham (D-040), not yet in force:* how to treat reconciliation mismatches that sit in Binance's own
files (see the phase 0–1 report). Until he decides, the criterion above applies unchanged.

**G1b — Live data (LBank, D-073).** The LBank recorder (D-068) has run **≥ 7 consecutive days** healthy:
- ≥ 99 % of hours have ≥ 99 % of their expected 10 s order-book snapshots;
- every trade poll overlaps the previous one in time (`gap = 0`). Any `gap = 1` or `gap = 2` poll is
  explained in the G1b report (restart, exchange outage);
- funding and klines have no stream silent beyond its healthcheck limit;
- every nightly archive and backup succeeded.

The Binance-vs-LBank BTC/USDT basis (median and p95, in bps, on 1h closes) is measured over the same window.
LBank bars come from the recorder; they are stored apart from research data and the research loader cannot read
them (D-074). The Binance side needs bars after `holdout_start`: a separate live-compare fetch limited to the
G1b window is **approved with conditions (D-039, D-074)**: Binance closes at the matching LBank timestamps only,
kept in `data/live_compare/`, never read by the research loader, not a trial.

G1a and G1b are decided separately: phase 2 may start once G1a passes (D-016). *Legacy:* the Tabdeal G1b
definition (D-024, D-037) is historical.

**G2 — Engine.** Truncation test passes exactly; a hand-computed 5-bar example matches the engine to the
cent; buy-and-hold and SMA-filter equity curves match an independent vectorbt run to ≤ 0.1 % final equity;
fees and slippage appear in the ledger and reduce returns by the expected amount.
- *Clarification made after seeing the result (Parham, GO on 2026-10-11, D-081).* The vectorbt comparison is
  run with **identical sizing assumptions on both sides**. The first validation compared the engine, which then
  held back a 10 bps cash buffer on every buy (a sizing policy decided before the run), with a naive all-in
  vectorbt run: B&H −0.075 %, SMA100 −0.144 % (over 0.1 %). Evidence that the gap was the buffer and not an
  engine error: feeding the engine's own orders into vectorbt agrees to 5.5e-6 (SMA100) and 7.8e-8 (B&H); the
  closed-form buffer holdback predicts −0.152 % against −0.144 % measured; with identical sizing the gap is
  −0.0013 % (SMA100) and −0.0005 % (B&H). **The official G2 vectorbt result is the identical-sizing run**
  (default engine, buffer 0, BTCUSDT 1d 2018-01-02 → 2025-09-30, fee 10 bps, slippage 5 bps, next open;
  laptop-measured); the literal-run numbers stay in `docs/reports/g2-phase2-validation.md`.

**G3 — Strategy (out-of-sample, after costs).** `maxDD ≤ 0.60 × maxDD(buy-and-hold)`,
`Sharpe ≥ 0.80 × Sharpe(buy-and-hold)`, `PBO < 0.30`, Deflated Sharpe > 0 at the 95 % level using the trial
count from `research/EXPERIMENTS.md`; neighbouring parameter sets (±1 grid step) keep ≥ 70 % of the Sharpe
(no knife-edge optimum); ETH/USDT Sharpe > 0 with the same rules. *For S0* (one pre-registered configuration,
never selected) PBO is not applicable, and the neighbour test is the six robustness variants fixed in §7.1
(SMA 50/75/150/200; vol target 0.20/0.40), each logged as a sensitivity run (not a trial) and each keeping
≥ 70 % of S0's Sharpe. *For S1* (no parameters of its own) PBO and the neighbour test are not applicable; every member must
have passed them. *For S0 and S1*: fees and slippage ×2 (i.e. 20 bps taker fee per side plus twice the §7 G3
slippage model, D-060) keep the strategy profitable net of costs. Every phase-3 report states annual turnover and the
annual cost drag (fees + slippage, % of equity per year) next to the returns.
Every G3 number is reported twice (D-051): fill at the next bar's open, and fill after the manual delay. The
gating delay is **6 h, fixed**: the fill price is the open of the Binance 1h bar that starts 6 h after the signal
bar's close, adjusted by **adverse** slippage (buys fill higher, sells lower). The slippage model is a
depth-at-size curve: for each side, the median over all recorder order-book snapshots from the G1b start to the
day G1b passes (that window is then frozen; D-085) of the cost in bps of walking the book for a given notional,
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
decision is made before the gates and is part of the strategy's configuration. The one-shot sealed-holdout run must pass the band defined in §7.1 (stationary
bootstrap, adverse tails only: Sharpe ≥ 2.5th percentile and maxDD ≤ 97.5th percentile). Run once, logged in `research/HOLDOUT_LOG.md`.
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

**S0 — locked default.** One strategy, fully specified here, never tuned on phase-3 results.
**LOCKED by Parham on 2026-10-10 (D-063).** The lock commit is recorded in `research/EXPERIMENTS.md`. Every
number below is fixed.

`sma100_1d`, BTCUSDT, Binance 1d bars (a bar's `ts` is its close, 00:00 UTC).

- *Indicators (all finite and start-independent; a value at close t uses closes up to and including t).*
  - `SMA100_t` = the mean of `close_{t−99} … close_t`.
  - `ATR20_t` = the **simple** mean of the last 20 true ranges up to and including t,
    `TR_k = max(high_k − low_k, |high_k − close_{k−1}|, |low_k − close_{k−1}|)`. This is **not** Wilder's
    ATR, whose memory is infinite.
  - `σ̂_t` = the finite 150-return kernel of D-056: `r_t = ln(close_t / close_{t−1})`, α = 2/31 (span 30),
    K = 150, `σ̂_t = sqrt( Σ_{k=0}^{K−1} (1−α)^k · r_{t−k}² / Σ_{k=0}^{K−1} (1−α)^k ) × sqrt(365)`.
- *State machine.* The state is FLAT, LONG or STOPPED, updated on every Binance 1d bar by **anchored replay**
  (below).
  - **FLAT → LONG** at close t if `close_t > SMA100_t` and t is a tradable close (warm-up below).
  - **LONG → FLAT** at close t if `close_t ≤ SMA100_t`.
  - **LONG → STOPPED** on bar u (u ≥ t+1, *including the entry bar t+1*) if `low_u ≤ stop`. The stop-out
    happens inside bar u, before its close is evaluated.
  - **STOPPED → FLAT** at the first close `≤ SMA100`. This includes the close of the stop-out bar u itself:
    the close comes after the intraday stop.
  - **Re-entry after a stop-out** therefore needs a close `≤ SMA100` and then a fresh close `> SMA100`. A
    trend that never dips back to the SMA is not re-entered.
  - If the stop and an exit signal fall on the same bar, the stop comes first, then the close moves the state
    to FLAT. The final state is the same either way.
- *Protective stop (Q1: FIXED, not trailing; D-050).*
  - **Level:** `stop = P_ref − 3 × ATR20_t`, where t is the entry signal bar and `P_ref` = the **Binance open of
    bar t+1**, i.e. the next-open entry price before slippage. The stop stays fixed for the whole position.
    If `P_ref − 3 × ATR20_t < 0.01`, the stop is 0.01.
  - **Why `P_ref` and not each model's own fill price (clarifies Parham's "entry fill price").** In the
    next-open model the two differ only by the entry slippage, a few bps. Using `P_ref` makes the stop level,
    and therefore the stop-out state, identical in backtest, paper and live, whatever the fill model, the
    slippage or the real LBank fill (G5 parity).
  - **Daily stop fill (next-open model).** On bar u with `low_u ≤ stop`, the fill is `min(open_u, stop)` minus
    adverse slippage. A gap below the stop fills at the open, and the fill is never better than the stop.
  - **Manual-delay variant (D-051).** The stop level is the same. It is active from the delayed entry fill
    and is checked on 1h bars from that hour on: the first 1h bar h with `low_h ≤ stop` fills at
    `min(open_h, stop)` minus slippage. The stop-out **state** is still defined only by the daily rule above.
    Execution never feeds back into the state:
    - if the state is STOPPED but the delayed position was not stopped (e.g. the daily low came before the
      delayed fill), the target is 0 and the position exits 6 h after the close of the stop-out bar;
    - if the 1h execution stopped out while the state is still LONG, the position stays flat until the
      state's next FLAT → LONG transition (the engine's re-arm rule).
  - **Live parity requirement (phase 5).** The backtest fills stops at the stop level intraday. Live must
    therefore either keep an exchange-side stop resting on LBank or watch the price intraday (≤ 1 min) and
    send a market sell. Checking only at the daily close would fill far worse than the backtest assumes.
    Whether LBank spot offers stop orders is verified when the private API is in use. Until then the bot
    plans for intraday monitoring.
- *Sizing.* `w_target = min(1, 0.30 / σ̂_t)` while the state is LONG, otherwise 0. At BTC's usual σ̂ (0.6–0.8)
  this is ≈ 0.375–0.5 of equity.
- *Rebalance rule.* Let `w_now` be the actual (drifted) weight at the close of t.
  1. Entries (FLAT → LONG) and exits (to FLAT or STOPPED) **always** trade.
  2. Vol-driven resizing trades only while in a position (`w_now > 0`), and only if
     `|w_target − w_now| ≥ 0.25 × max(w_target, w_now)` (25 % relative band).
  3. A RiskManager-forced reduction or exit, and a stop-out, are never blocked by the band.
- *Fills.* A signal at the close of t fills at the open of t+1 plus slippage. The manual-delay variant (D-051,
  6 h) fills at the open of the Binance 1h bar that starts 6 h after the close of t.
- *Warm-up and anchored replay.* `warmup_bars = 151` (σ̂ needs 151 closes; SMA100 needs 100; ATR20 needs 21).
  The first tradable close is close #151 counted from `history_start`, i.e. 2018-06-01 00:00 UTC. Close #1 is
  the bar opening at 2018-01-01 00:00 UTC.
  - The state has memory (STOPPED, and the fixed stop of an open position), so it is defined by **replay from
    the anchor** `history_start` = 2018-01-01. The state starts FLAT and every Binance 1d bar up to t is
    replayed.
  - The backtest, **every** walk-forward fold, and the paper/live runner do this identically. Folds never
    restart the strategy; they only slice the replayed series. This, not `warmup_bars`, is what makes
    backtest and live agree on the same bars (G5).
  - The same replay rule applies to **every** stateful strategy. In walk-forward with per-fold parameters,
    each fold's parameter set is replayed from the anchor. The jump at a fold boundary is charged as a trade
    (fees + slippage).
- *Robustness (sensitivity runs, not trials; fixed now).* Six one-at-a-time variants:
  - SMA 50, 75, 150 and 200, at vol target 0.30;
  - vol target 0.20 and 0.40, at SMA 100.

  Each must keep **≥ 70 % of S0's Sharpe** (pooled OOS, manual-delay fills). In every variant the ATR period
  stays at 20 (Q3), the multiplier at 3, and the re-entry rule, the band and σ̂ are unchanged.

These are textbook parameters with no search, so S0 counts as **one trial** (N = 1 in its DSR).

**S1 — equal-weight blend.** Equal-weight average of the target weights of every strategy that passed G3 (S0
included if it passed), netted into one position, with S0's rebalance rule (relative 25 % band; transitions
always traded). When S1 is compared with a candidate C, S1 is computed **without C** (leave-one-out). S1's DSR uses the
same trial count as every other non-S0 contender (see "Deflated Sharpe" below).

**S2 — champion/challenger (from phase 7b only).** A challenger replaces the running strategy only if all hold
over the evaluation window = the **paper and live days after the challenger's lock date** — the commit date of
the challenger's frozen entry in `research/EXPERIMENTS.md` — (historical
walk-forward days all predate the lock and do not count), minimum 365 days. **The test is run only at pre-registered dates: day 365 after the challenger's lock date and then on each further anniversary of that date.** There is no daily or weekly re-testing; a failed evaluation is final until the next date, so repeated looks cannot inflate the false-positive rate:
(a) **Sharpe-difference test (D-064, replaces the fixed 0.10 DSR margin).** A one-sided Ledoit–Wolf (2008)
test of `H0: SR_challenger − SR_champion ≤ 0` on the two daily net return series over **the same days**:
  - the series are paper returns for the challenger, and the champion's realised returns (paper or live, after
    all costs);
  - `z = (SR̂_c − SR̂_ch) / se`, where `se` comes from the delta method on the joint first and second moments
    of the two series;
  - the HAC covariance uses a **Parzen** kernel with **Andrews (1991)** automatic bandwidth (AR(1) plug-in),
    fixed;
  - the challenger replaces the champion only if `z ≥ 1.645` (p ≤ 0.05).

  When m > 1 challengers are tested against the same champion at the same evaluation date, the threshold is the
  one-sided Bonferroni value `z ≥ Φ⁻¹(1 − 0.05 / m)`. Every challenger tested is logged in
  `research/EXPERIMENTS.md`, so m is honest. Unlike the old margin, a strong champion can still be replaced by
  a significantly better challenger;
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
- SR* = the expected maximum of N independent daily Sharpes. For **every contender other than S0** (each
  candidate C and S1) N is the same number: all non-S0 trials logged in `research/EXPERIMENTS.md` up to **the
  date X is chosen**, across all strategy types (no "family" partitioning), and V[SR] = the variance of the
  daily Sharpe across exactly those trials, each computed on its own pooled OOS manual-delay series. If that
  count is 1, SR* = 0. Only S0 — locked before any result — always uses N = 1, so SR* = 0 and DSR = PSR(0).
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
- *Parameters of X for the holdout and for live.* A strategy with per-fold parameters runs with the parameter
  set its own selection procedure picks on the final training window that ends at `holdout_start` (all research
  data); that set is logged with the band, before unsealing. S0 has no parameters to pick.
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

### 7.2 Phase 3 pre-registration — final text v3, APPROVED by Parham on 2026-10-12 at commit 73c7646; IN FORCE from the merge of this PR

Status: **final text v3, submitted 2026-10-12 after Parham's review of v2 (d9c6c38; v1 2f719a6 and v2 were
merged to main unapproved and never in force); APPROVED by Parham on 2026-10-12 at commit 73c7646; IN FORCE
from the merge of this PR.** Labels: **[RECORDED]** = copied from decisions already in this document; **[DECIDED
2026-10-11]** = Parham's decisions on the 2026-10-11 draft; **[OPERATIONAL]** = a detail needed to make a
decided item exact, fixed now, before any result. Nothing below may change after any Phase 3 result is seen.
Engine commits must be at or after `8d99335`; the sealed holdout (from `holdout_start` = 2025-10-01) stays sealed.

**A. Data availability** — research data on hand (laptop store, checked 2026-10-11): Binance BTCUSDT and
ETHUSDT klines at 1h, 4h and 1d from the bar opening 2018-01-01 to the bar closing 2025-10-01 00:00 UTC.

| # | Strategy | Inputs | History before 2025-10-01 | Testable for G3 now? |
|---|---|---|---|---|
| S0 | `sma100_1d` [RECORDED §7.1] | BTCUSDT 1d OHLC (+1h for delayed fills and stops) | 2018-01-01 → 2025-09-30 | **Yes** |
| C1 | Donchian ensemble | 1d OHLC (+1h) | same | **Yes** |
| C2 | Time-series momentum | 1d close (+1h) | same | **Yes** |
| C3 | EWMAC (1d and 4h) | 1d / 4h close (+1h) | same | **Yes** |
| C4 | Volatility-compression breakout | 1d OHLC (+1h) | same | **Yes** |
| C5 | Pullback mean-reversion (weak evidence) | 1d close (+1h) | same | **Yes** |
| C6 | Order-book imbalance (hourly) | LBank order-book snapshots | none before the holdout (LBank book history starts 2026-10-10 13:40 UTC) | **No** — see J |
| — | Funding risk-off overlay (phase 4, G4) | Binance BTCUSDT perpetual funding | from the perpetual's launch (≈ 2019-09; *to verify on download*) | Not a G3 item |
| ETH | Robustness [RECORDED G3] | ETHUSDT 1d/4h/1h | 2018-01-01 → 2025-09-30 | **Yes** (Sharpe > 0) |
| Idx | S&P 500 / Nasdaq-100 [RECORDED D-071] | Stooq daily price index | *to verify on download* | Informational only |

MinTRL [RECORDED G3] is reported, not gating. The 365-day minimum belongs to S2 (phase 7b), not to G3. The pooled
OOS series covers 2020-01-01 → 2025-09-30 (≈ 2,100 days).

**B. Walk-forward** [DECIDED 2026-10-11]
- Anchored, expanding training window starting at **2018-06-01** (S0's first tradable close).
- **12 test periods**: eleven 6-month windows 2020-01-01–2020-06-30, 2020-07-01–2020-12-31, …,
  2025-01-01–2025-06-30, plus 2025-07-01–2025-09-30. Each test period's training window is 2018-06-01 up to the
  day before the period starts.
- Each strategy is replayed from the anchor 2018-01-01 in every period [RECORDED §7.1]; a parameter change at a
  period boundary is charged as a trade [RECORDED §7.1].
- **Per-period selection** [DECIDED 2026-10-11]: among a candidate's eligible points, the one with the highest
  annualised Sharpe (365, after all costs, **6 h manual-delay fills**) computed for every point over the
  **same days**: the candidate's eligible points' **common tradable days** inside that training window (from the
  latest first-tradable close among the eligible points to the end of the window), never over each point's own
  span; ties go to the base point.
- [OPERATIONAL] A grid point is **eligible** for a period only if it has at least 365 tradable days inside that
  period's training window (a point's first tradable close is the close at which all its indicators have their
  full window). If no point of a candidate is eligible, the candidate is flat for that period. Training-window
  days are the daily closes with ts in [2018-06-01 00:00, period start 00:00) UTC; the last research daily close
  is 2025-09-30 00:00 UTC (the bar closing 2025-10-01 00:00 is sealed).
- **Eligibility table** [OPERATIONAL, computed 2026-10-11 from the Binance BTCUSDT data on hand: 2,829 daily
  closes 2018-01-02 → 2025-09-30 with no gaps, 16,963 4h closes; first tradable close = the first close at which
  S0's σ̂ (151 daily closes), ATR20 (21) and every strategy window are full; ✓ = eligible, – = not]. Columns are
  the start months of the 12 test periods.

| Strategy | Point | First tradable close (UTC) | 2020-01 | 2020-07 | 2021-01 | 2021-07 | 2022-01 | 2022-07 | 2023-01 | 2023-07 | 2024-01 | 2024-07 | 2025-01 | 2025-07 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C1 | (10, 28, 50) | 2018-06-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C1 | (20, 55, 100) base | 2018-06-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C1 | (40, 110, 200) | 2018-07-21 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C2 | (15, 45, 90) | 2018-06-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C2 | (30, 90, 180) base | 2018-07-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C2 | (60, 180, 360) | 2018-12-28 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | 1d f=(8,16,32) | 2019-01-20 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | 1d f=(16,32,64) base | 2020-02-08 00:00 | – | – | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | 4h f=(8,16,32) | 2018-06-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | 4h f=(16,32,64) | 2018-06-01 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | 4h f=(32,64,128) | 2018-09-15 16:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C4 | p=10 | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C4 | p=20 base | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C4 | p=30 | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=5 | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=10 base | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=15 | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

- Pooled OOS series = the 12 test periods concatenated [RECORDED §7.1].

**C. Strategies and exact grids** [DECIDED 2026-10-11; exact definitions OPERATIONAL]

*Common to C1–C5:* BTCUSDT; long-only; target weight `w = min(1, 0.30 / σ̂_t)` while the strategy is long, else 0,
with S0's σ̂ (D-056) on the latest completed **daily** close; S0's rebalance rule (transitions always traded; 25 %
relative band while in position); protective stop fixed for the position at `P_ref − 3 × ATR20`, with `P_ref` = the
open of the first bar after the entry signal bar and **ATR20 = S0's daily simple ATR20** at the latest completed
daily close (also for the 4h strategy), floored at 0.01; stop fills as in §7.1 (daily rule for next-open, 1h bars
for the delayed model); after a stop-out the strategy stays flat until its own entry condition goes false and then
true again (a fresh entry). All indicators are finite-window and start-independent; `x_{a..b}` means bars a to b
inclusive; t is the latest closed bar of the strategy's timeframe.

| # | Definition (base point in bold) | Grid (each point = one trial) | Trials |
|---|---|---|---|
| C1 | For each lookback n: `U_n = max(high_{t−n..t−1})`, `L_n = min(low_{t−n..t−1})`, `M_n = (U_n + L_n)/2`. Channel n turns long when `close_t > U_n`, turns flat when `close_t < M_n`, else keeps its state. Strategy long iff **at least 2 of the 3** channels are long | lookbacks (n1, n2, n3) ∈ {(10, 28, 50), **(20, 55, 100)**, (40, 110, 200)} | 3 |
| C2 | `s_h = sign(ln(close_t / close_{t−h}))` (sign 0 = 0). Long iff `mean(s_h1, s_h2, s_h3) > 0` | (h1, h2, h3) days ∈ {(15, 45, 90), **(30, 90, 180)**, (60, 180, 360)} | 3 |
| C3 | For each pair (f, 4f): `E_k` = finite EWMA of close with α = 2/(k+1), truncated at K = 3k bars and renormalised; `raw = E_f − E_4f`; `σ_p = close_t × stdev(ln returns over the last 36 bars)` (ddof 1); `F = clip(raw / σ_p × c_f, −20, 20)`. Long iff `mean(F over the three pairs) > 0`. Spans are in bars of the timeframe | 1d: fast spans f ∈ {(8, 16, 32), **(16, 32, 64)**}; 4h: f ∈ {(8, 16, 32), (16, 32, 64), (32, 64, 128)} (1d (32, 64, 128) dropped, D-087) | 5 |
| C4 | `rv_t` = stdev of the last 20 daily ln returns (ddof 1); `pct_t` = share of `rv_{t−364..t}` that is ≤ `rv_t`. Enter when flat if `pct_t ≤ p/100` **and** `close_t > max(high_{t−20..t−1})`; exit when `close_t < (max(high_{t−20..t−1}) + min(low_{t−20..t−1}))/2` | p ∈ {10, **20**, 30} | 3 |
| C5 | `RSI2_t` = Cutler RSI over 2 bars (simple means of up and down closes over the last 2 changes; 100 if no down move); `SMA200_t` = mean of the last 200 closes. Enter when flat if `close_t > SMA200_t` **and** `RSI2_t < x`; exit when `RSI2_t > 70` | x ∈ {5, **10**, 15} | 3 |

C3 forecast scalars `c_f` [OPERATIONAL]: Carver's published values 8→5.3, 16→3.75, 32→2.65, 64→1.87; for f = 128,
which is not in his table, **1.32** (the table's ratio of about 1/√2 per doubling, applied once); after D-087 the
f = 128 scalar is used only by the 4h point (32, 64, 128). C3 on 4h uses
4h bars for its signal; sizing, stop and band follow the common rules.

**Total non-S0 trials: 17** (3 + 3 + 5 + 3 + 3; D-087). S0 = 1 trial (N = 1) [RECORDED §7.1]. S1 has no parameters. No
other configuration is run in Phase 3.

**D. Trial count** [RECORDED D-059, DECIDED wording]: every configuration ever run counts toward the DSR trial
count, **including failed and abandoned ones**; a configuration counts once it has produced any backtest number.
For every non-S0 contender N = all non-S0 trials logged in `research/EXPERIMENTS.md` up to the date X is chosen.
All 17 grid points are logged in `research/EXPERIMENTS.md` **before the first run**.

**E. Thresholds** [RECORDED G3 / §7.1, unchanged]: maxDD ≤ 0.60 × maxDD(B&H); Sharpe ≥ 0.80 × Sharpe(B&H); PBO
< 0.30; DSR ≥ 0.95 (§7.1 definition); ±1-grid-step neighbours keep ≥ 70 % of the Sharpe; ETH/USDT Sharpe > 0 with
the same rules and parameters; for S0 the six §7.1 robustness variants each keep ≥ 70 %; for S0 and S1 the ×2 cost
stress stays profitable.
- PBO [DECIDED]: CSCV per candidate over its grid, on the daily manual-delay return matrix of the grid points over
  2018-06-01 → 2025-09-30, **S = 16** equal contiguous blocks, Sharpe as the measure. For C3 the grid is all 5
  points (D-087). A point is included from its first tradable close; days before it count as flat.
- **Neighbour test for per-period-selected candidates** [DECIDED 2026-10-11]: take the grid point P that the
  candidate's own selection procedure picks on the final training window ending at `holdout_start`; compute P's
  **fixed-parameter** pooled OOS manual-delay Sharpe over the 12 test periods; each ±1-step neighbour of P, also
  run with fixed parameters over the same days, must keep ≥ 70 % of it. The ±1 step is the adjacent scale (C1,
  C2, C3 speed), the adjacent p (C4) or the adjacent x (C5); for C3 the timeframe is not a neighbour axis
  [OPERATIONAL]. A point at the edge of its grid has one neighbour; after D-087 the 1d C3 base point (16, 32, 64)
  has the single neighbour (8, 16, 32).
- **Buy-and-hold benchmark for the G3 ratios** [DECIDED 2026-10-11]: BTCUSDT at weight 1, held continuously over
  the pooled OOS days, with the same fee and slippage model and the entry charged once (no rebalancing); computed
  in both fill modes, and the gating ratios (maxDD, Sharpe) use the manual-delay version.
- All gating numbers use the pooled OOS manual-delay series; next-open numbers are reported alongside.

**F. Costs** [RECORDED D-060, DECIDED 2026-10-11]
- Fees: LBank spot VIP 0, taker 10 bps per side (taker assumed).
- Slippage: the **D-067 provisional curve** (from the G0 probe). **Every Phase 3 number is labelled PROVISIONAL.**
- **The G3 verdict comes from ONE complete rerun** of this whole pre-registered procedure (A–I) with the frozen
  LBank depth-at-size curve (§7 G3 model; window from the G1b start to the day G1b passes, then frozen and logged,
  D-085; "the G1b start" is the start of the consecutive healthy run that passes G1b, so if G1b fails and
  restarts, the window starts at the restart), **including per-period parameter selection**. The provisional results are informational only and never
  enter the verdict. No choice between keeping and re-selecting parameters may be made after seeing any result
  [DECIDED 2026-10-11; corrects v1].
- ×2 cost stress: 20 bps fee per side plus twice the slippage curve, for S0 and S1 (gating) and reported for every
  candidate.

**G. Fill modes** [RECORDED D-051, D-081]: every number twice — next open, and the 6 h manual delay on Binance 1h
execution bars (the first 1h bar opening at or after signal close + 6 h). **The gate must pass on the manual-delay
version.** Engine at or after `8d99335` (D-081 fill-time sizing, D-082 resize classification).

**H. T6 selection procedure** [RECORDED §7.1], step by step:
1. Run S0 (no search) and the 17 grid points of C1–C5 through the walk-forward (B), both fill modes; log every run.
2. Apply the walk-forward gates to each candidate: G3 (E) plus the non-holdout part of G4 (regime-overlay
   keep/drop, phase 4).
3. Build S1 = equal-weight blend of every strategy that passed G3 (S0 included if it passed), with S0's band;
   leave-one-out when compared with a candidate C.
4. C qualifies only if it passes the walk-forward gates, its pooled OOS manual-delay Sharpe exceeds S0's and S1's
   (S1 without C), it beats S0 in at least half of the periods, and DSR(C) ≥ 0.95 with every variant counted.
5. X = the qualifying C with the highest pooled OOS Sharpe; if none, the better of S0/S1 among those passing the
   walk-forward gates; if neither passes, X = none and nothing goes live.
6. Fix X's parameters on the final training window ending at `holdout_start`; compute and log the holdout band
   (stationary bootstrap, mean block 20 days, 10,000 resamples, seed 20261008) **before** unsealing.
7. Unseal the holdout once, for X only (G4). Pass ⇒ X runs; fail ⇒ nothing goes live, with no fallback.
- Steps 1–5 are computed PROVISIONAL first (F). Steps 6–7 happen only after the frozen-curve recompute and Parham's
  G3 decision. S2 applies only from phase 7b; S3 is ablation only.

**I. Robustness** [RECORDED G3, D-071; DECIDED 2026-10-11]: ETH/USDT runs with the same rules and **BTC's
per-period parameter choices unchanged** (no re-selection on ETH); gating: Sharpe > 0. S&P 500 and Nasdaq-100
(Stooq, daily, 252-day annualisation, price index) run with the same daily rules and BTC's per-period choices
unchanged (C3 on 1d only; a period whose BTC choice is a 4h point is flat for the indices), reported for context
only, never a gate.

**J. Data rules** [DECIDED 2026-10-11]
- **D-040 open:** Phase 3 may run while D-040 is open. No Phase 3 strategy reads bar volume, enforced by two tests
  kept side by side: the static `tests/strategies/test_no_volume_dependency.py`, and the behavioural
  `tests/strategies/test_volume_invariance.py` — S0 and all 17 grid points run through the real engine path
  twice, with the original volume and with volume replaced (random, and separately NaN); targets, orders and
  equity must be bit-identical; parametrised over the registry `tbot.strategies.phase3_registry`; with a
  negative control (a volume-dependent dummy strategy must make it fail). Any future strategy that uses volume
  is blocked until D-040 is closed.
- **4h ATR rule test:** `tests/strategies/test_c3_4h_daily_atr.py` proves the 4h C3 points use only S0's daily
  ATR20 of the last COMPLETED daily bar: changing the 4h bars of the incomplete day, or changing a 4h bar of a
  completed day (its high, low and close) in a way that leaves that day's daily high, low and close unchanged,
  leaves the stop level unchanged; changing the last completed day's daily range changes it. The same test
  proves the 4h points' target weight uses only the σ̂ of the last COMPLETED daily close: changing the incomplete
  day, or only the intrabar high/low of a 4h bar of a completed day with that day's daily high, low and close
  unchanged, leaves the target weight unchanged; changing the last completed day changes it. A changed 4h close
  is not covered for the target weight, because 4h closes feed the C3 signal by design (D-089).
- Until the strategies exist, the strategy-specific cases of both tests skip with the reason "not implemented
  yet"; a strategy PR may not merge while any of its cases skips.
- **C6 (order book):** option (ii), forward collection. Not evaluated in Phase 3 and **no proxy trials**, so the DSR
  trial count is not inflated. The LBank recorder keeps collecting. **Earliest evaluation date: 2027-10-10**
  (365 days of LBank book history from 2026-10-10 13:40 UTC). Any evaluation must be pre-registered before then
  and is limited to a paper/shadow strategy or an execution filter (D-069).
- The pivot plan referred to in this section is `docs/reports/pivot-lbank-plan.md` (historical; this SPEC
  supersedes it).

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
| D-039 | **Approved by Parham 2026-10-10, with conditions (see D-074):** G1b's Binance-vs-LBank basis stores only Binance close prices at timestamps matching LBank candles in `data/live_compare/`; no indicator or return is computed from them, the research loader cannot read that path (tested), and the fetch is not a trial. (Originally proposed for Tabdeal.) | the seal and G1b otherwise contradict each other (review m10) |
| D-040 | **Open, pending Parham:** G1a reconciliation fails on exactly 3 timestamps (2021-01-21, 2021-04-23, 2022-04-13), volume only, identical in BTC and ETH and in both 4h and 1d — Binance's own files disagree; OHLC reconciles everywhere outside outages | §7 says criteria may never be relaxed after seeing data, so this is Parham's call, not the orchestrator's |
| D-041 | Recorder review round 2: the candle sweep needs evidence the feed has passed the hour (a later trade, or a 2 h quiet timeout after a verified poll); `[]` after data is not verified; the 24 h age filter is replaced by a plausibility check; schema v4 self-healing rewrites; fatal exit after 20 failed cycles | round-2 MAJOR-A (age filter rejected ~17 % of every 29 h window), MAJOR-B (`[]` sealed hours with missing trades) and m-C (stale/cached responses, clock skew). Cost: a genuinely silent hour seals up to 2 h late |
| D-042 | Amends D-036: gap auto-classification is evidence-scoped — `exchange_outage` only when a *dropped* anomaly overlaps the missing-bar window `(from, to − Δ]`; `exchange_outage_after_short_bar` when a stored short bar's label equals the gap start; `exchange_wide_outage` when the identical window is missing in the other symbol from the same source. Every gap records `classified_by` (anomaly_overlap / after_short_bar / cross_symbol / manual); no rule overwrites a non-`unknown` classification. A raw close later than its label (beyond the 1-unit epsilon) is `long` and dropped | round-2 m-J: the first rule was broad enough to explain away unrelated gaps. Cross-symbol corroboration is evidence, not a guess: an exchange-wide stop hits every market at once |
| D-043 | Recorder review round 3: a trade arriving for an already-sealed hour records a `late_trades` gap and rewrites (or first writes) that hour's candle `complete=False`; an unparsable item's gap is anchored to its own hour and recorded once per (reason, hour); schema v5 adds indexes on `gaps(hour_close_ms)` and `gaps(reason, rewritten)`; backoff also counts consecutive cycle exceptions; the heartbeat carries a redacted `last_cycle_error`; `transaction()` refuses nesting; the plausibility clock is read after the HTTP response | round-3 MINOR-1..3, NIT-2..4: a feed frozen > 2 h could seal an hour with missing trades; one bad item marked ~29 h incomplete |
| D-044 | A Binance gap classification loaded without `classified_by` is `legacy` and is re-checked against the D-042 rules on every ingest (falls back to `unknown`); only an explicit `classified_by: manual` is never revisited. Close-time classification uses exact integer arithmetic in the file's own unit | round-3 MINOR-4 and NIT-1 |
| D-045 | Tabdeal `exchangeInfo` is a bare JSON list of markets (1047 on 2026-10-05, measured from the Turkey server), not Binance's `{"symbols": [...]}`; the probe accepts both. BTCUSDT filters: tick 0.01, step 0.000001, min notional 1 USDT, market max qty 8.84 BTC | the first server probe reported BTCUSDT "not found" because of the Binance-shaped parser |
| D-046 | The G0 probe also samples BTCIRT and USDTIRT depth each round, for comparison only (the traded pair stays BTCUSDT), reports fillable BUY/SELL size within 0.1 % / 0.5 % of mid (median and p10 across samples) and the implied BTC/USDT basis (BTCIRT/USDTIRT vs BTCUSDT) | Parham's request: execution capacity and the IRT market as context for G0 |
| D-047 | **Fees (supersedes D-018):** Tabdeal tier 1 (30-day volume < 1,000 USDT) taker 35 bps, maker 33 bps per side (tier 2: 35/31, tier 3: 33/28, tier 4: 31/26). Backtests assume taker unless a strategy explicitly uses limit orders; the G3 ×2 stress means 70 bps per side. Phase-3 reports must show turnover and annual cost drag. Whether USDT markets use the same table is still to verify | Parham's fee-table review (2026-10-06). A pre-registration update made **before any strategy result** exists — it tightens, never loosens: a round trip now costs ≈ 0.8 % (2 × 35 bps fee + spread/slippage), which leaves only slow, low-turnover variants realistic |
| D-048 | Allocator acceptance rule S0/S1/S2/S3 and the go-live rule (§7.1). S0's definition is **locked by Parham on 2026-10-10 (D-063)** | Parham, design additions from the peer project (2026-10-06). Locking a default before results is the only defence against picking the best of many backtests |
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
| D-059 | Amends D-058 after the fifth T6 review: every non-S0 contender (each candidate and S1) uses one DSR trial count — all non-S0 trials logged up to the date X is chosen — and SR* = 0 if that count is 1; "lock date" is defined only for S2 (commit date of the challenger's frozen EXPERIMENTS.md entry); X's parameters for holdout and live = its own procedure on the final training window ending at holdout_start, logged before unsealing; G4 points to the one-sided §7.1 band. Guardrail: the CI audit runs with no project ini (`-c /dev/null`) and no autoloaded plugins, and requires the binding tests by name; `/order` must be a whole path segment | Fifth review MAJOR-1 (a pyproject `python_functions` edit dropped the scanning test while keeping the count) and MAJOR-2 (two contradicting trial-count rules for S1, undefined lock date) |
| D-060 | **Fees (supersedes D-047 for the traded venue):** LBank **spot**, Parham's VIP 0 account, verified 2026-10-10: taker 10 bps, maker 10 bps per side (VIP1 8/8, VIP2 7/6.5, VIP3 6/5, VIP4 5/4, VIP5 4/3, VIP6 3.5/2, SVIP 3/0). Backtests assume taker unless a strategy explicitly uses limit orders; the G3 ×2 stress means **20 bps per side**. Spread and slippage are **not assumed**: the 5 bps slippage default is a provisional placeholder that the LBank G0 probe's measured spread/depth replaces before any phase-3 result. The perpetual table (taker 6 / maker 2 bps at VIP 0) is recorded in `docs/COMPARISON.md` only — we do not trade it (D-061) | The venue changed (Tabdeal → LBank) and the fee is read from the account, not guessed. This **lowers** the cost assumption (35 → 10 bps), which D-047 said pre-registration must not do after results are seen; it is legitimate here only because no strategy result exists yet (no result row in `research/EXPERIMENTS.md`) and the number is a verified fact of the new venue, not a choice. **Fee rule from now on (tightened after review):** after the first phase-3 result is logged in `research/EXPERIMENTS.md`, every gate is evaluated only at the schedule pre-registered on that date (here VIP 0, 10/10). A **higher** verified fee always applies. A **lower** verified fee (a higher VIP tier, a token discount, another venue) may appear only as a labelled sensitivity line and never decides a gate. The venue change itself is D-062 |
| D-061 | **We stay on spot; no perpetual, even unleveraged.** Typical long funding is about 0.01 % per 8 h, i.e. roughly 11 % a year, and more in euphoric markets — far above the perpetual's fee saving, and a perpetual adds liquidation/ADL risk. Binance BTCUSDT funding, 2020-01 to 2025-09 (pre-holdout, 6,300 events, `research/funding_vs_spot_fees.py`): an always-long perpetual paid **13.2 % a year** (17.2 / 30.6 / 4.2 / 7.9 / 12.0 % in 2020–2024, 4.0 % in Jan–Sep 2025; longs paid in ~88 % of 8 h periods). Held only while an SMA100 proxy (close > SMA100 on 1d, long/flat, weight 1, no stop and no vol sizing — S0's signal only; 62.7 % in the market) is long, it still paid **12.0 % a year**, because funding is highest in the bull trends a trend follower holds. The proxy traded 14.8 sides a year, so the fee saving is only **0.6 % a year** (perp taker 6 vs spot 10 bps) or **1.2 %** (perp maker 2 bps): the perpetual costs **10.8–11.4 % a year more** than spot. Breakeven needs 150–300 full-equity sides a year, 10–20× the proxy's turnover. With volatility sizing, funding scales with the average weight while rebalancing adds some turnover, but even 10× the proxy's turnover stays below breakeven, so the conclusion holds | Parham's request 2026-10-10: back the spot decision with data, not intuition. Caveats: Binance funding is the stand-in; LBank perpetual funding is recorded from 2026-10-10 13:39:59 UTC and its correlation with Binance is reported once they overlap. S0's stop and sizing add turnover, but stay far below breakeven. This is a cost computation, not a strategy trial (no return metric), so it is not in the trial count |
| D-062 | **Venue pivot (approved by Parham 2026-10-10): the traded venue is LBank spot; Tabdeal becomes legacy.** Tabdeal code, notes and recorded data stay in the repo and on the server but are not traded and not extended. Tabdeal's recorder is stopped only after the LBank recorder has had its first successful nightly backup and 24 h without errors, after one online `trades.sqlite` backup with `integrity_check` has been taken and handed to Parham | LBank offers public order-book, kline and trade endpoints that Tabdeal lacks (no kline endpoint, ~29 h of trades), and a VIP 0 spot fee of 10 bps against Tabdeal's 35 bps (D-060). The raw Tabdeal trades are the one dataset that cannot be re-downloaded, hence the ordered shutdown (CLAUDE.md §3.8) |
| D-063 | **S0 is locked by Parham on 2026-10-10:** SMA100 trend filter on Binance 1d bars (`close_t > SMA100_t` long, else flat; fill at the next open, plus the 6 h manual-delay variant of D-051), sizing `w = min(1, 0.30/σ̂)` with the finite 150-return kernel (D-056), vol-driven reweighting only at a ≥ 25 % relative change and only while in position, fixed stop `P_ref − 3×ATR20_t` (`P_ref` = Binance open of bar t+1 before slippage, §7.1, so the stop-out state is identical in backtest, paper and live; ATR20 = simple 20-day mean of True Range) with the gap rule (low ≤ stop fills at the stop, an open below the stop fills at the open, slippage applies, never better than the stop), and re-entry only after a close ≤ SMA100 followed by a fresh close above it. Robustness is pre-registered: anchored replay from 2018-01-01 and six variants (SMA 50/75/150/200; vol target 20 %/40 %), each keeping ≥ 70 % of S0's Sharpe. S0 counts as **one** trial. The lock content was first committed in `8736334` (branch `feat/lbank-adaptation`) before any phase-3 result existed; the lock is logged in `research/EXPERIMENTS.md` | S0 is the champion every challenger must beat (D-064); it was proposed in Parham's own pivot message, not selected from data, so locking it before results keeps the trial count honest |
| D-064 | **The champion/challenger test is a one-sided Ledoit–Wolf (2008) Sharpe-difference test** (H0: SR_challenger − SR_champion ≤ 0) on the two daily net return series over the same days, with a HAC estimator (Parzen kernel, Andrews automatic bandwidth), pass at z ≥ 1.645, minimum 365 days of overlap, together with the tripwire and 2-of-3 regime conditions in §7.1. It replaces the fixed 0.10 DSR margin | a fixed margin ignores the sampling noise of a Sharpe difference between two correlated series; an explicit test with a fixed bandwidth rule has no tunable knob |
| D-065 | **Capital and venue-risk rule:** only capital Parham can afford to lose is ever placed on the exchange. Parham accepts, as of 2026-10-10, the jurisdiction risk, the risk of an account freeze and the risk of withdrawal delays at LBank. No larger sum is placed than the written scaling rule of G6 allows | the exchange sits outside any regulator we could appeal to; the bot cannot mitigate that, so the exposure is capped by the amount, and the acceptance is dated |
| D-066 | **Funding-rate risk-off overlay (regime layer):** the backtest proxy is **Binance** BTCUSDT funding history (the only deep history; LBank's perpetual API gives the current value only and is recorded every 60 s from now on). Thresholds are set from Binance history alone by a pre-registered rule that is **causal**: an expanding-window percentile computed up to t, or a value fitted inside each walk-forward training fold only (never on the full 2020–2025 sample, which would leak into the out-of-sample folds, CLAUDE.md §3.1); every rule variant tried is a logged trial. They are never taken from the aggregates already viewed in `research/funding_vs_spot_fees.py` (disclosed in `research/EXPERIMENTS.md`). Live uses LBank funding; once an overlap window exists the LBank-vs-Binance correlation is reported. The overlay is kept only if it beats no-overlay out of sample (CLAUDE.md §9 phase 4) | funding is highest in the bull trends a trend follower holds (D-061), so an extreme-funding risk-off rule is plausible; the proxy is declared so nobody mistakes it for LBank data |
| D-067 | **Cost model.** Spread and slippage come from measured order-book depth, never from reported volume (LBank's reported 24 h volume is likely partly inflated). Until recorded snapshots exist, a **temporary depth-at-size curve** from the G0 probe (the largest market order that fills within 0.1 % and 0.5 % at the median sample) is used, and every report that uses it must label it *temporary*. The 5 bps slippage default of D-060 stays provisional until then | a number measured from our own server is worth more than an assumption, but one probe is not a distribution, so the label is mandatory |
| D-068 | **LBank recorder and storage plan.** One SQLite file per UTC day (`lbank-YYYY-MM-DD.sqlite`) with order-book snapshots every 10 s (top 20 levels), public trades (UUID ids, deduped by id, continuity by time overlap), klines and perpetual funding. A nightly job (~02:40 UTC) writes a zstd archive of each closed day, verifies it by reading it back, and only then deletes the source; an online backup and an 80 % disk alarm are in force. Order-book history starts 2026-10-10 13:40:00.871 UTC. Raw-first (CLAUDE.md §3.8) applies: no downstream failure stops recording | the order-book history is the only way to replace the temporary cost curve (D-067) and cannot be re-downloaded; per-day files keep a corrupt file from costing more than a day |
| D-069 | **Strategy universe.** Seven strategies with small pre-fixed parameter grids: S0, Donchian ensemble, time-series momentum, EWMAC, volatility-compression breakout, pullback mean-reversion (weak evidence; included for completeness), and order-book imbalance (paper/shadow only, evaluated first as an execution filter). Every grid point is a trial in `research/EXPERIMENTS.md`; non-S0 DSR counting follows D-059 | a pre-fixed small universe makes the trial count finite and honest; the weak and shadow-only entries are named so they are not silently promoted |
| D-070 | **Dependencies approved by Parham (2026-10-10):** `ccxt` (broker adapter side; the exact version is recorded here when it is added to `pyproject.toml`/`uv.lock`, and only read-only methods may be used before phase 5b), `fastapi` + `uvicorn` + `jinja2` (dashboard, D-072), and **uPlot vendored** into the repo with its version and checksum recorded (no CDN, no npm). Each is added in the PR that first uses it, **except `ccxt`: it may not enter `pyproject.toml` or `uv.lock` until Parham has applied the D-075 guard patch and CI is green with it** | CLAUDE.md §7 requires approval and a one-line justification for each new dependency; vendoring keeps the dashboard free of third-party network calls |
| D-071 | **S&P 500 and Nasdaq-100 daily history from Stooq** is allowed as *research/informational* data only (never traded, never used for gate decisions). Annualisation is 252 days and the series are price indices (dividends excluded), reported as such | a benchmark for context; the different annualisation and the price-only series must be stated wherever the numbers appear |
| D-072 | **Dashboard.** A read-only GET API (FastAPI) with an OpenAPI schema and `docs/dashboard-api.md`, plus a simple Jinja2 UI that reads only that public API; bound to `127.0.0.1` only; no write endpoint of any kind. Claude builds the UI | one source of truth for what the UI shows, and no remote control surface on a machine that will hold an exchange key |
| D-073 | **LBank gates.** G0 is the public part only (§7 G0): measured from the Turkey server, ≥ 30 samples over ≥ 6 h, conservative rate-limit probing that stops at the first 429/418/403 or other ban-type response; the private signed-read part is postponed until Parham creates a read-only key from an account in his own name and must pass before phase 7a. G1b is ≥ 7 consecutive healthy recorder days (§7 G1b), earliest 2026-10-17 13:40 UTC. G1a and G1b are decided separately (D-016) | the server is our only IP, so a ban must never be risked; the private key does not exist yet, so no private call is made |
| D-074 | **Live-compare data (D-039 approved, with conditions).** Binance close prices at timestamps matching LBank candles are stored in `data/live_compare/` for the G1b basis only. No indicator, return or other statistic is computed from them for strategy evaluation, the research loader cannot read that path (a test proves it), and the fetch is **not a trial** and does not count toward the trial count. LBank bars from the recorder are likewise stored apart from research data. **Implementation (`tbot.data.live_compare`):** per-UTC-month Parquet files keyed by (ts, symbol, timeframe), append-only (on conflict the stored value wins), written under a per-month lock with a tempfile + fsync + `os.replace` swap; the writer refuses unclosed bars and any root that overlaps the research Parquet root. The only price-consuming API is `compare_to_lbank`, which returns per-timestamp differences in bps and refuses an LBank close more than 20 % from the stored close, so it cannot serve as a blind price lookup. The tbot research readers (`store`, `candles`, `binance_loader.load_bars`/`load_frame`) refuse any path that resolves through a directory named `live_compare` (`..`, absolute, symlink and junction routes, any case). **Accepted limits:** a plain `pandas.read_parquet` in a notebook is not blocked, a caller with a good price guess can still narrow down a stored close, and a hardlink into the research tree cannot be detected by path; review is the binding check | the sealed holdout (D-032) and the G1b basis otherwise contradict each other; storing closes only, in an unreadable place, lets the basis be measured without opening the holdout |
| D-075 | **ccxt order-method guard.** The order-code guardrail also blocks ccxt's order, cancel, edit, margin and transfer methods in snake_case and camelCase (including `*_ws`, `create_orders`, `cancel_orders`, `edit_order`, `create_order_with_*`, `private_post*`, `transfer` and `getattr`-style dynamic calls). The guardrail files are self-protected, so Parham applies the change by hand from `docs/patches/ccxt-order-guard.patch` with `git apply`; nothing is bypassed. **Status: pending until Parham applies it**; the patch covers the spellings listed in its tests, and the guard remains defence-in-depth, with review as the binding check | ccxt exposes order paths under names the existing pattern list does not cover, and the broker adapter arrives with ccxt in phase 5b |
| D-076 | **Telegram alerts, health only (approved 2026-10-10).** A separate small host-side health-monitor service sends alerts when a recorder stream is stale or erroring, a backup fails or fails its integrity check, disk use exceeds 80 %, or a container is unhealthy. It is the only component that holds the bot token and chat id (a root-only env file); recorder containers receive no secrets. **Implementation:** `tbot.monitoring.health_monitor`, stdlib only, outbound Telegram GET only (no inbound commands), systemd unit `tbot-health-monitor` running from its own pinned checkout `/opt/tbot/monitor-run`; env file `/opt/tbot/health-monitor.env` (mode 600) holds `TBOT_TELEGRAM_BOT_TOKEN` and `TBOT_TELEGRAM_CHAT_ID`, plus an optional `TBOT_DEADMAN_URL` ping. A check that cannot run keeps its open alerts open (never a false RESOLVED); the LBank backup is judged from `status.json`. Limit: if the whole server dies the monitor dies too, so only the optional external dead-man ping can report it. Trade alerts come later, in phase 5a | alerts that need secrets must not widen what the recorder containers can read; a host-side unit also needs no docker socket inside a container |
| D-077 | **Model reporting.** Every delegated task's report-back states the model the subagent actually ran on. As of 2026-10-10 the aliases resolved to `claude-sonnet-5-5` (agents) and `claude-opus-5-5` (reviewer). On 2026-10-11 the API logs showed subagent requests sent as `claude-sonnet-5`, so, per Parham's instruction, all agent files are **pinned**: the seven implementation agents to `claude-sonnet-5-5`, the reviewer to `claude-opus-5-5`. A pin is changed only with Parham's approval | an alias can silently change under an update; the report makes a change visible |
| D-078 | *(Superseded by D-081 for next-open mode.)* **Fill-time BUY cap, manual-delay mode only (`fill_delay > 0`, D-051).** The risk manager sizes at close(t). When the fill comes, the SimulatedBroker caps the BUY quantity at `floor_to_step(cash / (fill_price × (1 + taker_fee_rate)))`, records each cap as a `ResizeRecord` (requested qty, filled qty, reason) and counts it as "resizes" in the report. A cap that leaves zero, or less than min qty or min notional, is rejected. In next-open mode (`fill_delay = 0`) there is no cap: an unaffordable BUY is rejected (`INSUFFICIENT_FUNDS`), counted as a fill rejection, and the run is marked INVALID (CLI exit 3). SELLs are never capped. Benchmarks run through the same engine and the same holdout locks | sizing at close(t) and rejecting at fill time made every delay-mode strategy and benchmark miss entries in rising markets, a systematic bias in exactly the series G3 gates on; a manual trader sizes when they place the order, while an automated fixed-quantity market order (5b) is rejected like next-open mode |
| D-079 | **G0 clock re-test (approved by Parham 2026-10-11).** The sampling run (32 samples, 2026-10-11 04:28–10:40 UTC) passed every G0 item except the clock, which was *inconclusive*: median \|offset\| 410 ms ± 596 ms half-RTT straddled 1 s, while the offset at the minimum RTT was −141 ms and the host clock was NTS-synced to 0.09 ms. A dedicated, stricter re-test is registered in §7 G0 before any re-test code or data exists: 120 `timestamp.do` calls over 1 h, offset = median of the fastest 10 % by RTT, pass iff \|offset\| + min_rtt/2 < 500 ms; pass ⇒ G0 passes, fail ⇒ G0 stays failed; run once | the 1 s rule's uncertainty band (half the 1.19 s median RTT of the slow time endpoint) was wider than the margin, so the sampling run could not decide; a min-RTT estimate is the standard NTP-style remedy, and tightening the threshold keeps the re-test from being a relaxation after seeing data |
| D-080 | **Signed-client clock calibration (phase 5b, approved by Parham 2026-10-11, independent of the D-079 result).** The LBank signed client measures its offset to LBank server time from minimum-RTT `timestamp.do` samples (as in D-079), applies it to every signed request's timestamp, re-calibrates periodically and after any timestamp/recvWindow rejection, and logs each calibration (offset, min RTT, sample count, UTC time). A calibration that fails or shows a large jump halts signed calls and alerts instead of guessing | LBank's time endpoint is slow (median RTT ≈ 1.2 s), so a naive single-call offset can be several hundred ms wrong; signed requests must not depend on that |
| D-081 | **Buy sizing at fill time in both fill modes; resize classification in D-082 (Parham, 2026-10-11, locked before any phase 3 result; supersedes D-078's next-open rule).** (1) In next-open AND manual-delay mode a BUY is capped at fill time to `floor_to_step(cash / (fill_price × (1 + taker_fee_rate)))`; the fill price is re-derived at the final capped quantity (size-dependent slippage) and affordability re-checked, so notional + fee never exceeds cash. A cap that leaves zero or less than min qty / min notional is rejected; SELLs are never capped; exits are sized from the held quantity booked from fills. (2) The stub buy buffer is 0 (the cap covers the taker fee; the stub sizing covers fee and slippage). (3) Manual-delay mode requires 1h execution bars, so a 6 h delay fills exactly at signal close + 6 h. Real data (laptop-measured, 6 h delay, SMA100 1d): 21 resizes, all `price_move`, largest 6.25 % on 2020-04-30 | a live market buy is sized at the open from the real price and cash (quote-amount market order) and a manual order when it is placed; rejecting the whole buy on a gap-up biased every strategy and benchmark against entries in rising markets |
| D-082 | **Resize anomalies by cause — post-review clarification, made before any Phase 3 result (Parham, 2026-10-11; replaces the fixed 1 % threshold briefly tried in review).** For EVERY BUY that carries a sizing reference (the signal-bar close and the cash at submit time, passed by the engine through `SimulatedBroker.submit(..., sizing_price=, sizing_cash=)`; `core/types.py` unchanged), the broker tests at fill time whether `requested_qty × sizing_price × (1 + taker fee) ≤ sizing_cash`. Not affordable ⇒ `anomaly` (a sizing bug), recorded whether or not a cap follows and whatever the price did, and the run is INVALID (CLI exit 3) like a fill rejection. Affordable but capped at fill ⇒ counted `price_move` (the reason says whether the open rose above the sizing price or only fees/slippage forced the cap), whatever the size of the move; the run stays valid. A capped BUY with no sizing reference ⇒ `anomaly` (fail safe); an uncapped BUY without a reference is not recorded. `ResizeRecord` carries `capped_quantity`, `filled`, `kind`, `sizing_price`, `sizing_cash`. Real data (laptop-measured, final engine, 6 h delay, SMA100 1d): 21 resizes, all `price_move`, 0 anomalies | a fixed percentage threshold would flag genuine 2–6 % price moves over a 6 h delay as bugs (six such moves in the BTC data) while missing an oversized order whose price then fell; testing affordability at the sizing reference separates a mis-sized order from price movement exactly |
| D-083 | **G0 clock item: scope — post-hoc scoping clarification, written after the 2026-10-11 clock verdict (Parham, 2026-10-11).** G0 stays recorded as FAIL on the clock item (rule of commit 6dd5b07) and PASS on every other item; the criterion itself is unchanged. The clock item gates **signed (private) requests only**: it is a blocker for the first phase that sends any signed request (the postponed private G0 read, before phase 7a, and phase 5b), and it does **not** gate phases 1–4, which send no signed request. Before the first signed request the functional signed-request test (§10.3, once approved) must also pass | the clock item exists because LBank validates the timestamp of signed requests; phases 1–4 only read public endpoints and Binance files, so a clock offset cannot affect them; written after the verdict and labelled as such so it is not mistaken for a pre-registered rule |
| D-084 | **Design constraint: network round trip Turkey server ↔ LBank public API ≈ 1.1–1.3 s** (min 1090 ms, median ≈ 1300 ms, p95 ≈ 2.8 s, max 6.8 s; 120 `timestamp.do` calls, 2026-10-11 16:17–17:17 UTC, `research/reports/g0_lbank_clock_20261011/`). **No strategy may depend on execution or reaction faster than a few seconds**; every latency assumption in a backtest or live component must be at least this slow | measured from the only server we have; a strategy that needs sub-second reaction would be untestable and unexecutable from here |
| D-085 | **Slippage calibration window — amendment, decided 2026-10-11 before any Phase 3 result (Parham).** The G3 depth-at-size curve is the median over recorder snapshots from the **G1b start to the day G1b passes** (then frozen and logged); "the G1b start" is the start of the consecutive healthy run that passes G1b — if G1b fails and restarts, the window starts at the restart (clarified by Parham 2026-10-12), replacing "to the day phase 3 starts". §7 G3 and §7.2 F state the same single rule. Phase 3 runs first with the D-067 provisional curve, labelled PROVISIONAL; the G3 verdict comes only from one complete rerun with the frozen curve (§7.2 F) | phase 3 starts before the LBank book history is long enough: G1b can start no earlier than 2026-10-17, so a window ending when phase 3 starts would be empty or too short |
| D-086 | **Venue decision by the owner (Parham, 2026-10-11): the venue remains LBank spot.** LBank's User Service Agreement (§10.2 item 4, quoted verbatim, page date 2026-07-22) restricts residents of listed countries; eligibility under these terms is the owner's responsibility, and the owner accepts the risk of account restriction or frozen funds. Phase 7a risk controls (risk-engineer scope): a hard capital cap set by Parham before 7a, the exchange balance kept to what the strategy needs with the excess withdrawn on a fixed schedule, and a Telegram alert on any account-restriction or KYC-related API error. No workaround of any exchange restriction is designed or documented | the owner's decision; the controls limit the loss if the account is restricted, consistent with D-065 (only capital Parham can afford to lose) |
| D-087 | **C3 grid: the 1d point (32, 64, 128) is dropped — owner decision of 2026-10-12, made before any result (Parham).** C3 now has 5 points (1d: (8, 16, 32), **(16, 32, 64)**; 4h: (8, 16, 32), (16, 32, 64), (32, 64, 128)); total non-S0 trials 17; PBO for C3 over 5 points; the 1d base point keeps the single neighbour (8, 16, 32); the f = 128 forecast scalar 1.32 is used only by the 4h point; eligibility table and registry updated | under the common-tradable-days selection rule this point's first tradable close (2022-03-17) would cut the selection window of every C3 point from about 1,240 to about 470 days from period 2023-07 on, and the point itself is eligible in only 5 of the 12 periods |
| D-088 | **SPEC §7.2 (Phase 3 pre-registration) v3 APPROVED by Parham on 2026-10-12 at commit `73c764660320ddbe8d844f5d159604dadc45c6e5`; in force from the merge of the status-flip PR.** Phase 3 runs only under this text; engine commits at or after `8d99335`; every change to it needs a new D-row and Parham's written approval of the exact head commit (CLAUDE.md §10) | pre-registration (CLAUDE.md §3.5): the evaluation rules are fixed and approved before any Phase 3 result exists |
| D-089 | **§7.2 J 4h test bullet — text-precision fix, 2026-10-12, no rule change (Parham).** The claim "or any 4h-only data" is made exactly true and narrowed to what the extended `tests/strategies/test_c3_4h_daily_atr.py` proves: for the stop level, any change to a 4h bar of a completed day (high, low and close) that leaves that day's daily high, low and close unchanged leaves the stop unchanged, with a control that changes the day's high; for the target weight, only intrabar high/low changes of such a bar are covered, because 4h closes feed the C3 signal by design | a sentence in an in-force pre-registration must state exactly what its test proves; no evaluation rule, threshold, grid or data changes |

---

## 9. Open questions (need Parham or the probe to answer)

1. ~~Fee tier~~ — answered by Parham: LBank spot VIP 0, taker 10 / maker 10 bps, verified from his account
   (D-060; supersedes the Tabdeal answer in D-047). Spread and slippage: from the LBank G0 probe.
2. **Real exchange filters** (LBank `accuracy.do`; legacy Tabdeal: `exchangeInfo`) for BTCUSDT/ETHUSDT (tick, step, min-notional) — unknown until G0.
3. **Rate limits** (undocumented). Start at 5 req/s; the probe reports observed headers and any 429.
4. *(Legacy, Tabdeal)* **Does any endpoint require `tabdealSymbol=BTC_USDT`** instead of `symbol=BTCUSDT`?
5. **Server static IP** for the API-key whitelist.
6. *(Legacy, Tabdeal)* **`/trades` behaviour**: default and maximum `limit`, whether the window is id- or time-based — determines
   the recorder's polling interval.
   *Answered from the Turkey server (2026-10-04, curl):* recent-trades window; `limit=1000` accepted (~29 h of
   BTCUSDT); `time` in integer ms; ids global across markets (D-037). The full probe run will re-confirm.
7. Whether CI should run on GitHub-hosted runners for a private repo (minutes cost) or a self-hosted runner.
8. LBank spot-buy fee asset — moved to the phase 5b checklist (§10, item 2).
9. LBank quote-amount market buys — moved to the phase 5b checklist (§10, item 1).

---

## 10. Pre-live checklists (phase 7a and phase 5b blockers)

Items stay **UNVERIFIED** until checked against LBank itself (docs, and a read-only or minimum-size test once the
key exists), with the evidence recorded in `docs/vendor/` and the date here. Phases 1–4 are unaffected.

### 10.1 Phase 5b checklist (order code; every item must be VERIFIED before any order code ships)

1. **UNVERIFIED — quote-amount (USDT) market buy.** Does LBank spot accept a market BUY sized by quote amount
   (USDT), as the `SimulatedBroker` docstring and D-081 assume (the backtest caps a BUY at fill time to what cash
   affords at the real price)? If not, the live path needs explicit fill-time quantity sizing: a fresh price and
   the real cash immediately before sending, rounded to the step, so live cannot reject a buy the backtest fills.
2. **UNVERIFIED — spot-buy fee asset.** Which asset does LBank deduct the spot-buy fee in, BTC (base) or USDT
   (quote)? The `SimulatedBroker` charges it in USDT. If LBank deducts BTC, live reconciliation must expect a
   base quantity of `filled × (1 − fee rate)`, or every buy shows a mismatch and halts trading.
3. **Signed-client clock calibration (D-080).** Offset to LBank server time measured from minimum-RTT samples,
   applied to every signed request, re-calibrated periodically and after any timestamp rejection, and logged.

### 10.2 Phase 7a blockers (planning only, no code)

4. **Exchange account eligibility — clause verified 2026-10-11; owner decision recorded below (D-086).** LBank's
   own *LBank User
   Service Agreement* (https://www.lbank.com/support/articles/21436496711705, page date **2026-07-22**, read
   2026-10-11) says in its Important Notice: "This Platform does not accept registration or investment from
   individuals in the following countries or regions: Afghanistan, Antigua and Barbuda, Azerbaijan, Bangladesh,
   Burkina Faso, Burundi, Central African Republic, Crimea, Cuba, Democratic Republic of the Congo, Donetsk,
   Egypt, Guinea-Bissau, Hong Kong SAR, Iran, Iraq, Kyrgyzstan, Lebanon, Lesotho, Libya, Luhansk, Macao SAR,
   Mainland China, Mali, Mauritania, North Korea, Somalia, South Sudan, Sudan and Darfur, Syria, United States,
   Uruguay, Yemen, and Canada." Its Special Disclaimer repeats it: "LBank has determined not to provide
   services to clients from the following countries or regions: [...] Iran [...]". A secondary source
   (cryptowinrate, updated 2026-09-30) also lists Iran.
   - **Owner decision 2026-10-11: venue remains LBank. LBank's User Service Agreement (quoted above) restricts
     residents of listed countries. Eligibility under these terms is the owner's responsibility, and the owner
     accepts the risk of account restriction or frozen funds.** This replaces the earlier "exchange choice
     returns to Parham" outcome; the quoted clause stays unchanged.
   - *Risk controls, enforced in phase 7a (risk-engineer scope; no code now):* (a) a **hard capital cap** on the
     exchange, set by Parham before 7a; (b) the exchange balance is kept to what the strategy needs, and the
     excess is withdrawn on a fixed schedule; (c) a **Telegram alert** if the API returns any account-restriction
     or KYC-related error.
   - No workaround of any exchange restriction is designed or documented by this project.
5. **Server location rule — UNVERIFIED for each location against the exchange's terms.** Every exchange-facing
   service (recorder, signal/paper service, any signed client) runs from a location permitted by the exchange's
   terms. Any server move requires re-running the G0 network checks (latency and the clock measurement) from the
   new server, and restarts the G1b 7-day clock.

### 10.3 DRAFT pre-registration — functional signed-request test (NOT final; Parham reviews before it is committed as final; not run)

6. **Purpose.** With the D-080 offset correction, LBank must accept signed read-only requests without timestamp or
   recv-window errors. Runs only after item 4 is resolved and a read-only key exists; G0's clock item stays FAIL
   regardless (D-083).
   - *LBank's rule* (LBank API docs, https://www.lbank.com/docs/index.html, read 2026-10-11): a signed request is
     invalid if it is received more than **5000 ms** after its `timestamp`, or if its `timestamp` is more than
     **1000 ms** ahead of server time; error 10600 "Request timeout, please check the difference between
     timestamp and server time". Error 10028 "Wrong query time" is also counted as time-related.
   - *Method.* Immediately before the run, calibrate per D-080: at least 30 `timestamp.do` calls, offset = median
     of the fastest 10 % by RTT. Then **240 signed read-only calls** (one fixed read-only endpoint, e.g. account
     balances), **30 s** apart over **2 h**, one in flight; each `timestamp` = host wall clock read immediately
     before sending + calibrated offset (no added skew). Record per call the RTT, HTTP status and LBank error
     code. A first-attempt time-related rejection is re-signed with a fresh timestamp and retried **once**,
     immediately (read-only, so a retry is safe).
   - *Pass iff all hold:* (a) no 429/403/418 at all (a ban-type response stops the run and fails it); (b) at least
     216 of 240 calls (90 %) get an HTTP response; (c) **at least 97 % of the responded calls are accepted on the
     first attempt** with no time-related error; (d) every first-attempt time-related rejection succeeds on its
     single retry; (e) **no time-related rejection on any call whose own measured RTT is below 3000 ms**. Run
     once; a re-run only for a documented infrastructure fault, after a new pre-registration.
   - *Structural passability* (checked before registration, CLAUDE.md §3.5; uses only the 2026-10-11 clock
     evidence: RTT min 1090 / median 1304 / p95 2785 / max 6807 ms, n = 120). Let `d` be the uplink delay
     (0 ≤ d ≤ RTT) and `e` the offset-estimate error, bounded by path asymmetry to |e| ≤ min_rtt/2 = 545 ms. The
     server sees `receive − timestamp = d + e`. The future check `d + e > −1000` always holds (d ≥ 0, e ≥ −545).
     The late check `d + e < 5000` holds for every call with RTT < 5000 − 545 = 4455 ms. In the evidence 2/120 =
     1.7 % of RTTs exceeded 4455 ms, so the worst-case first-attempt failure rate is about 1.7 % < 3 %: (c) is
     passable. A call with RTT < 3000 ms has `d + e ≤ 3545 < 5000`, so (e) can fail only through a calibration
     error larger than about 1.4 s, which is exactly what the test must detect. A rule of "zero time-related
     errors in 240 calls" would be structurally fragile (at a 1.7 % spike rate a clean run has about a 2 % chance)
     and is therefore not used.
   - *Guardrail note.* LBank's signed read endpoints use HTTP POST, which the order-code guardrail blocks under
     `src/` and `scripts/`; the client for this test needs Parham's explicit, scoped unlock and review.
