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
.delta`` and use ``close_time`` only as a corruption sanity-check (must land within 1s of the
derived value minus that one-unit epsilon). This satisfies "ts is the bar's close time,
normalised to the exact close instant" (docs/SPEC.md section 5.1) without guessing a unit for
the epsilon.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import zipfile
from collections.abc import Iterator
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
    "TimestampUnitError",
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


def parse_kline_csv_bytes(data: bytes, *, timeframe: Timeframe) -> list[store_mod.KlineRecord]:
    """Parse one Binance monthly kline CSV (already extracted from its ZIP) into records.

    Columns (no header in files observed through 2025-09; a header row, if ever present, is
    skipped defensively): open_time, open, high, low, close, volume, close_time, quote_volume,
    count, taker_buy_volume, taker_buy_quote_volume, ignore.
    """
    text = data.decode("utf-8")
    records: list[store_mod.KlineRecord] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 9:
            raise ValueError(f"unexpected kline CSV row (expected >= 9 columns): {line!r}")
        try:
            open_time_raw = int(parts[0])
        except ValueError:
            continue  # header row (e.g. "open_time,open,..."), skip defensively
        close_time_raw = int(parts[6])

        open_unit = detect_timestamp_unit(open_time_raw)
        close_unit = detect_timestamp_unit(close_time_raw)
        if open_unit != close_unit:
            raise TimestampUnitError(
                f"open_time unit ({open_unit}) disagrees with close_time unit ({close_unit}) "
                f"in row {line!r}"
            )

        open_dt = to_utc_datetime(open_time_raw, open_unit)
        close_ts = open_dt + timeframe.delta  # exact close instant; see module docstring

        close_raw_dt = to_utc_datetime(close_time_raw, close_unit)
        epsilon = timedelta(milliseconds=1) if close_unit == "ms" else timedelta(microseconds=1)
        expected_close_raw = close_ts - epsilon
        if abs((close_raw_dt - expected_close_raw).total_seconds()) > 1.0:
            raise ValueError(
                f"close_time {close_raw_dt.isoformat()} is not open_time + {timeframe.value} - "
                f"1 {close_unit} ({expected_close_raw.isoformat()}) for row {line!r}"
            )

        records.append(
            store_mod.KlineRecord(
                ts=close_ts,
                open=Decimal(parts[1]),
                high=Decimal(parts[2]),
                low=Decimal(parts[3]),
                close=Decimal(parts[4]),
                volume=Decimal(parts[5]),
                quote_volume=Decimal(parts[7]),
                trades=int(parts[8]),
            )
        )
    return records


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


def _as_py_datetime(value: datetime) -> datetime:
    to_pydatetime = getattr(value, "to_pydatetime", None)
    return to_pydatetime() if callable(to_pydatetime) else value


