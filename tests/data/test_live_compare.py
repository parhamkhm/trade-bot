"""Tests for src/tbot/data/live_compare.py (docs/SPEC.md D-039).

Promises pinned here:

1. Research code cannot read the live-comparison store (every route: relative, absolute, ``..``,
   symlink/junction, symlinked part file, a junction at the year level reached through
   ``candles._read_candles`` directly).
2. The store is append-only/idempotent, exposes no returns/indicator/price-series API, and
   ``compare_to_lbank`` refuses a candidate close that is not within 20% of the stored Binance
   close (so it cannot be used as a blind price lookup -- see the module docstring's honest
   statement of what this does and does not protect against).
3. The writer refuses a root that overlaps the configured research parquet root, not just one
   missing ``live_compare`` in its name.
4. Each month's part file is written under a lock and atomically: concurrent writers lose no
   rows, and a failure between the tmp write and the replace leaves the previous file intact.
"""

from __future__ import annotations

import ast
import concurrent.futures
import dataclasses
import inspect
import os
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from tbot.core.config import DataConfig
from tbot.core.types import Timeframe
from tbot.data import binance_loader as loader
from tbot.data import candles as candles_mod
from tbot.data import live_compare as lc
from tbot.data import store as store_mod

NOW = datetime(2026, 12, 1, tzinfo=UTC)
JAN_END = datetime(2024, 1, 31, 20, 0, tzinfo=UTC)  # 4h grid
FEB_START = datetime(2024, 2, 1, 0, 0, tzinfo=UTC)  # 4h grid, belongs to February (CLOSE time)


def _point(
    ts: datetime, close: str, *, symbol: str = "BTCUSDT", tf: Timeframe = Timeframe.H4
) -> lc.BinanceClose:
    return lc.BinanceClose(ts=ts, symbol=symbol, timeframe=tf, close=Decimal(close))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "live_compare"


# ---------------------------------------------------------------------------------
# writer: idempotency, append-only, validation
# ---------------------------------------------------------------------------------


def test_append_is_idempotent_on_ts_symbol_timeframe(root: Path) -> None:
    points = [_point(JAN_END, "42000.10"), _point(FEB_START, "42100.20")]
    first = lc.append_binance_closes(root, points, now=NOW)
    files_after_first = {p: p.read_bytes() for p in root.rglob("*.parquet")}
    second = lc.append_binance_closes(root, points, now=NOW)

    assert (first.written, first.already_present, first.conflicts) == (2, 0, ())
    assert (second.written, second.already_present, second.conflicts) == (0, 2, ())
    assert {p: p.read_bytes() for p in root.rglob("*.parquet")} == files_after_first  # untouched on re-run
    assert lc.coverage(root, symbol="BTCUSDT", timeframe=Timeframe.H4).rows == 2


def test_duplicate_inside_one_batch_is_stored_once(root: Path) -> None:
    result = lc.append_binance_closes(root, [_point(JAN_END, "1.5"), _point(JAN_END, "1.50")], now=NOW)
    assert (result.written, result.already_present, result.conflicts) == (1, 1, ())  # 1.5 == 1.50
    assert lc.coverage(root, symbol="BTCUSDT", timeframe=Timeframe.H4).rows == 1


def test_append_never_overwrites_a_stored_value(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "100")], now=NOW)
    result = lc.append_binance_closes(root, [_point(JAN_END, "101")], now=NOW)
    assert result.written == 0
    assert result.conflicts == (JAN_END,)
    # the first value still wins: +5 bps against 100 (not 101)
    cmp = lc.compare_to_lbank(
        root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("100.05")}
    )
    assert cmp.differences[0].bps == Decimal("5")


def test_append_is_additive_across_calls(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "100")], now=NOW)
    lc.append_binance_closes(root, [_point(FEB_START, "200")], now=NOW)
    cov = lc.coverage(root, symbol="BTCUSDT", timeframe=Timeframe.H4)
    assert (cov.rows, cov.first_ts, cov.last_ts) == (2, JAN_END, FEB_START)


