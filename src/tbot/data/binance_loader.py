"""Download, verify and load Binance kline data (docs/SPEC.md sections 5.1, 5.2; decision D-017).

Three jobs live here:

1. **Download + verify** monthly kline ZIPs from ``data.binance.vision`` for BTCUSDT/ETHUSDT,
   checksum each against its published ``.CHECKSUM`` (SHA-256), and hand parsed rows to
   ``tbot.data.store`` for idempotent Parquet storage (``ingest_symbol_timeframe``).
2. **Parse** the kline CSV, detecting per-file whether timestamps are milliseconds or
   microseconds from the raw magnitude (D-017: data.binance.vision switched spot kline files
   from ms to µs on 2025-01-01) and normalising to UTC.
3. **Load** (``load_bars`` / ``load_frame``), enforcing the sealed-holdout double lock
   (docs/SPEC.md section 5.2): the default excludes every bar with ``ts >= CANONICAL_HOLDOUT_START``;
   unsealing needs both ``allow_holdout=True`` *and* ``TBOT_UNSEAL_HOLDOUT=G4``, and every
   attempt (granted or refused) is appended to ``research/HOLDOUT_LOG.md``.

Decision D-026 (the blocker this module was patched for): the holdout boundary used for the
actual read-time filter is **always** the code constant ``CANONICAL_HOLDOUT_START``, never
``DataConfig.holdout_start``. A ``DataConfig`` whose ``holdout_start`` disagrees with that
constant is refused outright (logged, then ``HoldoutLockError``) before any row is read, unless
both unseal locks are engaged -- a YAML edit, or ``--config my.yaml``, must never be able to move
the seal, silently or otherwise.

Close-time convention: Binance's ``close_time`` column is "open_time + interval - 1 unit" (one
millisecond pre-2025, one microsecond after — confirmed against real files; the epsilon scales
with the file's own unit, so trusting ``close_time`` directly would require re-deriving the
epsilon per file anyway). We instead derive the exact close instant as ``open_time + timeframe
.delta`` and use ``close_time`` only as a corruption sanity-check: it must land within
``_EARLY_TOLERANCE_SECONDS`` (1s) *before* the derived value minus the one-unit epsilon, or
within that same epsilon *after* it (NIT, fix round: the late side used to share the 1s
tolerance too, which could store a row whose raw data runs up to 1s past its own label -- a
causality violation, however small). This satisfies "ts is the bar's close time, normalised to
the exact close instant" (docs/SPEC.md section 5.1) without guessing a unit for the epsilon.

Decision D-036: real Binance history has rows around exchange outages that fail that sanity
check (short bars, zero-trade bars with a nonsensical ``close_time``, bars whose window runs
long) and rows with an off-grid ``open_time``. None of these abort the whole file any more --
``_parse_kline_row`` (called per row by ``parse_kline_csv_bytes``) classifies each one
(``misaligned`` / ``short`` / ``empty_irregular`` / ``long``), stores the ones that are still
causal (``short``, at the normal label) and drops the rest, returning both the clean records and
the anomaly list. ``ingest_symbol_timeframe`` persists the anomalies in the ``_dataset.json``
sidecar. Only genuine file-level corruption (an unparseable row, or an open/close timestamp unit
disagreement within one row) still raises and aborts ingestion of that file.

Decision D-036 amendment (finding m-J, fix round 2): a resulting gap is auto-classified by
``_classify_gap_from_anomalies`` using exactly two rules -- see that function and
``_classify_fresh_gaps`` -- plus the cross-symbol pass (``apply_cross_symbol_gap_classification``,
run separately by ``scripts/download_binance.py`` once every configured symbol has been
ingested). A classification any rule or a human already recorded for a gap's exact window is
never revisited by a later rule.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
import pandas as pd  # type: ignore[import-untyped]
import structlog

from tbot.core.config import DataConfig
from tbot.core.types import Bar, Timeframe
from tbot.data import quality as quality_mod
from tbot.data import store as store_mod

logger = structlog.get_logger(__name__)

__all__ = [
    "CANONICAL_HOLDOUT_START",
    "HOLDOUT_UNSEAL_ENV",
    "HOLDOUT_UNSEAL_VALUE",
    "ChecksumMismatchError",
    "HoldoutLockError",
    "IngestOutcome",
    "ParsedKlineCsv",
    "TimestampUnitError",
    "apply_cross_symbol_gap_classification",
    "checksum_url",
    "detect_timestamp_unit",
    "ingest_symbol_timeframe",
    "latest_complete_month",
    "load_bars",
    "load_frame",
    "monthly_zip_name",
    "monthly_zip_url",
    "months_between",
    "parse_checksum_text",
    "parse_kline_csv_bytes",
    "sha256_hex",
    "to_utc_datetime",
]

SOURCE = "binance"
HOLDOUT_UNSEAL_ENV = "TBOT_UNSEAL_HOLDOUT"
HOLDOUT_UNSEAL_VALUE = "G4"

# The sealed-holdout boundary (decision D-008) as a CODE CONSTANT, not a configurable value
# (decision D-026). ``DataConfig.holdout_start`` still exists (pydantic needs a field, and
# config/default.yaml documents the date for humans), but it is never trusted for the actual
# read-time filter below -- only compared against this constant, and refused on any disagreement.
CANONICAL_HOLDOUT_START = datetime(2025, 10, 1, tzinfo=UTC)

# Valid epoch bounds for calendar years 2000-01-01 .. 2100-01-01, used to tell ms from µs
# timestamps by magnitude alone (D-017). The two ranges are five orders of magnitude apart, so
# there is no ambiguity between them.
_MS_LO = 946_684_800_000
_MS_HI = 4_102_444_800_000
_US_LO = _MS_LO * 1000
_US_HI = _MS_HI * 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ChecksumMismatchError(RuntimeError):
    """A downloaded file's SHA-256 does not match its published .CHECKSUM."""


