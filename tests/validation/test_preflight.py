"""Pre-flight gate of the Phase 3 run script: it must refuse an incomplete setup."""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from tbot.core.types import Strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, S0_VARIANTS, Phase3Entry
from tbot.strategies.sma_filter import SmaFilter
from tbot.validation.preflight import (
    check_data,
    check_engine,
    check_engine_commit,
    check_registry_importable,
    check_s0_variants,
    check_slippage,
    check_trial_log,
    preflight,
)
from tbot.validation.run_record import read_run_records
from tbot.validation.runs import NORMAL_COSTS
from tbot.validation.trials import parse_trial_log

HEADER = (
    "| # | d | p | strategy | params | r | tf | sym | res | n |\n|---|---|---|---|---|---|---|---|---|---|\n"
)
S0 = "| S0 | 2026-10-10 | lock | S0 | fixed | n/a | 1d | BTCUSDT | none | n |\n"


def _grid_rows(keys: list[str]) -> str:
    return "".join(
        f"| T{i} | 2026-10-12 | 3 | {k} | p | r | tf | BTCUSDT | none | n |\n" for i, k in enumerate(keys)
    )


GRID_KEYS = [e.key for e in PHASE3_ENTRIES if e.key != "S0"]
COMPLETE = HEADER + S0 + _grid_rows(GRID_KEYS)


class _Curve:
    """Stands in for the depth-curve model: anything with ``slippage_bps``."""

    def slippage_bps(self, ctx: object) -> Decimal:  # noqa: ARG002
        return Decimal(7)


def test_complete_log_passes() -> None:
    assert check_trial_log(parse_trial_log(COMPLETE)) == []


def test_missing_grid_points_are_named() -> None:
    log = parse_trial_log(HEADER + S0 + _grid_rows(GRID_KEYS[:-2]))
    problems = check_trial_log(log)
    assert any(GRID_KEYS[-1] in p for p in problems)
    assert any("non-S0 trial rows" in p for p in problems)


def test_unregistered_s0_is_a_problem() -> None:
    problems = check_trial_log(parse_trial_log(HEADER + _grid_rows(GRID_KEYS)))
    assert any("S0 is not registered" in p for p in problems)


def test_s0_family_rows_do_not_count_toward_the_seventeen() -> None:
    extra = "| S0-SMA50 | d | 3 | S0 variant | p | r | 1d | BTCUSDT | none | n |\n"
    assert check_trial_log(parse_trial_log(COMPLETE + extra)) == []


def test_an_eighteenth_non_s0_row_is_refused() -> None:
    extra = "| T99 | d | 3 | C9 an extra trial | p | r | 1d | BTCUSDT | none | n |\n"
    problems = check_trial_log(parse_trial_log(COMPLETE + extra))
    assert any("18 non-S0 trial rows logged, exactly 17 required" in p for p in problems)


def test_the_six_registered_variants_pass() -> None:
    assert check_s0_variants() == []
    assert len(S0_VARIANTS) == 6 and not any(v.counts_as_trial for v in S0_VARIANTS)


def test_a_variant_that_counts_as_a_trial_or_is_missing_is_refused() -> None:
    counted = (dataclasses.replace(S0_VARIANTS[0], counts_as_trial=True), *S0_VARIANTS[1:])
    assert any("must not count as a trial" in p for p in check_s0_variants(counted))
    assert any("not exactly SMA" in p for p in check_s0_variants(S0_VARIANTS[:5]))
    assert any("not exactly SMA" in p for p in check_s0_variants(()))


def test_a_variant_keyed_like_a_trial_entry_is_refused() -> None:
    clash = (dataclasses.replace(S0_VARIANTS[0], key="C5-x5"), *S0_VARIANTS[1:])
    assert any("must not be one of the trial entries" in p for p in check_s0_variants(clash))


def test_registry_loader_failures_are_reported_per_entry() -> None:
    def loader(entry: Phase3Entry) -> Strategy:
        if entry.key == "S0":
            return SmaFilter(100)
        if entry.key.startswith("C1"):
            raise ModuleNotFoundError("no donchian", name="tbot.strategies.donchian")
        raise ValueError("bad params")

    problems = check_registry_importable(loader)
    assert any(p.startswith("C1-(10,28,50): module not implemented") for p in problems)
    assert any(p.startswith("C2-(15,45,90): cannot be built (ValueError") for p in problems)
    assert not any(p.startswith("S0") for p in problems)


def test_preflight_loads_the_variants_too() -> None:
    seen: list[str] = []

    def loader(entry: Phase3Entry) -> Strategy:
        seen.append(entry.key)
        return SmaFilter(100)

    preflight(parse_trial_log(COMPLETE), loader)
    assert {v.key for v in S0_VARIANTS} <= set(seen)


