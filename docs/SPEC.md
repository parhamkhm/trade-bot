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
7. Decisions log (in [`DECISIONS.md`](DECISIONS.md)).
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

### 7.2 Phase 3 pre-registration — final text v3, APPROVED by Parham on 2026-10-12 at commit 73c7646 (in force, D-088); amended by D-090–D-093

Status: **final text v3, submitted 2026-10-12 after Parham's review of v2 (d9c6c38; v1 2f719a6 and v2 were
merged to main unapproved and never in force); APPROVED by Parham on 2026-10-12 at commit 73c7646; IN FORCE
from the merge of PR #24 (D-088).** **Amended by D-090–D-093** (decided 2026-10-12, before any Phase 3 result; in
force from the merge of PR #31, after Parham's written approval of its exact head commit). Labels: **[RECORDED]** = copied from decisions already in this document; **[DECIDED
2026-10-11]** = Parham's decisions on the 2026-10-11 draft; **[DECIDED 2026-10-12]** = Parham's decisions of 2026-10-12, before any Phase 3 result (D-090–D-093);
**[OPERATIONAL]** = a detail needed to make a decided item exact, fixed now, before any result. Nothing below may change after any Phase 3 result is seen.
Engine commits must be at or after `8d99335`; the sealed holdout (from `holdout_start` = 2025-10-01) stays sealed.

**A. Data availability** — research data on hand (laptop store, checked 2026-10-11): Binance BTCUSDT and
ETHUSDT klines at 1h, 4h and 1d from the bar opening 2018-01-01 to the bar closing 2025-10-01 00:00 UTC.

| # | Strategy | Inputs | History before 2025-10-01 | Testable for G3 now? |
|---|---|---|---|---|
| S0 | `sma100_1d` [RECORDED §7.1] | BTCUSDT 1d OHLC (+1h for delayed fills and stops) | 2018-01-01 → 2025-09-30 | **Yes** |
| C1 | Donchian ensemble | 1d OHLC (+1h) | same | **Yes** |
| C2 | Time-series momentum | 1d close (+1h) | same | **Yes** |
| C3 | EWMAC (1d; the 4h points are out of Phase 3, D-090) | 1d close (+1h) | same | **Yes** |
| C4 | Volatility-compression breakout | 1d OHLC (+1h) | same | **Yes** |
| C5 | Pullback mean-reversion (weak evidence) | 1d close (+1h) | same | **Yes** |
| C6 | Order-book imbalance (hourly) | LBank order-book snapshots | none before the holdout (LBank book history starts 2026-10-10 13:40 UTC) | **No** — see J |
| — | Funding risk-off overlay (phase 4, G4) | Binance BTCUSDT perpetual funding | from the perpetual's launch (≈ 2019-09; *to verify on download*) | Not a G3 item |
| ETH | Robustness [RECORDED G3] | ETHUSDT 1d/1h | 2018-01-01 → 2025-09-30 | **Yes** (Sharpe > 0) |
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
| C4 | p=10 | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C4 | p=20 base | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C4 | p=30 | 2019-01-21 00:00 | – | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=5 | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=10 base | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | x=15 | 2018-07-20 00:00 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

The three C3 4h rows of the approved v3 table were removed by D-090 [DECIDED 2026-10-12].

- Pooled OOS series = the 12 test periods concatenated [RECORDED §7.1].

**C. Strategies and exact grids** [DECIDED 2026-10-11; exact definitions OPERATIONAL]

*Common to C1–C5:* BTCUSDT; long-only; target weight `w = min(1, 0.30 / σ̂_t)` while the strategy is long, else 0,
with S0's σ̂ (D-056) on the latest completed **daily** close; S0's rebalance rule (transitions always traded; 25 %
relative band while in position); protective stop fixed for the position at `P_ref − 3 × ATR20`, with `P_ref` = the
open of the first bar after the entry signal bar and **ATR20 = S0's daily simple ATR20** at the latest completed
daily close, floored at 0.01; stop fills as in §7.1 (daily rule for next-open, 1h bars
for the delayed model); after a stop-out the strategy stays flat until its own entry condition goes false and then
true again (a fresh entry). All indicators are finite-window and start-independent; `x_{a..b}` means bars a to b
inclusive; t is the latest closed bar of the strategy's timeframe.

