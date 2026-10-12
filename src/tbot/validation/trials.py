"""The trial log: how many trials ``research/EXPERIMENTS.md`` holds, and whether the 14 grid points are in it.

SPEC 7.2 D: every configuration that ever produced a backtest number counts toward the DSR trial count, and
"all 14 grid points are logged in ``research/EXPERIMENTS.md`` before the first run". This module reads the
**trial table** (the table whose header row starts with ``#``; one trial per data row) so the run scripts can
refuse to start otherwise, and so N in the DSR is the logged number and not a constant typed in code. Any
other table in the file (a results table, a disclosure table) is ignored.

A data row is a line of the trial table whose first cell is not a separator or the placeholder dash. Rows of
the S0 family are never non-S0 trials: S0 itself (N = 1 in its own DSR), the S0 robustness variants
(sensitivity runs, not trials: SPEC 7.1) and any row such as ``S0-SMA50`` whose id or strategy cell starts
with ``S0``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TrialLog", "is_s0_family", "normalise_key", "parse_trial_log", "read_trial_log"]

_PLACEHOLDER = {"—", "-", ""}
_S0_FAMILY = re.compile(r"^[`*\s]*s0", re.IGNORECASE)
STRATEGY_COLUMN = 3  # "# | date | phase | strategy / variant | ..."


def normalise_key(text: str) -> str:
    """Compare keys regardless of spacing, dashes, equals signs and backticks: ``C4 p=10`` == ``C4-p10``."""
    return re.sub(r"[\s`*_=\-]", "", text).lower()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_s0_family(cells: list[str]) -> bool:
    """True for S0, an S0 variant or an ``S0-...`` row (matched on the id cell or the strategy cell)."""
    probes = [cells[0]]
    if len(cells) > STRATEGY_COLUMN:
        probes.append(cells[STRATEGY_COLUMN])
    return any(_S0_FAMILY.match(p) for p in probes)


@dataclass(frozen=True)
class TrialLog:
    rows: tuple[str, ...]  # raw data rows of the trial table (header, separator, placeholder excluded)
    non_s0_rows: tuple[str, ...]  # the trials counted in N: everything outside the S0 family
    s0_rows: tuple[str, ...] = ()  # S0, its variants and ``S0-...`` rows: never counted

    @property
    def n_non_s0(self) -> int:
        return len(self.non_s0_rows)

    @property
    def s0_registered(self) -> bool:
        """The row whose id cell is exactly ``S0`` exists."""
        return any(_cells(r)[0] == "S0" for r in self.s0_rows)

    def missing(self, keys: Iterable[str]) -> list[str]:
        """Grid keys that no non-S0 row mentions (normalised substring match on the row text)."""
        hay = [normalise_key(r) for r in self.non_s0_rows]
        return [k for k in keys if not any(normalise_key(k) in h for h in hay)]


def parse_trial_log(text: str) -> TrialLog:
    """Read the trial table only: from its ``#`` header row to the first line that is not a table row."""
    rows: list[str] = []
    in_table = False
    seen_table = False
    for line in text.splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        first = cells[0]
        if first == "#":
            if seen_table:
                break  # a second table with a "#" header is not the trial table
            in_table = seen_table = True
            continue
        if not in_table:
            continue
        if set(first) <= {"-", ":", " "} and first.strip("-: ") == "":
            continue
        if first in _PLACEHOLDER:
            continue
        rows.append(line)
    if not seen_table:
        raise ValueError("no trial table found (a table whose header row starts with '#')")
    s0 = tuple(r for r in rows if is_s0_family(_cells(r)))
    non_s0 = tuple(r for r in rows if not is_s0_family(_cells(r)))
    return TrialLog(rows=tuple(rows), non_s0_rows=non_s0, s0_rows=s0)


def read_trial_log(path: Path) -> TrialLog:
    return parse_trial_log(path.read_text(encoding="utf-8"))