def test_preflight_aggregates_every_check() -> None:
    def loader(entry: Phase3Entry) -> Strategy:
        raise ModuleNotFoundError("x", name="x")

    problems = preflight(parse_trial_log(HEADER), loader, costs=NORMAL_COSTS, missing_series=["ETHUSDT/1d"])
    assert any("not logged" in p for p in problems)
    assert any("module not implemented" in p for p in problems)
    assert any("flat slippage" in p for p in problems)
    assert any("required data missing: ETHUSDT/1d" in p for p in problems)
    assert isinstance(check_engine(), list)


@pytest.mark.parametrize("n", [16, 18])
def test_a_registry_that_is_not_17_points_is_refused(n: int) -> None:
    entries = PHASE3_ENTRIES[: n + 1] if n < 17 else (*PHASE3_ENTRIES, PHASE3_ENTRIES[-1])
    problems = check_trial_log(parse_trial_log(COMPLETE), entries)
    assert any("pre-registers 17" in p for p in problems)


def test_flat_slippage_is_refused_unless_explicitly_allowed_for_tests() -> None:
    assert any("flat slippage" in p for p in check_slippage(NORMAL_COSTS, allow_flat=False))
    assert check_slippage(NORMAL_COSTS, allow_flat=True) == []


def test_a_slippage_model_object_satisfies_the_check_and_survives_the_stress() -> None:
    costs = NORMAL_COSTS.with_model(_Curve(), "10 bps fee + curve")
    assert not costs.is_flat_slippage and check_slippage(costs, allow_flat=False) == []
    stressed = costs.stressed()
    assert not stressed.is_flat_slippage and check_slippage(stressed, allow_flat=False) == []
    assert stressed.fee_bps == 2 * costs.fee_bps


def test_missing_data_is_a_refusal_naming_the_series() -> None:
    assert check_data([]) == []
    assert check_data(["ETHUSDT/4h", "BTCUSDT/1h"]) == [
        "required data missing: ETHUSDT/4h",
        "required data missing: BTCUSDT/1h",
    ]


def test_the_engine_commit_is_checked_with_git() -> None:
    assert check_engine_commit() == []  # this checkout contains the Phase 2 engine merge
    assert any("not an ancestor" in p or "git failed" in p for p in check_engine_commit(min_commit="0" * 40))


def test_an_unreadable_repository_is_a_refusal(tmp_path: Path) -> None:
    assert check_engine_commit(tmp_path)  # not a git repository: cannot verify -> refuse


@pytest.fixture
def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    from scripts.validation import phase3_run

    monkeypatch.setattr(phase3_run, "RUN_LOG", tmp_path / "run-log.jsonl")
    monkeypatch.setattr(phase3_run, "_missing_series", lambda: [])

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not load data or evaluate after a failed pre-flight")

    monkeypatch.setattr(phase3_run, "_load_market", boom)
    monkeypatch.setattr(phase3_run, "evaluate", boom)
    return phase3_run


def test_run_script_refuses_to_evaluate_with_an_empty_trial_log(
    tmp_path: Path, script: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(HEADER, encoding="utf-8")
    monkeypatch.setattr(script, "EXPERIMENTS", log)
    out = script.REPORTS / "x.md"
    assert script.main(["--out", str(out), "--allow-flat-slippage-for-tests"]) == 2
    assert "PRE-FLIGHT FAILED" in capsys.readouterr().err
    assert not out.exists()
    (record,) = read_run_records(script.RUN_LOG)
    assert record.outcome == "preflight-failed" and record.report is None and record.problems


def test_run_script_refuses_a_file_without_a_trial_table(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text("no table here\n", encoding="utf-8")
    monkeypatch.setattr(script, "EXPERIMENTS", log)
    assert script.main(["--allow-flat-slippage-for-tests"]) == 2
    assert "no trial table" in capsys.readouterr().err


def test_run_script_refuses_flat_slippage_by_default(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(COMPLETE, encoding="utf-8")
    monkeypatch.setattr(script, "EXPERIMENTS", log)
    assert script.main(["--preflight-only"]) == 2
    assert "flat slippage" in capsys.readouterr().err
    assert read_run_records(script.RUN_LOG)[-1].flat_slippage_allowed is False


def test_run_script_refuses_when_eth_data_are_missing(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(COMPLETE, encoding="utf-8")
    monkeypatch.setattr(script, "EXPERIMENTS", log)
    monkeypatch.setattr(script, "_missing_series", lambda: ["ETHUSDT/1d"])
    assert script.main(["--preflight-only", "--allow-flat-slippage-for-tests"]) == 2
    assert "required data missing: ETHUSDT/1d" in capsys.readouterr().err


def test_run_script_refuses_an_output_path_outside_research_reports(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(COMPLETE, encoding="utf-8")
    monkeypatch.setattr(script, "EXPERIMENTS", log)
    argv = ["--out", str(tmp_path / "r.md"), "--preflight-only", "--allow-flat-slippage-for-tests"]
    assert script.main(argv) == 2


def test_the_script_does_not_import_the_tests_package() -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "validation" / "phase3_run.py"
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert modules and not any(m == "tests" or m.startswith("tests.") for m in modules)