def test_month_is_the_utc_month_of_the_close_time(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "1"), _point(FEB_START, "2")], now=NOW)
    base = root / "BTCUSDT" / "4h"
    assert sorted(p.name for p in base.glob("*.parquet")) == ["2024-01.parquet", "2024-02.parquet"]
    assert [r["ts"] for r in pq.read_table(base / "2024-02.parquet").to_pylist()] == [FEB_START]


def test_stored_schema_is_exactly_four_columns_with_decimal_string(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "67123.45000001")], now=NOW)
    table = pq.read_table(root / "BTCUSDT" / "4h" / "2024-01.parquet")
    assert table.column_names == ["ts", "symbol", "timeframe", "binance_close"]
    assert table.schema.field("ts").type == pa.timestamp("us", tz="UTC")
    assert table.to_pylist() == [
        {"ts": JAN_END, "symbol": "BTCUSDT", "timeframe": "4h", "binance_close": "67123.45000001"}
    ]
    assert not list(root.rglob("*.tmp"))  # atomic write leaves no temp file behind


def test_symbols_and_timeframes_are_separate_keys(root: Path) -> None:
    ts = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)  # on the 4h and the 1d grid
    lc.append_binance_closes(
        root,
        [
            _point(ts, "1", symbol="BTCUSDT", tf=Timeframe.H4),
            _point(ts, "2", symbol="BTCUSDT", tf=Timeframe.D1),
            _point(ts, "3", symbol="ETHUSDT", tf=Timeframe.H4),
        ],
        now=NOW,
    )
    for symbol, tf, expected in (
        ("BTCUSDT", Timeframe.H4, 1),
        ("BTCUSDT", Timeframe.D1, 1),
        ("ETHUSDT", Timeframe.H4, 1),
    ):
        assert lc.coverage(root, symbol=symbol, timeframe=tf).rows == expected


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _point(datetime(2024, 1, 31, 20, 0), "1")  # noqa: DTZ001 - naive on purpose


def test_non_utc_offset_timestamp_is_rejected() -> None:
    plus_three_thirty = timezone(timedelta(hours=3, minutes=30))
    with pytest.raises(ValueError, match="UTC"):
        _point(datetime(2024, 1, 31, 23, 30, tzinfo=plus_three_thirty), "1")


def test_off_grid_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="grid"):
        _point(datetime(2024, 1, 31, 21, 0, tzinfo=UTC), "1")  # 21:00 is not a 4h close


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity"])
def test_non_positive_or_non_finite_price_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        _point(JAN_END, bad)


@pytest.mark.parametrize("bad", ["btcusdt", "../BTCUSDT", "BTC/USDT", "", "BTC USDT"])
def test_hostile_symbol_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid symbol"):
        _point(JAN_END, "1", symbol=bad)


def test_future_bar_is_refused_so_unclosed_bars_are_never_stored(root: Path) -> None:
    with pytest.raises(ValueError, match="future"):
        lc.append_binance_closes(root, [_point(JAN_END, "1")], now=JAN_END - timedelta(seconds=1))
    assert not root.exists()
    lc.append_binance_closes(root, [_point(JAN_END, "1")], now=JAN_END)  # exactly closed is fine


def test_writer_refuses_a_root_outside_a_live_compare_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live_compare"):
        lc.append_binance_closes(tmp_path / "data" / "parquet", [_point(JAN_END, "1")], now=NOW)
    assert not (tmp_path / "data").exists()


def test_writer_refuses_a_root_nested_under_the_research_parquet_root(tmp_path: Path) -> None:
    parquet_root = tmp_path / "data" / "parquet"
    nested = parquet_root / "live_compare"  # named right, but physically inside the research tree
    with pytest.raises(ValueError, match="overlaps"):
        lc.append_binance_closes(nested, [_point(JAN_END, "1")], now=NOW, parquet_root=parquet_root)
    assert not nested.exists()


