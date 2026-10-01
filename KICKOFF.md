# Kickoff — how to start, and the first prompt (Phase 0 + Phase 1)

## A. One-time setup on your laptop (≈10 minutes)

```bash
git clone https://github.com/parhamkhm/trade-bot.git
cd trade-bot
# copy CLAUDE.md, KICKOFF.md and the .claude/ folder from this starter pack into the repo root
git add CLAUDE.md KICKOFF.md .claude/
git commit -m "chore: add project constitution and agent definitions"
git push
claude --model opus        # main session = Opus (orchestrator); subagents run on Sonnet automatically
```

Inside Claude Code, run `/agents` once to confirm the 8 agents are listed.
Then paste the prompt in section B.

## B. The first prompt (paste into Claude Code)

```text
Read CLAUDE.md fully, then .claude/agents/*. You are the orchestrator (Opus). We are starting Phase 0 and Phase 1.
Talk to me in Persian; write everything in the repo in English.

Step 1 — plan (do this yourself, no subagents yet):
- Write docs/SPEC.md: finalize the core contracts from CLAUDE.md §5 (with any improvements you justify),
  config schema, directory layout, the decisions log, and the pre-registered gate criteria from §9.
- Implement src/tbot/core/types.py + tests exactly as in SPEC. These are owned by you.
- Create the repo skeleton: pyproject.toml (uv, Python 3.12), ruff + mypy config (strict on core/risk/execution),
  pytest, .gitignore (.env, data/, *.parquet), .env.example, GitHub Actions CI (pytest, ruff, mypy).
- Show me the task breakdown for Phase 0–1 as task briefs (template in CLAUDE.md §10) before delegating.

Step 2 — delegate in parallel (after I say OK):
- exchange-integrator → scripts/tabdeal_probe.py: a READ-ONLY probe for the Turkey server.
  Public: ping, time (report clock skew), exchangeInfo (BTCUSDT + ETHUSDT filters: tick size, step size, min notional,
  status), depth (best bid/ask, spread %, cumulative depth within 0.1% / 0.5% / 1%), trades (count, time span of
  the returned window to size the recorder's polling interval). Private, only if TABDEAL_API_KEY/SECRET are set in
  .env: account balances only. The script must not import or contain any order endpoint. Output a JSON report
  + a short human summary. Include tests with mocked responses.
- data-engineer → scripts/download_binance.py + src/tbot/data/: BTCUSDT and ETHUSDT klines 1h, 4h, 1d from
  2018-01 to the latest complete month from data.binance.vision with CHECKSUM verification, Parquet store,
  data-quality report (gaps, duplicates, zero-volume bars). Mark the last 12 months as SEALED_HOLDOUT in the store
  metadata; the default loader must exclude them.
- data-engineer (second task, after the probe report exists) → src/tbot/data/tabdeal_recorder.py: poll /trades,
  dedupe by id, persist raw trades (SQLite or Parquet), build 1h candles, record gaps explicitly; runnable as a
  long-lived service; tests with recorded fixtures.
- infra-ops → deploy/SERVER_SETUP.md for Ubuntu (user, firewall, NTP check, git, uv, Docker, .env, how to run the
  probe and the recorder), Dockerfile + docker-compose.yml with a `recorder` service, systemd unit.

Step 3 — review: run reviewer on every diff; fix BLOCKER/MAJOR findings; merge.

Step 4 — report to me in Persian:
- what was built and how to verify it,
- the exact commands to run the probe on the Turkey server,
- what you need from me (probe output, API key creation steps: read-only, no withdrawal, IP-whitelisted),
- your G0/G1 recommendation once I paste the probe output.

Constraints: no real orders anywhere; no secrets in the repo; never load the sealed holdout; if anything in the plan
looks wrong to you, tell me before building it.
```

## C. Prompts for later phases (use after each gate is passed)

- **Phase 2:** "Gate G1 passed (summary: …). Start Phase 2 per CLAUDE.md §9: write task briefs for backtest-engineer
  (engine, SimulatedBroker, Portfolio, metrics), strategy-researcher (buy-and-hold + SMA-filter baselines only),
  validation-analyst (truncation test + vectorbt cross-check). Show briefs, then delegate, review, report G2."
- **Phase 3:** "Start Phase 3: strategy-researcher implements Donchian ensemble and EWMAC trend rules + vol-targeted
  sizing helper; validation-analyst runs walk-forward, PBO, DSR, ETH robustness, cost ×2 stress; report G3 in Persian
  with a clear go / no-go."
- **Phase 4:** "Start Phase 4: risk-engineer builds RiskManager (states, limits, kill switch); strategy-researcher builds
  the causal regime overlay; validation-analyst tests overlay vs no-overlay OOS (drop it if it does not win), then —
  only after my explicit OK — performs the one-shot G4 holdout run."
- **Phase 5:** "Start Phase 5: exchange-integrator builds TabdealBroker + OMS + reconciliation; infra-ops builds the
  bot service, Telegram and health checks; reviewer reviews every money-path diff; fault-injection tests required."
- **Phase 6:** "Deploy dry-run on the server. Daily: compare live signals with a backtest on the same bars; weekly
  Persian report. After 8–12 weeks, report G5."
- **Phase 7:** "Prepare the live switch checklist (keys, limits, capital, halt rules). Do not enable LIVE_TRADING until I
  confirm each item."
