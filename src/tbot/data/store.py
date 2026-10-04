"""Parquet storage for Binance kline data (docs/SPEC.md section 5.1).

Layout (Hive-partitioned):

    data/parquet/klines/source=<source>/symbol=<symbol>/timeframe=<tf>/year=<yyyy>/part-<yyyy>-<mm>.parquet

Each ``symbol/timeframe`` directory carries a ``_dataset.json`` sidecar describing rows,
coverage, per-file checksums and known gaps. One part file per calendar month keeps ingestion
naturally idempotent: a month is only (re)written when its sidecar entry is missing.

OHLCV is stored as ``decimal128(38, 12)`` parsed directly from the source CSV strings (never
through ``float``); ``ts`` is the bar's CLOSE time as ``timestamp[us, tz=UTC]`` (decision D-011,
D-002). Research code casts to ``float64`` on load.

WARNING (decision D-028): ``_read_symbol_timeframe`` below is a raw, holdout-UNAWARE reader -- it
returns every row on disk, sealed holdout included, with no lock and no log. It is private and
unexported precisely so a future caller does not reach for it by accident; the only sanctioned
read path for research/strategy code is ``tbot.data.binance_loader.load_bars`` /
``load_frame``, which enforce the sealed-holdout double lock (docs/SPEC.md section 5.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from tbot.core.types import Timeframe, ensure_utc

__all__ = [
    "KLINE_SCHEMA",
    "TOOL_VERSION",
    "DatasetSidecar",
    "FileRecord",
    "GapRecord",
    "KlineRecord",
    "month_part_path",
    "partition_dir",
    "sidecar_path",
    "write_month_part",
]

TOOL_VERSION = "0.1.0"

KLINE_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("open", pa.decimal128(38, 12)),
        pa.field("high", pa.decimal128(38, 12)),
        pa.field("low", pa.decimal128(38, 12)),
        pa.field("close", pa.decimal128(38, 12)),
        pa.field("volume", pa.decimal128(38, 12)),
        pa.field("quote_volume", pa.decimal128(38, 12)),
        pa.field("trades", pa.int64()),
    ]
)


@dataclass(frozen=True, slots=True)
class KlineRecord:
    """One parsed, closed kline row — the canonical row shared by the loader and the store.

    Richer than ``tbot.core.types.Bar`` (adds ``quote_volume``/``trades`` for the quality
    report); ``ts`` is always the bar's CLOSE time.
    """

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, "KlineRecord.ts"))


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One ingested source file, as recorded in the ``_dataset.json`` sidecar."""

    name: str
    sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "verified": self.verified}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        return cls(name=str(data["name"]), sha256=str(data["sha256"]), verified=bool(data["verified"]))


