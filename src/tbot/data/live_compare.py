"""Live-comparison store: Binance CLOSE prices at LBank candle timestamps (docs/SPEC.md D-039).

Purpose (and the only purpose): measure the price difference between Binance and the trading
venue's own candles (gate G1b / G5) *after* the sealed-holdout date, where decision D-032 forbids
putting Binance bars into the research store. To stay clear of that seal this module is built so
that it can answer exactly one question -- "by how many basis points did the venue's close differ
from Binance's close at the same timestamp?" -- and nothing that could feed research:

* It stores one number per ``(ts, symbol, timeframe)``: the Binance close, as a ``Decimal`` string.
  No open/high/low/volume, so there is nothing to build a candle, an indicator or a return from.
* It exposes **no direct price-series reader**: there is no function that hands back the stored
  Binance closes for a symbol/timeframe. Be honest about what that does and does not buy you,
  though: ``compare_to_lbank`` still takes the caller's own candidate closes and returns a
  per-timestamp difference in bps, so a caller who already has a good guess at the Binance price
  can solve the returned bps for the exact stored value -- it is a diagnostic cross-check, not a
  price oracle, and research code has no legitimate reason to call it at all. Two mitigations,
  neither of them a cryptographic guarantee: (a) every candidate close is checked against the
  stored Binance close and refused outside +/- ``_MAX_RELATIVE_DIFF`` (20%), so a blind sentinel
  such as ``lbank_closes={ts: Decimal("1")}`` is refused rather than silently leaking the real
  price; (b) no ``tbot`` research reader can reach this module in the first place (see the next
  bullet). (b) is the real protection, backed by this module's own guard and by code review -- not
  by anything ``compare_to_lbank`` itself withholds from a caller willing to spend a few queries.
  There is no ``returns``/``pct_change``/rolling/indicator function, and the module imports
  neither pandas nor numpy; ``tests/data/test_live_compare.py`` pins all of this.
* The research read path (``tbot.data.store`` and hence ``binance_loader.load_bars``/``load_frame``
  and the candle readers) refuses any path that resolves through a directory named
  ``live_compare`` (``refuse_live_compare_path``), including ``..``, absolute and symlink routes.
* The writer, in turn, refuses a root that is not inside a ``live_compare`` directory, AND refuses
  one that overlaps the configured research parquet root in either direction (nested inside it, or
  containing it) -- so the data can never be written into, or around, the research tree by mistake.

This is not a trial (nothing here is evaluated against a strategy) and it is not research data.

Known, accepted limits (see also the proposed D-039 SPEC wording in the task report): a plain
``pandas.read_parquet`` or ``pyarrow.parquet.read_table`` pointed at this store directly, from a
notebook or a shell, is not blocked -- only the ``tbot`` research readers are guarded. A hardlink
from inside the research tree to a file in this store is also not detectable by path alone.

Layout::

    <root>/<SYMBOL>/<timeframe>/<YYYY-MM>.parquet     # month of the bar's CLOSE time, UTC

Schema: ``ts`` (timestamp[us, UTC], bar CLOSE time), ``symbol``, ``timeframe``, ``binance_close``
(string, exact decimal). Append-only: a key that is already stored is never overwritten; writing
the same value again is a no-op, writing a *different* value is reported as a conflict and the
stored value wins. Each month's part file is written through a per-month lock (``_MonthLock``) and
an atomic write (unique tmp name via ``tempfile``, ``fsync`` before ``os.replace``), so concurrent
writers never corrupt or lose a month, and a crash between the tmp write and the replace leaves the
previous, still-valid file in place.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from tbot.core.config import DataConfig
from tbot.core.types import Timeframe, ensure_utc

__all__ = [
    "DEFAULT_ROOT",
    "LIVE_COMPARE_DIRNAME",
    "AppendResult",
    "BinanceClose",
    "CompareResult",
    "Coverage",
    "LiveCompareAccessError",
    "PriceDifference",
    "append_binance_closes",
    "compare_to_lbank",
    "coverage",
    "refuse_live_compare_path",
]

LIVE_COMPARE_DIRNAME = "live_compare"
DEFAULT_ROOT = Path("data") / LIVE_COMPARE_DIRNAME

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_BPS = Decimal(10_000)
_MAX_RELATIVE_DIFF = Decimal("0.20")  # compare_to_lbank's sanity bound (review MAJOR-1)

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.02

_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("symbol", pa.string()),
        pa.field("timeframe", pa.string()),
        pa.field("binance_close", pa.string()),
    ]
)


class LiveCompareAccessError(PermissionError):
    """Research code tried to touch the live-comparison store (decision D-039)."""


def refuse_live_compare_path(path: str | os.PathLike[str], *, what: str = "path") -> None:
    """Raise ``LiveCompareAccessError`` when ``path`` resolves through a ``live_compare`` directory.

    The path is fully resolved first (``..`` collapsed, symlinks/junctions followed, relative
    paths made absolute), so none of those routes can hide the directory. The test is by path
    *component name* (case-insensitive), not by a hard-coded location, so it holds wherever the
    data lives (repo ``data/``, a mounted server volume, a test temp dir). The price of that
    robustness: no research directory anywhere may be named ``live_compare``.
    """
    resolved = Path(path).resolve()
    if any(part.casefold() == LIVE_COMPARE_DIRNAME for part in resolved.parts):
        raise LiveCompareAccessError(
            f"refusing to use {what} {str(path)!r} (resolves to {str(resolved)!r}): it lies inside "
            f"a '{LIVE_COMPARE_DIRNAME}' directory. The live-comparison store holds Binance prices "
            "after the sealed-holdout date and is unreadable by research code (docs/SPEC.md D-039)."
        )


def _overlaps(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are the same directory, or one is nested inside the other."""
    return a == b or a in b.parents or b in a.parents