def test_writer_refuses_a_root_that_contains_the_research_parquet_root(tmp_path: Path) -> None:
    live_root = tmp_path / "live_compare"
    parquet_root = live_root / "parquet"  # the reverse overlap: research root nested in live_compare
    with pytest.raises(ValueError, match="overlaps"):
        lc.append_binance_closes(live_root, [_point(JAN_END, "1")], now=NOW, parquet_root=parquet_root)
    assert not live_root.exists()


def test_writer_checks_the_default_configured_parquet_root_when_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit ``parquet_root``, the guard must use the real configured default
    (``DataConfig().parquet_root`` == ``data/parquet``, relative to the process), not skip the
    check entirely -- otherwise a caller who simply forgets the argument gets no protection."""
    monkeypatch.chdir(tmp_path)
    nested = tmp_path / "data" / "parquet" / "live_compare"
    with pytest.raises(ValueError, match="overlaps"):
        lc.append_binance_closes(nested, [_point(JAN_END, "1")], now=NOW)


# ---------------------------------------------------------------------------------
# compare_to_lbank
# ---------------------------------------------------------------------------------


def test_compare_returns_bps_only_for_matched_timestamps(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "100"), _point(FEB_START, "200")], now=NOW)
    unmatched = datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
    result = lc.compare_to_lbank(
        root,
        symbol="BTCUSDT",
        timeframe=Timeframe.H4,
        lbank_closes={
            FEB_START: Decimal("199.8"),  # -10 bps
            JAN_END: Decimal("100.05"),  # +5 bps
            unmatched: Decimal("5"),  # no Binance point, and its month file does not exist
        },
    )
    assert [(d.ts, d.bps) for d in result.differences] == [
        (JAN_END, Decimal("5")),
        (FEB_START, Decimal("-10")),
    ]
    assert result.lbank_without_binance == 1
    assert result.binance_months_missing == 1


def test_compare_with_nothing_stored_is_empty_not_an_error(root: Path) -> None:
    result = lc.compare_to_lbank(
        root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("1")}
    )
    assert result.differences == ()
    assert result.lbank_without_binance == 1


def test_compare_validates_inputs(root: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        lc.compare_to_lbank(
            root,
            symbol="BTCUSDT",
            timeframe=Timeframe.H4,
            lbank_closes={datetime(2024, 1, 31, 20): Decimal("1")},  # noqa: DTZ001 - naive on purpose
        )
    with pytest.raises(ValueError, match="finite positive"):
        lc.compare_to_lbank(
            root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("0")}
        )
    with pytest.raises(ValueError, match="invalid symbol"):
        lc.compare_to_lbank(root, symbol="../x", timeframe=Timeframe.H4, lbank_closes={})


def test_compare_refuses_a_blind_sentinel_instead_of_leaking_the_price(root: Path) -> None:
    """Review MAJOR-1: without a sanity bound, ``lbank_closes={ts: Decimal("1")}`` at every
    timestamp would let a caller solve the returned bps for the exact stored Binance close --
    compare_to_lbank is a diagnostic cross-check against a value the caller already has, not a
    price lookup. A universal sentinel must be refused, not answered."""
    lc.append_binance_closes(root, [_point(JAN_END, "67123.45")], now=NOW)
    with pytest.raises(ValueError, match="sanity bound"):
        lc.compare_to_lbank(
            root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("1")}
        )


def test_compare_accepts_exactly_the_20_percent_boundary_and_rejects_just_past_it(root: Path) -> None:
    lc.append_binance_closes(root, [_point(JAN_END, "100")], now=NOW)
    ok = lc.compare_to_lbank(
        root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("120")}
    )
    assert ok.differences[0].bps == Decimal("2000")  # exactly +20%, still accepted

    with pytest.raises(ValueError, match="sanity bound"):
        lc.compare_to_lbank(
            root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("120.01")}
        )


def test_compare_sanity_bound_only_applies_to_matched_timestamps(root: Path) -> None:
    """An LBank close with nothing stored for it is reported as unmatched, never checked against
    a Binance close that does not exist."""
    result = lc.compare_to_lbank(
        root, symbol="BTCUSDT", timeframe=Timeframe.H4, lbank_closes={JAN_END: Decimal("1")}
    )
    assert result.differences == ()
    assert result.lbank_without_binance == 1


# ---------------------------------------------------------------------------------
# no returns / indicator / series API
# ---------------------------------------------------------------------------------

_EXPECTED_PUBLIC_API = {
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
}
_FORBIDDEN_NAME = re.compile(
    r"returns?\b|ret_|pct|roll|ewm|ema\b|sma\b|rsi|atr\b|indicator|mean|std|momentum|volatil|series|load_|read_",
    re.I,
)


def test_public_api_is_pinned_so_any_addition_is_a_conscious_decision() -> None:
    assert set(lc.__all__) == _EXPECTED_PUBLIC_API
    defined_here = {
        name
        for name, obj in vars(lc).items()
        if not name.startswith("_")
        and getattr(obj, "__module__", None) == lc.__name__
        and (inspect.isfunction(obj) or inspect.isclass(obj))
    }
    assert defined_here <= _EXPECTED_PUBLIC_API


def test_no_public_name_suggests_returns_indicators_or_a_price_reader() -> None:
    offenders = [n for n in lc.__all__ if _FORBIDDEN_NAME.search(n)]
    assert offenders == []


def test_result_dataclasses_hold_only_bps_and_counts_never_a_raw_binance_close() -> None:
    """What is actually guaranteed: none of the result types has a field that is itself a stored
    Binance price. This does NOT mean ``compare_to_lbank`` cannot be used to infer one -- see the
    module docstring and ``test_compare_refuses_a_blind_sentinel_instead_of_leaking_the_price``
    for the honest version of that claim and its mitigation."""
    fields = {
        cls.__name__: {f.name for f in dataclasses.fields(cls)}
        for cls in (lc.CompareResult, lc.PriceDifference, lc.Coverage, lc.AppendResult)
    }
    assert fields == {
        "CompareResult": {"differences", "lbank_without_binance", "binance_months_missing"},
        "PriceDifference": {"ts", "bps"},
        "Coverage": {"rows", "first_ts", "last_ts"},
        "AppendResult": {"written", "already_present", "conflicts"},
    }


def test_module_imports_no_dataframe_or_numeric_library() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(lc) or "").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported.isdisjoint({"pandas", "numpy", "scipy", "statsmodels", "vectorbt", "quantstats"})


# ---------------------------------------------------------------------------------
# per-month lock + atomic write (review MAJOR-4)
# ---------------------------------------------------------------------------------


def test_concurrent_appends_to_the_same_month_lose_no_rows(root: Path) -> None:
    """Many threads append distinct timestamps in the same month concurrently. Without the
    per-month lock this is a classic read-modify-write race: two threads can both read the month
    file before either writes, and the second write silently discards the first thread's row."""
    base = datetime(2024, 1, 10, 0, 0, tzinfo=UTC)  # on the 4h grid, far from a month edge
    points = [_point(base + timedelta(hours=4 * i), str(100 + i)) for i in range(24)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda p: lc.append_binance_closes(root, [p], now=NOW), points))

    cov = lc.coverage(root, symbol="BTCUSDT", timeframe=Timeframe.H4)
    assert cov.rows == len(points)  # every row survived; none lost to a race
    cmp = lc.compare_to_lbank(
        root,
        symbol="BTCUSDT",
        timeframe=Timeframe.H4,
        lbank_closes={p.ts: p.close for p in points},  # each point compared against itself: 0 bps
    )
    assert len(cmp.differences) == len(points)
    assert all(d.bps == 0 for d in cmp.differences)


