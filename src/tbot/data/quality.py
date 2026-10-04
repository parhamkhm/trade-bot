"""Binance kline data-quality report (docs/SPEC.md section 5.3, gate G1a).

Every function here is pure: it takes already-loaded data (a ``pandas.DataFrame`` of float
OHLCV, as produced by ``binance_loader.load_frame(as_float=True)``) and returns findings. No
function reaches the network or touches the Parquet store directly, so these are trivially
unit-testable against small hand-built fixtures.

Expected ``DataFrame`` columns: ``ts`` (tz-aware UTC, the bar's CLOSE time), ``open``, ``high``,
``low``, ``close``, ``volume`` (float64).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from tbot.core.types import Timeframe
from tbot.data import store as store_mod

__all__ = [
    "RECONCILIATION_REL_TOLERANCE",
    "RECONCILIATION_TOLERANCE",
    "RETURN_OUTLIER_THRESHOLD",
    "GapFinding",
    "OutlierFinding",
    "QualityReport",
    "ReconciliationMismatch",
    "build_quality_report",
    "coverage_by_month",
    "find_duplicate_timestamps",
    "find_gaps",
    "find_high_eq_low",
    "find_out_of_order",
    "find_return_outliers",
    "find_zero_volume",
    "reconcile_resample",
    "write_report",
]

# docs/SPEC.md section 5.3 / CLAUDE.md section "quality" only pin down 1h (>20%) and 1d (>40%).
# 4h is not specified; we conservatively reuse the 1h bound since 4h moves are closer in scale
# to 1h than to 1d. Flag this to the orchestrator if a tighter number is wanted (see report).
RETURN_OUTLIER_THRESHOLD: dict[Timeframe, float] = {
    Timeframe.H1: 0.20,
    Timeframe.H4: 0.20,
    Timeframe.D1: 0.40,
}

# Reviewer finding m7: comparing float-summed resampled values against a fixed *absolute*
# tolerance alone produced false mismatches once volumes reach realistic magnitudes (95 of 2000
# random 24h sums of hourly volumes in 1e4-9e5 flagged as "mismatched" purely from float
# accumulation noise). ``math.isclose``'s combined rule -- tolerance = max(rel_tol * max(|a|,
# |b|), abs_tol) -- fixes this: the relative term scales with the value for large numbers, and
# the absolute term still catches a genuine mismatch between two small/zero values.
RECONCILIATION_TOLERANCE = 1e-9
RECONCILIATION_REL_TOLERANCE = 1e-12

_RESAMPLE_RULE: dict[Timeframe, str] = {Timeframe.H4: "4h", Timeframe.D1: "1D"}


@dataclass(frozen=True, slots=True)
class GapFinding:
    """A run of missing bars between two known, present timestamps."""

    from_ts: datetime
    to_ts: datetime
    missing_bars: int
    classification: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": _iso(self.from_ts),
            "to": _iso(self.to_ts),
            "missing_bars": self.missing_bars,
            "classification": self.classification,
        }


@dataclass(frozen=True, slots=True)
class OutlierFinding:
    """A bar whose |close-over-close return| exceeds the timeframe's threshold."""

    ts: datetime
    pct_return: float

    def to_dict(self) -> dict[str, Any]:
        return {"ts": _iso(self.ts), "pct_return": self.pct_return}


@dataclass(frozen=True, slots=True)
class ReconciliationMismatch:
    """One resampled higher-timeframe value that disagrees with the stored value.

    ``in_outage_window`` (decision D-036, point 6) is true when the higher-timeframe bin this
    mismatch belongs to overlaps a gap or a source anomaly in either the 1h or the
    higher-timeframe series -- i.e. a mismatch that is plausibly just a symptom of the same
    outage, not an independent data problem. G1a's reconciliation criterion is reported as two
    numbers (total mismatches, and mismatches outside outage windows) rather than hiding either.
    """

    ts: datetime
    column: str
    resampled: float
    stored: float
    in_outage_window: bool = False

    @property
    def diff(self) -> float:
        return abs(self.resampled - self.stored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": _iso(self.ts),
            "column": self.column,
            "resampled": self.resampled,
            "stored": self.stored,
            "diff": self.diff,
            "in_outage_window": self.in_outage_window,
        }


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------------


def find_duplicate_timestamps(ts: pd.Series) -> list[datetime]:
    """Every timestamp that appears more than once, sorted."""
    counts = ts.value_counts()
    dupes = counts[counts > 1].index.tolist()
    return sorted(dupes)