@dataclass(frozen=True, slots=True)
class GapRecord:
    """A run of missing bars between two known timestamps. Defaults to ``classification='unknown'``."""

    from_ts: datetime
    to_ts: datetime
    missing_bars: int
    classification: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_ts", ensure_utc(self.from_ts, "GapRecord.from_ts"))
        object.__setattr__(self, "to_ts", ensure_utc(self.to_ts, "GapRecord.to_ts"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_ts.isoformat().replace("+00:00", "Z"),
            "to": self.to_ts.isoformat().replace("+00:00", "Z"),
            "missing_bars": self.missing_bars,
            "classification": self.classification,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GapRecord:
        return cls(
            from_ts=datetime.fromisoformat(str(data["from"]).replace("Z", "+00:00")),
            to_ts=datetime.fromisoformat(str(data["to"]).replace("Z", "+00:00")),
            missing_bars=int(data["missing_bars"]),
            classification=str(data.get("classification", "unknown")),
        )


@dataclass(slots=True)
class DatasetSidecar:
    """``_dataset.json`` — one per ``symbol/timeframe`` directory (docs/SPEC.md section 5.1)."""

    source: str
    symbol: str
    timeframe: Timeframe
    rows: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    holdout_start: datetime | None = None
    files: list[FileRecord] = field(default_factory=list)
    gaps: list[GapRecord] = field(default_factory=list)
    downloaded_at: datetime | None = None
    tool_version: str = TOOL_VERSION

    def verified_file_names(self) -> set[str]:
        return {f.name for f in self.files if f.verified}

    def has_verified_file(self, name: str) -> bool:
        return name in self.verified_file_names()

    def add_file(self, name: str, sha256: str, *, verified: bool) -> None:
        self.files = [f for f in self.files if f.name != name]
        self.files.append(FileRecord(name=name, sha256=sha256, verified=verified))

    def to_dict(self) -> dict[str, Any]:
        def _iso(dt: datetime | None) -> str | None:
            return None if dt is None else dt.isoformat().replace("+00:00", "Z")

        return {
            "source": self.source,
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "rows": self.rows,
            "first_ts": _iso(self.first_ts),
            "last_ts": _iso(self.last_ts),
            "holdout_start": _iso(self.holdout_start),
            "files": [f.to_dict() for f in self.files],
            "gaps": [g.to_dict() for g in self.gaps],
            "downloaded_at": _iso(self.downloaded_at),
            "tool_version": self.tool_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetSidecar:
        def _parse(value: str | None) -> datetime | None:
            if value is None:
                return None
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        return cls(
            source=str(data["source"]),
            symbol=str(data["symbol"]),
            timeframe=Timeframe(data["timeframe"]),
            rows=int(data.get("rows", 0)),
            first_ts=_parse(data.get("first_ts")),
            last_ts=_parse(data.get("last_ts")),
            holdout_start=_parse(data.get("holdout_start")),
            files=[FileRecord.from_dict(f) for f in data.get("files", [])],
            gaps=[GapRecord.from_dict(g) for g in data.get("gaps", [])],
            downloaded_at=_parse(data.get("downloaded_at")),
            tool_version=str(data.get("tool_version", TOOL_VERSION)),
        )


# ---------------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------------


def partition_dir(
    parquet_root: Path, *, source: str, symbol: str, timeframe: Timeframe, year: int
) -> Path:
    return (
        parquet_root
        / "klines"
        / f"source={source}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe.value}"
        / f"year={year:04d}"
    )


def dataset_dir(parquet_root: Path, *, source: str, symbol: str, timeframe: Timeframe) -> Path:
    return parquet_root / "klines" / f"source={source}" / f"symbol={symbol}" / f"timeframe={timeframe.value}"


def month_part_path(
    parquet_root: Path, *, source: str, symbol: str, timeframe: Timeframe, year: int, month: int
) -> Path:
    return partition_dir(parquet_root, source=source, symbol=symbol, timeframe=timeframe, year=year) / (
        f"part-{year:04d}-{month:02d}.parquet"
    )


def sidecar_path(parquet_root: Path, *, source: str, symbol: str, timeframe: Timeframe) -> Path:
    return dataset_dir(parquet_root, source=source, symbol=symbol, timeframe=timeframe) / "_dataset.json"


# ---------------------------------------------------------------------------------
# sidecar I/O (atomic writes so a crash mid-write cannot corrupt it)
# ---------------------------------------------------------------------------------


def load_sidecar(
    path: Path, *, source: str, symbol: str, timeframe: Timeframe
) -> DatasetSidecar:
    if not path.is_file():
        return DatasetSidecar(source=source, symbol=symbol, timeframe=timeframe)
    data = json.loads(path.read_text(encoding="utf-8"))
    return DatasetSidecar.from_dict(data)


def save_sidecar(path: Path, sidecar: DatasetSidecar) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(sidecar.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


# ---------------------------------------------------------------------------------
# parquet I/O
# ---------------------------------------------------------------------------------


def _records_to_table(records: list[KlineRecord]) -> pa.Table:
    ordered = sorted(records, key=lambda r: r.ts)
    columns: dict[str, list[Any]] = {
        "ts": [r.ts for r in ordered],
        "open": [r.open for r in ordered],
        "high": [r.high for r in ordered],
        "low": [r.low for r in ordered],
        "close": [r.close for r in ordered],
        "volume": [r.volume for r in ordered],
        "quote_volume": [r.quote_volume for r in ordered],
        "trades": [r.trades for r in ordered],
    }
    return pa.table(columns, schema=KLINE_SCHEMA)


def write_month_part(path: Path, records: list[KlineRecord]) -> int:
    """Write one month's klines as a single part file. Overwrites any previous attempt at that path.

    Returns the number of rows written.
    """
    table = _records_to_table(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(path)
    return int(table.num_rows)


def _read_symbol_timeframe(
    parquet_root: Path,
    *,
    source: str,
    symbol: str,
    timeframe: Timeframe,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pa.Table:
    """Read every part file for ``symbol/timeframe``, concatenated and sorted by ``ts``.

    ``start``/``end`` prune whole year-partitions before reading (cheap) and are also applied
    as an exact row filter (``start`` inclusive, ``end`` exclusive) so callers get exact bounds.

    WARNING (decision D-028): this is a raw reader with **no holdout awareness whatsoever** --
    it happily returns sealed rows. Private and unexported on purpose; use
    ``tbot.data.binance_loader.load_bars``/``load_frame`` instead.
    """
    ds_dir = dataset_dir(parquet_root, source=source, symbol=symbol, timeframe=timeframe)
    if not ds_dir.is_dir():
        return KLINE_SCHEMA.empty_table()

    year_dirs = sorted(p for p in ds_dir.iterdir() if p.is_dir() and p.name.startswith("year="))
    tables: list[pa.Table] = []
    for year_dir in year_dirs:
        year = int(year_dir.name.split("=", 1)[1])
        if start is not None and year < start.year:
            continue
        if end is not None and year > end.year:
            continue
        for part_file in sorted(year_dir.glob("part-*.parquet")):
            tables.append(pq.read_table(part_file, schema=KLINE_SCHEMA))

    if not tables:
        return KLINE_SCHEMA.empty_table()

    table = pa.concat_tables(tables)
    table = table.sort_by("ts")

    ts_type = pa.timestamp("us", tz="UTC")
    if start is not None:
        table = table.filter(pc.greater_equal(table["ts"], pa.scalar(start, type=ts_type)))
    if end is not None:
        table = table.filter(pc.less(table["ts"], pa.scalar(end, type=ts_type)))
    return table


def table_to_records(table: pa.Table) -> list[KlineRecord]:
    """Convert a kline ``pa.Table`` back into ``KlineRecord`` objects (exact ``Decimal``s)."""
    rows = table.to_pylist()
    return [
        KlineRecord(
            ts=row["ts"].replace(tzinfo=UTC) if row["ts"].tzinfo is None else row["ts"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            quote_volume=row["quote_volume"],
            trades=row["trades"],
        )
        for row in rows
    ]