def _last_month_before_holdout() -> tuple[int, int]:
    """The last calendar month strictly before ``CANONICAL_HOLDOUT_START`` (2025-10-01 -> Sept 2025)."""
    boundary = CANONICAL_HOLDOUT_START
    if boundary.month == 1:
        return boundary.year - 1, 12
    return boundary.year, boundary.month - 1


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

    MINOR-10 / decision D-032 (second fix round): by default, ingestion never requests, downloads
    or writes a part file for any month at or after ``CANONICAL_HOLDOUT_START`` -- previously this
    function ran all the way to ``latest_complete_month(now)`` regardless of the seal, so a plain
    ``download_binance.py`` run wrote holdout ZIPs to ``data/raw/`` and holdout Parquet parts to
    disk, and a stray ``pd.read_parquet(...)`` on ``year=2025/`` (the Oct-Dec part) or later was
    then the one remaining way to see sealed bars without going through the double-locked loader.
    Ingestion only reaches the holdout year when **both** unseal locks are engaged (the same
    ``allow_holdout=True`` AND ``TBOT_UNSEAL_HOLDOUT=G4`` double lock ``load_bars``/``load_frame``
    use) -- the SPEC's "the holdout year is fetched once, at G4" is enforced here, not just at
    read time. Any months skipped this way are logged
    (``binance_loader.ingest_skipped_sealed_months``) so a human can see, at a glance, that a run
    stopped early on purpose rather than failing silently.

    MINOR-8 (third fix round): the double-lock check itself is now delegated to
    ``_resolve_holdout_access`` (the exact same function ``load_bars``/``load_frame`` use) instead
    of re-implementing the two-lock comparison inline. The old inline version
    (``allow_holdout and os.environ.get(...) == "G4"``) enforced the clamp correctly but appended
    nothing to ``research/HOLDOUT_LOG.md`` and skipped the D-026 tamper check (a ``DataConfig``
    whose ``holdout_start`` disagrees with ``CANONICAL_HOLDOUT_START``) entirely -- a G4 ingest run
    left no audit trail, and a moved ``holdout_start`` was silently honoured here even though
    ``load_bars``/``load_frame`` would have refused it. Routing through the shared resolver fixes
    both: every ingest attempt that touches a lock is now logged, and a mismatched
    ``holdout_start`` is refused exactly as it would be at read time.
    """
    now = now or datetime.now(UTC)
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
        end_year, end_month = latest_year, latest_month
    else:
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

    sidecar_file = store_mod.sidecar_path(
        data_config.parquet_root, source=SOURCE, symbol=symbol, timeframe=timeframe
    )
    sidecar = store_mod.load_sidecar(sidecar_file, source=SOURCE, symbol=symbol, timeframe=timeframe)

    downloaded: list[str] = []
    skipped: list[str] = []

    for year, month in months_between(start_year, start_month, end_year, end_month):
        zip_name = monthly_zip_name(symbol, timeframe, year, month)
        part_path = store_mod.month_part_path(
            data_config.parquet_root,
            source=SOURCE,
            symbol=symbol,
            timeframe=timeframe,
            year=year,
            month=month,
        )
        if sidecar.has_verified_file(zip_name) and part_path.is_file():
            skipped.append(zip_name)
            continue

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

        records = parse_kline_csv_bytes(csv_bytes, timeframe=timeframe)
        store_mod.write_month_part(part_path, records)
        sidecar.add_file(zip_name, actual_hex, verified=True)
        downloaded.append(zip_name)
        # Minor fix 3 (fix round): persist after every month, not once at the end of the loop --
        # otherwise a crash on month 50 discards the verified-file record for months 1-49 and the
        # next run re-downloads everything from scratch instead of resuming.
        store_mod.save_sidecar(sidecar_file, sidecar)

    # D-032 (second fix round): by default the loop above never wrote a holdout-year part file, so
    # this read normally sees only pre-holdout rows already. It can still see holdout rows if a
    # prior *unsealed* (G4) ingest run wrote them to this same store, or in a test that wrote
    # synthetic holdout rows directly -- so the explicit filter below is kept regardless: per
    # minor fix 2, the sidecar must only ever *report* pre-holdout numbers. rows/first_ts/last_ts/
    # gaps computed over holdout rows would leak exactly the information the seal exists to hide
    # into a file nobody double-locks.
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
            findings = quality_mod.find_gaps(ts_series, timeframe)
            previous_classification = {
                (g.from_ts, g.to_ts): g.classification for g in sidecar.gaps
            }
            new_gaps = [
                store_mod.GapRecord(
                    from_ts=_as_py_datetime(f.from_ts),
                    to_ts=_as_py_datetime(f.to_ts),
                    missing_bars=f.missing_bars,
                    classification=previous_classification.get(
                        (_as_py_datetime(f.from_ts), _as_py_datetime(f.to_ts)), "unknown"
                    ),
                )
                for f in findings
            ]
            gaps = tuple(new_gaps)
            sidecar.rows = len(ts_series)
            sidecar.first_ts = _as_py_datetime(ts_series.min())
            sidecar.last_ts = _as_py_datetime(ts_series.max())
        sidecar.gaps = list(gaps)

    sidecar.holdout_start = CANONICAL_HOLDOUT_START
    sidecar.downloaded_at = now
    store_mod.save_sidecar(sidecar_file, sidecar)

    return IngestOutcome(
        symbol=symbol,
        timeframe=timeframe,
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
        rows_in_store=sidecar.rows,
        gaps=gaps,
    )


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