def find_out_of_order(ts: pd.Series) -> list[dict[str, Any]]:
    """Positions where a timestamp is strictly earlier than the one before it (as stored)."""
    values = list(ts)
    findings: list[dict[str, Any]] = []
    for i in range(1, len(values)):
        if values[i] < values[i - 1]:
            findings.append({"index": i, "ts": values[i], "previous_ts": values[i - 1]})
    return findings


def find_gaps(ts: pd.Series, timeframe: Timeframe) -> list[GapFinding]:
    """Runs of missing bars, computed on the sorted, de-duplicated timestamp set."""
    delta_seconds = timeframe.delta.total_seconds()
    ordered = sorted(set(ts))
    gaps: list[GapFinding] = []
    for prev, curr in pairwise(ordered):
        diff_seconds = (curr - prev).total_seconds()
        if diff_seconds <= delta_seconds:
            continue
        missing = round(diff_seconds / delta_seconds) - 1
        if missing > 0:
            gaps.append(GapFinding(from_ts=prev, to_ts=curr, missing_bars=missing))
    return gaps


def find_zero_volume(df: pd.DataFrame) -> list[datetime]:
    return sorted(df.loc[df["volume"] == 0, "ts"].tolist())


def find_high_eq_low(df: pd.DataFrame) -> list[datetime]:
    return sorted(df.loc[df["high"] == df["low"], "ts"].tolist())


def find_return_outliers(df: pd.DataFrame, timeframe: Timeframe) -> list[OutlierFinding]:
    """Bars whose close-over-close return exceeds the timeframe's outlier threshold."""
    threshold = RETURN_OUTLIER_THRESHOLD[timeframe]
    ordered = df.sort_values("ts").reset_index(drop=True)
    closes = ordered["close"].astype(float)
    returns = closes.pct_change()
    findings: list[OutlierFinding] = []
    for ts, ret in zip(ordered["ts"], returns, strict=False):
        if pd.isna(ret):
            continue
        if abs(float(ret)) > threshold:
            findings.append(OutlierFinding(ts=ts, pct_return=float(ret)))
    return findings


def coverage_by_month(ts: pd.Series, timeframe: Timeframe) -> dict[str, dict[str, Any]]:
    """Per-calendar-month bar count vs the number of bars a full month would hold."""
    delta_seconds = timeframe.delta.total_seconds()
    actual: dict[str, int] = {}
    for t in set(ts):
        key = f"{t.year:04d}-{t.month:02d}"
        actual[key] = actual.get(key, 0) + 1

    result: dict[str, dict[str, Any]] = {}
    for key in sorted(actual):
        year, month = (int(part) for part in key.split("-"))
        start = datetime(year, month, 1, tzinfo=UTC)
        end = (
            datetime(year + 1, 1, 1, tzinfo=UTC)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=UTC)
        )
        expected = round((end - start).total_seconds() / delta_seconds)
        result[key] = {
            "expected": expected,
            "actual": actual[key],
            "coverage": (actual[key] / expected) if expected else 0.0,
        }
    return result


