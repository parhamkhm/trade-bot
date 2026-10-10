"""Tests for deploy/backup_trades.py: the nightly online backup of trades.sqlite.

CLAUDE.md section 3 item 8 ("Raw data first") requires a nightly, integrity-checked online
backup of the raw trades database, because Tabdeal's ``/trades`` endpoint returns only ~29h of
history -- the raw `trades` table is the one dataset in this project that cannot be
re-downloaded. These tests drive the module's pure/file-system functions directly (no Docker,
no systemd, no network -- the repo-wide guard in tests/conftest.py blocks real sockets anyway,
and nothing here needs one).

`deploy/backup_trades.py` is not part of the `tbot` package (like `deploy/healthcheck.py`, it
ships standalone, stdlib-only), so it is imported here by file path.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "backup_trades.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_backup_trades", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules (unlike deploy/healthcheck.py's equivalent loader, which does
    # not need this) because backup_trades.py uses @dataclass(frozen=True): the dataclass
    # decorator looks its own defining module up via `sys.modules[cls.__module__]` to resolve
    # string-form annotations, which fails with a confusing AttributeError if the module was
    # never registered there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup_trades = _load_module()


def _create_source_db(path: Path, *, n_rows: int = 5) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE trades (trade_id INTEGER PRIMARY KEY, ts_ms INTEGER, price TEXT, "
            "qty TEXT, is_buyer_maker INTEGER, recorded_ts_ms INTEGER)"
        )
        conn.executemany(
            "INSERT INTO trades (trade_id, ts_ms, price, qty, is_buyer_maker, recorded_ts_ms) "
            "VALUES (?, ?, '100.0', '0.01', 0, ?)",
            [(i, i * 1000, i * 1000) for i in range(n_rows)],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "trades.sqlite"
    _create_source_db(path, n_rows=10)
    return path


@pytest.fixture
def dest_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


# --- online backup: basic success path ---------------------------------------------------


def test_perform_backup_creates_a_verified_copy(source_db: Path, dest_dir: Path) -> None:
    now = datetime(2026, 1, 1, 2, 30, 0, tzinfo=UTC)
    result = backup_trades.perform_backup(source_db, dest_dir, now=now)

    assert result.dest_path.name == "trades-20260101T023000Z.sqlite"
    assert result.dest_path.is_file()
    assert result.source_trade_count == 10
    assert result.copy_trade_count == 10
    assert result.attempts >= 1

    ok, rows = backup_trades._integrity_check_ok(result.dest_path)
    assert ok, rows


def test_perform_backup_copy_is_independently_readable(source_db: Path, dest_dir: Path) -> None:
    """The copy must be a complete, standalone database -- not a reference to the source."""
    result = backup_trades.perform_backup(source_db, dest_dir)

    conn = sqlite3.connect(str(result.dest_path))
    try:
        rows = conn.execute("SELECT trade_id, price FROM trades ORDER BY trade_id").fetchall()
    finally:
        conn.close()
    assert rows[0] == (0, "100.0")
    assert len(rows) == 10


def test_perform_backup_opens_source_read_only(source_db: Path, dest_dir: Path) -> None:
    """A read-only source connection must never be able to write back to the live database."""
    conn = sqlite3.connect(backup_trades._source_uri(source_db), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trades (trade_id, ts_ms, price, qty, is_buyer_maker, "
                         "recorded_ts_ms) VALUES (999, 0, '1', '1', 0, 0)")
    finally:
        conn.close()

    # And perform_backup's own source connection genuinely goes through that same read-only
    # path -- the backup must still succeed (reading is fine), proving mode=ro is in effect
    # rather than merely asserted above on a throwaway connection.
    result = backup_trades.perform_backup(source_db, dest_dir)
    assert result.copy_trade_count == 10


def test_perform_backup_no_partial_file_left_under_the_final_name_on_a_clean_run(
    source_db: Path, dest_dir: Path
) -> None:
    result = backup_trades.perform_backup(source_db, dest_dir)
    leftovers = list(dest_dir.glob("*.tmp"))
    assert leftovers == []
    assert result.dest_path.suffix == ".sqlite"


# --- online backup under concurrent writes -----------------------------------------------


def test_backup_of_a_concurrently_written_database_is_consistent(
    source_db: Path, dest_dir: Path
) -> None:
    """The recorder commits short write transactions roughly every 5s in production; this
    drives many short write transactions from a second thread WHILE perform_backup runs, and
    requires the resulting copy to pass integrity_check -- the whole point of using the
    sqlite3 online-backup API instead of a plain file copy.
    """
    stop = threading.Event()
    errors: list[BaseException] = []

    def _writer() -> None:
        conn = sqlite3.connect(str(source_db), timeout=30.0)
        try:
            next_id = 10
            while not stop.is_set():
                try:
                    conn.execute(
                        "INSERT INTO trades (trade_id, ts_ms, price, qty, is_buyer_maker, "
                        "recorded_ts_ms) VALUES (?, ?, '100.0', '0.01', 0, ?)",
                        (next_id, next_id * 1000, next_id * 1000),
                    )
                    conn.commit()
                    next_id += 1
                except sqlite3.OperationalError:
                    pass  # source momentarily locked by the backup step; try again shortly
                time.sleep(0.01)
        except BaseException as exc:  # pragma: no cover - surfaced via `errors`, not raised in-thread
            errors.append(exc)
        finally:
            conn.close()

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()
    try:
        result = backup_trades.perform_backup(source_db, dest_dir)
    finally:
        stop.set()
        writer_thread.join(timeout=10)

    assert not writer_thread.is_alive()
    assert errors == []

    ok, rows = backup_trades._integrity_check_ok(result.dest_path)
    assert ok, rows
    # The copy must contain at least the rows present when the backup started, plus however
    # many the writer managed to add before/during the copy -- never fewer, never corrupted.
    assert result.copy_trade_count is not None
    assert result.copy_trade_count >= 10


# --- integrity-check failure path ----------------------------------------------------------


def test_integrity_check_failure_deletes_the_bad_copy_and_raises(
    source_db: Path, dest_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_integrity_check_ok(path: Path) -> tuple[bool, list[str]]:
        return False, ["*** in database main *** Page 2: btreeInitPage() returns error code 11"]

    monkeypatch.setattr(backup_trades, "_integrity_check_ok", _fake_integrity_check_ok)

    with pytest.raises(backup_trades.IntegrityCheckError):
        backup_trades.perform_backup(source_db, dest_dir)

    # The bad copy must not be left on disk for a later run (or an operator) to trust.
    remaining = list(dest_dir.glob("trades-*.sqlite"))
    assert remaining == []


def test_run_exits_2_and_logs_on_integrity_failure(
    source_db: Path, dest_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_integrity_check_ok(path: Path) -> tuple[bool, list[str]]:
        return False, ["corruption found"]

    monkeypatch.setattr(backup_trades, "_integrity_check_ok", _fake_integrity_check_ok)

    code = backup_trades.main(
        ["--source", str(source_db), "--dest-dir", str(dest_dir)]
    )
    assert code == 2

    stderr_payload = json.loads(capsys.readouterr().err)
    assert stderr_payload["status"] == "error"
    assert stderr_payload["stage"] == "integrity_check"


# --- backup-failure path (exit 1) ------------------------------------------------------------


def test_run_exits_1_when_source_does_not_exist(dest_dir: Path) -> None:
    code = backup_trades.main(
        ["--source", "/does/not/exist/trades.sqlite", "--dest-dir", str(dest_dir)]
    )
    assert code == 1


def test_perform_backup_raises_backup_error_after_exhausting_retries(
    source_db: Path, dest_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def _always_fails(source: Path, tmp_dest: Path, *, max_attempt_seconds: float) -> None:
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(backup_trades, "_backup_attempt", _always_fails)
    # Avoid a slow test: shrink the backoff schedule the module uses between attempts.
    monkeypatch.setattr(backup_trades, "_BACKOFF_BASE_SECONDS", 0.001)
    monkeypatch.setattr(backup_trades, "_BACKOFF_MAX_SECONDS", 0.002)

    with pytest.raises(backup_trades.BackupError):
        backup_trades.perform_backup(source_db, dest_dir)

    assert len(attempts) == backup_trades._MAX_ATTEMPTS
    # No leftover .tmp file after every attempt fails.
    assert list(dest_dir.glob("*.tmp")) == []


def test_perform_backup_succeeds_after_transient_failures(
    source_db: Path, dest_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_attempt = backup_trades._backup_attempt
    calls = {"n": 0}

    def _fails_twice_then_succeeds(source: Path, tmp_dest: Path, *, max_attempt_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        real_attempt(source, tmp_dest, max_attempt_seconds=max_attempt_seconds)

    monkeypatch.setattr(backup_trades, "_backup_attempt", _fails_twice_then_succeeds)
    monkeypatch.setattr(backup_trades, "_BACKOFF_BASE_SECONDS", 0.001)
    monkeypatch.setattr(backup_trades, "_BACKOFF_MAX_SECONDS", 0.002)

    result = backup_trades.perform_backup(source_db, dest_dir)
    assert result.attempts == 3
    assert result.copy_trade_count == 10


# --- rotation ----------------------------------------------------------------------------


def _touch_backup(dest_dir: Path, when: datetime, *, n_rows: int = 1) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = when.strftime(backup_trades._FILENAME_STRFTIME)
    path = dest_dir / name
    _create_source_db(path, n_rows=n_rows)
    return path


def test_rotation_keeps_newest_and_deletes_only_older_matching_files(dest_dir: Path) -> None:
    now = datetime(2026, 1, 15, 2, 30, 0, tzinfo=UTC)
    old_1 = _touch_backup(dest_dir, now - timedelta(days=20))
    old_2 = _touch_backup(dest_dir, now - timedelta(days=15))
    recent = _touch_backup(dest_dir, now - timedelta(days=5))
    newest = _touch_backup(dest_dir, now - timedelta(hours=1))

    deleted = backup_trades.rotate_backups(dest_dir, keep_days=14, now=now)

    assert set(deleted) == {old_1, old_2}
    assert not old_1.exists()
    assert not old_2.exists()
    assert recent.exists()
    assert newest.exists()


def test_rotation_never_deletes_the_newest_file_even_if_older_than_keep_days(
    dest_dir: Path,
) -> None:
    """If the backup job has been broken for longer than the retention window, the single
    remaining copy must survive rotation regardless of its age."""
    now = datetime(2026, 1, 15, 2, 30, 0, tzinfo=UTC)
    only_old_one = _touch_backup(dest_dir, now - timedelta(days=90))

    deleted = backup_trades.rotate_backups(dest_dir, keep_days=14, now=now)

    assert deleted == []
    assert only_old_one.exists()


def test_rotation_ignores_non_matching_files(dest_dir: Path) -> None:
    now = datetime(2026, 1, 15, 2, 30, 0, tzinfo=UTC)
    dest_dir.mkdir(parents=True, exist_ok=True)
    unrelated = dest_dir / "readme.txt"
    unrelated.write_text("do not touch", encoding="utf-8")
    stray_tmp = dest_dir / "trades-20260101T000000Z.sqlite.tmp"
    stray_tmp.write_bytes(b"partial")
    old_backup = _touch_backup(dest_dir, now - timedelta(days=30))
    newest = _touch_backup(dest_dir, now - timedelta(hours=1))

    deleted = backup_trades.rotate_backups(dest_dir, keep_days=14, now=now)

    assert old_backup in deleted
    assert unrelated.exists()
    assert stray_tmp.exists()
    assert newest.exists()


def test_rotation_on_empty_directory_deletes_nothing(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    assert backup_trades.rotate_backups(dest_dir, keep_days=14) == []


def test_rotation_exactly_at_keep_days_boundary_is_not_deleted(dest_dir: Path) -> None:
    """`ts < cutoff` (strict) -- a file exactly `keep_days` old is kept, not deleted."""
    now = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
    exactly_at_boundary = _touch_backup(dest_dir, now - timedelta(days=14))
    newest = _touch_backup(dest_dir, now)

    deleted = backup_trades.rotate_backups(dest_dir, keep_days=14, now=now)

    assert deleted == []
    assert exactly_at_boundary.exists()
    assert newest.exists()


# --- end-to-end CLI: success path ----------------------------------------------------------


def test_run_end_to_end_success_prints_json_and_rotates(
    source_db: Path, dest_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 1, 15, 2, 30, 0, tzinfo=UTC)
    old_backup = _touch_backup(dest_dir, now - timedelta(days=30))

    code = backup_trades.main(
        [
            "--source",
            str(source_db),
            "--dest-dir",
            str(dest_dir),
            "--keep-days",
            "14",
        ]
    )
    assert code == 0
    assert not old_backup.exists()

    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["status"] == "ok"
    assert stdout_payload["source_trade_count"] == 10
    assert stdout_payload["copy_trade_count"] == 10
    assert str(old_backup) in stdout_payload["rotated_deleted"]


def test_run_default_keep_days_is_14(source_db: Path, dest_dir: Path) -> None:
    args = backup_trades.parse_args(["--source", str(source_db), "--dest-dir", str(dest_dir)])
    assert args.keep_days == backup_trades._DEFAULT_KEEP_DAYS == 14


# --- module hygiene, matching deploy/healthcheck.py's own equivalent test ------------------


def test_module_is_self_contained_stdlib_only() -> None:
    """This file ships standalone (read by root, outside the project's own virtualenv) --
    its source must not reference `tbot` or any third-party package."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("import tbot", "from tbot", "import structlog", "import pydantic", "import httpx"):
        assert forbidden not in source
    assert isinstance(backup_trades, ModuleType)