def _require_live_compare_root(root: Path, *, parquet_root: Path | None = None) -> Path:
    """Resolve ``root`` and refuse it unless it is a ``live_compare`` directory that sits clear
    of the research parquet root (review MAJOR-3): nested inside it, or containing it, are both
    refused, not just a name check -- a root that happens to be named ``live_compare`` but lives
    underneath ``data/parquet`` would otherwise still write research-tree-adjacent data that
    ``refuse_live_compare_path`` could not later distinguish from the real thing by name alone.

    ``parquet_root`` defaults to the configured ``DataConfig().parquet_root`` (``data/parquet``)
    so a caller gets the real guard for free; it is overridable for tests and for callers running
    against a non-default config.
    """
    resolved = root.resolve()
    if not any(part.casefold() == LIVE_COMPARE_DIRNAME for part in resolved.parts):
        raise ValueError(
            f"live-compare root {str(root)!r} must lie inside a '{LIVE_COMPARE_DIRNAME}' directory "
            "so it can never be mistaken for (or read as) research data"
        )
    research_root = (parquet_root if parquet_root is not None else DataConfig().parquet_root).resolve()
    if _overlaps(resolved, research_root):
        raise ValueError(
            f"live-compare root {str(resolved)!r} overlaps the research parquet root "
            f"{str(research_root)!r}; the live-comparison store must never sit inside, or contain, "
            "the research tree"
        )
    return resolved


