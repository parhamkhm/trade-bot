"""Review-round tests for deploy/backup_lbank.py: durability, run lock, archive failure, status.json."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
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

REPO = Path(__file__).resolve().parents[2]
DAY = "lbank-2026-10-11.sqlite"


def _status(dst: Path) -> dict[str, Any]:
    data = json.loads((dst / "status.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class FailingCompress(GzipCodec):
    def compress(self, src: Path, dst: Path) -> None:  # noqa: ARG002
        raise OSError("No space left on device")


# --- M1: durability barrier and re-verification ----------------------------------------------


def _production_codec() -> Any:
    return pytest.param(
        "zstd",
        id="real-zstd",
        marks=pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not installed"),
    )


def test_production_codec_is_zstd_and_the_stand_in_only_borrows_its_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert backup.ZstdCli.suffix == ".zst"
    assert GzipCodec.suffix == backup.ZstdCli.suffix  # so the names asserted below are the real ones
    used: list[str] = []

    class Spy(GzipCodec):
        def compress(self, src: Path, dst: Path) -> None:
            used.append("ZstdCli")
            super().compress(src, dst)

    monkeypatch.setattr(backup, "ZstdCli", Spy)
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 3)
    assert backup.run(src, dst, now=NOW) == 0  # no codec argument: the production default is used
    assert used == ["ZstdCli"]


@pytest.mark.parametrize("which", ["gzip-stand-in", _production_codec()])
def test_m1_everything_is_synced_then_reread_before_the_source_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    codec: Any = backup.ZstdCli() if which == "zstd" else GzipCodec()
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    events: list[tuple[str, str]] = []
    real_counts, real_file, real_dir = backup.archive_counts, backup._fsync_file, backup._fsync_dir

    def counts(archive: Path, scratch: Path, codec: Any) -> dict[str, int]:
        events.append(("verify", str(archive.parent)))
        result: dict[str, int] = real_counts(archive, scratch, codec)
        return result

    def fsync_file(path: Path) -> None:
        events.append(("fsync_file", path.name))
        real_file(path)

    def fsync_dir(path: Path) -> None:
        events.append(("fsync_dir", str(path)))
        real_dir(path)

    monkeypatch.setattr(backup, "archive_counts", counts)
    monkeypatch.setattr(backup, "_fsync_file", fsync_file)
    monkeypatch.setattr(backup, "_fsync_dir", fsync_dir)
    monkeypatch.setattr(backup, "_sync_all", lambda: events.append(("sync", "")))
    monkeypatch.setattr(
        backup, "_delete_closed_source", lambda source: events.append(("delete", source.name))
    )
    assert backup.run(src, dst, codec=codec, now=NOW) == 0
    kinds = [k for k, _ in events]
    sync_at, delete_at = kinds.index("sync"), kinds.index("delete")
    assert sync_at < max(i for i, k in enumerate(kinds) if k == "verify") < delete_at
    assert ("verify", str(dst)) in events[sync_at:delete_at]  # the BACKUP copy is re-read after the sync
    before_sync = events[:sync_at]
    assert ("fsync_dir", str(src)) in before_sync and ("fsync_dir", str(dst)) in before_sync
    fsynced = [name for kind, name in before_sync if kind == "fsync_file"]
    archive_name = f"{DAY}{codec.suffix}"  # codec.suffix is ".zst" for both codecs (see test above)
    assert sum(archive_name in name for name in fsynced) >= 2  # the archive and the backup copy
    magic = {"zstd": b"\x28\xb5\x2f\xfd", "gzip-stand-in": b"\x1f\x8b"}[which]
    for copy in (src / archive_name, dst / archive_name):
        assert copy.read_bytes().startswith(magic)  # really zstd for the production codec


def test_m1_a_failed_post_sync_check_keeps_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    real = backup.archive_counts

    def lying(archive: Path, scratch: Path, codec: Any) -> dict[str, int]:
        result: dict[str, int] = real(archive, scratch, codec)
        return {k: v - 1 for k, v in result.items()} if archive.parent == dst else result

    monkeypatch.setattr(backup, "archive_counts", lying)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 2
    assert _count(src / DAY) == 5
    assert _status(dst)["integrity_ok"] is False


def test_m1_source_changed_after_the_snapshot_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    real = backup._sync_all

    def late_writer() -> None:
        _make_day(src / DAY, 1, mtime=LATE)  # a row lands between the snapshot and the delete
        real()

    monkeypatch.setattr(backup, "_sync_all", late_writer)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert _count(src / DAY) == 6


_KILL_SCRIPT = """
import os, sys
sys.path.insert(0, {repo!r})
from tests.deploy.test_backup_lbank import backup, GzipCodec, NOW
from pathlib import Path
def die(*a, **k):
    os._exit(137)