class TimestampUnitError(ValueError):
    """A raw timestamp integer matches neither the millisecond nor the microsecond epoch range."""


class HoldoutLockError(RuntimeError):
    """Holdout access requested without both locks agreeing (docs/SPEC.md section 5.2)."""


# ---------------------------------------------------------------------------------
# timestamp unit detection (D-017)
# ---------------------------------------------------------------------------------


def detect_timestamp_unit(value: int) -> Literal["ms", "us"]:
    """Detect whether ``value`` is a millisecond or microsecond Unix epoch timestamp.

    Raises ``TimestampUnitError`` if it matches neither — fail loudly rather than guess.
    """
    if _MS_LO <= value <= _MS_HI:
        return "ms"
    if _US_LO <= value <= _US_HI:
        return "us"
    raise TimestampUnitError(
        f"timestamp {value} matches neither the millisecond range [{_MS_LO}, {_MS_HI}] "
        f"nor the microsecond range [{_US_LO}, {_US_HI}]"
    )


def to_utc_datetime(value: int, unit: Literal["ms", "us"]) -> datetime:
    """Convert a raw integer epoch timestamp to an exact, timezone-aware UTC datetime."""
    if unit == "ms":
        return _EPOCH + timedelta(milliseconds=value)
    return _EPOCH + timedelta(microseconds=value)


def _is_on_grid(ts: datetime, timeframe: Timeframe) -> bool:
    """True when ``ts`` lies exactly on the timeframe grid (decision D-036, point 2):
    UTC epoch multiples of ``timeframe.delta`` -- e.g. 1d bars must open at 00:00 UTC, 4h bars
    at 00/04/08/12/16/20 UTC. ``timedelta`` supports exact (non-float) modulo, so this is an
    exact check, not a tolerance-based one.
    """
    return (ts - _EPOCH) % timeframe.delta == timedelta(0)


# ---------------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------------


def monthly_zip_name(symbol: str, timeframe: Timeframe, year: int, month: int) -> str:
    return f"{symbol}-{timeframe.value}-{year:04d}-{month:02d}.zip"


def monthly_zip_url(base_url: str, symbol: str, timeframe: Timeframe, year: int, month: int) -> str:
    name = monthly_zip_name(symbol, timeframe, year, month)
    return f"{base_url.rstrip('/')}/data/spot/monthly/klines/{symbol}/{timeframe.value}/{name}"


def checksum_url(zip_url: str) -> str:
    return f"{zip_url}.CHECKSUM"


def months_between(
    start_year: int, start_month: int, end_year: int, end_month: int
) -> Iterator[tuple[int, int]]:
    """Yield ``(year, month)`` pairs from the start month to the end month, inclusive."""
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        yield year, month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def latest_complete_month(now: datetime) -> tuple[int, int]:
    """The most recent calendar month that has fully closed, relative to ``now`` (UTC)."""
    now_utc = now.astimezone(UTC)
    first_of_this_month = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.year, last_day_of_prev_month.month


# ---------------------------------------------------------------------------------
# checksum verification
# ---------------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_checksum_text(text: str, filename: str) -> str:
    """Extract the SHA-256 hex digest for ``filename`` from a ``.CHECKSUM`` file's contents.

    Accepts both the plain data.binance.vision format (``<hex>  <filename>``) and the
    ``sha256sum -b`` format (``<hex> *<filename>``).
    """
    for line in text.strip().splitlines():
        tokens = line.strip().split()
        if not tokens:
            continue
        candidate_hex = tokens[0].lower()
        if len(tokens) == 1:
            return candidate_hex
        name_token = tokens[1].lstrip("*")
        if name_token == filename:
            return candidate_hex
    raise ValueError(f"no checksum entry for {filename!r} found in CHECKSUM file")


# ---------------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedKlineCsv:
    """Result of parsing one monthly kline CSV: the rows safe to store, plus every row-level
    anomaly encountered (decision D-036) -- stored separately from each other so a caller never
    has to guess which rows in ``records`` were flagged.
    """

    records: tuple[store_mod.KlineRecord, ...]
    anomalies: tuple[store_mod.AnomalyRecord, ...]


_EARLY_TOLERANCE_SECONDS = 1.0  # how early close_time may land vs. nominal before calling it "short"


def _row_duration_diff_seconds(
    open_dt: datetime, close_raw_dt: datetime, *, close_unit: Literal["ms", "us"], timeframe: Timeframe
) -> tuple[float, float]:
    """Returns ``(duration_seconds, diff_seconds)`` for one row: ``diff_seconds`` is the raw
    duration minus the expected one (``timeframe.delta`` less one unit epsilon -- see the module
    docstring's close-time convention). Zero means the row matches the documented convention
    exactly; positive means the raw ``close_time`` landed *after* the nominal close instant.
    """
    epsilon = timedelta(milliseconds=1) if close_unit == "ms" else timedelta(microseconds=1)
    expected_duration_seconds = (timeframe.delta - epsilon).total_seconds()
    duration_seconds = (close_raw_dt - open_dt).total_seconds()
    return duration_seconds, duration_seconds - expected_duration_seconds


