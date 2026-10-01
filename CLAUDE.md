# CLAUDE.md — Trading Bot Project Constitution

> Master prompt for every Claude Code session in this repo. Read it fully before any task.
> Owner: Parham. Talk to Parham in **Persian**; write code, comments, commit messages and docs in **English**.

---

## 1. Mission

Build a **deterministic, rule-based spot trading bot for BTC/USDT on the Tabdeal exchange** (tabdeal.org),
validated with rigorous, fee-inclusive, out-of-sample testing before any real money is used.

The goal is **evidence, not a bot at any cost**. If the data shows no edge after fees, the correct outcome is to
stop before live trading — that is a successful result of this process, not a failure. Never hide or soften bad results.

## 2. Fixed decisions (do not change without Parham's explicit approval)

| Topic | Decision |
|---|---|
| Market / exchange | Crypto, Tabdeal (Iranian exchange, Binance-style REST API) |
| Pair | BTC/USDT spot (ETH/USDT used only as a robustness check, not traded) |
| Direction | Long or flat. No shorting, no leverage, no margin, no futures |
| Timeframes | Signals on 1d and 4h bars. No scalping / sub-hour strategies |
| Base strategy | Trend following (Donchian breakout ensemble and/or EWMAC) + volatility-targeted sizing |
| Regime layer | Simple, causal (realized-vol percentile + trend strength e.g. efficiency ratio/ADX). It **scales exposure or gates entries**; it does NOT hard-switch strategies |
| Research data | Binance public klines (data.binance.vision) |
| Live data | Tabdeal has **no kline endpoint** → build candles from Tabdeal `/trades`; signals may use Binance bars, execution on Tabdeal |
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
4. **Costs everywhere.** Every backtest includes Tabdeal taker fee (configurable; default 0.2% per side until the
   real tier is known) + slippage model (default 0.05% per side, configurable). Annualization uses **365** days
   (8760 for hourly).
5. **Pre-registered evaluation.** Acceptance criteria are fixed before results are seen (see §9).
   The **last 12 months of data are a sealed holdout**: no code may load them until gate G4, and only once.
   Every experiment (parameter set, variant) is logged in `research/EXPERIMENTS.md` so the trial count for
   Deflated Sharpe / PBO is honest.
6. **Safety by default.**
   - Dry-run is the default mode. The live order path is only reachable when `LIVE_TRADING=true` AND the
     config phase is ≥ 6.
   - Secrets only in `.env` on the server (never in the repo, logs, tracebacks, chat or Telegram).
   - Tabdeal API key: **no withdrawal permission**, IP-whitelisted to the server's static IP, read-only until phase 6.
   - Every order carries a unique `client_order_id`; on timeout, query by that ID — never blindly resend.
   - Any mismatch between internal state and the exchange (balances, open orders, position) → trading state
     `HALTED` + Telegram alert.
7. **Small, typed, tested.** Python 3.12, type hints everywhere, `mypy --strict` on `core/`, `risk/`, `execution/`.
   No function longer than ~60 lines without a reason. Pure functions for indicators and strategy logic.
8. **Honesty and push-back.** If a request from Parham (or from the plan) is illogical, unsafe or likely to produce
   an imaginary result, say so clearly in Persian, explain why, and propose the correct way. Do not just agree.

## 4. Architecture

```
            ┌──────────────┐     ┌──────────────┐
            │ Binance data │     │ Tabdeal /trades│ (recorder → 1h candles)
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
                 Broker interface (SimulatedBroker | TabdealBroker)
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
│   ├── data/                 # binance_loader.py, tabdeal_recorder.py, candles.py, store.py, quality.py
│   ├── indicators/           # pure, causal indicator functions
│   ├── strategies/           # base.py, trend_donchian.py, trend_ewmac.py, buy_and_hold.py, sma_filter.py
│   ├── regime/               # base.py, vol_trend.py
│   ├── risk/                 # manager.py, sizing.py (vol targeting), limits.py
│   ├── execution/            # broker.py (Protocol), simulated.py, tabdeal_client.py, tabdeal_broker.py, oms.py
│   ├── portfolio/            # portfolio.py, ledger.py, persistence.py
│   ├── backtest/             # engine.py, metrics.py, report.py
│   ├── validation/           # walk_forward.py, truncation.py, pbo.py, dsr.py, bootstrap.py
│   ├── live/                 # runner.py, reconcile.py, health.py
│   └── monitoring/           # telegram.py, logging.py
├── scripts/                  # tabdeal_probe.py, download_binance.py, run_backtest.py, ...
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
indicator math. Quantities are rounded to the exchange's step size and price to tick size via `exchangeInfo`
filters before submission; orders below min-notional are refused by the RiskManager, not sent.

## 6. Tabdeal API notes (from official docs/SDK/Postman — **re-verify with the probe**)

- Base URL `https://api1.tabdeal.org` (Postman also uses `https://api.tabdeal.org`).
  Write calls → `/api/v1/...`; read calls, including signed reads → `/r/api/v1/...`.
