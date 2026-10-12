"""Phase 3 walk-forward evaluation, steps H1-H5 (docs/SPEC.md section 7.2 H). PROVISIONAL numbers only.

Run from the repository root::

    uv run python -m scripts.validation.phase3_run --preflight-only --slippage-model pkg.module:factory
    uv run python -m scripts.validation.phase3_run --slippage-model pkg.module:factory \
        --out research/reports/phase3-walkforward.md [--stooq SPX=path/to/spx_d.csv ...]

The script REFUSES to start (exit code 2) until the pre-flight passes. A missing input is a reason to refuse,
never a failed gate:

* ``research/EXPERIMENTS.md`` holds exactly 17 non-S0 trial rows (S0, the S0 variants and ``S0-...`` rows are
  not counted), the 17 grid points are named in them and S0 is registered;
* the six SPEC 7.1 S0 variants are registered in ``S0_VARIANTS`` (sensitivity runs, not trials);
* the engine has the SPEC 7.1 stop/band mechanics and is at or after commit ``8d99335``;
* every registered strategy module (grid points and S0 variants) imports and builds;
* BTCUSDT AND ETHUSDT bars (1d, 4h, 1h) are in the local Parquet store;
* a depth-curve slippage model is supplied (``--slippage-model module:factory``, built from configuration
  and returning an object with ``slippage_bps(ctx)``). Flat slippage is refused unless
  ``--allow-flat-slippage-for-tests`` is given; that flag exists for the unit tests and is never used in the
  real run.

Stooq indices are informational: an absent or unreadable file is reported "not run" and never fails a gate.

Every invocation that gets past argument parsing appends one line to the run log (SPEC 7.2 H1 "log every
run"), ``research/reports/phase3-run-log.jsonl``: outcome, git HEAD, costs label, trial count, report hash.

Data: Binance klines from the local Parquet store through ``tbot.validation.real_data`` (end capped at the
sealed holdout start 2025-10-01; this script cannot load the holdout and never passes ``allow_holdout``).
H6-H7 (the one-shot holdout run) are NOT part of this script.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from tbot.core.types import Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, S0_VARIANTS, load
from tbot.validation.eligibility import first_tradable_close
from tbot.validation.grid import HISTORY_START, HOLDOUT_START, TRAIN_START, grid_points
from tbot.validation.pipeline import EvalInputs, evaluate
from tbot.validation.preflight import preflight
from tbot.validation.real_data import find_parquet_root, real_bars
from tbot.validation.report import render_report
from tbot.validation.robustness import parse_stooq_csv
from tbot.validation.run_record import RunRecord, append_run_record, git_head, sha256_of
from tbot.validation.runs import NORMAL_COSTS, CostSpec, Market
from tbot.validation.trials import TrialLog, read_trial_log
from tbot.validation.walk_forward import WalkForwardSpec, registry_candidates

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "research" / "EXPERIMENTS.md"
REPORTS = REPO / "research" / "reports"
RUN_LOG = REPORTS / "phase3-run-log.jsonl"
TIMEFRAMES = (Timeframe.D1, Timeframe.H4, Timeframe.H1)
SYMBOLS = (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"))
INDEX_PERIODS_PER_YEAR = 252.0
_PROBE_SPAN = timedelta(days=30)


def _load_market(symbol: str, base_asset: str) -> Market:
    bars = {tf: real_bars(symbol, tf, HISTORY_START, HOLDOUT_START) for tf in TIMEFRAMES}
    return Market(symbol, bars, base_asset=base_asset)


def _missing_series() -> list[str]:
    """``SYMBOL/tf`` for every required series without bars at the start and at the end of the range."""
    if find_parquet_root() is None:
        return [f"{sym}/{tf.value}" for sym, _ in SYMBOLS for tf in TIMEFRAMES]
    missing: list[str] = []
    for sym, _ in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                head = real_bars(sym, tf, HISTORY_START, HISTORY_START + _PROBE_SPAN)
                tail = real_bars(sym, tf, HOLDOUT_START - _PROBE_SPAN, HOLDOUT_START)
            except (OSError, ValueError, KeyError):
                head = tail = []
            if not head or not tail:
                missing.append(f"{sym}/{tf.value}")
    return missing


def _slippage_cost(spec: str | None) -> CostSpec:
    """Costs with the depth-curve model named ``module:attr`` (an object, or a no-argument factory)."""
    if not spec:
        return NORMAL_COSTS
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"--slippage-model expects module:factory, got {spec!r}")
    obj = getattr(importlib.import_module(module_name), attr)
    model = obj() if callable(obj) and not hasattr(obj, "slippage_bps") else obj
    if not hasattr(model, "slippage_bps"):
        raise SystemExit(f"{spec!r} did not produce a slippage model (no slippage_bps)")
    return NORMAL_COSTS.with_model(model, f"10 bps fee + slippage model {spec}")


def _index_market(spec: str) -> Market | None:
    """An index from ``NAME=path.csv``; None (reported "not run") when the file is absent or unreadable."""
    name, _, path = spec.partition("=")
    if not name or not path:
        raise SystemExit(f"--stooq expects NAME=path.csv, got {spec!r}")
    try:
        text = Path(path).read_text(encoding="utf-8")
        series = parse_stooq_csv(text, name)
    except (OSError, ValueError) as exc:
        print(f"Stooq index {name}: not run ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None
    return Market(name, {Timeframe.D1: series}, periods_per_year=INDEX_PERIODS_PER_YEAR, base_asset=name)


def _record(
    outcome: str,
    costs: CostSpec,
    allow_flat: bool,
    log: TrialLog | None,
    problems: Sequence[str] = (),
    report: Path | None = None,
    text: str | None = None,
) -> None:
    append_run_record(
        RUN_LOG,
        RunRecord(
            outcome=outcome,
            git_head=git_head(REPO),
            costs=costs.label,
            flat_slippage_allowed=allow_flat,
            non_s0_trials=None if log is None else log.n_non_s0,
            report=None if report is None else str(report),
            report_sha256=None if text is None else sha256_of(text),
            problems=tuple(problems),
        ),
    )


def _read_log() -> tuple[TrialLog | None, list[str]]:
    if not EXPERIMENTS.exists():
        return None, [f"{EXPERIMENTS} not found"]
    try:
        return read_trial_log(EXPERIMENTS), []
    except ValueError as exc:
        return None, [f"{EXPERIMENTS}: {exc}"]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preflight-only", action="store_true", help="run the guards and stop")
    ap.add_argument("--out", type=Path, default=REPORTS / "phase3-walkforward.md")
    ap.add_argument("--slippage-model", metavar="MODULE:FACTORY", help="depth-curve slippage model (config)")
    ap.add_argument(
        "--allow-flat-slippage-for-tests",
        action="store_true",
        help="TESTS ONLY: accept the flat-bps placeholder; never used in the real run",
    )
    ap.add_argument("--stooq", action="append", default=[], metavar="NAME=CSV", help="index CSV (repeatable)")
    args = ap.parse_args(argv)

    costs = _slippage_cost(args.slippage_model)
    allow_flat: bool = args.allow_flat_slippage_for_tests
    log, problems = _read_log()
    if log is not None:
        problems.extend(
            preflight(
                log,
                load,
                costs=costs,
                allow_flat_slippage=allow_flat,
                missing_series=_missing_series(),
            )
        )
    out = args.out.resolve()
    if REPORTS.resolve() not in out.parents:
        problems.append(f"--out must be inside {REPORTS} (got {out})")
    if problems or log is None:
        print("PRE-FLIGHT FAILED, nothing was evaluated:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        _record("preflight-failed", costs, allow_flat, log, problems)
        return 2
    print(f"pre-flight ok: {log.n_non_s0} logged non-S0 trials")
    if args.preflight_only:
        _record("preflight-ok", costs, allow_flat, log)
        return 0
    try:
        text = _evaluate_and_render(args.stooq, costs, log)
    except Exception as exc:
        _record("error", costs, allow_flat, log, [f"{type(exc).__name__}: {exc}"])
        raise
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _record("evaluated", costs, allow_flat, log, report=out.relative_to(REPO), text=text)
    print(f"wrote {out}")
    return 0


def _evaluate_and_render(stooq: Sequence[str], costs: CostSpec, log: TrialLog) -> str:
    btc = _load_market("BTCUSDT", "BTC")
    eth = _load_market("ETHUSDT", "ETH")
    bars = {tf: btc.series(tf) for tf in (Timeframe.D1, Timeframe.H4)}
    points = grid_points()
    first = {key: first_tradable_close(point, bars) for key, point in points.items()}
    first["S0"] = TRAIN_START
    entry_by_key = {e.key: e for e in (*PHASE3_ENTRIES, *S0_VARIANTS)}
    indices = [m for m in (_index_market(s) for s in stooq) if m is not None]
    inputs = EvalInputs(
        btc=btc,
        spec=WalkForwardSpec(),
        factory=lambda key: load(entry_by_key[key]),
        candidates=registry_candidates(),
        first_tradable=first,
        logged_non_s0_trials=log.n_non_s0,
        eth=eth,
        s0_variant_keys=[v.key for v in S0_VARIANTS],
        indices={m.symbol: m for m in indices},
        costs=costs,
    )
    return render_report(evaluate(inputs), inputs.spec)


if __name__ == "__main__":
    raise SystemExit(main())