# ---------------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BinanceClose:
    """One stored point: the Binance close of the bar that closed at ``ts``."""

    ts: datetime
    symbol: str
    timeframe: Timeframe
    close: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "BinanceClose.ts"))
        if not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError(
                f"invalid symbol {self.symbol!r}: expected upper-case letters/digits, e.g. BTCUSDT"
            )
        object.__setattr__(self, "timeframe", Timeframe(self.timeframe))
        _validate_price(self.close, "BinanceClose.close")
        _validate_on_grid(self.ts, self.timeframe)


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Outcome of one ``append_binance_closes`` call."""

    written: int
    already_present: int
    conflicts: tuple[datetime, ...]  # keys whose stored value differs from the offered one


@dataclass(frozen=True, slots=True)
class PriceDifference:
    """LBank close versus Binance close at one timestamp, in basis points of the Binance close."""

    ts: datetime
    bps: Decimal  # (lbank - binance) / binance * 10_000


@dataclass(frozen=True, slots=True)
class CompareResult:
    """Matched differences only; counts say how much did not match, never by how much."""

    differences: tuple[PriceDifference, ...]
    lbank_without_binance: int
    binance_months_missing: int = 0


@dataclass(frozen=True, slots=True)
class Coverage:
    """Row count and time span of the stored points -- deliberately carries no prices."""

    rows: int
    first_ts: datetime | None
    last_ts: datetime | None


# ---------------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------------


def _validate_price(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a finite positive Decimal, got {value!r}")


def _validate_on_grid(ts: datetime, timeframe: Timeframe) -> None:
    if (ts - _EPOCH) % timeframe.delta != timedelta(0):
        raise ValueError(
            f"ts {ts.isoformat()} is not on the {timeframe.value} close grid (UTC epoch multiples)"
        )


def _validate_symbol(symbol: str) -> None:
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"invalid symbol {symbol!r}: expected upper-case letters/digits, e.g. BTCUSDT")


def _validate_close_to_binance(lbank_price: Decimal, binance_price: Decimal, ts: datetime) -> None:
    """Refuse a candidate LBank close that is not within +/- 20% of the stored Binance close.

    Review MAJOR-1: without this, ``compare_to_lbank`` is a de-facto price-series reader -- pass
    a sentinel like ``Decimal("1")`` at every timestamp and the returned bps solves exactly for
    the stored Binance close. This does not make the function a true oracle-proof boundary (a
    caller who already has a decent estimate of the real price can still narrow it down with a
    few queries), but it does refuse the blind, universal sentinel this review found, and any
    other candidate implausible enough to be a probing attempt rather than a genuine LBank close.
    """
    relative = abs(lbank_price - binance_price) / binance_price
    if relative > _MAX_RELATIVE_DIFF:
        raise ValueError(
            f"lbank close {lbank_price} at {ts.isoformat()} differs from the stored Binance close "
            f"{binance_price} by {relative * 100:.2f}%, more than the {_MAX_RELATIVE_DIFF * 100:.0f}% "
            "sanity bound -- compare_to_lbank is a diagnostic cross-check, not a price lookup; refusing"
        )


def _month_path(root: Path, symbol: str, timeframe: Timeframe, year: int, month: int) -> Path:
    return root / symbol / timeframe.value / f"{year:04d}-{month:02d}.parquet"


# ---------------------------------------------------------------------------------
# per-month lock + atomic write (review MAJOR-4)
# ---------------------------------------------------------------------------------


class _MonthLock:
    """A simple, portable mutual-exclusion lock for one month's part file.

    ``os.O_CREAT | os.O_EXCL`` atomically creating the lock file fails with ``FileExistsError``
    when another writer (thread, process, or another machine on a shared volume) already holds
    it, on both POSIX and Windows -- no third-party locking library needed (CLAUDE.md section 7:
    new dependencies need a one-line SPEC justification). Polls with a short sleep rather than
    blocking indefinitely, so a crashed writer that left its lock file behind cannot wedge every
    future run forever silently -- it raises ``TimeoutError`` instead.
    """

    def __init__(self, target: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> None:
        self._lock_path = target.with_name(target.name + ".lock")
        self._timeout = timeout

    def __enter__(self) -> _MonthLock:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError) as exc:
                # Windows reports a lock file still pending deletion by the previous holder as
                # PermissionError, not FileExistsError; on POSIX a PermissionError is a real error.
                if isinstance(exc, PermissionError) and sys.platform != "win32":
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out after {self._timeout}s waiting for the lock on {self._lock_path}"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            os.close(fd)
            return self

    def __exit__(self, *exc_info: object) -> None:
        self._lock_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Best-effort: fsync the directory entry too, so the rename itself is durable, not just the
    new file's bytes. Not supported on every platform (opening a directory this way raises on
    Windows) -- silently skipped there. The per-file ``fsync`` in ``_atomic_write_table`` is the
    one that matters for correctness; this is defence in depth for a real crash on the server
    (CLAUDE.md: Ubuntu VPS), where it does work.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_write_table(path: Path, table: pa.Table) -> None:
    """Write ``table`` to ``path`` atomically: a uniquely-named tmp file in the same directory
    (``tempfile.mkstemp``, so concurrent writers to different months, or a retried writer, never
    collide on a fixed ``.tmp`` name), ``fsync``ed before ``os.replace`` swaps it into place. If
    anything fails before the swap, the previous ``path`` (if any) is untouched and the tmp file
    is removed; ``os.replace`` itself is atomic on both POSIX and Windows when src/dst are on the
    same filesystem, which they are here (same parent directory).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.stem}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            pq.write_table(table, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)  # noqa: PTH105 - os.replace is the seam the atomicity test patches
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