def _late_tolerance_seconds(close_unit: Literal["ms", "us"]) -> float:
    """NIT (fix round): the old code tolerated ``close_time`` landing up to a whole second
    *after* its nominal close as "normal" -- which can store a row whose raw data extends up to
    1s past its label, a (tiny) causality violation. The late side now only tolerates the one
    unit (1ms/1us) epsilon already baked into the convention itself; the early side is unchanged
    (a thin/short bar is still just "short", not an error).
    """
    epsilon = timedelta(milliseconds=1) if close_unit == "ms" else timedelta(microseconds=1)
    return epsilon.total_seconds()


def _parse_row_fields(
    line: str,
) -> tuple[list[str], datetime, datetime, Literal["ms", "us"], int, Decimal] | None:
    """Parse one CSV row's raw open/close times, converting the epoch ints to UTC datetimes.

    Returns ``(parts, open_dt, close_raw_dt, close_unit, n_trades, volume)``, or ``None`` for a
    header row (non-numeric ``open_time``). Raises ``TimestampUnitError`` for a genuine
    open/close unit disagreement within the row -- the one per-row error that still aborts the
    whole file (every other anomaly is classified by the caller, never raised).
    """
    parts = line.split(",")
    if len(parts) < 9:
        raise ValueError(f"unexpected kline CSV row (expected >= 9 columns): {line!r}")
    try:
        open_time_raw = int(parts[0])
    except ValueError:
        return None  # header row (e.g. "open_time,open,..."), skip defensively
    close_time_raw = int(parts[6])

    open_unit = detect_timestamp_unit(open_time_raw)
    close_unit = detect_timestamp_unit(close_time_raw)
    if open_unit != close_unit:
        # File-level corruption, not a per-row anomaly: still aborts the whole file.
        raise TimestampUnitError(
            f"open_time unit ({open_unit}) disagrees with close_time unit ({close_unit}) "
            f"in row {line!r}"
        )

    open_dt = to_utc_datetime(open_time_raw, open_unit)
    close_raw_dt = to_utc_datetime(close_time_raw, close_unit)
    return parts, open_dt, close_raw_dt, close_unit, int(parts[8]), Decimal(parts[5])


def _parse_kline_row(
    line: str, *, timeframe: Timeframe
) -> tuple[store_mod.KlineRecord | None, store_mod.AnomalyRecord | None]:
    """Parse and classify one kline CSV row (decision D-036; docs/SPEC.md section 5.1a).

    Returns ``(record, anomaly)``. A normal row returns ``(record, None)``; every dropped
    classification (``misaligned``/``empty_irregular``/``long``) returns ``(None, anomaly)``;
    only ``short`` returns both (stored at the normal label, flagged). A header row (non-numeric
    ``open_time``) returns ``(None, None)``. Raises ``TimestampUnitError`` for a genuine
    open/close unit disagreement within the row -- the one case that still aborts the whole file.
    """
    fields = _parse_row_fields(line)
    if fields is None:
        return None, None
    parts, open_dt, close_raw_dt, close_unit, n_trades, volume = fields
    nominal_close = open_dt + timeframe.delta  # exact close instant; see module docstring
    duration_seconds, diff_seconds = _row_duration_diff_seconds(
        open_dt, close_raw_dt, close_unit=close_unit, timeframe=timeframe
    )

    def _anomaly(classification: str, action: str) -> store_mod.AnomalyRecord:
        return store_mod.AnomalyRecord(
            raw_open_ts=open_dt,
            raw_close_ts=close_raw_dt,
            duration_seconds=duration_seconds,
            n_trades=n_trades,
            volume=volume,
            classification=classification,
            action=action,
        )

    def _record(ts: datetime) -> store_mod.KlineRecord:
        return store_mod.KlineRecord(
            ts=ts,
            open=Decimal(parts[1]),
            high=Decimal(parts[2]),
            low=Decimal(parts[3]),
            close=Decimal(parts[4]),
            volume=volume,
            quote_volume=Decimal(parts[7]),
            trades=n_trades,
        )

    if not _is_on_grid(open_dt, timeframe):
        return None, _anomaly("misaligned", "dropped")

    late_tolerance = _late_tolerance_seconds(close_unit)
    if -_EARLY_TOLERANCE_SECONDS <= diff_seconds <= late_tolerance:
        return _record(nominal_close), None  # normal row, no anomaly

    if n_trades == 0:
        return None, _anomaly("empty_irregular", "dropped")
    if diff_seconds < -_EARLY_TOLERANCE_SECONDS:
        return _record(nominal_close), _anomaly("short", "stored_flagged")  # causal: label >= real data
    if diff_seconds > late_tolerance:
        return None, _anomaly("long", "dropped")  # data after the label -> look-ahead
    raise ValueError(f"unreachable close_time classification for row {line!r}")  # pragma: no cover