def test_atomic_write_leaves_the_previous_file_intact_if_replace_fails(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a crash between the tmp file being fully written (and fsynced) and the atomic
    swap into place: ``os.replace`` itself fails. If the write were NOT done through a tmp file +
    atomic replace -- e.g. truncating and rewriting ``path`` directly -- this same injected
    failure would leave a corrupt or partial file; because it is, the original file must still be
    exactly as it was, and no stray tmp file should be left behind."""
    lc.append_binance_closes(root, [_point(JAN_END, "100")], now=NOW)
    path = root / "BTCUSDT" / "4h" / "2024-01.parquet"
    original = path.read_bytes()

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("simulated crash between the tmp write and the atomic replace")

    monkeypatch.setattr(os, "replace", _boom)  # same module object lc.os refers to
    jan_mid = datetime(2024, 1, 15, 0, 0, tzinfo=UTC)  # same month file, a new row to add
    with pytest.raises(OSError, match="simulated crash"):
        lc.append_binance_closes(root, [_point(jan_mid, "200")], now=NOW)

    assert path.read_bytes() == original  # the previous, valid file was never touched
    assert not list(path.parent.glob("*.tmp"))  # and the failed attempt's tmp file was cleaned up
    assert not list(path.parent.glob("*.lock"))  # the lock is released even when the write fails


def test_write_fsyncs_the_file_before_replacing_it(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy)  # same module object lc.os refers to
    lc.append_binance_closes(root, [_point(JAN_END, "1")], now=NOW)
    assert len(calls) >= 1  # the part file is fsynced; a directory fsync may also be attempted


def test_write_uses_a_unique_tmp_name_not_a_fixed_one(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two back-to-back writes to the same month must not reuse one fixed tmp filename -- that
    would let a second writer's tmp file collide with (and be clobbered by) a first one still in
    flight. Captures the name ``tempfile.mkstemp`` hands back on each write and checks they
    differ."""
    seen: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _spy(
        suffix: str | None = None, prefix: str | None = None, dir: str | None = None, text: bool = False
    ) -> tuple[int, str]:
        fd, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _spy)  # same module object lc.tempfile refers to
    lc.append_binance_closes(root, [_point(JAN_END, "1")], now=NOW)
    lc.append_binance_closes(root, [_point(datetime(2024, 1, 15, 0, 0, tzinfo=UTC), "2")], now=NOW)
    assert len(seen) == 2
    assert seen[0] != seen[1]