# ---------------------------------------------------------------------------------
# parquet I/O (private: no price series leaves this module)
# ---------------------------------------------------------------------------------


def _read_month(path: Path) -> dict[datetime, str]:
    if not path.is_file():
        return {}
    table = pq.read_table(path, schema=_SCHEMA)
    out: dict[datetime, str] = {}
    for row in table.to_pylist():
        ts = row["ts"]
        out[ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)] = row["binance_close"]
    return out


def _write_month(path: Path, symbol: str, timeframe: Timeframe, rows: Mapping[datetime, str]) -> None:
    ordered = sorted(rows.items())
    columns: dict[str, list[Any]] = {
        "ts": [ts for ts, _ in ordered],
        "symbol": [symbol] * len(ordered),
        "timeframe": [timeframe.value] * len(ordered),
        "binance_close": [value for _, value in ordered],
    }
    table = pa.table(columns, schema=_SCHEMA)
    _atomic_write_table(path, table)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


# ---------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------


def append_binance_closes(
    root: Path,
    points: Iterable[BinanceClose],
    *,
    now: datetime | None = None,
    parquet_root: Path | None = None,
) -> AppendResult:
    """Append Binance closes, idempotently on ``(ts, symbol, timeframe)``.

    * A point whose key is already stored with the same value is skipped (re-runs are no-ops).
    * A point whose key is stored with a *different* value is NOT written; its ``ts`` is returned
      in ``conflicts`` (append-only: history is never rewritten).
    * Only closed bars are accepted: ``ts`` in the future relative to ``now`` (default: the
      current UTC time) raises ``ValueError``.

    ``root`` must lie inside a ``live_compare`` directory and must not overlap the research
    parquet root (``parquet_root``, default ``DataConfig().parquet_root`` -- see
    ``_require_live_compare_root``). Each touched month file is updated under a per-month lock
    and written atomically (``_atomic_write_table``): a crash or a concurrent writer can delay a
    write, never corrupt or lose one.
    """
    resolved_root = _require_live_compare_root(root, parquet_root=parquet_root)
    now_utc = ensure_utc(now, "now") if now is not None else datetime.now(UTC)

    grouped: dict[tuple[str, Timeframe, int, int], list[BinanceClose]] = {}
    for point in points:
        if point.ts > now_utc:
            raise ValueError(f"ts {point.ts.isoformat()} is in the future; refusing to store an unclosed bar")
        grouped.setdefault((point.symbol, point.timeframe, point.ts.year, point.ts.month), []).append(point)

    written = 0
    already = 0
    conflicts: list[datetime] = []
    for key in sorted(grouped):
        symbol, timeframe, year, month = key
        group = grouped[key]
        path = _month_path(resolved_root, symbol, timeframe, year, month)
        path.parent.mkdir(parents=True, exist_ok=True)  # so the lock file has somewhere to live
        with _MonthLock(path):
            rows = _read_month(path)
            new_rows = 0
            for point in sorted(group, key=lambda p: p.ts):
                text = _decimal_text(point.close)
                stored = rows.get(point.ts)
                if stored is None:
                    rows[point.ts] = text
                    new_rows += 1
                elif Decimal(stored) == point.close:
                    already += 1
                else:
                    conflicts.append(point.ts)
            if new_rows:
                _write_month(path, symbol, timeframe, rows)
                written += new_rows
    return AppendResult(written=written, already_present=already, conflicts=tuple(conflicts))