| # | Definition (base point in bold) | Grid (each point = one trial) | Trials |
|---|---|---|---|
| C1 | For each lookback n: `U_n = max(high_{t−n..t−1})`, `L_n = min(low_{t−n..t−1})`, `M_n = (U_n + L_n)/2`. Channel n turns long when `close_t > U_n`, turns flat when `close_t < M_n`, else keeps its state. Strategy long iff **at least 2 of the 3** channels are long | lookbacks (n1, n2, n3) ∈ {(10, 28, 50), **(20, 55, 100)**, (40, 110, 200)} | 3 |
| C2 | `s_h = sign(ln(close_t / close_{t−h}))` (sign 0 = 0). Long iff `mean(s_h1, s_h2, s_h3) > 0` | (h1, h2, h3) days ∈ {(15, 45, 90), **(30, 90, 180)**, (60, 180, 360)} | 3 |
| C3 | For each pair (f, 4f): `E_k` = finite EWMA of close with α = 2/(k+1), truncated at K = 3k bars and renormalised; `raw = E_f − E_4f`; `σ_p = close_t × stdev(ln returns over the last 36 bars)` (ddof 1); `F = clip(raw / σ_p × c_f, −20, 20)`. Long iff `mean(F over the three pairs) > 0`. Spans are in daily bars | 1d: fast spans f ∈ {(8, 16, 32), **(16, 32, 64)**} (1d (32, 64, 128) dropped, D-087; the three 4h points dropped, D-090) | 2 |
| C4 | `rv_t` = stdev of the last 20 daily ln returns (ddof 1); `pct_t` = share of `rv_{t−364..t}` that is ≤ `rv_t`. Enter when flat if `pct_t ≤ p/100` **and** `close_t > max(high_{t−20..t−1})`; exit when `close_t < (max(high_{t−20..t−1}) + min(low_{t−20..t−1}))/2` | p ∈ {10, **20**, 30} | 3 |
| C5 | `RSI2_t` = Cutler RSI over 2 bars (simple means of up and down closes over the last 2 changes; 100 if no down move); `SMA200_t` = mean of the last 200 closes. Enter when flat if `close_t > SMA200_t` **and** `RSI2_t < x`; exit when `RSI2_t > 70` | x ∈ {5, **10**, 15} | 3 |