# ---------------------------------------------------------------------------------
# the research read path cannot reach live_compare
# ---------------------------------------------------------------------------------


def _plant_research_format_file(live_root: Path) -> Path:
    """A file that a research reader WOULD happily parse if it reached it: the research layout
    and schema, sitting inside live_compare."""
    part = (
        live_root
        / "klines"
        / "source=binance"
        / "symbol=BTCUSDT"
        / "timeframe=1d"
        / "year=2024"
        / "part-2024-01.parquet"
    )
    part.parent.mkdir(parents=True)
    record = store_mod.KlineRecord(
        ts=datetime(2024, 1, 2, tzinfo=UTC),
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
        quote_volume=Decimal(1),
        trades=1,
    )
    table = pa.table(
        {
            "ts": [record.ts],
            "open": [record.open],
            "high": [record.high],
            "low": [record.low],
            "close": [record.close],
            "volume": [record.volume],
            "quote_volume": [record.quote_volume],
            "trades": [record.trades],
        },
        schema=store_mod.KLINE_SCHEMA,
    )
    pq.write_table(table, part)
    return part


_ON_WINDOWS = sys.platform == "win32"


def _symlink_dir(link: Path, target: Path) -> None:
    """Directory symlink. Must work on the Linux CI runner; Windows without the privilege skips."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if _ON_WINDOWS:
            pytest.skip("Windows without symlink privilege; the junction case covers this platform")
        raise


def _junction_dir(link: Path, target: Path) -> None:
    """NTFS junction (needs no privilege). Windows only; the parametrisation skips it elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        pytest.skip("junctions exist only on Windows")


LinkMaker = Callable[[Path, Path], None]
LINK_KINDS = [
    pytest.param(_symlink_dir, id="symlink"),
    pytest.param(
        _junction_dir,
        id="junction",
        marks=pytest.mark.skipif(not _ON_WINDOWS, reason="junctions exist only on Windows"),
    ),
]