def parse_kline_csv_bytes(data: bytes, *, timeframe: Timeframe) -> ParsedKlineCsv:
    """Parse one Binance monthly kline CSV (already extracted from its ZIP) into records.

    Columns (no header in files observed through 2025-09; a header row, if ever present, is
    skipped defensively): open_time, open, high, low, close, volume, close_time, quote_volume,
    count, taker_buy_volume, taker_buy_quote_volume, ignore.

    Decision D-036: a row-level anomaly (a short/empty/misaligned/overlong bar around a real
    exchange outage) must never abort ingestion of the whole file -- see ``_parse_kline_row``
    for the per-row classification rules. Only file-level problems (an unparseable row, or an
    open_time/close_time unit disagreement within one row) still raise and abort the whole file.
    """
    text = data.decode("utf-8")
    records: list[store_mod.KlineRecord] = []
    anomalies: list[store_mod.AnomalyRecord] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        record, anomaly = _parse_kline_row(line, timeframe=timeframe)
        if record is not None:
            records.append(record)
        if anomaly is not None:
            anomalies.append(anomaly)

    return ParsedKlineCsv(records=tuple(records), anomalies=tuple(anomalies))


# ---------------------------------------------------------------------------------
# download + ingest orchestration (network)
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """Summary of one ``ingest_symbol_timeframe`` run."""

    symbol: str
    timeframe: Timeframe
    downloaded: tuple[str, ...]
    skipped: tuple[str, ...]
    rows_in_store: int
    gaps: tuple[store_mod.GapRecord, ...]
    anomalies: tuple[store_mod.AnomalyRecord, ...] = ()


def _as_py_datetime(value: datetime) -> datetime:
    to_pydatetime = getattr(value, "to_pydatetime", None)
    return to_pydatetime() if callable(to_pydatetime) else value


def _missing_bar_window(from_ts: datetime, to_ts: datetime, delta: timedelta) -> tuple[datetime, datetime]:
    """The open-left, closed-right window spanning exactly the bars missing between two PRESENT
    timestamps: ``(from_ts, to_ts - delta]`` -- i.e. every missing bar's own nominal interval.

    Finding m-J: the old check used the closed interval ``[from_ts, to_ts]``, which includes the
    next *present* bar's own raw window -- so a dropped anomaly right next to an unrelated gap
    (sharing only that present bar's edge) could wrongly "explain" it.
    """
    return from_ts, to_ts - delta


def _dropped_anomaly_overlaps_missing_window(
    anomaly: store_mod.AnomalyRecord, window_start: datetime, window_end: datetime
) -> bool:
    """True when a ``dropped`` anomaly's raw time window intersects ``(window_start, window_end]``.

    Only ``action == "dropped"`` rows (``misaligned``/``empty_irregular``/``long``) are eligible:
    a ``short`` row is still *stored*, not dropped, so it never explains a gap this way -- see
    ``_short_bar_ends_right_before_gap`` for the rule that covers short bars instead. Decision
    D-036 (point 4), tightened per finding m-J. ``raw_close_ts`` can precede ``raw_open_ts`` (an
    ``empty_irregular`` row), so both ends are taken via ``min``/``max`` rather than assumed
    ordered; the window is open at ``window_start`` because that instant is a PRESENT bar, not a
    missing one.
    """
    if anomaly.action != "dropped":
        return False
    lo = min(anomaly.raw_open_ts, anomaly.raw_close_ts)
    hi = max(anomaly.raw_open_ts, anomaly.raw_close_ts)
    return lo <= window_end and hi > window_start


def _short_bar_ends_right_before_gap(
    anomaly: store_mod.AnomalyRecord, gap_from_ts: datetime, timeframe: Timeframe
) -> bool:
    """True when ``anomaly`` is a stored ``short`` bar whose close label (``raw_open_ts +
    timeframe.delta`` -- the same label ``_parse_kline_row`` stores it at) is exactly the gap's
    ``from_ts``: the outage began right after this bar, which is *why* it came in short. Finding
    m-J, rule (b) -- the common "short bar THEN outage" case the old, overly-broad check missed
    (it only matched when a *dropped* anomaly happened to sit in the closed gap window).
    """
    return anomaly.classification == "short" and anomaly.raw_open_ts + timeframe.delta == gap_from_ts


def _classify_gap_from_anomalies(
    *,
    from_ts: datetime,
    to_ts: datetime,
    timeframe: Timeframe,
    anomalies: Sequence[store_mod.AnomalyRecord],
) -> tuple[str, str] | None:
    """Decision D-036 amendment (finding m-J): classify a gap using exactly two evidence-based
    rules, nothing else. Returns ``(classification, classified_by)``, or ``None`` if neither
    rule matches -- the gap then stays ``unknown`` until a human, or the cross-symbol pass
    (``apply_cross_symbol_gap_classification``), says otherwise.
    """
    window_start, window_end = _missing_bar_window(from_ts, to_ts, timeframe.delta)
    if any(_dropped_anomaly_overlaps_missing_window(a, window_start, window_end) for a in anomalies):
        return "exchange_outage", "anomaly_overlap"
    if any(_short_bar_ends_right_before_gap(a, from_ts, timeframe) for a in anomalies):
        return "exchange_outage_after_short_bar", "after_short_bar"
    return None


