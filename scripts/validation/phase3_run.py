"""Phase 3 walk-forward evaluation, steps H1-H5 (docs/SPEC.md section 7.2 H). PROVISIONAL numbers only.

Run from the repository root::

    uv run python -m scripts.validation.phase3_run --preflight-only
    uv run python -m scripts.validation.phase3_run --out research/reports/phase3-walkforward.md \
        [--eth] [--stooq SPX=path/to/spx_d.csv ...]

The script REFUSES to start (exit code 2) until the pre-flight passes: all 17 grid points and S0 are logged in
``research/EXPERIMENTS.md``, the engine has the SPEC 7.1 stop/band mechanics, every registered strategy module
imports and builds, and the local Parquet store is present. Only then does it evaluate.

Data: Binance klines from the local Parquet store through ``tests.validation.data.real_bars`` (end capped at
the sealed holdout start 2025-10-01; this script cannot load the holdout and never passes ``allow_holdout``).
H6-H7 (the one-shot holdout run) are NOT part of this script.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tests.validation.data import find_parquet_root, real_bars

from tbot.core.types import Timeframe
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, load
from tbot.validation.eligibility import first_tradable_close
from tbot.validation.grid import HISTORY_START, HOLDOUT_START, TRAIN_START, grid_points
from tbot.validation.pipeline import EvalInputs, evaluate
from tbot.validation.preflight import preflight
from tbot.validation.report import render_report
from tbot.validation.robustness import parse_stooq_csv
from tbot.validation.runs import Market
from tbot.validation.trials import read_trial_log
from tbot.validation.walk_forward import WalkForwardSpec, registry_candidates

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "research" / "EXPERIMENTS.md"
REPORTS = REPO / "research" / "reports"
TIMEFRAMES = (Timeframe.D1, Timeframe.H4, Timeframe.H1)
INDEX_PERIODS_PER_YEAR = 252.0


def _load_market(symbol: str, base_asset: str) -> Market:
    bars = {tf: real_bars(symbol, tf, HISTORY_START, HOLDOUT_START) for tf in TIMEFRAMES}
    return Market(symbol, bars, base_asset=base_asset)


def _index_market(spec: str) -> Market:
    name, _, path = spec.partition("=")
    if not name or not path:
        raise SystemExit(f"--stooq expects NAME=path.csv, got {spec!r}")
    text = Path(path).read_text(encoding="utf-8")
    return Market(
        name,
        {Timeframe.D1: parse_stooq_csv(text, name)},
        periods_per_year=INDEX_PERIODS_PER_YEAR,
        base_asset=name,
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preflight-only", action="store_true", help="run the guards and stop")
    ap.add_argument("--out", type=Path, default=REPORTS / "phase3-walkforward.md")
    ap.add_argument("--eth", action="store_true", help="also run the ETH/USDT robustness reuse")
    ap.add_argument("--stooq", action="append", default=[], metavar="NAME=CSV", help="index CSV (repeatable)")
    args = ap.parse_args(argv)

    problems: list[str] = []
    log = read_trial_log(EXPERIMENTS) if EXPERIMENTS.exists() else None
    if log is None:
        problems.append(f"{EXPERIMENTS} not found")
    else:
        problems.extend(preflight(log, load))
    if find_parquet_root() is None:
        problems.append("local Parquet store not found (set TBOT_VALIDATION_DATA)")
    out = args.out.resolve()
    if REPORTS.resolve() not in out.parents:
        problems.append(f"--out must be inside {REPORTS} (got {out})")
    if problems or log is None:
        print("PRE-FLIGHT FAILED, nothing was evaluated:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    print(f"pre-flight ok: {log.n_non_s0} logged non-S0 trials")
    if args.preflight_only:
        return 0

    btc = _load_market("BTCUSDT", "BTC")
    bars = {tf: btc.series(tf) for tf in (Timeframe.D1, Timeframe.H4)}
    points = grid_points()
    first = {key: first_tradable_close(point, bars) for key, point in points.items()}
    first["S0"] = TRAIN_START
    entry_by_key = {e.key: e for e in PHASE3_ENTRIES}
    inputs = EvalInputs(
        btc=btc,
        spec=WalkForwardSpec(),
        factory=lambda key: load(entry_by_key[key]),
        candidates=registry_candidates(),
        first_tradable=first,
        logged_non_s0_trials=log.n_non_s0,
        eth=_load_market("ETHUSDT", "ETH") if args.eth else None,
        indices={m.symbol: m for m in (_index_market(s) for s in args.stooq)},
    )
    evaluation = evaluate(inputs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(evaluation, inputs.spec), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