def _load(parquet_root: Path) -> list[object]:
    return list(loader.load_bars("BTCUSDT", Timeframe.D1, config=DataConfig(parquet_root=parquet_root)))


@pytest.fixture
def planted(tmp_path: Path) -> Path:
    live_root = tmp_path / "data" / "live_compare"
    _plant_research_format_file(live_root)
    (tmp_path / "data" / "parquet").mkdir()
    return live_root


def test_control_a_normal_research_root_still_loads(tmp_path: Path) -> None:
    root = tmp_path / "data" / "parquet"
    part = store_mod.month_part_path(
        root, source="binance", symbol="BTCUSDT", timeframe=Timeframe.D1, year=2024, month=1
    )
    record = store_mod.KlineRecord(
        ts=datetime(2024, 1, 2, tzinfo=UTC),
        open=Decimal(1),
        high=Decimal(8),
        low=Decimal(1),
        close=Decimal(7),
        volume=Decimal(1),
        quote_volume=Decimal(1),
        trades=1,
    )
    store_mod.write_month_part(part, [record])
    bars = loader.load_bars("BTCUSDT", Timeframe.D1, config=DataConfig(parquet_root=root))
    assert [b.close for b in bars] == [Decimal(7)]


def test_loader_refuses_absolute_path(planted: Path) -> None:
    with pytest.raises(lc.LiveCompareAccessError):
        _load(planted)


