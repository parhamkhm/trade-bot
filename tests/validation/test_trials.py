"""The trial-log reader: N for the DSR is the logged row count, not a constant."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbot.validation.trials import normalise_key, parse_trial_log, read_trial_log

HEADER = (
    "| # | date | phase | strategy | parameters | range | tf | symbol | result | notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)
PLACEHOLDER = "| — | — | — | — | — | — | — | — | — | none yet |\n"
S0_ROW = "| S0 | 2026-10-10 | lock | S0 `sma100_1d` | fixed | n/a | 1d | BTCUSDT | none | locked |\n"


def _row(n: int, name: str) -> str:
    return f"| T{n} | 2026-10-11 | 3 | {name} | p | r | 1d | BTCUSDT | none | n |\n"


def test_header_separator_and_placeholder_rows_are_not_trials() -> None:
    log = parse_trial_log(HEADER + PLACEHOLDER)
    assert log.rows == () and log.n_non_s0 == 0


def test_s0_is_a_row_but_not_a_non_s0_trial() -> None:
    log = parse_trial_log(HEADER + PLACEHOLDER + S0_ROW)
    assert len(log.rows) == 1 and log.n_non_s0 == 0


def test_every_other_data_row_is_one_trial() -> None:
    text = HEADER + S0_ROW + _row(1, "C1 scale=1") + _row(2, "C1 scale=2") + _row(3, "C4 p=10")
    log = parse_trial_log(text)
    assert len(log.rows) == 4 and log.n_non_s0 == 3


def test_non_table_lines_are_ignored() -> None:
    text = "# EXPERIMENTS\n\nsome prose | with a pipe\n- bullet\n" + HEADER + _row(1, "C2 x")
    assert parse_trial_log(text).n_non_s0 == 1


def test_missing_reports_keys_no_row_mentions() -> None:
    log = parse_trial_log(HEADER + S0_ROW + _row(1, "C4 p=10") + _row(2, "C1-s2"))
    assert log.missing(["C4-p10", "C1 s2", "C5 x=3"]) == ["C5 x=3"]


def test_s0_row_does_not_satisfy_a_grid_key() -> None:
    log = parse_trial_log(HEADER + S0_ROW)
    assert log.missing(["S0"]) == ["S0"]  # only non-S0 rows count


def test_normalise_key_ignores_spacing_dashes_equals_and_case() -> None:
    assert normalise_key("C4 p=10") == normalise_key("`C4-P10`") == "c4p10"


def test_read_trial_log_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "EXPERIMENTS.md"
    path.write_text(HEADER + S0_ROW + _row(1, "C1"), encoding="utf-8")
    assert read_trial_log(path).n_non_s0 == 1


RESULTS_TABLE = "\n| # | metric | value |\n|---|---|---|\n| R1 | sharpe | 0.5 |\n| R2 | maxdd | 0.2 |\n"


def test_only_the_trial_table_is_parsed() -> None:
    """A second table whose header also starts with '#' (results, disclosures) is not part of the count."""
    text = HEADER + S0_ROW + _row(1, "C1") + "\n\nSome prose.\n" + RESULTS_TABLE
    log = parse_trial_log(text)
    assert log.n_non_s0 == 1 and len(log.rows) == 2


def test_a_table_before_the_trial_table_is_ignored() -> None:
    other = "| name | value |\n|---|---|\n| foo | 1 |\n| bar | 2 |\n\n"
    assert parse_trial_log(other + HEADER + _row(1, "C1")).n_non_s0 == 1


def test_a_file_without_a_trial_table_is_an_error_not_zero_trials() -> None:
    with pytest.raises(ValueError, match="no trial table"):
        parse_trial_log("# EXPERIMENTS\n\njust prose\n")


@pytest.mark.parametrize(
    "row",
    [
        "| S0-SMA50 | 2026-10-12 | 3 | S0 variant SMA50 | p | r | 1d | BTCUSDT | none | n |\n",
        "| T9 | 2026-10-12 | 3 | S0 `sma100_1d` vol 0.20 | p | r | 1d | BTCUSDT | none | n |\n",
        "| T9 | 2026-10-12 | 3 | `S0var-vol40` | p | r | 1d | BTCUSDT | none | n |\n",
    ],
)
def test_s0_family_rows_are_never_counted(row: str) -> None:
    log = parse_trial_log(HEADER + S0_ROW + _row(1, "C1") + row)
    assert log.n_non_s0 == 1 and len(log.s0_rows) == 2
    assert log.s0_registered


def test_s0_registered_needs_the_exact_s0_row() -> None:
    log = parse_trial_log(HEADER + "| S0-SMA50 | d | p | S0 variant | p | r | 1d | B | n | n |\n")
    assert not log.s0_registered and log.n_non_s0 == 0


def test_a_c_row_starting_with_s_is_not_s0() -> None:
    assert parse_trial_log(HEADER + _row(1, "S1 blend")).n_non_s0 == 1


def test_the_repository_log_registers_s0() -> None:
    """Reads the real log (read-only): S0 must be registered. Skips if the file is absent.

    SPEC 7.2 D asks for the 14 grid points to be logged before the first run; the run script refuses to start
    until they are, so this test deliberately does not assert the grid rows.
    """
    path = Path(__file__).resolve().parents[2] / "research" / "EXPERIMENTS.md"
    if not path.exists():
        pytest.skip("research/EXPERIMENTS.md not found")
    log = read_trial_log(path)
    assert any(r.strip("| ").split("|")[0].strip() == "S0" for r in log.rows)
