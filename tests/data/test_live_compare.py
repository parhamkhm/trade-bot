"""Tests for src/tbot/data/live_compare.py (docs/SPEC.md D-039).

Two promises are pinned here:

1. Research code cannot read the live-comparison store (every route: relative, absolute, ``..``,
   symlink/junction, symlinked part file).
2. The store is append-only/idempotent and exposes no returns/indicator/price-series API.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import re
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


def test_result_types_carry_no_price_series() -> None:
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


def _link_dir(link: Path, target: Path) -> None:
    """Directory symlink; on Windows without symlink privilege fall back to a junction; else skip."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":  # junctions need no privilege; created via the stdlib's own helper (cmd mangles "=")
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, OSError):
            pass
    pytest.skip("platform cannot create directory symlinks or junctions here")


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


def test_loader_refuses_symlink_or_junction_to_live_compare(planted: Path) -> None:
    link = planted.parent / "totally_innocent_parquet"
    _link_dir(link, planted)
    assert (link / "klines").is_dir()  # the link really does reach the data
    with pytest.raises(lc.LiveCompareAccessError):
        _load(link)


def test_loader_refuses_link_nested_below_the_root(planted: Path) -> None:
    root = planted.parent / "parquet"
    tf_dir = Path("klines") / "source=binance" / "symbol=BTCUSDT" / "timeframe=1d"
    (root / tf_dir).mkdir(parents=True)
    # Only the year directory is a link: the dataset directory itself looks innocent, so this
    # exercises the per-part-file check rather than the directory check.
    _link_dir(root / tf_dir / "year=2024", planted / tf_dir / "year=2024")
    with pytest.raises(lc.LiveCompareAccessError):
        _load(root)


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
        pytest.skip("platform cannot create file symlinks here")
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