def _classify_fresh_gaps(
    findings: Sequence[quality_mod.GapFinding],
    *,
    previous_gaps: Sequence[store_mod.GapRecord],
    anomalies: Sequence[store_mod.AnomalyRecord],
    timeframe: Timeframe,
) -> tuple[store_mod.GapRecord, ...]:
    """Overlay a classification onto each freshly-found gap: whatever a human (or an earlier
    run) already recorded for that exact window is kept untouched (never overwritten -- decision
    D-036 point 4), otherwise ``_classify_gap_from_anomalies`` gets one attempt, otherwise the
    gap stays ``unknown``.
    """
    previous_by_window = {(g.from_ts, g.to_ts): g for g in previous_gaps}
    new_gaps: list[store_mod.GapRecord] = []
    for finding in findings:
        from_ts = _as_py_datetime(finding.from_ts)
        to_ts = _as_py_datetime(finding.to_ts)
        previous = previous_by_window.get((from_ts, to_ts))
        if previous is not None and previous.classification != "unknown":
            classification, classified_by = previous.classification, previous.classified_by
        else:
            auto = _classify_gap_from_anomalies(
                from_ts=from_ts, to_ts=to_ts, timeframe=timeframe, anomalies=anomalies
            )
            classification, classified_by = auto if auto is not None else ("unknown", None)
        new_gaps.append(
            store_mod.GapRecord(
                from_ts=from_ts,
                to_ts=to_ts,
                missing_bars=finding.missing_bars,
                classification=classification,
                classified_by=classified_by,
            )
        )
    return tuple(new_gaps)


def _last_month_before_holdout() -> tuple[int, int]:
    """The last calendar month strictly before ``CANONICAL_HOLDOUT_START`` (2025-10-01 -> Sept 2025)."""
    boundary = CANONICAL_HOLDOUT_START
    if boundary.month == 1:
        return boundary.year - 1, 12
    return boundary.year, boundary.month - 1


def _resolve_month_range(
    *,
    data_config: DataConfig,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    allow_holdout: bool,
    reason: str,
    holdout_log_path: Path | None,
) -> tuple[int, int, int, int]:
    """Returns ``(start_year, start_month, end_year, end_month)`` to ingest.

    MINOR-10 / decision D-032 (second fix round): by default, ingestion never requests, downloads
    or writes a part file for any month at or after ``CANONICAL_HOLDOUT_START`` -- previously this
    ran all the way to ``latest_complete_month(now)`` regardless of the seal, so a plain
    ``download_binance.py`` run wrote holdout ZIPs/parts to disk, and a stray
    ``pd.read_parquet(...)`` on ``year=2025/`` or later was then the one remaining way to see
    sealed bars without going through the double-locked loader. The holdout year is only reached
    when **both** unseal locks are engaged (the same double lock ``load_bars``/``load_frame``
    use, via ``_resolve_holdout_access`` -- MINOR-8, third fix round: this also means every ingest
    attempt that touches a lock is logged to ``research/HOLDOUT_LOG.md`` and a mismatched
    ``holdout_start`` is refused exactly as it would be at read time). Any months skipped this way
    are logged (``binance_loader.ingest_skipped_sealed_months``).
    """
    start_year, start_month = data_config.history_start.year, data_config.history_start.month
    latest_year, latest_month = latest_complete_month(now)

    unsealed = _resolve_holdout_access(
        allow_holdout=allow_holdout,
        caller="ingest_symbol_timeframe",
        reason=reason,
        log_path=holdout_log_path,
        configured_holdout_start=data_config.holdout_start,
    )
    if unsealed:
        return start_year, start_month, latest_year, latest_month

    last_allowed_year, last_allowed_month = _last_month_before_holdout()
    end_year, end_month = min((latest_year, latest_month), (last_allowed_year, last_allowed_month))
    all_months = list(months_between(start_year, start_month, latest_year, latest_month))
    allowed_months = (
        list(months_between(start_year, start_month, end_year, end_month))
        if (start_year, start_month) <= (end_year, end_month)
        else []
    )
    n_skipped_as_sealed = len(all_months) - len(allowed_months)
    if n_skipped_as_sealed > 0:
        logger.info(
            "binance_loader.ingest_skipped_sealed_months",
            symbol=symbol,
            timeframe=timeframe.value,
            n_months=n_skipped_as_sealed,
            holdout_start=CANONICAL_HOLDOUT_START.isoformat(),
        )
    return start_year, start_month, end_year, end_month


