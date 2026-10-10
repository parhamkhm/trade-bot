"""Live-comparison store: Binance CLOSE prices at LBank candle timestamps (docs/SPEC.md D-039).

Purpose (and the only purpose): measure the price difference between Binance and the trading
venue's own candles (gate G1b / G5) *after* the sealed-holdout date, where decision D-032 forbids
putting Binance bars into the research store. To stay clear of that seal this module is built so
that it can answer exactly one question -- "by how many basis points did the venue's close differ
from Binance's close at the same timestamp?" -- and nothing that could feed research:

* It stores one number per ``(ts, symbol, timeframe)``: the Binance close, as a ``Decimal`` string.
  No open/high/low/volume, so there is nothing to build a candle, an indicator or a return from.
* It exposes **no price-series reader**. The only function that reads stored prices is
  ``compare_to_lbank``, which takes the caller's own LBank timestamps/closes and hands back only
  per-timestamp differences in bps (a cross-venue basis, not a time-series). There is no
  ``returns``/``pct_change``/rolling/indicator function and the module imports neither pandas nor
  numpy; ``tests/data/test_live_compare.py`` pins all of this.
* The research read path (``tbot.data.store`` and hence ``binance_loader.load_bars``/``load_frame``
  and the candle readers) refuses any path that resolves through a directory named
  ``live_compare`` (``refuse_live_compare_path``), including ``..``, absolute and symlink routes.
* The writer, in turn, refuses a root that is not inside a ``live_compare`` directory, so the data
  can never be written into the research tree by mistake.

This is not a trial (nothing here is evaluated against a strategy) and it is not research data.

Layout::

    <root>/<SYMBOL>/<timeframe>/<YYYY-MM>.parquet     # month of the bar's CLOSE time, UTC

Schema: ``ts`` (timestamp[us, UTC], bar CLOSE time), ``symbol``, ``timeframe``, ``binance_close``
(string, exact decimal). Append-only: a key that is already stored is never overwritten; writing
the same value again is a no-op, writing a *different* value is reported as a conflict and the
stored value wins.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

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


def _require_live_compare_root(root: Path) -> Path:
    resolved = root.resolve()
    if not any(part.casefold() == LIVE_COMPARE_DIRNAME for part in resolved.parts):
        raise ValueError(
            f"live-compare root {str(root)!r} must lie inside a '{LIVE_COMPARE_DIRNAME}' directory "
            "so it can never be mistaken for (or read as) research data"
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


def _month_path(root: Path, symbol: str, timeframe: Timeframe, year: int, month: int) -> Path:
    return root / symbol / timeframe.value / f"{year:04d}-{month:02d}.parquet"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(path)


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
) -> AppendResult:
    """Append Binance closes, idempotently on ``(ts, symbol, timeframe)``.

    * A point whose key is already stored with the same value is skipped (re-runs are no-ops).
    * A point whose key is stored with a *different* value is NOT written; its ``ts`` is returned
      in ``conflicts`` (append-only: history is never rewritten).
    * Only closed bars are accepted: ``ts`` in the future relative to ``now`` (default: the
      current UTC time) raises ``ValueError``.

    ``root`` must lie inside a ``live_compare`` directory. Each touched month file is rewritten
    atomically (tmp file + replace).
    """
    resolved_root = _require_live_compare_root(root)
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
