"""The trial log: how many trials ``research/EXPERIMENTS.md`` holds, and whether the 17 grid points are in it.

SPEC 7.2 D: every configuration that ever produced a backtest number counts toward the DSR trial count, and
"all 17 grid points are logged in ``research/EXPERIMENTS.md`` before the first run". This module reads the
table (one trial per data row) so the run scripts can refuse to start otherwise, and so N in the DSR is the
logged number and not a constant typed in code.

A row is a table line whose first cell is not ``#``, a separator, or the placeholder dash. S0 is a row but is
not a *non-S0* trial: DSR for the grid candidates uses N = rows other than S0 (SPEC 7.2 D); S0 uses N = 1.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TrialLog", "normalise_key", "parse_trial_log"]

_PLACEHOLDER = {"—", "-", ""}


def normalise_key(text: str) -> str:
    """Compare keys regardless of spacing, dashes, equals signs and backticks: ``C4 p=10`` == ``C4-p10``."""
    return re.sub(r"[\s`*_=\-]", "", text).lower()


@dataclass(frozen=True)
class TrialLog:
    rows: tuple[str, ...]  # raw table rows, header/separator/placeholder excluded
    non_s0_rows: tuple[str, ...]

    @property
    def n_non_s0(self) -> int:
        return len(self.non_s0_rows)

    def missing(self, keys: Iterable[str]) -> list[str]:
        """Grid keys that no non-S0 row mentions (normalised substring match on the row text)."""
        hay = [normalise_key(r) for r in self.non_s0_rows]
        return [k for k in keys if not any(normalise_key(k) in h for h in hay)]


def parse_trial_log(text: str) -> TrialLog:
    rows: list[str] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        first = cells[0]
        if first == "#" or (set(first) <= {"-", ":", " "} and first.strip("-: ") == ""):
            continue
        if first in _PLACEHOLDER:
            continue
        rows.append(line)
    non_s0 = tuple(r for r in rows if r.strip("| ").split("|")[0].strip() != "S0")
    return TrialLog(rows=tuple(rows), non_s0_rows=non_s0)


def read_trial_log(path: Path) -> TrialLog:
    return parse_trial_log(path.read_text(encoding="utf-8"))
