from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "backup_lbank.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_backup_lbank", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup = _load()


def _make_day(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def _count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        conn.close()


def test_backs_up_every_day_file_with_integrity_check(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    _make_day(src / "lbank-2026-10-09.sqlite", 5)
    _make_day(src / "lbank-2026-10-10.sqlite", 3)
    (src / "heartbeat.json").write_text("{}", encoding="utf-8")
    assert backup.run(src, dst) == 0
    assert sorted(p.name for p in dst.iterdir()) == ["lbank-2026-10-09.sqlite", "lbank-2026-10-10.sqlite"]
    assert _count(dst / "lbank-2026-10-09.sqlite") == 5
    conn = sqlite3.connect(dst / "lbank-2026-10-10.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_unchanged_file_is_skipped_and_changed_file_is_recopied(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    day = src / "lbank-2026-10-10.sqlite"
    _make_day(day, 3)
    assert backup.run(src, dst) == 0
    copied_mtime = (dst / day.name).stat().st_mtime
    assert backup.run(src, dst) == 0  # unchanged: skipped
    assert (dst / day.name).stat().st_mtime == copied_mtime
    _make_day(day, 2)
    future = copied_mtime + 10
    os.utime(day, (future, future))
    assert backup.run(src, dst) == 0
    assert _count(dst / day.name) == 5


def test_corrupt_source_is_reported_and_leaves_no_tmp(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "lbank-2026-10-10.sqlite").write_bytes(b"not a database" * 100)
    assert backup.run(src, dst) in (1, 2)
    assert not list(dst.glob("*.tmp"))
    assert not (dst / "lbank-2026-10-10.sqlite").exists()