backup.{hook} = die
sys.exit(backup.run(Path({src!r}), Path({dst!r}), codec=GzipCodec(), now=NOW))
"""


@pytest.mark.parametrize("hook", ["_sync_all", "_delete_closed_source"])
def test_m1_process_killed_before_the_delete_is_finished_next_run(tmp_path: Path, hook: str) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 7)
    code = _KILL_SCRIPT.format(repo=str(REPO), hook=hook, src=str(src), dst=str(dst))
    killed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert killed.returncode == 137, killed.stderr
    assert _count(src / DAY) == 7  # still there: nothing was lost by the kill
    killed_status = _status(dst)  # the killed run never claimed success and is visibly unfinished
    assert killed_status["ok"] is False and killed_status["in_progress"] is True
    assert killed_status["exit_code"] is None and killed_status["started_ts"]
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert _status(dst)["ok"] is True and _status(dst)["in_progress"] is False
    assert not (src / DAY).exists()
    assert _unzip_count(dst / f"{DAY}.zst", tmp_path) == 7
    assert not list(dst.glob("*.tmp")) and not list(src.glob("*.tmp"))


# --- M2: one run at a time, unique temp names -------------------------------------------------


def test_m2_second_run_exits_3_without_touching_anything(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    dst.mkdir()
    (dst / "status.json").write_text('{"sentinel": 1}', encoding="utf-8")
    holder = backup.RunLock(dst / ".lock")
    assert holder.acquire()
    try:
        assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 3
    finally:
        holder.release()
    assert _count(src / DAY) == 5
    assert (dst / "status.json").read_text(encoding="utf-8") == '{"sentinel": 1}'
    assert not (src / f"{DAY}.zst").exists()
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0  # released: runs again


_SLOW_SCRIPT = """
import sys, time
sys.path.insert(0, {repo!r})
from tests.deploy.test_backup_lbank import backup, GzipCodec, NOW
from pathlib import Path
class Slow(GzipCodec):
    def compress(self, src, dst):
        print("compressing", flush=True)
        time.sleep(4)
        super().compress(src, dst)
