"""Build 1h candles for the Tabdeal recorder from polled public trades (docs/SPEC.md section 5.4).

Resampling uses ``label='right', closed='right'`` semantics: the bar that closes at time ``H``
covers trades with ``H - 1h < ts <= H``. A trade landing exactly on an hour boundary closes that
hour's bar -- it never opens the next one. An hour with zero trades produces **no** bar, only a
gap row (never forward-filled) -- see ``tbot.data.tabdeal_recorder.build_due_candles``, which is
the only caller that may skip emitting a bar.

Output schema matches docs/SPEC.md section 5.1 (``decimal128(38,12)`` OHLCV, ``ts`` = CLOSE time,
``timestamp[us, tz=UTC]``) plus ``complete`` (bool) and ``n_trades`` (int64), per section 5.4.

``tbot.data.store`` (owned by another agent, not modified here) is reused for its schema-agnostic
path helpers (``partition_dir`` / ``month_part_path`` / ``dataset_dir``) so the Tabdeal candle
store sits on disk in exactly the same ``source=.../symbol=.../timeframe=.../year=...`` layout as
the Binance store. Its ``KLINE_SCHEMA`` / ``write_month_part`` / ``_read_symbol_timeframe`` could
not be reused as-is: the Tabdeal schema carries two extra columns (``complete``, ``n_trades``)
that the Binance schema does not have, so this module defines its own schema and read/write
helpers rather than bolting incompatible columns onto a schema another agent owns.

WARNING (decision D-028): ``_read_candles`` below is a raw, unguarded reader -- it returns every
row on disk with no gating of any kind. It is private and unexported so a future phase-2
``BacktestFeed`` author does not reach for it directly as a second read path.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from tbot.core.types import Timeframe
from tbot.data import store as store_mod

__all__ = [
    "HOUR_MS",
    "TABDEAL_KLINE_SCHEMA",
    "TabdealCandleRecord",
    "Trade",
    "build_candle",
    "hour_bounds_ms",
    "is_due",
    "last_written_close_ms",
    "ms_to_utc",
    "pending_hour_bounds",
    "table_to_candle_records",
    "write_candle",
]

HOUR_MS = 3_600_000
_SOURCE = "tabdeal"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TABDEAL_KLINE_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("open", pa.decimal128(38, 12)),
        pa.field("high", pa.decimal128(38, 12)),
        pa.field("low", pa.decimal128(38, 12)),
        pa.field("close", pa.decimal128(38, 12)),
        pa.field("volume", pa.decimal128(38, 12)),
        pa.field("complete", pa.bool_()),
        pa.field("n_trades", pa.int64()),
    ]
)


@dataclass(frozen=True, slots=True)
class Trade:
    """One trade row, as persisted by the recorder. ``ts_ms`` is the exchange trade timestamp
    (epoch milliseconds) -- kept as an integer end-to-end in this module to avoid any rounding
    ambiguity at hour boundaries; it is only converted to a ``datetime`` for the final bar's
    ``ts`` (the bar's close time)."""

    trade_id: int
    ts_ms: int
    price: Decimal
    qty: Decimal
    is_buyer_maker: bool


@dataclass(frozen=True, slots=True)
class TabdealCandleRecord:
    """One closed 1h bar built from Tabdeal trades, plus the two SPEC 5.4 extra columns."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    complete: bool
    n_trades: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() != timedelta(0):
            raise ValueError(f"TabdealCandleRecord.ts must be timezone-aware UTC, got {self.ts!r}")
        if self.n_trades <= 0:
            raise ValueError(f"TabdealCandleRecord.n_trades must be > 0, got {self.n_trades}")


# ---------------------------------------------------------------------------------
# pure hour-bucket math
# ---------------------------------------------------------------------------------


def ms_to_utc(ms: int) -> datetime:
    """Convert an epoch-millisecond integer to a timezone-aware UTC ``datetime``."""
    return _EPOCH + timedelta(milliseconds=ms)


def hour_bounds_ms(ts_ms: int) -> tuple[int, int]:
    """Return ``(open_ms, close_ms)`` for the 1h bucket containing ``ts_ms`` under
    ``closed='right', label='right'``: the bucket is the half-open interval
    ``(open_ms, close_ms]``.

    A timestamp exactly on an hour boundary closes that hour (belongs to the bucket ending at
    itself) -- it never opens the next one.
    """
    close_ms = ts_ms if ts_ms % HOUR_MS == 0 else (ts_ms // HOUR_MS + 1) * HOUR_MS
    return close_ms - HOUR_MS, close_ms


def is_due(hour_close_ms: int, now_ms: int, grace_period_seconds: float) -> bool:
    """True once ``now_ms`` has passed ``hour_close_ms`` by at least the grace period.

    A candle for hour ``H`` is only written once ``H_end + grace`` has passed (docs/SPEC.md
    section 5.4), so a too-fresh hour is deliberately withheld rather than written early and
    possibly missing late-arriving trades.
    """
    return now_ms >= hour_close_ms + int(grace_period_seconds * 1000)


def pending_hour_bounds(
    *,
    last_written_close_ms: int | None,
    earliest_open_ms: int | None,
    now_ms: int,
    grace_period_seconds: float,
) -> Iterator[tuple[int, int]]:
    """Yield ``(open_ms, close_ms)`` for every hour that is due to be written, oldest first.

    Resumes right after ``last_written_close_ms`` when known (the normal case after a restart --
    the cursor lives on disk, in the Parquet store itself, not in any separate state file). If
    nothing has ever been written, starts at the hour bucket containing ``earliest_open_ms`` (the
    earliest trade ever recorded). Yields nothing if neither is known (no trades recorded yet) or
    if the next hour is not yet due.
    """
    if last_written_close_ms is not None:
        next_close_ms = last_written_close_ms + HOUR_MS
    elif earliest_open_ms is not None:
        next_close_ms = hour_bounds_ms(earliest_open_ms)[1]
    else:
        return
    while is_due(next_close_ms, now_ms, grace_period_seconds):
        yield next_close_ms - HOUR_MS, next_close_ms
        next_close_ms += HOUR_MS


def _is_finite_trade(trade: Trade) -> bool:
    """True when both ``price`` and ``qty`` are finite, positive-price, non-negative-qty Decimals.

    Defensive guard (third fix round, the MINOR-4-promoted-to-MAJOR finding): going forward,
    ``tbot.data.tabdeal_recorder.parse_trade_item`` rejects a non-finite/non-positive price or a
    non-finite/negative qty before a trade is ever persisted (the same guard
    ``tbot.data.depth.parse_depth_levels`` already applied to order-book levels). But a row written
    by older, pre-fix code -- or restored from a backup -- can still have ``price=Decimal('NaN')``
    or ``Decimal('Infinity')`` sitting in the database. Without this filter,
    ``max(t.price for t in ordered)``/``min(...)`` below raised ``decimal.InvalidOperation`` on
    such a row every time this hour's bucket was processed: ``run_forever``'s broad
    ``except Exception`` swallowed it, the heartbeat was never rewritten again, and -- because the
    poisoned row is already stored -- every subsequent cycle failed identically (a permanent wedge
    that needed manual database surgery to clear). A poisoned trade is simply excluded from the
    aggregation here, the same way a trade that failed to parse in the first place never reaches
    this function at all.
    """
    return trade.price.is_finite() and trade.price > 0 and trade.qty.is_finite() and trade.qty >= 0


def build_candle(
    trades: Sequence[Trade], hour_open_ms: int, hour_close_ms: int, *, complete: bool
) -> TabdealCandleRecord | None:
    """Aggregate OHLCV for the bucket ``(hour_open_ms, hour_close_ms]`` from ``trades``.

    ``trades`` need not be pre-filtered or pre-sorted -- this function filters to the exact
    half-open window itself (so a boundary trade is attributed correctly regardless of what the
    caller passed), drops any poisoned (non-finite/non-positive) trade that should never have
    reached here (see ``_is_finite_trade``), and sorts by ``(ts_ms, trade_id)`` before reading the
    open/close prices. Returns ``None`` when no trade falls in the window -- callers must never
    forward-fill that into a synthetic bar.
    """
    window = [t for t in trades if hour_open_ms < t.ts_ms <= hour_close_ms and _is_finite_trade(t)]
    if not window:
        return None
    ordered = sorted(window, key=lambda t: (t.ts_ms, t.trade_id))
    volume = Decimal("0")
    for trade in ordered:
        volume += trade.qty
    return TabdealCandleRecord(
        ts=ms_to_utc(hour_close_ms),
        open=ordered[0].price,
        high=max(t.price for t in ordered),
        low=min(t.price for t in ordered),
        close=ordered[-1].price,
        volume=volume,
        complete=complete,
        n_trades=len(ordered),
    )


# ---------------------------------------------------------------------------------
# parquet I/O (own schema; store_mod's path helpers are source-agnostic and reused)
# ---------------------------------------------------------------------------------


def _table_from_records(records: list[TabdealCandleRecord]) -> pa.Table:
    ordered = sorted(records, key=lambda r: r.ts)
    columns: dict[str, list[Any]] = {
        "ts": [r.ts for r in ordered],
        "open": [r.open for r in ordered],
        "high": [r.high for r in ordered],
        "low": [r.low for r in ordered],
        "close": [r.close for r in ordered],
        "volume": [r.volume for r in ordered],
        "complete": [r.complete for r in ordered],
        "n_trades": [r.n_trades for r in ordered],
    }
    return pa.table(columns, schema=TABDEAL_KLINE_SCHEMA)


def table_to_candle_records(table: pa.Table) -> list[TabdealCandleRecord]:
    """Convert a Tabdeal-candle ``pa.Table`` back into ``TabdealCandleRecord`` objects."""
    rows = table.to_pylist()
    return [
        TabdealCandleRecord(
            ts=row["ts"] if row["ts"].tzinfo is not None else row["ts"].replace(tzinfo=UTC),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            complete=row["complete"],
            n_trades=row["n_trades"],
        )
        for row in rows
    ]


def write_candle(parquet_root: Path, symbol: str, record: TabdealCandleRecord) -> None:
    """Idempotently upsert one hourly candle into its month's part file.

    Reads the existing month file (if any), drops any row sharing ``record.ts`` and replaces it
    with ``record`` (so re-processing the same hour -- e.g. a late reclassification from
    ``complete=False`` to ``True`` once a gap is resolved -- corrects the row rather than
    duplicating it), then writes the whole month back atomically (tmp file + ``replace``), the
    same crash-safety pattern ``store.write_month_part`` uses.
    """
    path = store_mod.month_part_path(
        parquet_root,
        source=_SOURCE,
        symbol=symbol,
        timeframe=Timeframe.H1,
        year=record.ts.year,
        month=record.ts.month,
    )
    existing: list[TabdealCandleRecord] = []
    if path.is_file():
        existing = table_to_candle_records(pq.read_table(path, schema=TABDEAL_KLINE_SCHEMA))
    merged = [r for r in existing if r.ts != record.ts]
    merged.append(record)
    table = _table_from_records(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(path)


def _read_candles(parquet_root: Path, symbol: str) -> pa.Table:
    """Read every part file for ``symbol`` (1h, source=tabdeal), concatenated and sorted by ``ts``.

    WARNING (decision D-028): raw, unguarded reader -- private and unexported, see the module
    docstring.
    """
    ds_dir = store_mod.dataset_dir(parquet_root, source=_SOURCE, symbol=symbol, timeframe=Timeframe.H1)
    if not ds_dir.is_dir():
        return TABDEAL_KLINE_SCHEMA.empty_table()
    tables: list[pa.Table] = []
    for year_dir in sorted(p for p in ds_dir.iterdir() if p.is_dir() and p.name.startswith("year=")):
        for part_file in sorted(year_dir.glob("part-*.parquet")):
            tables.append(pq.read_table(part_file, schema=TABDEAL_KLINE_SCHEMA))
    if not tables:
        return TABDEAL_KLINE_SCHEMA.empty_table()
    return pa.concat_tables(tables).sort_by("ts")


def last_written_close_ms(parquet_root: Path, symbol: str) -> int | None:
    """Epoch-ms close time of the most recent candle on disk, or ``None`` if none exists yet.

    This is the recorder's restart-resumption cursor: it lives entirely in the Parquet store, so
    a restarted process picks up exactly where the last one left off with no separate state file.
    """
    table = _read_candles(parquet_root, symbol)
    if table.num_rows == 0:
        return None
    last_ts = table.column("ts")[-1].as_py()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=UTC)
    return int((last_ts - _EPOCH).total_seconds() * 1000)
