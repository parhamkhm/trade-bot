# CLAUDE.md — Trading Bot Project Constitution

> Master prompt for every Claude Code session in this repo. Read it fully before any task.
> Owner: Parham. **Terminal replies in English. Reports for Parham (task breakdowns, gate reports, questions,
> recommendations) in Persian under `docs/reports/<name>.md`. Code, comments, commits and docs in English.**

---

## 1. Mission

Build a **deterministic, rule-based spot trading bot for BTC/USDT on the LBank exchange** (lbank.com),
validated with rigorous, fee-inclusive, out-of-sample testing before any real money is used.

The goal is **evidence, not a bot at any cost**. If the data shows no edge after fees, the correct outcome is to
stop before live trading — that is a successful result of this process, not a failure. Never hide or soften bad results.

## 2. Fixed decisions (do not change without Parham's explicit approval)

| Topic | Decision |
|---|---|
| Market / exchange | Crypto, **LBank spot** (public REST v2; ccxt for the broker adapter). Tabdeal is **legacy** (code and data kept, not traded) — pivot approved by Parham 2026-10-10 (SPEC D-062) |
| Capital | **Only capital Parham can afford to lose is ever placed on the exchange.** Jurisdiction, account-freeze and withdrawal-delay risks are accepted by Parham (SPEC D-065) |
| Pair | BTC/USDT spot (ETH/USDT used only as a robustness check, not traded) |
| Direction | Long or flat. No shorting, no leverage, no margin, no futures |
| Timeframes | Signals on 1d and 4h bars. No scalping / sub-hour strategies. (The hourly order-book-imbalance strategy #7 is paper/shadow only and is evaluated first as an execution filter) |
| Base strategy | Locked default **S0 = SMA100 trend filter + volatility-targeted sizing + fixed 3×ATR20 stop** (SPEC §7.1, D-063). Candidates: the seven-strategy universe with small pre-fixed parameter grids (D-069), trend following first |
| Regime layer | Simple, causal (realized-vol percentile + trend strength e.g. efficiency ratio/ADX), plus a **funding-rate risk-off overlay** (D-066: Binance funding as the declared backtest proxy, thresholds set on Binance history only, LBank funding live). It **scales exposure or gates entries**; it does NOT hard-switch strategies; kept only if it beats no-overlay out of sample |
| Research data | Binance public klines (data.binance.vision) |
| Live data | LBank klines, public trades and order-book snapshots, recorded raw by the LBank recorder (per-day SQLite). LBank bars are stored **separately** from research data; the research loader never reads them. Signals use Binance bars; execution on LBank. Costs come from **measured order-book depth, never reported volume** (D-067) |
| Runtime | Ubuntu VPS in Turkey, Docker Compose + systemd |
| Code hosting | Private GitHub repo |
| Decision loop | **No LLM in the trading decision loop.** Claude builds the bot; it is not the bot |

## 3. Non-negotiable engineering principles

1. **Causality.** Any value used at bar `t` is computed only from data with timestamp ≤ close of bar `t`.
   Orders generated at bar `t` fill at the **open of bar `t+1`** plus slippage. Never fill at the signal bar's close.
   - Forbidden patterns: `shift(-n)`, `.iloc[-1]` inside vectorized indicator code, full-column aggregates
     (`df.col.mean()` instead of `.rolling()`/`.expanding()`), `resample()` without `label='right', closed='right'`,
     merging higher-timeframe data onto lower timeframe without shifting to the higher bar's close,
     HMM Viterbi/smoothed states, fitting any model on the full dataset.
   - Every strategy/indicator must pass the **truncation test** (see §8).
2. **Backtest/live parity.** The same `Strategy`, `RegimeModel`, `RiskManager` and `Portfolio` code runs in backtest,
   paper and live. Only the `DataFeed` and `Broker` implementations differ.
3. **Strategies emit intents, never orders.** A strategy returns a `TargetIntent` (target weight in [0, 1]).
   The `RiskManager` approves, resizes or refuses it. Only approved intents become `OrderRequest`s.
4. **Costs everywhere.** Every backtest includes the real LBank spot fee (VIP 0: taker 0.10 %, maker 0.10 % per side,
   verified; taker unless a strategy explicitly uses limit orders — SPEC D-060) + slippage model (provisional 0.05 % per
   side, configurable; replaced by the spread/slippage the LBank G0 probe measures from recorded order-book snapshots).
   The G3 ×2 stress means 20 bps fee per side. Every phase-3 report shows turnover and annual cost drag. Annualization uses **365** days (8760 for hourly).
5. **Pre-registered evaluation.** Acceptance criteria are fixed before results are seen (see §9).
   The **last 12 months of data are a sealed holdout**: no code may load them until gate G4, and only once.
   Every experiment (parameter set, variant) is logged in `research/EXPERIMENTS.md` so the trial count for
   Deflated Sharpe / PBO is honest.
6. **Safety by default.**
   - Dry-run is the default mode. **No exchange order code exists before phase 5b** (see §9): until then the bot
     only emits signals and a human places orders by hand (phase 7a). From 5b on, the automated order path is only
     reachable when `LIVE_TRADING=true` AND the config phase is ≥ 7b.
   - **Order-code guardrail.** A Claude Code `PreToolUse` hook (`.claude/hooks/block_order_code.py`, wired in
     `.claude/settings.json`, matcher `Write|Edit|MultiEdit`) blocks any edit under `src/`, `scripts/`, `deploy/` or `config/` (D-055) that
     introduces order / cancel / OCO / margin / withdrawal / `userDataStream` endpoints or HTTP `POST`/`DELETE`
     calls to the exchange. It also blocks edits to itself and to `.claude/settings.json`. Only Parham unlocks it,
     by setting `TBOT_ALLOW_ORDER_CODE=1` himself (phase 5b). The same patterns are scanned by a pytest audit test
     and a CI step, which are the real enforcement: the hook does not see files written through a shell command.
   - Secrets only in `.env` on the server (never in the repo, logs, tracebacks, chat or Telegram).
   - LBank API key: created by Parham **only from an account in his own name**; **no withdrawal and no trading
     permission**, IP-whitelisted to the server's static IP, read-only until phase 5b. Until it exists, no private
     LBank call is made (the private G0 part is postponed, D-073).
   - Every order carries a unique `client_order_id`; on timeout, query by that ID — never blindly resend.
   - Any mismatch between internal state and the exchange (balances, open orders, position) → trading state
     `HALTED` + Telegram alert.
7. **Small, typed, tested.** Python 3.12, type hints everywhere, `mypy --strict` on `core/`, `risk/`, `execution/`.
   No function longer than ~60 lines without a reason. Pure functions for indicators and strategy logic.
8. **Raw data first.** Raw market-data ingestion (LBank order book, trades, klines and funding → per-day SQLite;
   legacy: Tabdeal `/trades` → SQLite) must never depend on any downstream step — candle building, Parquet reads
   or writes, quality checks, reports, the heartbeat. A downstream failure may stop only that step, log at error
   level (alert) and mark the healthcheck degraded; raw rows keep being recorded and are never dropped silently.
   Reason: order-book snapshots, the last ~4 min of trades and the current funding rate cannot be re-downloaded,
   while everything derived from them can be rebuilt. Enforced by the stream-isolation tests
   (`test_heartbeat_failure_never_touches_ingestion`, `test_a_failing_stream_never_stops_another`; legacy
   `test_trade_ingestion_survives_downstream_failure`) and by a nightly verified zstd archive + online backup of
   every closed day file (D-068).
9. **Honesty and push-back.** If a request from Parham (or from the plan) is illogical, unsafe or likely to produce
   an imaginary result, say so clearly, explain why, and propose the correct way. Do not just agree.
   Short push-back goes in the terminal in English; a full written argument goes to `docs/reports/` in Persian.

## 4. Architecture

```
            ┌──────────────┐     ┌──────────────┐
            │ Binance data │     │LBank recorder│ (book, trades, klines, funding)
            └──────┬───────┘     └──────┬───────┘
                   └──────── DataFeed ──┘   (BacktestFeed | LiveFeed)
                                 │ Bar events (closed bars only)
                                 ▼
                       ┌───────────────────┐
                       │  RegimeModel      │ → exposure multiplier m ∈ [0,1]
                       └────────┬──────────┘
                                ▼
                       ┌───────────────────┐
                       │  Strategy plugins │ → TargetIntent (weight ∈ [0,1])
                       └────────┬──────────┘
                                ▼
                       ┌───────────────────┐
                       │  Allocator        │ (v1: single strategy × m; later: static blend)
                       └────────┬──────────┘
                                ▼
                       ┌───────────────────┐   states: ACTIVE / REDUCING / HALTED
                       │  RiskManager      │ → Approved(OrderRequest) | Refused(reason)
                       └────────┬──────────┘
                                ▼
                       ┌───────────────────┐
                       │  OMS              │ client_order_id, retries, idempotency, reconciliation
                       └────────┬──────────┘
                                ▼
                 Broker interface (SimulatedBroker | LBankBroker)
                                ▼
                       Portfolio state (SQLite) → Telegram / logs / reports
```

### Module map

```
trading-bot/
├── CLAUDE.md                 # this file
├── docs/SPEC.md              # finalized contracts + decisions log (owned by the orchestrator)
├── pyproject.toml            # uv-managed
├── config/                   # default.yaml, backtest.yaml, paper.yaml, live.yaml
├── src/tbot/
│   ├── core/                 # types.py (contracts), events.py, clock.py, config.py
│   ├── data/                 # binance_loader.py, lbank_recorder.py, store.py, quality.py, depth.py
│   │                         #   (legacy: tabdeal_recorder.py, candles.py)
│   ├── indicators/           # pure, causal indicator functions
│   ├── strategies/           # base.py, s0_sma100.py, buy_and_hold.py, sma_filter.py, candidates (D-069)
│   ├── regime/               # base.py, vol_trend.py, funding.py (overlay, D-066)
│   ├── risk/                 # manager.py, sizing.py (vol targeting), limits.py
│   ├── execution/            # broker.py (Protocol), simulated.py, lbank_client.py (read-only), oms.py
│   │                         #   (5b: lbank_broker.py; legacy: tabdeal_client.py)
│   ├── portfolio/            # portfolio.py, ledger.py, persistence.py
│   ├── backtest/             # engine.py, metrics.py, report.py
│   ├── validation/           # walk_forward.py, truncation.py, pbo.py, dsr.py, bootstrap.py
│   ├── live/                 # runner.py, reconcile.py, health.py
│   ├── monitoring/           # telegram.py, logging.py
│   └── dashboard/            # read-only GET API (FastAPI) + Jinja2 UI, 127.0.0.1 only (D-072)
├── scripts/                  # record_lbank.py, lbank_probe.py, download_binance.py, run_backtest.py, ...
├── research/                 # notebooks + EXPERIMENTS.md (trial log)
├── tests/                    # mirrors src/tbot
└── deploy/                   # Dockerfile, docker-compose.yml, systemd units, SERVER_SETUP.md
```

## 5. Core contracts (draft — the orchestrator finalizes them in `src/tbot/core/types.py` + `docs/SPEC.md`)

```python
# All timestamps are timezone-aware UTC. A bar's `ts` is its CLOSE time.
@dataclass(frozen=True)
class Bar:
    symbol: str; timeframe: str; ts: datetime
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; volume: Decimal

@dataclass(frozen=True)
class TargetIntent:
    strategy_id: str; ts: datetime
    target_weight: float          # 0.0 = flat, 1.0 = fully invested (long-only)
    reason: str

class Strategy(Protocol):
    id: str
    warmup_bars: int
    def on_bar(self, window: BarWindow) -> TargetIntent: ...   # window = closed bars up to and incl. t
    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal: ...
        # protective stop for an open long (e.g. ATR- or channel-based); mandatory — a strategy without it
        # fails registration, and the RiskManager refuses a long without a valid stop below the price.
        # The stop rule is part of the strategy: its parameters are fixed in advance and counted as trials.

class RegimeModel(Protocol):
    def update(self, window: BarWindow) -> RegimeState: ...     # RegimeState.exposure ∈ [0, 1]

class TradingState(Enum): ACTIVE; REDUCING; HALTED    # REDUCING = only position-reducing orders

class RiskManager(Protocol):
    def evaluate(self, intent: TargetIntent, portfolio: PortfolioView,
                 market: MarketState) -> Approved | Refused: ...

@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str; symbol: str; side: Side; type: OrderType   # MARKET | LIMIT
    quantity: Decimal; limit_price: Decimal | None; created_ts: datetime

class Broker(Protocol):
    def submit(self, req: OrderRequest) -> OrderAck: ...
    def cancel(self, client_order_id: str) -> None: ...
    def get_order(self, client_order_id: str) -> OrderStatus: ...
    def open_orders(self, symbol: str) -> list[OrderStatus]: ...
    def balances(self) -> dict[str, Decimal]: ...
```

Rules: money and quantities are `Decimal` in execution/portfolio code; `float` is fine inside research and
indicator math. Quantities are rounded to the exchange's step size and price to tick size via the exchange's
precision filters (LBank `accuracy.do`) before submission; orders below min-notional are refused by the RiskManager, not sent.

## 6. LBank API notes (measured from the Turkey server 2026-10-10 — **re-verify with the G0 probe**)

- Spot REST base `https://api.lbkex.com`, public v2 endpoints: `/v2/timestamp.do`, `/v2/accuracy.do`
  (BTC/USDT: price 2 dp, quantity 5 dp, min qty 0.00001, min order 1 USDT), `/v2/depth.do?symbol=btc_usdt&size=…`
  (≤ 200 levels), `/v2/supplement/trades.do?symbol&size` (latest ≤ 600 trades ≈ 4 min),
  `/v2/kline.do?symbol&size&type=minute1|hour1|day1|…&time=<unix s>` (≤ 2000 rows; 1d history from 2018-03-13),
  `/v2/ticker/24hr.do`. Symbols are lower-case with underscore (`btc_usdt`).
- Perpetual market data (funding only; we never trade it, D-061):
  `https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData?productGroup=SwapU` → `fundingRate`, `nextFeeTime`,
  `positionFeeTime` (28800 s). **Current value only, no history** → recorded every 60 s.
- Latency ≈ 1.1 s per call, trades up to 8.6 s → one thread/client per stream. Trade ids are **UUID strings**,
  not increasing integers → dedupe by id, continuity by **time** overlap.
- Reported 24 h volume is likely partly inflated; liquidity is judged by **measured order-book depth** only.
- Rate limits: probe **conservatively**, stop at the first 429 or ban-type response — the Turkey server is our
  only IP (D-073). Client-side token bucket + exponential backoff on 429/5xx.
- Adapter: `ccxt` for the broker side (exact version pinned in `uv.lock` when first added, which happens only after Parham has applied the D-075 guard patch), read-only methods only; once that patch is applied the order-code
  guardrail also blocks ccxt's order/cancel/edit/margin/transfer methods in snake_case **and camelCase** (D-075).
  Direct REST is used where ccxt is slow or incomplete (the recorder).
- Private API: postponed until Parham creates a read-only key from an account in his own name (D-073).

**Legacy — Tabdeal (not traded; code and data kept).** Notes, client and recorder remain in
`docs/vendor/tabdeal-api-notes.md`, `src/tbot/execution/tabdeal_client.py`, `src/tbot/data/tabdeal_recorder.py`.

## 7. Tech stack

Python 3.12 · uv · pandas · numpy · pyarrow (Parquet) · pydantic v2 (config/models) · httpx · SQLite ·
structlog · pytest (+ hypothesis for property tests) · ruff · mypy · vectorbt (research/cross-checks only) ·
quantstats (`periods=365`) · python-telegram-bot · Docker Compose · systemd · GitHub Actions CI.
Adding any other dependency requires a one-line justification in `docs/SPEC.md` and the orchestrator's approval.

## 8. Validation toolkit (required before any strategy is trusted)

- **Truncation test:** run the strategy on data[:N] and data[:N+k]; all positions for bars ≤ N must be identical.
- **Warm-up test:** indicator values must be stable (≤ 1e-9 difference) after `warmup_bars` regardless of start.
- **Cross-check:** buy-and-hold and SMA-filter baselines must match an independent vectorbt run within tolerance.
- **Walk-forward:** anchored/expanding train windows, out-of-sample test windows, parameters chosen only on train.
- **Overfitting stats:** PBO (CSCV) and Deflated Sharpe using the full trial count from `research/EXPERIMENTS.md`.
- **Robustness:** same rules on ETH/USDT; neighbouring parameters must not collapse (no knife-edge optimum).
- **Resampling:** stationary block bootstrap of returns for the drawdown distribution (not plain trade shuffling).
- **Benchmarks:** always report vs buy-and-hold and vs buy-and-hold scaled to equal volatility.
- **Two fill assumptions (from phase 3):** every backtest reports results twice — fill at the next bar's open,
  and fill after a configurable manual delay (default 6 h) with slippage taken from the recorder's order-book
  snapshots. Phase 7a executes by hand, so the delayed version is the one that must still clear the gate.
- **Minimum Track Record Length:** validation reports state MinTRL per candidate. Live or paper results shorter
  than MinTRL cannot be used to choose between strategies.
- **Allocator rule (phase 4):** S0 / S1 / S2 / S3 as defined in `docs/SPEC.md` D-048. A candidate replaces the
  locked default only if it beats both S0 and S1 out of sample after costs.

## 9. Phases and gates (summary — details in the plan doc)

Order of execution: 0 → 1 → 2 → 3 → 4 → **5a → 6 → 7a → 5b → 7b** (approved by Parham 2026-10-06, D-049).

| Phase | Output | Gate to pass |
|---|---|---|
| 0 Foundations | Repo skeleton, CI, SPEC, contracts, LBank read-only probe, server setup guide | G0 (LBank, SPEC §7): public endpoints, latency, clock, filters, spread/depth from the Turkey server; private part postponed until the key exists |
| 1 Data | Binance 1h/4h/1d BTC+ETH since 2018 in Parquet, quality report; LBank recorder running | G1a: no unexplained gaps; G1b: LBank recorder healthy 7 days, Binance-vs-LBank basis measured |
| 2 Engine | Event-driven backtester, SimulatedBroker, Portfolio, metrics, baselines, truncation test | G2: truncation test passes; baselines match vectorbt |
| 3 Strategy | Trend strategy + vol targeting, walk-forward, PBO/DSR, ETH robustness | G3: OOS after costs: maxDD ≤ 60% of B&H, Sharpe ≥ 80% of B&H, PBO < 0.3 |
| 4 Risk + regime + holdout | RiskManager (states, limits, kill switch), regime overlay (kept only if it beats no-overlay OOS), one-shot sealed holdout | G4: holdout inside the 95% bootstrap band |
| 5a Signal infra | Telegram alerts, `/confirm` and `/skip`, manual fill logging (price, qty, time), `/status`, `/halt`, persistence, Docker + systemd. **No order code** | Fault-injection tests pass (restart mid-signal, Telegram down); every signal and every manual fill is logged |
| 6 Paper | 8–12 weeks of automatic dry-run on the server with live data, no money | G5: zero unresolved mismatches; live signals == backtest on the same bars |
| 7a Small live — manual | Minimal capital. The bot signals; Parham places each order on LBank by hand and logs the fill | G6a: 3 months inside the expected band of the manual-delay backtest; halt if DD > 95th pct of bootstrap. Too short to choose between strategies (MinTRL) — it tests execution, not edge |
| 5b Order code | LBankBroker (ccxt), OMS (client order ids, idempotency, reconciliation), exchange-side protective stops, kill switch. Starts only after 7a passes; the order-code hook is unlocked by Parham | Fault-injection tests pass (timeouts, 429, restart mid-order, non-atomic cancel-all) |
| 7b Automated live | Automated execution with the same capital as 7a; scale only by rule | G6b: 3 months inside the expected band; same halt rule |

## 10. Agent workflow

The main session runs on **Opus** and is the **orchestrator**: it owns `docs/SPEC.md`, `src/tbot/core/types.py`,
task breakdown, integration and gate decisions. It delegates implementation to the subagents in
`.claude/agents/` (Sonnet), and reviews with `reviewer` (Opus, read-only).

**Task brief template** (the orchestrator writes one per delegated task):
```
TASK: <one sentence goal>
PHASE: <n>        AGENT: <name>
CONTEXT: <relevant SPEC sections / files to read first>
ALLOWED FILES: <paths the agent may create/modify>
CONTRACTS USED: <types/protocols from core/types.py — do not modify>
ACCEPTANCE: <exact tests/commands that must pass + expected outputs>
OUT OF SCOPE: <what not to do>
REPORT BACK: files changed, test results, open questions, risks found
```

**Definition of Done:** `uv run pytest` green · `uv run ruff check .` clean · `uv run mypy` clean on strict
packages · new code has tests · no secrets · `docs/SPEC.md` decision log updated if behaviour changed.
**A PR is "ready to merge" only when its GitHub Actions CI run on the PR's current head commit is fully
green** — every step (secrets check, ruff, mypy, order-code audit, pytest) ran and passed on the Linux runner.
Local checks on the Windows laptop do not count. Every "ready to merge" message to Parham states the CI status
and run link per PR. Platform-specific code or tests are guarded with `sys.platform` (which mypy narrows) plus
`pytest.mark.skipif`, and the protection they test must also be exercised on Linux (Parham, 2026-10-11).

**Rules**
1. One task → one agent. Independent tasks run in parallel; dependent tasks run in sequence.
2. Subagents never modify `core/types.py` or `docs/SPEC.md`; they propose changes in their report.
3. The agent that writes a strategy never evaluates it; `validation-analyst` does.
4. Every change under `risk/` or `execution/` is reviewed by `reviewer`, then by Parham, before merge.
5. Work on feature branches; small PRs; conventional commit messages.
6. When a gate is reached, the orchestrator writes a short gate report in Persian for Parham at
   `docs/reports/<name>.md` with numbers, a recommendation (go / no-go / fix) and what is needed from him,
   then names the file path in the terminal reply.

## 11. References (read for design ideas; do not copy GPL code)

- Kill switch states: NautilusTrader `crates/risk/src/engine/` (TradingState Active/Reducing/Halted)
- Risk veto: barter-rs `barter/src/risk/mod.rs` (RiskApproved / RiskRefused)
- Reconciliation: NautilusTrader `docs/concepts/execution/reconciliation.md`; pysystemtrade `sysexecution/stack_handler/checks.py`
- Protections / persistence / Telegram: Freqtrade `freqtrade/plugins/protections/`, `persistence/`, `rpc/telegram.py` (GPL — ideas only)
- Broker parity: Lumibot `lumibot/brokers/broker.py`, quanttrader `brokerage/`
- Trading rules + vol estimate: pysystemtrade `systems/provided/rules/ewmac.py`, `breakout.py`, `sysquant/estimators/vol.py` (GPL — reimplement)
- Causal regime model (optional challenger): `jump-models` `predict_online()` (shift one bar)
- Look-ahead checks: Freqtrade docs "lookahead-analysis"
- Strategy evidence: Zarattini–Pagani–Barbon "Catching Crypto Trends" (2025); Le & Ruthbah (Monash) trend following for crypto
- Overfitting: Bailey & López de Prado (PBO, Deflated Sharpe); `pypbo`, `jsharpe`, `skfolio` (WalkForward)

## 12. Ask Parham before

- changing any fixed decision in §2 or any contract in §5;
- adding dependencies outside §7;
- anything that touches real money, API keys, or the server's firewall;
- moving to the next phase (gate decision).