def _ingest_month(
    client: httpx.Client,
    *,
    symbol: str,
    timeframe: Timeframe,
    data_config: DataConfig,
    year: int,
    month: int,
    sidecar: store_mod.DatasetSidecar,
    sidecar_file: Path,
) -> str | None:
    """Download, verify and store one calendar month, unless already verified and on disk
    (idempotent re-run). Returns the zip filename on a fresh download, or ``None`` when skipped.

    Mutates ``sidecar`` in place and persists it immediately on success (minor fix 3, fix round)
    so a crash partway through a multi-month run doesn't discard already-verified months.
    """
    zip_name = monthly_zip_name(symbol, timeframe, year, month)
    part_path = store_mod.month_part_path(
        data_config.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe, year=year, month=month
    )
    if sidecar.has_verified_file(zip_name) and part_path.is_file():
        return None

    zip_url = monthly_zip_url(data_config.binance_base_url, symbol, timeframe, year, month)
    checksum_resp = client.get(checksum_url(zip_url))
    checksum_resp.raise_for_status()
    expected_hex = parse_checksum_text(checksum_resp.text, zip_name)

    zip_resp = client.get(zip_url)
    zip_resp.raise_for_status()
    actual_hex = sha256_hex(zip_resp.content)
    if actual_hex != expected_hex:
        raise ChecksumMismatchError(
            f"checksum mismatch for {zip_name}: expected {expected_hex}, got {actual_hex}"
        )

    raw_path = data_config.raw_root / SOURCE / symbol / timeframe.value / zip_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(zip_resp.content)

    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"{zip_name} contains no CSV entry")
        csv_bytes = zf.read(csv_names[0])

    parsed = parse_kline_csv_bytes(csv_bytes, timeframe=timeframe)
    store_mod.write_month_part(part_path, list(parsed.records))
    sidecar.add_file(zip_name, actual_hex, verified=True)
    # Decision D-036 (points 4/7): persist this month's anomalies so a re-run (idempotent, never
    # re-parses the CSV) and a --report-only run both still see them.
    sidecar.replace_anomalies_in_month(year, month, parsed.anomalies)
    store_mod.save_sidecar(sidecar_file, sidecar)
    return zip_name


def _recompute_sidecar_gaps(
    sidecar: store_mod.DatasetSidecar,
    *,
    data_config: DataConfig,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
) -> tuple[store_mod.GapRecord, ...]:
    """Re-derive ``rows``/``first_ts``/``last_ts``/``gaps`` from the pre-holdout slice of
    whatever is actually on disk, auto-classifying any still-``unknown`` gap per the D-036
    amendment (finding m-J, via ``_classify_fresh_gaps``). Mutates ``sidecar`` in place.

    D-032 (second fix round): the sidecar must only ever *report* pre-holdout numbers -- rows/
    first_ts/last_ts/gaps computed over holdout rows would leak exactly the information the seal
    exists to hide into a file nobody double-locks, even after a prior *unsealed* (G4) run wrote
    holdout rows to this same store.
    """
    full_table = store_mod._read_symbol_timeframe(
        data_config.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe
    )
    gaps: tuple[store_mod.GapRecord, ...] = ()
    sidecar.rows = 0
    sidecar.first_ts = None
    sidecar.last_ts = None
    if full_table.num_rows > 0:
        ts_series_full = full_table.column("ts").to_pandas()
        ts_series = ts_series_full[ts_series_full < CANONICAL_HOLDOUT_START]
        if len(ts_series) > 0:
            gaps = _classify_fresh_gaps(
                quality_mod.find_gaps(ts_series, timeframe),
                previous_gaps=sidecar.gaps,
                anomalies=sidecar.anomalies,
                timeframe=timeframe,
            )
            sidecar.rows = len(ts_series)
            sidecar.first_ts = _as_py_datetime(ts_series.min())
            sidecar.last_ts = _as_py_datetime(ts_series.max())
        sidecar.gaps = list(gaps)

    # Same reasoning as the rows/first_ts/last_ts/gaps trim above, for anomalies.
    sidecar.anomalies = [a for a in sidecar.anomalies if a.raw_open_ts < CANONICAL_HOLDOUT_START]
    sidecar.holdout_start = CANONICAL_HOLDOUT_START
    sidecar.downloaded_at = now
    return gaps


def _ingest_all_months(
    client: httpx.Client,
    *,
    symbol: str,
    timeframe: Timeframe,
    data_config: DataConfig,
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
    sidecar: store_mod.DatasetSidecar,
    sidecar_file: Path,
) -> tuple[list[str], list[str]]:
    """Runs ``_ingest_month`` over every ``(year, month)`` in range. Returns ``(downloaded,
    skipped)`` zip filenames."""
    downloaded: list[str] = []
    skipped: list[str] = []
    for year, month in months_between(start_year, start_month, end_year, end_month):
        zip_name = _ingest_month(
            client,
            symbol=symbol,
            timeframe=timeframe,
            data_config=data_config,
            year=year,
            month=month,
            sidecar=sidecar,
            sidecar_file=sidecar_file,
        )
        if zip_name is None:
            skipped.append(monthly_zip_name(symbol, timeframe, year, month))
        else:
            downloaded.append(zip_name)
    return downloaded, skipped


def ingest_symbol_timeframe(
    client: httpx.Client,
    *,
    symbol: str,
    timeframe: Timeframe,
    data_config: DataConfig,
    now: datetime | None = None,
    allow_holdout: bool = False,
    reason: str = "",
    holdout_log_path: Path | None = None,
) -> IngestOutcome:
    """Download, verify and store every monthly file from ``history_start`` to the latest
    complete month, skipping any month already verified and ingested (idempotent re-run).

    The holdout double-lock (``_resolve_month_range``), the per-month download (``_ingest_month``
    via ``_ingest_all_months``) and the gap/anomaly bookkeeping (``_recompute_sidecar_gaps``) are
    all delegated to helpers above; this function just ties them together.
    """
    now = now or datetime.now(UTC)
    start_year, start_month, end_year, end_month = _resolve_month_range(
        data_config=data_config, symbol=symbol, timeframe=timeframe, now=now,
        allow_holdout=allow_holdout, reason=reason, holdout_log_path=holdout_log_path,
    )

    sidecar_file = store_mod.sidecar_path(
        data_config.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source=SOURCE, symbol=symbol, timeframe=timeframe)

    downloaded, skipped = _ingest_all_months(
        client, symbol=symbol, timeframe=timeframe, data_config=data_config,
        start_year=start_year, start_month=start_month, end_year=end_year, end_month=end_month,
        sidecar=sidecar, sidecar_file=sidecar_file,
    )

    gaps = _recompute_sidecar_gaps(
        sidecar, data_config=data_config, symbol=symbol, timeframe=timeframe, now=now
    )
    store_mod.save_sidecar(sidecar_file, sidecar)

    return IngestOutcome(
        symbol=symbol,
        timeframe=timeframe,
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
        rows_in_store=sidecar.rows,
        gaps=gaps,
        anomalies=tuple(sidecar.anomalies),
    )