sys.exit(backup.run(Path({src!r}), Path({dst!r}), codec=Slow(), now=NOW))
"""


_CLI_SCRIPT = """
import sys
sys.path.insert(0, {repo!r})
sys.path.insert(0, {deploy!r})
from tests.deploy.test_backup_lbank import backup, NOW
sys.exit(backup.main(["--source-dir", {src!r}, "--dest-dir", {dst!r}], clock=lambda: NOW))
"""


def test_m2_two_processes_at_once_second_does_nothing_destructive(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 9)
    script = _SLOW_SCRIPT.format(repo=str(REPO), src=str(src), dst=str(dst))
    first = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "compressing"  # A is inside the critical section
        cli_script = _CLI_SCRIPT.format(
            repo=str(REPO), deploy=str(REPO / "deploy"), src=str(src), dst=str(dst)
        )
        second = subprocess.run(  # the real CLI entry point, on the same fixed clock as process A
            [sys.executable, "-c", cli_script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert second.returncode == 3, second.stderr
        assert _count(src / DAY) == 9  # B did not delete or archive anything while A was working
        assert first.wait(timeout=120) == 0
    finally:
        first.kill()
        first.wait()
        if first.stdout is not None:
            first.stdout.close()
    assert _unzip_count(dst / f"{DAY}.zst", tmp_path) == 9
    assert not (src / DAY).exists()


def test_m2_temp_names_are_unique_per_call() -> None:
    target = Path("x/lbank-2026-10-11.sqlite.zst")
    assert backup._tmp_for(target) != backup._tmp_for(target)
    assert backup._tmp_for(target).name.endswith(".tmp")


def test_m2_stale_temp_files_of_a_crashed_run_are_cleaned(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    dst.mkdir()
    (dst / "lbank-2026-10-10.sqlite.zst.99.deadbeef.tmp").write_bytes(b"partial")
    (src / "lbank-2026-10-10.sqlite.zst.99.deadbeef.tmp").write_bytes(b"partial")
    (src / "lbank-2026-10-09.sqlite-wal").write_bytes(b"orphan")  # database already deleted
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert not list(dst.glob("*.tmp")) and not list(src.glob("*.tmp"))
    assert not (src / "lbank-2026-10-09.sqlite-wal").exists()


# --- M5: archive failure is safe and loud ----------------------------------------------------


def test_m5_archive_failure_keeps_source_and_takes_a_full_verified_copy(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 50)
    dst.mkdir()
    partial = tmp_path / "partial.sqlite"
    _make_day(partial, 5)  # last night's 02:40 copy: only the first rows of the day
    shutil.copyfile(partial, dst / DAY)
    _stamp(dst / DAY, OLD - 3600)  # copyfile stamps the real clock; last night's copy is older
    assert backup.run(src, dst, codec=FailingCompress(), now=NOW) == 1
    assert _count(src / DAY) == 50  # never deleted
    assert not list(src.glob("*.zst*"))
    assert _count(dst / DAY) == 50  # the partial copy was replaced by a complete one
    conn = sqlite3.connect(dst / DAY)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    status = _status(dst)
    assert status["ok"] is False and status["exit_code"] == 1
    assert status["days_pending"] == ["2026-10-11"] and status["days_archived"] == []
    assert "No space left" in status["message"]
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0  # next night it recovers by itself
    assert _status(dst)["days_pending"] == []
    assert not (src / DAY).exists()


def test_m5_if_even_the_full_copy_fails_the_run_says_so(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / DAY).write_bytes(b"garbage" * 200)
    _stamp(src / DAY, OLD)  # a closed day: untouched for longer than the grace period (not "just created")
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 2
    assert (src / DAY).exists() and not (dst / DAY).exists()
    status = _status(dst)
    assert status["days_pending"] == ["2026-10-11"] and status["integrity_ok"] is False
    assert "NO complete backup" in status["message"]


# --- M6: status.json contract ------------------------------------------------------------------


def test_m6_status_json_schema_on_success(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 3)
    _make_day(src / "lbank-2026-10-12.sqlite", 2, mtime=TODAY)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    status = _status(dst)
    assert set(status) == {
        "ts",
        "ok",
        "in_progress",
        "started_ts",
        "exit_code",
        "days_archived",
        "days_pending",
        "message",
        "integrity_ok",
    }
    assert status["in_progress"] is False
    started = datetime.fromisoformat(status["started_ts"])
    assert started.utcoffset() is not None and started <= datetime.fromisoformat(status["ts"])
    ts = datetime.fromisoformat(status["ts"])
    assert ts.utcoffset() is not None and ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert ts == NOW and started == NOW  # every stamp comes from the run's clock, not the real one
    assert status["ok"] is True and status["exit_code"] == 0
    assert status["days_archived"] == ["2026-10-11"] and status["days_pending"] == []
    assert isinstance(status["message"], str) and status["integrity_ok"] is True
    assert not list(dst.glob("status.json.*"))  # atomic: no temp left behind


def test_m6_status_json_integrity_is_null_when_nothing_was_checked(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    assert _status(dst)["integrity_ok"] is None


def test_m6_status_json_reports_corruption(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 20)
    assert backup.run(src, dst, codec=GzipCodec(corrupt=True), now=NOW) == 2
    status = _status(dst)
    assert status["ok"] is False and status["exit_code"] == 2 and status["integrity_ok"] is False
    assert status["days_pending"] == ["2026-10-11"]


def test_unexpected_exception_is_still_reported_through_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _dirs(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("surprise")

    monkeypatch.setattr(backup, "_handle_day", boom)
    _make_day(src / DAY, 1)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert "surprise" in _status(dst)["message"]


# --- MINOR: invalid date in a file name --------------------------------------------------------


def test_minor_invalid_date_name_is_skipped_and_reported_not_fatal(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / "lbank-2026-02-30.sqlite").write_bytes(b"whatever")
    _make_day(src / DAY, 4)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert (src / "lbank-2026-02-30.sqlite").exists()  # untouched
    assert not (src / DAY).exists() and _unzip_count(dst / f"{DAY}.zst", tmp_path) == 4
    status = _status(dst)
    assert status["ok"] is False and "lbank-2026-02-30.sqlite" in status["message"]
    assert status["days_archived"] == ["2026-10-11"]


# --- MAJOR 1: stuck / dead lock holders and the in-progress status -----------------------------


def test_major1_status_is_in_progress_and_not_ok_while_the_run_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 2)
    dst.mkdir()
    (dst / "status.json").write_text(
        json.dumps({"ts": "2026-10-11T02:41:00+00:00", "ok": True, "exit_code": 0}), encoding="utf-8"
    )  # last night's OK
    seen: list[dict[str, Any]] = []
    real = backup._process

    def spy(*args: Any, **kwargs: Any) -> None:
        seen.append(_status(dst))
        real(*args, **kwargs)

    monkeypatch.setattr(backup, "_process", spy)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    mid = seen[0]
    assert mid["ok"] is False and mid["in_progress"] is True and mid["exit_code"] is None
    assert mid["ts"] == mid["started_ts"] and mid["days_pending"] == [] and mid["days_archived"] == []
    final = _status(dst)
    assert final["ok"] is True and final["in_progress"] is False
    assert final["started_ts"] == mid["started_ts"]


def test_major1_lock_file_records_pid_and_start_time(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    lock = backup.RunLock(lock_path, lambda: NOW)
    assert lock.acquire()
    try:
        holder = backup.read_holder(lock_path)
        assert holder is not None and holder.pid == os.getpid()
        assert holder.started == pytest.approx(NOW.timestamp(), abs=0.001)
    finally:
        lock.release()


def test_major1_holder_older_than_the_timeout_exits_4_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 5)
    dst.mkdir()
    (dst / "status.json").write_text('{"sentinel": 1}', encoding="utf-8")
    holder = backup.RunLock(dst / ".lock", lambda: NOW)  # the holder started at NOW ...
    assert holder.acquire()
    later = NOW + timedelta(seconds=backup.STUCK_AFTER_S + 5)  # ... and the second run is past the timeout
    try:
        assert backup.run(src, dst, codec=GzipCodec(), now=NOW, clock=lambda: later) == 4
    finally:
        holder.release()
    assert "running" in capsys.readouterr().err  # said so in the log
    assert _count(src / DAY) == 5 and not (src / f"{DAY}.zst").exists()
    assert (dst / "status.json").read_text(encoding="utf-8") == '{"sentinel": 1}'


def test_major1_holder_younger_than_the_timeout_still_exits_3(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    dst.mkdir()
    holder = backup.RunLock(dst / ".lock", lambda: NOW)
    assert holder.acquire()
    later = NOW + timedelta(seconds=backup.STUCK_AFTER_S - 60)
    try:
        assert backup.run(src, dst, codec=GzipCodec(), now=NOW, clock=lambda: later) == 3
    finally:
        holder.release()


def test_major1_unreadable_holder_info_is_treated_as_a_normal_busy_lock(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    dst.mkdir()
    holder = backup.RunLock(dst / ".lock")
    assert holder.acquire()
    try:
        (dst / ".lock").write_text("not a pid\n", encoding="ascii")
        assert backup.read_holder(dst / ".lock") is None
        assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 3
    finally:
        holder.release()


@pytest.mark.skipif(sys.platform == "win32", reason="replacing a locked file needs POSIX semantics")
def test_major1_lock_of_a_dead_pid_is_taken_over(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 4)
    dst.mkdir()
    dead = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    dead_pid = int(dead.stdout)
    assert not backup._pid_alive(dead_pid)
    orphan = backup.RunLock(dst / ".lock")  # stands for a descriptor inherited by an orphaned child
    assert orphan.acquire()
    (dst / ".lock").write_text(f"{dead_pid} {NOW.timestamp():.3f}\n", encoding="ascii")
    try:
        assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0
    finally:
        orphan.release()
    assert not (src / DAY).exists() and _unzip_count(dst / f"{DAY}.zst", tmp_path) == 4


def test_major1_exit_4_is_not_a_systemd_success_status() -> None:
    unit = (REPO / "deploy" / "systemd" / "tbot-lbank-backup.service").read_text(encoding="utf-8")
    line = next(ln for ln in unit.splitlines() if ln.startswith("SuccessExitStatus="))
    assert line.split("=", 1)[1].split() == ["3"]
    timeout = next(ln for ln in unit.splitlines() if ln.startswith("TimeoutStartSec="))
    assert float(timeout.split("=", 1)[1]) == backup.STUCK_AFTER_S


# --- MINOR 4/5: the safeguard also covers resume failures and corrupt copies -------------------


def test_minor4_failed_resume_still_takes_a_full_copy(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 6)
    (src / f"{DAY}.zst").write_bytes(b"not a valid archive")  # interrupted run left a broken archive
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1  # (a real corrupt zstd gives exit 2)
    assert not (dst / f"{DAY}.zst").exists()  # the unverified archive is not copied as if it were good
    assert _count(src / DAY) == 6  # kept
    assert _count(dst / DAY) == 6  # but a full, verified plain copy is in the backup directory
    assert _status(dst)["days_pending"] == ["2026-10-11"]  # still reported as pending


def test_minor4_conflict_still_takes_a_full_copy(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 3)
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 0  # archived with 3 rows
    _make_day(src / DAY, 4)  # a writer reappears after closing: archive and plain file now differ
    assert backup.run(src, dst, codec=GzipCodec(), now=NOW) == 1
    assert "conflict" in _status(dst)["message"]
    assert _count(src / DAY) == 4 and (src / f"{DAY}.zst").exists()  # both kept
    assert _count(dst / DAY) == 4  # and the new rows are backed up in full


def test_minor5_corrupt_copy_in_dest_is_replaced_not_trusted(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    _make_day(src / DAY, 8)
    dst.mkdir()
    (dst / DAY).write_bytes(b"corrupt copy" * 100)
    future = NOW.timestamp() + 3600  # newer than the source: only the content check can catch it
    os.utime(dst / DAY, (future, future))
    assert backup.run(src, dst, codec=FailingCompress(), now=NOW) == 1  # archive fails, safeguard runs
    assert _count(dst / DAY) == 8
    assert "NO complete backup" not in _status(dst)["message"]


def test_minor5_unreadable_dest_means_needs_copy(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    dst.mkdir()
    _make_day(src / DAY, 2)
    counts = backup._source_counts(src / DAY)
    assert backup._dest_is_current_copy(src / DAY, dst / DAY, counts) is False  # missing
    (dst / DAY).write_bytes(b"x" * 10)
    newer = (src / DAY).stat().st_mtime + 3600  # newer than the source, never the real clock
    os.utime(dst / DAY, (newer, newer))
    assert backup._dest_is_current_copy(src / DAY, dst / DAY, counts) is False  # garbage