C3 forecast scalars `c_f` [OPERATIONAL]: Carver's published values 8→5.3, 16→3.75, 32→2.65, 64→1.87. The f = 128
scalar (**1.32**, the table's ratio of about 1/√2 per doubling, applied once) was used only by the 4h point
(32, 64, 128); after D-090 it is not used in Phase 3.

**Total non-S0 trials: 14** (3 + 3 + 2 + 3 + 3; D-087, D-090). S0 = 1 trial (N = 1) [RECORDED §7.1]. S1 has no parameters. No
other configuration is run in Phase 3.

**D. Trial count** [RECORDED D-059, DECIDED wording]: every configuration ever run counts toward the DSR trial
count, **including failed and abandoned ones**; a configuration counts once it has produced any backtest number.
For every non-S0 contender N = all non-S0 trials logged in `research/EXPERIMENTS.md` up to the date X is chosen.
All 14 grid points are logged in `research/EXPERIMENTS.md` **before the first run**.

**E. Thresholds** [RECORDED G3 / §7.1, unchanged]: maxDD ≤ 0.60 × maxDD(B&H); Sharpe ≥ 0.80 × Sharpe(B&H); PBO
< 0.30; DSR ≥ 0.95 (§7.1 definition); ±1-grid-step neighbours keep ≥ 70 % of the Sharpe; ETH/USDT Sharpe > 0 with
the same rules and parameters; for S0 the six §7.1 robustness variants each keep ≥ 70 %; for S0 and S1 the ×2 cost
stress stays profitable.
- PBO [DECIDED]: CSCV per candidate over its grid, on the daily manual-delay return matrix of the grid points over
  2018-06-01 → 2025-09-30, **S = 16** equal contiguous blocks, Sharpe as the measure. For C3 the grid is its 2
  points (D-087, D-090). A point is included from its first tradable close; days before it count as flat.
- PBO convention [DECIDED 2026-10-12, D-093]: in each CSCV split, the in-sample winner's out-of-sample rank r
  among the N points (r = 1 the worst, r = N the best; ties take the average rank) gives ω = r / (N + 1) and
  λ = ln(ω / (1 − ω)) [OPERATIONAL]; the split counts as **overfit when λ ≤ 0** (a winner that lands exactly on
  the out-of-sample median rank counts as overfit). PBO = the share of overfit splits, and this value is the one
  used for the gate. The share of splits with λ < 0 is reported next to it for information only.
- **Neighbour test for per-period-selected candidates** [DECIDED 2026-10-11]: take the grid point P that the
  candidate's own selection procedure picks on the final training window ending at `holdout_start`; compute P's
  **fixed-parameter** pooled OOS manual-delay Sharpe over the 12 test periods; each ±1-step neighbour of P, also
  run with fixed parameters over the same days, must keep ≥ 70 % of it. The ±1 step is the adjacent scale (C1,
  C2, C3 speed), the adjacent p (C4) or the adjacent x (C5). A point at the edge of its grid has one neighbour; after D-087 the 1d C3 base point (16, 32, 64)
  has the single neighbour (8, 16, 32).
- **Buy-and-hold benchmark for the G3 ratios** [DECIDED 2026-10-11]: BTCUSDT at weight 1, held continuously over
  the pooled OOS days, with the same fee and slippage model and the entry charged once (no rebalancing); computed
  in both fill modes, and the gating ratios (maxDD, Sharpe) use the manual-delay version.
- All gating numbers use the pooled OOS manual-delay series; next-open numbers are reported alongside.

**F. Costs** [RECORDED D-060, DECIDED 2026-10-11]
- Fees: LBank spot VIP 0, taker 10 bps per side (taker assumed).
- Slippage: the **D-067 provisional curve** (from the G0 probe). **Every Phase 3 number is labelled PROVISIONAL.**
  Construction [DECIDED 2026-10-12, D-091; the §7 G3 statistic, not a new rule]: the **per-notional median**. Per
  side (a buy walks the asks, a sell the bids), the points are (0, 0), (the median over the probe samples of
  the largest market order whose volume-weighted fill price stays within 0.1 % of the best price, 10 bps) and
  (the same within 0.5 %, 50 bps). Each sample's cost (VWAP walking the book) rises monotonically with notional,
  so a quantile of those order sizes is the notional at which the opposite quantile of cost equals 10 or 50 bps:
  exactly for the nearest-rank p10, and for the median (the mean of the two middle of 32 samples) up to the gap
  between those two samples. These are points of the §7 G3 per-notional median curve. Interpolation,
  extrapolation, the 10,000 USDT equity normaliser and half the median spread (added once) are as in §7 G3. At
  0.5 %, one sample per side reached the end of the visible 200-level book, so its size is a lower bound; it can
  affect only the segment beyond the 0.1 % point, which orders at the 10,000 USDT normaliser never reach.
- Slippage sensitivity [DECIDED 2026-10-12, D-091], **reported only, never gating**: the adverse tail, the
  per-notional **p90 of cost** (thin book). By the same argument its points are the **p10 of the order sizes**,
  plus half the p95 spread (the probe stores no p90 spread; p95 is the more adverse choice).
- [OPERATIONAL, added 2026-10-19, before any Phase 3 result] The curve construction is data-independent. The median is the middle sample (odd count) or the mean of the two middle samples (even count); p10 is the nearest-rank p10. A sample whose fill reaches the end of the visible book is a lower bound for its order size; the report counts such samples per side and per band. Before the G3 run on the frozen curve: the largest possible order — a weight change of 1 at the 10,000 USDT normaliser as defined in §7 G3 — must be ≤ the frozen curve's 0.1 % notional point on each side; otherwise stop and report before running. The sample counts and the book-end note above describe the G0 probe (provisional curve) only.
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
1. Run S0 (no search) and the 14 grid points of C1–C5 through the walk-forward (B), both fill modes; log every run.
2. Apply the walk-forward gates to each candidate: G3 (E) plus the non-holdout part of G4 (regime-overlay
   keep/drop, phase 4).
3. Build S1 = equal-weight blend of every strategy that passed G3 (S0 included if it passed), with S0's band;
   leave-one-out when compared with a candidate C. *Stops* [DECIDED 2026-10-12, D-092]: S1 has **no protective
   stop of its own**. It follows each member's own daily state through that member's target weight (a member in
   STOPPED contributes 0). A reduction of S1's target caused by a member's stop-out **always trades** and is never
   blocked by the 25 % band (§7.1 rule 3); [OPERATIONAL] at a close where any member enters STOPPED, S1 trades to
   its full new target (other members' moves at that close included), ignoring the band. Other changes of S1's
   target, including a member's ordinary signal exit, keep S0's rebalance rule on the netted position. The report
   states that S1 has no intraday stop of its own. *Open item, outside Phase 3:* CLAUDE.md §5 and D-050 require a
   valid protective stop for every long that the phase 4 RiskManager approves; how S1 meets that live (for
   example a stop per member sub-position) must be decided by Parham before S1 can be deployed. Phase 3 is not
   affected (it uses the stub risk manager).
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
unchanged, reported for context
only, never a gate.

**J. Data rules** [DECIDED 2026-10-11]
- **D-040 open:** Phase 3 may run while D-040 is open. No Phase 3 strategy reads bar volume, enforced by two tests
  kept side by side: the static `tests/strategies/test_no_volume_dependency.py`, and the behavioural
  `tests/strategies/test_volume_invariance.py` — S0 and all 14 grid points run through the real engine path
  twice, with the original volume and with volume replaced (random, and separately NaN); targets, orders and
  equity must be bit-identical; parametrised over the registry `tbot.strategies.phase3_registry`; with a
  negative control (a volume-dependent dummy strategy must make it fail). Any future strategy that uses volume
  is blocked until D-040 is closed.
- **4h ATR rule test — retired by D-090** [DECIDED 2026-10-12]: `tests/strategies/test_c3_4h_daily_atr.py`
  (D-089) is removed with the 4h points. The rule it protected (the stop's ATR20 and the sizing σ̂ are S0's daily
  values at the latest completed daily close) stays in the common rules (C).
- Until the strategies exist, the strategy-specific cases of the behavioural volume test skip with the reason "not implemented
  yet"; a strategy PR may not merge while any of its cases skips.
- **C6 (order book):** option (ii), forward collection. Not evaluated in Phase 3 and **no proxy trials**, so the DSR
  trial count is not inflated. The LBank recorder keeps collecting. **Earliest evaluation date: 2027-10-10**
  (365 days of LBank book history from 2026-10-10 13:40 UTC). Any evaluation must be pre-registered before then
  and is limited to a paper/shadow strategy or an execution filter (D-069).
- The pivot plan referred to in this section is `docs/reports/pivot-lbank-plan.md` (historical; this SPEC
  supersedes it).

---

## 8. Decisions log

The decisions table (D-001 onward) lives in [`DECISIONS.md`](DECISIONS.md) (moved 2026-10-18, content unchanged). "SPEC D-nnn" references anywhere in the repo mean that table.

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