def apply_cross_symbol_gap_classification(
    data_config: DataConfig, *, symbols: Sequence[str], timeframe: Timeframe
) -> dict[str, int]:
    """Decision D-036 amendment (new rule, finding m-J follow-up): a gap whose exact
    missing-bar window -- ``(from_ts, to_ts)``, unchanged -- is ALSO a gap in another configured
    symbol's same-timeframe series is exchange-wide evidence no single symbol's anomaly rows can
    provide on their own. Classified ``exchange_wide_outage`` / ``classified_by="cross_symbol"``.

    Run once per timeframe, after every symbol in ``symbols`` has been ingested -- or, in
    ``--report-only`` mode, straight from whatever sidecars are already on disk. Only ever
    touches a gap that is still ``"unknown"``; a classification any other rule (or a human) has
    already recorded is never revisited here, so this can never overwrite one. A gap present in
    only one symbol's series stays ``unknown``. Writes straight to each sidecar that changed and
    returns ``{symbol: n_gaps_reclassified}``.
    """
    sidecars: dict[str, store_mod.DatasetSidecar] = {}
    sidecar_files: dict[str, Path] = {}
    for symbol in symbols:
        sidecar_file = store_mod.sidecar_path(
            data_config.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe
        )
        sidecar_files[symbol] = sidecar_file
        sidecars[symbol] = store_mod.load_sidecar(
            sidecar_file, source=SOURCE, symbol=symbol, timeframe=timeframe
        )

    windows_by_symbol = {
        symbol: {(g.from_ts, g.to_ts) for g in sidecar.gaps} for symbol, sidecar in sidecars.items()
    }

    reclassified: dict[str, int] = {}
    for symbol, sidecar in sidecars.items():
        other_windows: set[tuple[datetime, datetime]] = set()
        for other_symbol, windows in windows_by_symbol.items():
            if other_symbol != symbol:
                other_windows |= windows

        new_gaps = []
        n_reclassified = 0
        for gap in sidecar.gaps:
            if gap.classification == "unknown" and (gap.from_ts, gap.to_ts) in other_windows:
                new_gaps.append(
                    store_mod.GapRecord(
                        from_ts=gap.from_ts,
                        to_ts=gap.to_ts,
                        missing_bars=gap.missing_bars,
                        classification="exchange_wide_outage",
                        classified_by="cross_symbol",
                    )
                )
                n_reclassified += 1
            else:
                new_gaps.append(gap)
        reclassified[symbol] = n_reclassified
        if n_reclassified > 0:
            sidecar.gaps = new_gaps
            store_mod.save_sidecar(sidecar_files[symbol], sidecar)

    return reclassified


# ---------------------------------------------------------------------------------
# sealed holdout (docs/SPEC.md section 5.2, decisions D-008/D-009)
# ---------------------------------------------------------------------------------


def _caller_tag(depth: int = 2) -> str:
    frame = inspect.stack()[depth]
    return f"{Path(frame.filename).name}:{frame.function}:{frame.lineno}"


