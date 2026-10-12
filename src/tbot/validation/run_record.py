"""The run record of SPEC 7.2 H1 ("log every run"): one JSON line per invocation of the Phase 3 script.

A record is written for EVERY invocation that gets past argument parsing: a refused pre-flight, a
pre-flight-only check and a full evaluation alike, so the log shows how often the harness was started and
what happened, not only the runs that produced a report. The file is append-only; this module never rewrites
or deletes a line.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["RunRecord", "append_run_record", "git_head", "read_run_records", "sha256_of"]

OUTCOMES = ("preflight-failed", "preflight-ok", "evaluated", "error")


@dataclass(frozen=True)
class RunRecord:
    outcome: str  # one of OUTCOMES
    git_head: str
    costs: str  # CostSpec.label: names the slippage model in force
    flat_slippage_allowed: bool  # --allow-flat-slippage-for-tests was given (never in the real run)
    non_s0_trials: int | None
    report: str | None = None  # path of the report written, relative to the repo, if any
    report_sha256: str | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)
    started_utc: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown run outcome {self.outcome!r}")


def git_head(repo: Path) -> str:
    """HEAD commit of ``repo`` (``unknown`` when git is unavailable: the record is still written)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_run_record(path: Path, record: RunRecord, *, now: datetime | None = None) -> RunRecord:
    """Append ``record`` (stamped with the UTC time) as one JSON line; returns the stamped record."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    stamped = RunRecord(**{**asdict(record), "started_utc": stamp, "problems": tuple(record.problems)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(stamped), sort_keys=True) + "\n")
    return stamped


def read_run_records(path: Path) -> Sequence[RunRecord]:
    if not path.exists():
        return ()
    out: list[RunRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            data["problems"] = tuple(data.get("problems", ()))
            out.append(RunRecord(**data))
    return tuple(out)