def test_loader_refuses_relative_path(planted: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(planted.parent)
    with pytest.raises(lc.LiveCompareAccessError):
        _load(Path("live_compare"))


def test_loader_refuses_dotdot_path(planted: Path) -> None:
    sneaky = planted.parent / "parquet" / ".." / "live_compare"
    with pytest.raises(lc.LiveCompareAccessError):
        _load(sneaky)
    sneakier = planted.parent / "parquet" / ".." / "parquet" / ".." / "LIVE_COMPARE"  # case-insensitive too
    with pytest.raises(lc.LiveCompareAccessError):
        _load(sneakier)


def test_loader_refuses_subdirectory_of_live_compare(planted: Path) -> None:
    with pytest.raises(lc.LiveCompareAccessError):
        _load(planted / "klines")


@pytest.mark.parametrize("make_link", LINK_KINDS)
def test_loader_refuses_symlink_or_junction_to_live_compare(planted: Path, make_link: LinkMaker) -> None:
    link = planted.parent / "totally_innocent_parquet"
    make_link(link, planted)
    assert (link / "klines").is_dir()  # the link really does reach the data
    with pytest.raises(lc.LiveCompareAccessError):
        _load(link)


@pytest.mark.parametrize("make_link", LINK_KINDS)
def test_loader_refuses_link_nested_below_the_root(planted: Path, make_link: LinkMaker) -> None:
    root = planted.parent / "parquet"
    tf_dir = Path("klines") / "source=binance" / "symbol=BTCUSDT" / "timeframe=1d"
    (root / tf_dir).mkdir(parents=True)
    # Only the year directory is a link: the dataset directory itself looks innocent, so this
    # exercises the per-part-file check rather than the directory check.
    make_link(root / tf_dir / "year=2024", planted / tf_dir / "year=2024")
    with pytest.raises(lc.LiveCompareAccessError):
        _load(root)


def _plant_candle_research_file(live_root: Path) -> Path:
    """A Tabdeal-candle-schema file (the schema ``candles._read_candles`` actually parses, not
    the Binance kline one) sitting inside live_compare, built WITHOUT going through any of
    ``candles``'/``store``'s own guarded path helpers -- exactly as a file planted by an
    unrelated process on disk would be."""
    part = (
        live_root
        / "klines"
        / "source=tabdeal"
        / "symbol=BTCUSDT"
        / "timeframe=1h"
        / "year=2024"
        / "part-2024-01.parquet"
    )
    part.parent.mkdir(parents=True)
    record = candles_mod.TabdealCandleRecord(
        ts=datetime(2024, 1, 2, tzinfo=UTC),
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
        complete=True,
        n_trades=1,
    )
    pq.write_table(candles_mod._table_from_records([record]), part)
    return part


@pytest.mark.parametrize("make_link", LINK_KINDS)
def test_read_candles_refuses_a_link_at_the_year_level(tmp_path: Path, make_link: LinkMaker) -> None:
    """Review MAJOR-5: a dedicated test that reaches ``candles._read_candles`` directly through a
    link at the ``year=`` level (mirroring ``test_loader_refuses_link_nested_below_the_root`` for
    the Binance kline reader), so that removing the per-part-file guard added to
    ``candles.py``'s ``_read_candles`` makes a test in THIS file fail, not just one in the loader
    path."""
    live_root = tmp_path / "data" / "live_compare"
    _plant_candle_research_file(live_root)
    root = tmp_path / "data" / "parquet"
    tf_dir = Path("klines") / "source=tabdeal" / "symbol=BTCUSDT" / "timeframe=1h"
    (root / tf_dir).mkdir(parents=True)
    make_link(root / tf_dir / "year=2024", live_root / tf_dir / "year=2024")
    with pytest.raises(lc.LiveCompareAccessError):
        candles_mod._read_candles(root, "BTCUSDT")


def test_loader_refuses_symlinked_part_file(planted: Path) -> None:
    secret = (
        planted
        / "klines"
        / "source=binance"
        / "symbol=BTCUSDT"
        / "timeframe=1d"
        / "year=2024"
        / "part-2024-01.parquet"
    )
    root = planted.parent / "parquet"
    decoy = (
        root
        / "klines"
        / "source=binance"
        / "symbol=BTCUSDT"
        / "timeframe=1d"
        / "year=2024"
        / "part-2024-01.parquet"
    )
    decoy.parent.mkdir(parents=True)
    try:
        decoy.symlink_to(secret)
    except (OSError, NotImplementedError):
        if not _ON_WINDOWS:
            raise
        pytest.skip("Windows without symlink privilege cannot create file symlinks")
    with pytest.raises(lc.LiveCompareAccessError):
        _load(root)


def test_load_frame_refuses_too(planted: Path) -> None:
    with pytest.raises(lc.LiveCompareAccessError):
        loader.load_frame("BTCUSDT", Timeframe.D1, config=DataConfig(parquet_root=planted))


def test_hostile_symbol_string_cannot_walk_into_live_compare(planted: Path) -> None:
    with pytest.raises(lc.LiveCompareAccessError):
        store_mod.dataset_dir(
            planted.parent / "parquet", source="../../../live_compare", symbol="x", timeframe=Timeframe.D1
        )


def test_store_level_helpers_refuse(planted: Path) -> None:
    for call in (
        lambda: store_mod._read_symbol_timeframe(
            planted, source="binance", symbol="BTCUSDT", timeframe=Timeframe.D1
        ),
        lambda: store_mod.month_part_path(
            planted, source="binance", symbol="BTCUSDT", timeframe=Timeframe.D1, year=2024, month=1
        ),
        lambda: store_mod.sidecar_path(planted, source="binance", symbol="BTCUSDT", timeframe=Timeframe.D1),
        lambda: store_mod.load_sidecar(
            planted / "_dataset.json", source="binance", symbol="BTCUSDT", timeframe=Timeframe.D1
        ),
        lambda: store_mod.write_month_part(planted / "x" / "part.parquet", []),
        lambda: candles_mod._read_candles(planted, "BTCUSDT"),
    ):
        with pytest.raises(lc.LiveCompareAccessError):
            call()


def test_refusal_message_names_the_decision() -> None:
    with pytest.raises(lc.LiveCompareAccessError, match=r"D-039"):
        lc.refuse_live_compare_path(Path("data") / "live_compare" / "BTCUSDT")


def test_refuse_allows_ordinary_paths(tmp_path: Path) -> None:
    lc.refuse_live_compare_path(tmp_path / "data" / "parquet")
    lc.refuse_live_compare_path(tmp_path / "live_compare_notes.txt")  # a component must match exactly
