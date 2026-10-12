"""Pre-flight gate of the Phase 3 run script: it must refuse an incomplete setup."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbot.core.types import Strategy
from tbot.strategies.phase3_registry import PHASE3_ENTRIES, Phase3Entry
from tbot.strategies.sma_filter import SmaFilter
from tbot.validation.preflight import check_engine, check_registry_importable, check_trial_log, preflight
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


def test_complete_log_passes() -> None:
    assert check_trial_log(parse_trial_log(HEADER + S0 + _grid_rows(GRID_KEYS))) == []


def test_missing_grid_points_are_named() -> None:
    log = parse_trial_log(HEADER + S0 + _grid_rows(GRID_KEYS[:-2]))
    problems = check_trial_log(log)
    assert any(GRID_KEYS[-1] in p for p in problems)
    assert any("non-S0 trial rows" in p for p in problems)


def test_unregistered_s0_is_a_problem() -> None:
    problems = check_trial_log(parse_trial_log(HEADER + _grid_rows(GRID_KEYS)))
    assert any("S0 is not registered" in p for p in problems)


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


def test_preflight_aggregates_every_check() -> None:
    def loader(entry: Phase3Entry) -> Strategy:
        raise ModuleNotFoundError("x", name="x")

    problems = preflight(parse_trial_log(HEADER), loader)
    assert any("not logged" in p for p in problems)
    assert any("module not implemented" in p for p in problems)
    assert isinstance(check_engine(), list)


@pytest.mark.parametrize("n", [16, 18])
def test_a_registry_that_is_not_17_points_is_refused(n: int) -> None:
    entries = PHASE3_ENTRIES[: n + 1] if n < 17 else (*PHASE3_ENTRIES, PHASE3_ENTRIES[-1])
    problems = check_trial_log(parse_trial_log(HEADER + S0 + _grid_rows(GRID_KEYS)), entries)
    assert any("pre-registers 17" in p for p in problems)


def test_run_script_refuses_to_evaluate_with_an_empty_trial_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.validation import phase3_run

    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(HEADER, encoding="utf-8")
    monkeypatch.setattr(phase3_run, "EXPERIMENTS", log)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not load data or evaluate after a failed pre-flight")

    monkeypatch.setattr(phase3_run, "_load_market", boom)
    monkeypatch.setattr(phase3_run, "evaluate", boom)
    assert phase3_run.main(["--out", str(phase3_run.REPORTS / "x.md")]) == 2
    assert "PRE-FLIGHT FAILED" in capsys.readouterr().err
    assert not (phase3_run.REPORTS / "x.md").exists()


def test_run_script_refuses_an_output_path_outside_research_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.validation import phase3_run

    log = tmp_path / "EXPERIMENTS.md"
    log.write_text(HEADER + S0 + _grid_rows(GRID_KEYS), encoding="utf-8")
    monkeypatch.setattr(phase3_run, "EXPERIMENTS", log)
    assert phase3_run.main(["--out", str(tmp_path / "r.md"), "--preflight-only"]) == 2
