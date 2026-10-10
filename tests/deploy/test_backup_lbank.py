from __future__ import annotations

import gzip
import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "backup_lbank.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_backup_lbank", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup = _load()

# "now" is 2026-10-12 04:00 UTC: 10-10 and 10-11 are closed (if untouched for 2 h), 10-12 is open
NOW = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
OLD = datetime(2026, 10, 12, 0, 30, tzinfo=UTC).timestamp()


class GzipCodec:
    """Same contract as ZstdCli, without needing the zstd binary on the test machine."""

    suffix = ".zst"

    def __init__(self, corrupt: bool = False) -> None:
        self.corrupt = corrupt

    def compress(self, src: Path, dst: Path) -> None:
        data = src.read_bytes()
        if self.corrupt:
            data = data[: len(data) // 2]
        dst.write_bytes(gzip.compress(data))

    def decompress(self, src: Path, dst: Path) -> None:
        dst.write_bytes(gzip.decompress(src.read_bytes()))


def _make_day(path: Path, rows: int, mtime: float | None = OLD) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()
    if mtime is not None:
        for p in (path, path.with_name(path.name + "-wal")):
            if p.exists():
                os.utime(p, (mtime, mtime))


def _count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        conn.close()


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    return src, dst


def _unzip_count(archive: Path, tmp_path: Path) -> int:
    out = tmp_path / "check.sqlite"
    out.write_bytes(gzip.decompress(archive.read_bytes()))
    return _count(out)


def test_closed_day_is_archived_verified_backed_up_then_removed(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / "lbank-2026-10-11.sqlite", 5)
    (dst).mkdir()
    (dst / "lbank-2026-10-11.sqlite").write_bytes(b"last night's plain copy")
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert sorted(p.name for p in src.iterdir()) == ["lbank-2026-10-11.sqlite.zst"]
    assert sorted(p.name for p in dst.iterdir() if p.is_file()) == ["lbank-2026-10-11.sqlite.zst"]
    assert _unzip_count(dst / "lbank-2026-10-11.sqlite.zst", tmp_path) == 5
    assert not list((dst / ".staging").iterdir())


def test_open_day_gets_a_plain_copy_and_is_never_removed(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / "lbank-2026-10-12.sqlite", 3, mtime=None)  # today, written just now
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert (src / "lbank-2026-10-12.sqlite").exists()
    assert _count(dst / "lbank-2026-10-12.sqlite") == 3
    conn = sqlite3.connect(dst / "lbank-2026-10-12.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_recently_written_past_day_is_not_archived_yet(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    recent = datetime(2026, 10, 12, 3, 0, tzinfo=UTC).timestamp()  # 1 h ago: inside the grace period
    _make_day(src / "lbank-2026-10-11.sqlite", 2, mtime=recent)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert (src / "lbank-2026-10-11.sqlite").exists()
    assert not (src / "lbank-2026-10-11.sqlite.zst").exists()
    assert _count(dst / "lbank-2026-10-11.sqlite") == 2


def test_failed_verification_keeps_the_source_and_leaves_no_archive(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / "lbank-2026-10-11.sqlite", 500)
    assert backup.run(src, dst, codec=GzipCodec(corrupt=True), now=NOW) in (1, 2)
    assert _count(src / "lbank-2026-10-11.sqlite") == 500
    assert not list(src.glob("*.zst*"))
    assert not list(dst.glob("*.zst*"))


def test_interrupted_run_is_finished_next_night(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    day = src / "lbank-2026-10-11.sqlite"
    _make_day(day, 4)
    staging = tmp_path / "staging"
    staging.mkdir()
    plain = staging / "plain.sqlite"
    backup.snapshot(day, plain)
    (src / "lbank-2026-10-11.sqlite.zst").write_bytes(
        gzip.compress(plain.read_bytes())
    )  # crash before step 4
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert not day.exists()
    assert _unzip_count(dst / "lbank-2026-10-11.sqlite.zst", tmp_path) == 4


def test_reappeared_plain_file_with_other_rows_is_a_conflict_and_both_are_kept(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    day = src / "lbank-2026-10-11.sqlite"
    _make_day(day, 4)
    staging = tmp_path / "staging"
    staging.mkdir()
    plain = staging / "plain.sqlite"
    backup.snapshot(day, plain)
    archive = src / "lbank-2026-10-11.sqlite.zst"
    archive.write_bytes(gzip.compress(plain.read_bytes()))
    _make_day(day, 1)  # a late writer added a row after the archive was made
    before = archive.read_bytes()
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert _count(day) == 5
    assert archive.read_bytes() == before


def test_archive_whose_backup_copy_is_missing_is_copied(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / "lbank-2026-10-10.sqlite.zst").write_bytes(b"archived earlier")
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert (dst / "lbank-2026-10-10.sqlite.zst").read_bytes() == b"archived earlier"


def test_corrupt_open_day_is_reported_and_leaves_no_tmp(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / "lbank-2026-10-12.sqlite").write_bytes(b"not a database" * 100)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) in (1, 2)
    assert not list(dst.glob("*.tmp"))
    assert not (dst / "lbank-2026-10-12.sqlite").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not installed")
def test_real_zstd_round_trip(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / "lbank-2026-10-11.sqlite", 50)
    assert backup.run(src, dst, now=NOW) == 0
    out = tmp_path / "rt.sqlite"
    backup.ZstdCli().decompress(dst / "lbank-2026-10-11.sqlite.zst", out)
    assert _count(out) == 50