def _resolve_repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: this file) until a repo-checkout marker is found.

    Minor fix 1 (fix round): the old code computed ``Path(__file__).resolve().parents[3]``
    once at import time, which is simply wrong once this package is installed into
    site-packages inside a container -- there is no ``research/`` directory three levels above
    an installed module. Fail loudly instead of guessing a nonsense path.
    """
    here = (start or Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        f"could not locate the repo root (no pyproject.toml found above {here}) -- this looks "
        "like an installed package, not a repo checkout; pass holdout_log_path explicitly."
    )


def _default_holdout_log_path() -> Path:
    return _resolve_repo_root() / "research" / "HOLDOUT_LOG.md"


def _append_holdout_log(log_path: Path, *, caller: str, reason: str, outcome: str) -> None:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    row = f"| {timestamp} | {caller} | {reason or '(no reason given)'} | — | {outcome} |\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.is_file():
        log_path.write_text(
            "# HOLDOUT LOG\n\n| timestamp (UTC) | caller | reason | approved by | outcome |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(row)


def _resolve_holdout_access(
    *,
    allow_holdout: bool,
    caller: str,
    reason: str,
    log_path: Path | None,
    configured_holdout_start: datetime,
) -> bool:
    """Returns True when the holdout may be included. Raises ``HoldoutLockError`` otherwise.

    Both locks must agree: ``allow_holdout=True`` AND ``TBOT_UNSEAL_HOLDOUT=G4``. Any attempt
    that engages exactly one of the two locks is refused and logged — a stray environment
    variable must never silently unseal data, and a code-level ``allow_holdout=True`` must
    never succeed without the explicit operator-set environment variable.

    BLOCKER B1 / decision D-026: before any of that, ``configured_holdout_start`` (i.e.
    ``DataConfig.holdout_start``, as loaded from whatever YAML the caller passed) is compared
    against the code constant ``CANONICAL_HOLDOUT_START``. A one-line YAML edit — or
    ``--config my.yaml`` pointing at a file with a later ``holdout_start`` — used to unseal
    extra months with no lock, no log and no error. Any disagreement, in either direction, is
    now refused and logged *unless both locks are already engaged* (a legitimate, fully-logged
    G4 run returns everything regardless of ``holdout_start`` anyway, so the mismatch is moot
    there).

    MINOR-1 (second fix round): ``log_path`` may be ``None`` -- the default-log-path resolution
    (``_default_holdout_log_path()``, which walks up from this file looking for
    ``pyproject.toml``) is deferred to ``_log()`` below and only actually runs on the branches
    that write a log row. The ordinary, silent "default sealed read" path (matching
    ``holdout_start``, no locks engaged) never calls it at all, so a ``--no-editable``/wheel
    install -- which has no ``pyproject.toml`` above it -- no longer raises on a plain read just
    because a log file might, hypothetically, need to exist somewhere.
    """
    env_value = os.environ.get(HOLDOUT_UNSEAL_ENV)
    env_matches = env_value == HOLDOUT_UNSEAL_VALUE
    both_locks = allow_holdout and env_matches

    def _log(outcome: str) -> None:
        resolved_path = log_path if log_path is not None else _default_holdout_log_path()
        _append_holdout_log(resolved_path, caller=caller, reason=reason, outcome=outcome)

    if configured_holdout_start != CANONICAL_HOLDOUT_START and not both_locks:
        _log("refused: holdout_start overridden")
        raise HoldoutLockError(
            f"DataConfig.holdout_start ({configured_holdout_start.isoformat()}) disagrees with "
            f"the canonical holdout boundary fixed in code ({CANONICAL_HOLDOUT_START.isoformat()}). "
            "The seal is not a configuration value (decision D-026) — a YAML edit or --config "
            "flag can never move it. See docs/SPEC.md section 5.2."
        )

    if both_locks:
        _log("unsealed")
        return True

    if allow_holdout or env_matches:
        _log("refused: locks mismatched")
        raise HoldoutLockError(
            "Holdout access requires BOTH allow_holdout=True AND "
            f"{HOLDOUT_UNSEAL_ENV}={HOLDOUT_UNSEAL_VALUE!r}; got allow_holdout={allow_holdout} "
            f"and {HOLDOUT_UNSEAL_ENV}={env_value!r}. See docs/SPEC.md section 5.2."
        )

    return False


# ---------------------------------------------------------------------------------
# loader API
# ---------------------------------------------------------------------------------


def load_bars(
    symbol: str,
    timeframe: Timeframe,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    allow_holdout: bool = False,
    config: DataConfig | None = None,
    caller: str | None = None,
    reason: str = "",
    holdout_log_path: Path | None = None,
) -> list[Bar]:
    """Load closed bars as ``Bar`` objects, excluding the sealed holdout by default.

    See the module docstring and docs/SPEC.md section 5.2 for the double-lock rule.
    """
    cfg = config or DataConfig()
    unsealed = _resolve_holdout_access(
        allow_holdout=allow_holdout,
        caller=caller or _caller_tag(),
        reason=reason,
        log_path=holdout_log_path,
        configured_holdout_start=cfg.holdout_start,
    )
    table = store_mod._read_symbol_timeframe(
        cfg.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe, start=start, end=end
    )
    records = store_mod.table_to_records(table)
    if not unsealed:
        records = [r for r in records if r.ts < CANONICAL_HOLDOUT_START]
    return [
        Bar(
            symbol=symbol,
            timeframe=timeframe,
            ts=r.ts,
            open=r.open,
            high=r.high,
            low=r.low,
            close=r.close,
            volume=r.volume,
        )
        for r in records
    ]


def load_frame(
    symbol: str,
    timeframe: Timeframe,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    allow_holdout: bool = False,
    as_float: bool = True,
    config: DataConfig | None = None,
    caller: str | None = None,
    reason: str = "",
    holdout_log_path: Path | None = None,
) -> pd.DataFrame:
    """Load bars as a ``pandas.DataFrame`` (``ts, open, high, low, close, volume, quote_volume,
    trades``), excluding the sealed holdout by default. ``as_float=True`` (default) casts OHLCV
    to float64 for research use; ``as_float=False`` keeps exact ``Decimal`` (object dtype).
    """
    cfg = config or DataConfig()
    unsealed = _resolve_holdout_access(
        allow_holdout=allow_holdout,
        caller=caller or _caller_tag(),
        reason=reason,
        log_path=holdout_log_path,
        configured_holdout_start=cfg.holdout_start,
    )
    table = store_mod._read_symbol_timeframe(
        cfg.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe, start=start, end=end
    )
    df = table.to_pandas()
    if not unsealed and not df.empty:
        df = df.loc[df["ts"] < CANONICAL_HOLDOUT_START].reset_index(drop=True)
    if as_float:
        for column in ("open", "high", "low", "close", "volume", "quote_volume"):
            df[column] = df[column].astype(float)
    return df