- Auth: header `X-MBX-APIKEY`; HMAC-SHA256 signature over the url-encoded params with the API secret;
  `timestamp` must be an **integer** in ms; `recvWindow` ≤ 60000. Error codes: 1100–1103 auth, 1200–1218 request.
- Public: `ping`, `time`, `exchangeInfo`, `depth`, `trades`. Private: `order` (new/cancel/query), `openOrders`,
  `allOrders`, OCO, `myTrades`, `account`, `userDataStream`.
- Symbols: `symbol=BTCUSDT`, and some endpoints use `tabdealSymbol=BTC_USDT` (verify).
- WebSocket `wss://api1.tabdeal.org/stream/`: depth stream `<sym>@depth@2000ms`, user-data via listen key.
  **No kline and no trade stream.**
- **No kline/OHLC REST endpoint.** Build candles from polled `/trades` (dedupe by trade id; poll often enough that
  the `limit` is never saturated; record gaps explicitly).
- Rate limits are **undocumented** → client-side token bucket (start conservative, e.g. 5 req/s) + exponential
  backoff on 429/5xx.
- Official SDK `tabdeal-python` (github.com/Tabdeal-Exchange/tabdeal-python) is a **reference only**: it has a
  mutable-default-dict bug that reuses stale signatures and sends float timestamps. Write our own thin client.
- Do **not** use `unofficial_tabdeal_api` — it authenticates with a browser session token (full account access).

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

## 9. Phases and gates (summary — details in the plan doc)

| Phase | Output | Gate to pass |
|---|---|---|
| 0 Foundations | Repo skeleton, CI, SPEC, contracts, Tabdeal read-only probe, server setup guide | G0: private endpoints reachable from the Turkey server; BTCUSDT exists; median spread < 0.2% |
| 1 Data | Binance 1h/4h/1d BTC+ETH since 2018 in Parquet, quality report; Tabdeal trade recorder running | G1: no unexplained gaps; Binance-vs-Tabdeal price difference measured |
| 2 Engine | Event-driven backtester, SimulatedBroker, Portfolio, metrics, baselines, truncation test | G2: truncation test passes; baselines match vectorbt |
| 3 Strategy | Trend strategy + vol targeting, walk-forward, PBO/DSR, ETH robustness | G3: OOS after costs: maxDD ≤ 60% of B&H, Sharpe ≥ 80% of B&H, PBO < 0.3 |
| 4 Risk + regime + holdout | RiskManager (states, limits, kill switch), regime overlay (kept only if it beats no-overlay OOS), one-shot sealed holdout | G4: holdout inside the 95% bootstrap band |
| 5 Live infra | TabdealBroker, OMS, reconciliation, persistence, Telegram (/status, /halt), Docker + systemd | Fault-injection tests pass (timeouts, 429, restart mid-order) |
| 6 Paper | 8–12 weeks dry-run on the server with live data | G5: zero unresolved mismatches; live signals == backtest on same bars |
| 7 Small live | Minimal capital, scale only by rule | G6: 3 months inside expected band; halt if DD > 95th pct of bootstrap |

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

**Rules**
1. One task → one agent. Independent tasks run in parallel; dependent tasks run in sequence.
2. Subagents never modify `core/types.py` or `docs/SPEC.md`; they propose changes in their report.
3. The agent that writes a strategy never evaluates it; `validation-analyst` does.
4. Every change under `risk/` or `execution/` is reviewed by `reviewer`, then by Parham, before merge.
5. Work on feature branches; small PRs; conventional commit messages.
6. When a gate is reached, the orchestrator writes a short gate report in Persian for Parham with numbers,
   a recommendation (go / no-go / fix) and what is needed from him.

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
