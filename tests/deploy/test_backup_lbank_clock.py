"""deploy/backup_lbank.py must not depend on the real wall clock.

The backup decides "closed day" and "source changed" from file mtimes. Opening a clean WAL-mode day
file read-only -- which the backup itself does to snapshot and count it -- makes SQLite create an empty
``-wal`` / ``-shm`` stamped with the *real* current time. Tests that inject ``now`` but let those
sidecars take the real clock were a time bomb: they passed until the real clock crossed the injected
``NOW - GRACE`` / the file mtimes, then failed on every machine at once (2026-10-12 ~00:00 UTC).

These tests pin the real clock to far-apart instants and require identical outcomes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.deploy.test_backup_lbank import (
    LATE,
    NOW,
    OLD,
    TODAY,
    GzipCodec,
    _count,
    _dirs,
    _make_day,
    _stamp,
    _unzip_count,
    backup,
)

DAY = "lbank-2026-10-11.sqlite"
OPEN = "lbank-2026-10-12.sqlite"

# The "real" instant the operating system stamps onto files SQLite creates while the test runs.
INSTANTS = {
    "long-before-everything": datetime(2020, 1, 1, tzinfo=UTC),
    "before-the-files": datetime(2026, 10, 11, 21, 0, tzinfo=UTC),  # when the old tests last passed
    "inside-the-grace-window": NOW - timedelta(hours=1),
    "just-after-NOW": NOW + timedelta(seconds=5),
    "a-year-later": NOW + timedelta(days=365),
}

OsClock = Callable[[datetime], None]


@pytest.fixture
def os_clock(monkeypatch: pytest.MonkeyPatch) -> OsClock:
    """Pin the mtime of every sidecar a read-only open creates (the part of the real clock SQLite owns).

    ``now=NOW`` is injected into ``run``; the backup itself never reads ``time.time`` (the lock, the
    status stamps and the closed-day cut-off all come from the injected clock), so the only thing
    left that the operating system stamps with the real instant is the mtime of the files SQLite
    creates. A sidecar that existed before the open is left alone (SQLite does not touch an existing
    ``-wal`` on a read-only open)."""

    def install(instant: datetime) -> None:
        stamp = instant.timestamp()

        def ro(db: Path) -> sqlite3.Connection:
            before = {p for p in backup._sidecars(db) if p.exists()}

            class Stamped(sqlite3.Connection):
                def close(self) -> None:
                    super().close()
                    for p in backup._sidecars(db):
                        if p.exists() and p not in before:
                            os.utime(p, (stamp, stamp))

            uri = f"file:{db.resolve().as_posix()}?mode=ro"
            return sqlite3.connect(uri, uri=True, timeout=30.0, factory=Stamped)

        monkeypatch.setattr(backup, "_ro", ro)

    return install


def _status_core(dst: Path) -> dict[str, Any]:
    data = json.loads((dst / "status.json").read_text(encoding="utf-8"))
    return {k: data[k] for k in ("ok", "exit_code", "days_archived", "days_pending", "message")}


def _files(folder: Path) -> list[str]:
    """Visible file names, without SQLite's sidecars (their presence differs by platform and version)."""
    return sorted(
        p.name
        for p in folder.iterdir()
        if not p.name.startswith(".") and not p.name.endswith(("-wal", "-shm"))
    )


def _nightly_run(tmp_path: Path) -> dict[str, Any]:
    """The key scenario: a closed day to archive, the open day to copy, an old archive to copy."""
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    _make_day(src / OPEN, 3, mtime=TODAY)
    (src / "lbank-2026-10-10.sqlite.zst").write_bytes(b"archived earlier")
    code = backup.run(src, dst, codec=GzipCodec(), now=NOW)
    return {
        "code": code,
        "src": _files(src),
        "dst": _files(dst),
        "status": _status_core(dst),
        "archive_rows": _unzip_count(dst / f"{DAY}.zst", tmp_path),
        "open_copy_rows": _count(dst / OPEN),
    }