def compare_to_lbank(
    root: Path,
    *,
    symbol: str,
    timeframe: Timeframe,
    lbank_closes: Mapping[datetime, Decimal],
) -> CompareResult:
    """Price difference in bps at every timestamp present in BOTH ``lbank_closes`` and the store.

    ``lbank_closes`` maps an LBank candle's CLOSE time (UTC) to its close price. For each match
    the result carries ``(lbank - binance) / binance * 10_000``; timestamps found on only one
    side are counted (``lbank_without_binance``), never filled or interpolated. Only the months
    containing a requested timestamp are opened. The output is a set of cross-venue differences
    at identical instants -- no returns, no rolling or other statistics are computed here.

    Every matched candidate is also checked against ``_MAX_RELATIVE_DIFF`` (see
    ``_validate_close_to_binance``): this function is a diagnostic cross-check against a value
    the caller already has, not a way to recover the stored Binance price from nothing.
    """
    _validate_symbol(symbol)
    timeframe = Timeframe(timeframe)
    resolved_root = _require_live_compare_root(root)

    wanted: dict[datetime, Decimal] = {}
    for raw_ts, price in lbank_closes.items():
        ts = ensure_utc(raw_ts, "lbank_closes key")
        _validate_price(price, f"lbank close at {ts.isoformat()}")
        wanted[ts] = price

    months = sorted({(ts.year, ts.month) for ts in wanted})
    stored: dict[datetime, str] = {}
    missing_months = 0
    for year, month in months:
        path = _month_path(resolved_root, symbol, timeframe, year, month)
        if not path.is_file():
            missing_months += 1
            continue
        stored.update(_read_month(path))

    differences: list[PriceDifference] = []
    for ts in sorted(wanted):
        binance_text = stored.get(ts)
        if binance_text is None:
            continue
        binance = Decimal(binance_text)
        _validate_close_to_binance(wanted[ts], binance, ts)
        bps = (wanted[ts] - binance) / binance * _BPS
        differences.append(PriceDifference(ts=ts, bps=bps))
    return CompareResult(
        differences=tuple(differences),
        lbank_without_binance=len(wanted) - len(differences),
        binance_months_missing=missing_months,
    )


def coverage(root: Path, *, symbol: str, timeframe: Timeframe) -> Coverage:
    """Row count and first/last close time stored for ``symbol/timeframe`` (no prices)."""
    _validate_symbol(symbol)
    timeframe = Timeframe(timeframe)
    resolved_root = _require_live_compare_root(root)
    directory = resolved_root / symbol / timeframe.value
    rows = 0
    first: datetime | None = None
    last: datetime | None = None
    if directory.is_dir():
        for path in sorted(directory.glob("*.parquet")):
            timestamps = list(_read_month(path))
            if not timestamps:
                continue
            rows += len(timestamps)
            lo, hi = min(timestamps), max(timestamps)
            first = lo if first is None else min(first, lo)
            last = hi if last is None else max(last, hi)
    return Coverage(rows=rows, first_ts=first, last_ts=last)
