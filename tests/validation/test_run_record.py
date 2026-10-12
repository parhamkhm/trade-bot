"""The H1 run record: one append-only JSON line per invocation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tbot.validation.run_record import RunRecord, append_run_record, git_head, read_run_records, sha256_of


def _record(outcome: str = "evaluated") -> RunRecord:
    return RunRecord(outcome, "abc", "10 bps", False, 14, problems=("p1",))


def test_records_are_appended_never_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "log.jsonl"
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    append_run_record(path, _record("preflight-failed"), now=now)
    append_run_record(path, _record("evaluated"), now=now)
    rows = read_run_records(path)
    assert [r.outcome for r in rows] == ["preflight-failed", "evaluated"]
    assert rows[0].started_utc == "2026-10-12T09:00:00+00:00"
    assert rows[0].problems == ("p1",) and rows[0].non_s0_trials == 14
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_an_unknown_outcome_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown run outcome"):
        RunRecord("passed", "abc", "c", False, None)


def test_read_of_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert read_run_records(tmp_path / "none.jsonl") == ()


def test_sha256_is_stable_and_git_head_is_a_commit() -> None:
    assert sha256_of("a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    head = git_head(Path(__file__).resolve().parents[2])
    assert head == "unknown" or len(head) == 40