def test_nightly_run_gives_identical_results_at_any_real_clock(tmp_path: Path, os_clock: OsClock) -> None:
    outcomes: dict[str, str] = {}
    for name, instant in INSTANTS.items():
        run_dir = tmp_path / name
        run_dir.mkdir()
        os_clock(instant)
        outcomes[name] = json.dumps(_nightly_run(run_dir), sort_keys=True)
    assert len(set(outcomes.values())) == 1, outcomes
    result = json.loads(next(iter(outcomes.values())))
    assert result["code"] == 0
    assert result["src"] == ["lbank-2026-10-10.sqlite.zst", f"{DAY}.zst", OPEN]
    assert result["dst"] == ["lbank-2026-10-10.sqlite.zst", f"{DAY}.zst", OPEN, "status.json"]
    assert result["status"]["days_archived"] == ["2026-10-11"] and result["status"]["days_pending"] == []
    assert result["archive_rows"] == 5 and result["open_copy_rows"] == 3


@pytest.mark.parametrize("instant", list(INSTANTS.values()), ids=list(INSTANTS))
def test_a_real_write_after_the_mark_is_detected_at_any_real_clock(
    tmp_path: Path, os_clock: OsClock, monkeypatch: pytest.MonkeyPatch, instant: datetime
) -> None:
    """A write that changes no row count (so only the mtime can reveal it) keeps the source."""
    os_clock(instant)
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    real_sync = backup._sync_all

    def recorder_writes() -> None:
        conn = sqlite3.connect(src / DAY)
        conn.execute("UPDATE t SET x = x + 100")  # same rows, different content
        conn.commit()
        conn.close()
        _stamp(src / DAY, LATE)
        real_sync()

    monkeypatch.setattr(backup, "_sync_all", recorder_writes)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert (src / DAY).exists() and _count(src / DAY) == 5
    assert "source changed after it was archived" in _status_core(dst)["message"]


# --- the backup's own read-only open is not a recorder write ----------------------------------------


def _wal(db: Path) -> Path:
    return db.with_name(db.name + "-wal")


def test_own_readonly_open_creates_an_empty_wal_that_is_not_a_write(tmp_path: Path) -> None:
    db = tmp_path / DAY
    _make_day(db, 4)
    assert not _wal(db).exists()  # a cleanly closed day has no -wal
    mark = backup._last_write(db)
    assert mark == OLD
    backup._source_counts(db)  # the backup's own read-only open
    assert _wal(db).exists() and _wal(db).stat().st_size == 0  # SQLite created an empty -wal for it
    later = (NOW + timedelta(days=30)).timestamp()  # whatever the real clock says
    for stray in (_wal(db), db.with_name(db.name + "-shm")):
        os.utime(stray, (later, later))
    assert backup._last_write(db) == mark
    assert backup.is_closed(db, NOW)


def test_a_wal_with_data_is_a_write_and_keeps_the_day_open(tmp_path: Path) -> None:
    db = tmp_path / DAY
    _make_day(db, 4)
    recorder = sqlite3.connect(db)  # stays open, like the recorder's connection
    try:
        recorder.execute("PRAGMA wal_autocheckpoint=0")
        recorder.execute("INSERT INTO t VALUES (99)")
        recorder.commit()
        assert _wal(db).stat().st_size > 0
        _stamp(db, OLD)
        stamp = (NOW - timedelta(hours=1)).timestamp()
        os.utime(_wal(db), (stamp, stamp))
        assert backup._last_write(db) == stamp  # the -wal that holds data counts
        assert not backup.is_closed(db, NOW)  # written 1 h ago: still inside the grace period
    finally:
        recorder.close()


def test_a_checkpoint_into_the_db_file_is_a_write_even_when_the_wal_is_empty(tmp_path: Path) -> None:
    db = tmp_path / DAY
    _make_day(db, 4)
    _make_day(db, 1, mtime=LATE)  # last connection closed: rows checkpointed into the db, -wal gone
    assert not _wal(db).exists() or _wal(db).stat().st_size == 0
    assert backup._last_write(db) == LATE != OLD


def test_clean_closed_day_is_archived_although_the_backup_read_it_before(tmp_path: Path) -> None:
    """The failure of 2026-10-12 without any clock fixture: a stray empty -wal from an earlier read."""
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 6)
    backup._source_counts(src / DAY)  # e.g. a failed earlier run or the safeguard read it
    future = (NOW + timedelta(days=1)).timestamp()
    os.utime(_wal(src / DAY), (future, future))
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert not (src / DAY).exists()
    assert _unzip_count(dst / f"{DAY}.zst", tmp_path) == 6