def _windows_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when the half-open-ish windows ``[a_start, a_end]`` and ``[b_start, b_end]`` overlap."""
    return a_start <= b_end and b_start <= a_end


def _bin_is_outage_window(
    bin_start: datetime,
    bin_end: datetime,
    *,
    gaps_1h: Sequence[GapFinding],
    gaps_higher: Sequence[GapFinding],
    anomalies_1h: Sequence[store_mod.AnomalyRecord],
    anomalies_higher: Sequence[store_mod.AnomalyRecord],
) -> bool:
    for gap in (*gaps_1h, *gaps_higher):
        if _windows_overlap(bin_start, bin_end, gap.from_ts, gap.to_ts):
            return True
    for anomaly in (*anomalies_1h, *anomalies_higher):
        lo = min(anomaly.raw_open_ts, anomaly.raw_close_ts)
        hi = max(anomaly.raw_open_ts, anomaly.raw_close_ts)
        if _windows_overlap(bin_start, bin_end, lo, hi):
            return True
    return False


def reconcile_resample(
    df_1h: pd.DataFrame,
    df_higher: pd.DataFrame,
    *,
    higher_timeframe: Timeframe,
    gaps_1h: Sequence[GapFinding] = (),
    gaps_higher: Sequence[GapFinding] = (),
    anomalies_1h: Sequence[store_mod.AnomalyRecord] = (),
    anomalies_higher: Sequence[store_mod.AnomalyRecord] = (),
) -> list[ReconciliationMismatch]:
    """Resample 1h bars to ``higher_timeframe`` (``label='right', closed='right'``) and compare.

    Both frames use ``ts`` as the bar's CLOSE time, which is already the right edge of the
    resampling window, so resampling directly on ``ts`` reproduces the higher-timeframe grid.

    Decision D-036 (point 6): this never hides a mismatch. ``gaps_1h``/``gaps_higher`` and
    ``anomalies_1h``/``anomalies_higher`` are used only to tag each mismatch's
    ``in_outage_window`` -- every mismatch is still returned, tagged or not; the caller (
    ``build_quality_report``) reports both the total count and the count outside outage windows.
    """
    rule = _RESAMPLE_RULE[higher_timeframe]
    indexed = df_1h.sort_values("ts").set_index("ts")
    agg: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    resampled = indexed.resample(rule, label="right", closed="right").agg(agg).dropna(how="all")

    stored = df_higher.sort_values("ts").set_index("ts")
    mismatches: list[ReconciliationMismatch] = []
    # Only compare timestamps present on both sides. A resampled bin with no stored counterpart
    # is an edge effect (a partial bucket at the start/end of the 1h range) or a genuine gap —
    # gaps are find_gaps()'s job, not a value mismatch, so they are not reported here.
    common = resampled.index.intersection(stored.index)
    for ts in common:
        bin_end = ts.to_pydatetime()
        bin_start = bin_end - higher_timeframe.delta
        in_outage = _bin_is_outage_window(
            bin_start,
            bin_end,
            gaps_1h=gaps_1h,
            gaps_higher=gaps_higher,
            anomalies_1h=anomalies_1h,
            anomalies_higher=anomalies_higher,
        )
        for column in ("open", "high", "low", "close", "volume"):
            resampled_value = float(resampled.loc[ts, column])
            stored_value = float(stored.loc[ts, column])
            if not math.isclose(
                resampled_value,
                stored_value,
                rel_tol=RECONCILIATION_REL_TOLERANCE,
                abs_tol=RECONCILIATION_TOLERANCE,
            ):
                mismatches.append(
                    ReconciliationMismatch(
                        ts=bin_end,
                        column=column,
                        resampled=resampled_value,
                        stored=stored_value,
                        in_outage_window=in_outage,
                    )
                )
    return mismatches


# ---------------------------------------------------------------------------------
# full report
# ---------------------------------------------------------------------------------


@dataclass(slots=True)
class QualityReport:
    source: str
    symbol: str
    timeframe: Timeframe
    rows: int
    first_ts: datetime | None
    last_ts: datetime | None
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    duplicate_timestamps: list[datetime] = field(default_factory=list)
    out_of_order: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[GapFinding] = field(default_factory=list)
    zero_volume_bars: list[datetime] = field(default_factory=list)
    high_eq_low_bars: list[datetime] = field(default_factory=list)
    return_outliers: list[OutlierFinding] = field(default_factory=list)
    reconciliation: list[ReconciliationMismatch] | None = None
    anomalies: list[store_mod.AnomalyRecord] = field(default_factory=list)

    @property
    def unclassified_gap_count(self) -> int:
        return sum(1 for g in self.gaps if g.classification == "unknown")

    @property
    def reconciliation_mismatches_outside_outages(self) -> list[ReconciliationMismatch] | None:
        """Decision D-036 (point 6): the subset of ``reconciliation`` that is NOT explained by
        an overlapping gap or source anomaly in either series -- the number that actually
        matters for "is the data clean", as opposed to "is the data clean during an outage we
        already know about"."""
        if self.reconciliation is None:
            return None
        return [m for m in self.reconciliation if not m.in_outage_window]

    def to_dict(self) -> dict[str, Any]:
        outside = self.reconciliation_mismatches_outside_outages
        return {
            "source": self.source,
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "rows": self.rows,
            "first_ts": _iso(self.first_ts) if self.first_ts else None,
            "last_ts": _iso(self.last_ts) if self.last_ts else None,
            "coverage_by_month": self.coverage,
            "duplicate_timestamps": [_iso(t) for t in self.duplicate_timestamps],
            "out_of_order": [
                {"index": o["index"], "ts": _iso(o["ts"]), "previous_ts": _iso(o["previous_ts"])}
                for o in self.out_of_order
            ],
            "gaps": [g.to_dict() for g in self.gaps],
            "zero_volume_bars": [_iso(t) for t in self.zero_volume_bars],
            "high_eq_low_bars": [_iso(t) for t in self.high_eq_low_bars],
            "return_outliers": [o.to_dict() for o in self.return_outliers],
            "source_anomalies": [
                {"symbol": self.symbol, "timeframe": self.timeframe.value, **a.to_dict()}
                for a in self.anomalies
            ],
            "reconciliation": (
                None if self.reconciliation is None else [m.to_dict() for m in self.reconciliation]
            ),
            "reconciliation_total_mismatches": (
                None if self.reconciliation is None else len(self.reconciliation)
            ),
            "reconciliation_mismatches_outside_outages": (None if outside is None else len(outside)),
            "unclassified_gap_count": self.unclassified_gap_count,
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Data quality — {self.source} {self.symbol} {self.timeframe.value}",
            "",
            f"- rows: {self.rows}",
            f"- first_ts: {_iso(self.first_ts) if self.first_ts else 'n/a'}",
            f"- last_ts: {_iso(self.last_ts) if self.last_ts else 'n/a'}",
            f"- duplicate timestamps: {len(self.duplicate_timestamps)}",
            f"- out-of-order timestamps: {len(self.out_of_order)}",
            f"- gaps: {len(self.gaps)} ({self.unclassified_gap_count} unclassified)",
            f"- zero-volume bars: {len(self.zero_volume_bars)}",
            f"- high==low bars: {len(self.high_eq_low_bars)}",
            f"- |return| outliers: {len(self.return_outliers)}",
            "",
            "## Coverage by month",
            "",
            "| month | expected | actual | coverage |",
            "|---|---|---|---|",
        ]
        for month, row in sorted(self.coverage.items()):
            lines.append(f"| {month} | {row['expected']} | {row['actual']} | {row['coverage']:.4f} |")

        if self.gaps:
            lines += ["", "## Gaps", "", "| from | to | missing_bars | classification |", "|---|---|---|---|"]
            for gap in self.gaps:
                lines.append(
                    f"| {_iso(gap.from_ts)} | {_iso(gap.to_ts)} | {gap.missing_bars} | {gap.classification} |"
                )

        if self.duplicate_timestamps:
            lines += ["", "## Duplicate timestamps", ""]
            lines += [f"- {_iso(t)}" for t in self.duplicate_timestamps]

        if self.out_of_order:
            lines += ["", "## Out-of-order timestamps", ""]
            lines += [
                f"- index {o['index']}: {_iso(o['ts'])} after {_iso(o['previous_ts'])}"
                for o in self.out_of_order
            ]

        if self.return_outliers:
            lines += ["", "## |return| outliers", ""]
            lines += [f"- {_iso(o.ts)}: {o.pct_return:+.2%}" for o in self.return_outliers]

        if self.anomalies:
            lines += [
                "",
                "## Source anomalies",
                "",
                "| symbol | timeframe | raw open | raw close | duration (s) | n_trades | volume "
                "| class | action |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for a in self.anomalies:
                lines.append(
                    f"| {self.symbol} | {self.timeframe.value} | {_iso(a.raw_open_ts)} | "
                    f"{_iso(a.raw_close_ts)} | {a.duration_seconds:.3f} | {a.n_trades} | {a.volume} | "
                    f"{a.classification} | {a.action} |"
                )

        if self.reconciliation is not None:
            outside = self.reconciliation_mismatches_outside_outages or []
            lines += [
                "",
                "## Resampling reconciliation",
                "",
                f"- total mismatches: {len(self.reconciliation)}",
                f"- mismatches outside outage windows: {len(outside)}",
            ]
            if self.reconciliation:
                lines += ["", "### All mismatches", ""]
                lines += [
                    f"- {_iso(m.ts)} {m.column}: resampled={m.resampled} stored={m.stored}"
                    f"{' (outage window)' if m.in_outage_window else ''}"
                    for m in self.reconciliation
                ]
            else:
                lines.append("- OK: resampled values match stored values within tolerance")
            if outside:
                lines += ["", "### Mismatches outside outage windows", ""]
                lines += [
                    f"- {_iso(m.ts)} {m.column}: resampled={m.resampled} stored={m.stored}"
                    for m in outside
                ]

        return "\n".join(lines) + "\n"


def _apply_gap_classifications(
    gaps: list[GapFinding], gap_classifications: Mapping[tuple[datetime, datetime], str] | None
) -> list[GapFinding]:
    """Overlay previously-investigated classifications (persisted in the Binance sidecar's
    ``gaps`` list, keyed by ``(from_ts, to_ts)``) onto freshly-found gaps.

    MAJOR M6 (fix round): ``find_gaps`` always emits ``classification="unknown"`` -- it has no
    memory of anything -- so without this, ``unclassified_gap_count`` could never drop below the
    total gap count and gate G1a ("every missing bar classified, no 'unknown' left") could never
    be satisfied no matter how many gaps got investigated and recorded on disk.
    """
    if not gap_classifications:
        return gaps
    return [
        GapFinding(
            from_ts=g.from_ts,
            to_ts=g.to_ts,
            missing_bars=g.missing_bars,
            classification=gap_classifications.get((g.from_ts, g.to_ts), g.classification),
        )
        for g in gaps
    ]


def build_quality_report(
    *,
    source: str,
    symbol: str,
    timeframe: Timeframe,
    df: pd.DataFrame,
    df_1h_for_reconciliation: pd.DataFrame | None = None,
    gap_classifications: Mapping[tuple[datetime, datetime], str] | None = None,
    anomalies: Sequence[store_mod.AnomalyRecord] = (),
    anomalies_1h_for_reconciliation: Sequence[store_mod.AnomalyRecord] = (),
) -> QualityReport:
    """Run every check in this module over ``df`` and assemble one report.

    ``df_1h_for_reconciliation`` is required (and used) only when ``timeframe`` is 4h or 1d —
    the 1h series it resamples from, used to validate the stored higher-timeframe bars.

    ``gap_classifications`` (decision M6) maps ``(from_ts, to_ts) -> classification`` for gaps
    already investigated and recorded (typically read back from the Binance ``_dataset.json``
    sidecar by the caller) -- without it every gap is reported ``"unknown"`` forever, even one
    that was classified yesterday, because this function (like every function in this module) is
    pure and has no memory of its own.

    ``anomalies`` (decision D-036) are this series' own source-row anomalies, for the "Source
    anomalies" report section. ``anomalies_1h_for_reconciliation`` are the 1h series' anomalies,
    used (together with ``anomalies``) only to tag reconciliation mismatches as inside/outside an
    outage window -- both are typically read back from the Binance sidecar(s) by the caller.
    """
    if df.empty:
        return QualityReport(
            source=source, symbol=symbol, timeframe=timeframe, rows=0, first_ts=None, last_ts=None,
            anomalies=list(anomalies),
        )

    ordered = df.sort_values("ts").reset_index(drop=True)
    gaps = _apply_gap_classifications(find_gaps(ordered["ts"], timeframe), gap_classifications)

    reconciliation: list[ReconciliationMismatch] | None = None
    if timeframe in _RESAMPLE_RULE and df_1h_for_reconciliation is not None:
        gaps_1h = find_gaps(df_1h_for_reconciliation["ts"], Timeframe.H1)
        reconciliation = reconcile_resample(
            df_1h_for_reconciliation,
            ordered,
            higher_timeframe=timeframe,
            gaps_1h=gaps_1h,
            gaps_higher=gaps,
            anomalies_1h=anomalies_1h_for_reconciliation,
            anomalies_higher=anomalies,
        )

    return QualityReport(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        rows=len(ordered),
        first_ts=ordered["ts"].iloc[0].to_pydatetime(),
        last_ts=ordered["ts"].iloc[-1].to_pydatetime(),
        coverage=coverage_by_month(ordered["ts"], timeframe),
        duplicate_timestamps=find_duplicate_timestamps(ordered["ts"]),
        out_of_order=find_out_of_order(ordered["ts"]),
        gaps=gaps,
        zero_volume_bars=find_zero_volume(ordered),
        high_eq_low_bars=find_high_eq_low(ordered),
        return_outliers=find_return_outliers(ordered, timeframe),
        reconciliation=reconciliation,
        anomalies=list(anomalies),
    )


def write_report(report: QualityReport, out_dir: Path, *, date_tag: str) -> tuple[Path, Path]:
    """Write ``data_quality_<date_tag>_<symbol>_<timeframe>.{json,md}`` under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"data_quality_{date_tag}_{report.symbol}_{report.timeframe.value}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_md = md_path.with_suffix(".md.tmp")
    tmp_json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp_md.write_text(report.render_markdown(), encoding="utf-8")
    tmp_json.replace(json_path)
    tmp_md.replace(md_path)
    return json_path, md_path
