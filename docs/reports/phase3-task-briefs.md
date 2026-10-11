# Phase 3 task briefs (DRAFT — not to be started until Parham approves SPEC §7.2)

Both briefs follow the CLAUDE.md §10 template. Bracketed grid values are the SPEC §7.2 **proposals**; if Parham
changes §7.2, these briefs are updated before any agent starts. No backtest result may be produced before that
approval. Engine commits must be at or after `8d99335`; the sealed holdout stays sealed.

---

## Brief 1 — strategy-researcher

```
TASK: Implement S0 and candidates C1–C5 as causal Strategy plugins with mandatory stop_price(), exactly as fixed
      in docs/SPEC.md §7.1 (S0) and §7.2 C (C1–C5), and prove causality. Do NOT evaluate them.
PHASE: 3        AGENT: strategy-researcher
CONTEXT: CLAUDE.md §3 (causality, intents not orders), §5 (Strategy protocol incl. stop_price), §8; docs/SPEC.md
         §7.1 (S0 state machine, σ̂ D-056, ATR20, P_ref stop, anchored replay, 25 % band), §7.2 C (grids, common
         sizing/stop/re-entry), D-050, D-051, D-081/D-082 (engine fill semantics), D-084 (no sub-second reaction);
         src/tbot/backtest/*, src/tbot/strategies/{buy_and_hold,sma_filter}.py for the plugin shape.
ALLOWED FILES: src/tbot/indicators/**, src/tbot/strategies/{s0_sma100,donchian,tsmom,ewmac,vol_breakout,
               pullback_mr}.py, tests/indicators/**, tests/strategies/**.
CONTRACTS USED: Strategy, TargetIntent, Bar, BarWindow from core/types.py — do not modify.
ACCEPTANCE:
  - every indicator is a pure, finite, start-independent function; warm-up test (≤ 1e-9 after warmup_bars,
    any start) and truncation test (positions on data[:N] == data[:N+k] for bars ≤ N) pass for S0 and for every
    grid point of C1–C5, on synthetic data AND on Binance BTCUSDT 1d before 2025-10-01 only;
  - S0 reproduces every §7.1 rule with hand-computed unit cases (FLAT→LONG, LONG→STOPPED incl. the entry bar,
    STOPPED→FLAT on the stop-out bar's close, re-entry only after a close ≤ SMA100, stop floor 0.01, the
    25 % band, transitions always traded);
  - each candidate's grid is a frozen tuple in code equal to SPEC §7.2 C; every entry of
    src/tbot/strategies/phase3_registry.py loads (module + `make(timeframe=..., **params)` factory);
  - tests/strategies/test_volume_invariance.py and tests/strategies/test_c3_4h_daily_atr.py run with ZERO
    skips (every registered strategy volume-invariant through the real engine path; the 4h C3 points use only
    the daily ATR20 of the last completed daily bar); the static volume test stays green;
  - uv run pytest, ruff, mypy, mypy --platform linux green.
OUT OF SCOPE: any walk-forward, metric, PBO/DSR, report or comparison between strategies; any change to the
              engine, broker, SPEC or core/types.py; touching data on/after 2025-10-01; logging trials (no results
              exist yet).
REPORT BACK: files changed, test results, the model you ran on, open questions, any SPEC ambiguity found.
```

---

## Brief 2 — validation-analyst

```
TASK: Build the Phase 3 walk-forward and evaluation harness and run the pre-registered evaluation of S0 and
      C1–C5 exactly per docs/SPEC.md §7.2, then write the G3 report. Do not edit strategy code.
PHASE: 3        AGENT: validation-analyst
CONTEXT: CLAUDE.md §3.5, §8, §9; docs/SPEC.md §7 G3/G4, §7.1 (DSR definition, go-live rule, holdout band),
         §7.2 (walk-forward folds, per-fold selection, grids, trial count, thresholds, PBO S=16, costs, both
         fill modes, T6 steps, robustness), D-059, D-060, D-067, D-071; research/EXPERIMENTS.md.
ALLOWED FILES: src/tbot/validation/**, tests/validation/**, scripts/validation/**, research/EXPERIMENTS.md
               (append-only rows), research/reports/phase3_*/**, docs/reports/g3-*.md (Persian).
CONTRACTS USED: engine/broker/strategy APIs as merged — do not modify.
ACCEPTANCE:
  - BEFORE the first run: every grid point to be run is listed in research/EXPERIMENTS.md with its parameters;
    every run that produces any number is logged (failed or abandoned included); N for DSR follows D-059;
  - walk-forward: 12 periods exactly as §7.2 B, anchored replay from 2018-01-01, per-period choice by training
    Sharpe (manual-delay, after costs) over the eligible points' COMMON tradable days, eligibility exactly as the
    §7.2 B table, pooled OOS series = periods concatenated; a test proves no OOS bar is used in any training
    choice (truncation-style test on the harness);
  - neighbour test, buy-and-hold benchmark and ETH/Stooq robustness exactly as §7.2 E and I (BTC's per-period
    choices reused unchanged, no re-selection);
  - every Phase 3 number labelled PROVISIONAL (D-067 curve); the G3 verdict comes only from ONE complete rerun
    of the whole procedure with the frozen curve (D-085), including per-period selection;
  - metrics: Sharpe (365), maxDD, PBO (CSCV, S = 16), DSR (§7.1 definition), MinTRL (95 %, vs 0 and vs B&H),
    turnover and annual cost drag; both fill modes; ×2 cost stress;
  - the slippage model used is exactly the one Parham chose in §7.2 F, labelled as such in every table;
  - ETH robustness with the same rules; Stooq indices reported as informational only;
  - nothing on/after 2025-10-01 is loaded (the loader's double lock stays engaged; no unsealing);
  - uv run pytest, ruff, mypy, mypy --platform linux green; Persian report with numbers, pass/fail per gate,
    the T6 outcome (X or none) and what is needed from Parham.
OUT OF SCOPE: unsealing the holdout (G4 is a separate, single, approved step); changing any threshold, grid or
              fold; editing src/tbot/strategies/**, the engine or SPEC.
REPORT BACK: files changed, the trial log diff, results tables, the model you ran on, risks, open questions.
```
