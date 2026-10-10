"""Nightly archive + backup of the LBank recorder's per-day SQLite files (stdlib + the ``zstd`` CLI).

Runs on the host as root from ``tbot-lbank-backup.timer`` (~02:40 UTC). Plan §4, approved 2026-10-10.

**Closed days** (the UTC day has ended at least ``GRACE`` ago and neither the database nor its ``-wal``
has been written for ``GRACE``): every recorder stream switches to the new day file on its first
write after midnight, the slowest within an hour, so a closed file has no writer left. Each one is

1. copied with SQLite's online backup API from a read-only connection into a staging file, switched
   to ``journal_mode=DELETE`` and checked with ``PRAGMA integrity_check``;
2. compressed with zstd (content checksum on) into ``lbank-YYYY-MM-DD.sqlite.zst`` next to the source;
3. verified: the archive is decompressed again, integrity-checked, and every table's row count must
   equal the source's;
4. copied to ``--dest-dir`` (sha256 compared after the copy), replacing last night's plain hot copy;
5. **durability barrier, then re-verification**: every file written (archive, backup copy, temp
   files) is ``fsync``-ed and so are the containing directories, then ``sync(2)`` is issued, then the
   page cache of both copies is dropped (``posix_fadvise DONTNEED``) and everything is re-read: the
   archive and the backup copy must have the same sha256, the backup copy must decompress to the
   source's exact row counts, and the source must be unchanged since step 1;
6. only then is the uncompressed source (``.sqlite``, ``-wal``, ``-shm``) deleted. The archive is
   left read-only.

Any failure in 1-5 keeps the source. If archiving fails, the closed file would otherwise have no
complete backup (the only copy in ``--dest-dir`` is the ~02:40 hot copy of the then-open day, about
the first 2.7 hours), so the run also takes a **full, verified plain copy of the closed file** into
``--dest-dir`` and reports the failure (exit 1 or 2) and the day as pending.

**The open day** (today, or a day that still has a recent write) gets a plain online copy in
``--dest-dir`` as before, re-copied whenever it changed, so at most one night of data is unbacked.

An archive whose plain file reappears (a writer after closing: should never happen) is reported as a
conflict and both files are kept. Files named like a day but with an invalid date (for example
``lbank-2026-02-30.sqlite``) are skipped and reported; they never stop the other days.

**One run at a time**: an exclusive non-blocking lock on ``<dest-dir>/.lock`` (``flock``; ``msvcrt``
on Windows). The holder writes ``"<pid> <start epoch>"`` into the file. A second instance does
nothing destructive and exits 3 -- unless the holder is dead (the lock is taken over and the run
proceeds) or has been running longer than ``STUCK_AFTER_S`` = 1800 s = ``TimeoutStartSec`` (then
exit 4, which systemd does NOT treat as success, so the unit fails visibly). All temp files have
unique names (pid + random) and are moved into place with an atomic rename.

Exit codes: 0 ok, 1 an operation failed, 2 an integrity/verification check failed (corrupt database or
archive), 3 another (healthy, recent) run holds the lock, 4 the lock holder is stuck or cannot be
replaced. One JSON line per run on stdout (stderr when not ok).

``<dest-dir>/status.json`` contract (written atomically, mode 0644, read by the health monitor).
It is written **twice per run** (never on exit 3 or 4: the holder owns it): first at the start with
``in_progress=true, ok=false`` so a run that is killed (SIGTERM / timeout / power loss) leaves a
visible non-ok status instead of the previous night's OK, then replaced at the end::

    {
      "ts": "2026-10-12T02:41:07.123456+00:00",   # ISO-8601 UTC, end of this run (start while in progress)
      "ok": true,                                  # exit_code == 0 (always false while in progress)
      "in_progress": false,                        # true only between the start write and the final write
      "started_ts": "2026-10-12T02:40:01.5+00:00", # ISO-8601 UTC: start of this run
      "exit_code": 0,                              # 0 ok | 1 failed | 2 corrupt/integrity | null in progress
      "days_archived": ["2026-10-11"],             # days archived or finished during this run
      "days_pending": [],                          # closed days still not archived+backed up (source kept)
      "message": "ok: archived 1, copied 1, skipped 0",   # or the failures, "; "-joined, <= 2000 chars
      "integrity_ok": true                         # false if any integrity check failed; null if none ran
    }

``in_progress`` and ``started_ts`` are additions; the other keys are unchanged. A monitor should
alert when ``ok`` is false (but tolerate ``in_progress=true`` for up to 1800 s after ``started_ts``;
beyond that the run was killed or is stuck), ``days_pending`` is non-empty, or ``ts`` is older than
~26 h (the run did not happen).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import IO, Protocol

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_NAME_RE = re.compile(r"^lbank-(\d{4}-\d{2}-\d{2})\.sqlite$")
GRACE = timedelta(hours=2)
EXIT_OK, EXIT_FAILED, EXIT_CORRUPT, EXIT_LOCKED, EXIT_STUCK = 0, 1, 2, 3, 4
STUCK_AFTER_S = 1800.0  # = TimeoutStartSec of tbot-lbank-backup.service
_MESSAGE_MAX = 2000


class IntegrityError(Exception):
    """A database, archive or copy is corrupt or differs from what it must equal (exit 2)."""


class BackupError(Exception):
    """An operation could not be completed safely (exit 1); nothing was deleted."""


class Codec(Protocol):
    suffix: str

    def compress(self, src: Path, dst: Path) -> None: ...

    def decompress(self, src: Path, dst: Path) -> None: ...


class ZstdCli:
    """The system ``zstd`` binary (``apt install zstd``; present on Ubuntu 26.04 by default)."""

    suffix = ".zst"

    def compress(self, src: Path, dst: Path) -> None:
        subprocess.run(["zstd", "-q", "-19", "-T0", "--check", "-f", "-o", str(dst), str(src)], check=True)
        try:
            subprocess.run(["zstd", "-q", "-t", str(dst)], check=True)
        except subprocess.CalledProcessError as exc:
            raise IntegrityError(f"{dst.name}: zstd -t failed on the fresh archive") from exc

    def decompress(self, src: Path, dst: Path) -> None:
        subprocess.run(["zstd", "-q", "-d", "-f", "-o", str(dst), str(src)], check=True)


# --- durability helpers ----------------------------------------------------------------------


def _token() -> str:
    return f"{os.getpid()}.{uuid.uuid4().hex[:8]}"


def _tmp_for(path: Path) -> Path:
    """A unique temp name next to ``path`` (never shared with a concurrent or crashed run)."""
    return path.with_name(f"{path.name}.{_token()}.tmp")


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except PermissionError:  # read-only archive: Linux allows fsync on a read-only descriptor
        if os.name == "nt":  # Windows cannot flush a read-only handle (dev/test machines only)
            return
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Make renames/creations/unlinks inside ``path`` durable (no-op on Windows)."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_all() -> None:
    """``sync(2)``: flush every dirty page of every filesystem. The last step before any delete."""
    sync = getattr(os, "sync", None)
    if sync is not None:
        sync()


def _drop_cache(path: Path) -> None:
    """Best effort: evict the (now clean) pages of ``path`` so the next read comes from the disk."""
    advise = getattr(os, "posix_fadvise", None)
    if advise is None:
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            advise(fd, 0, 0, getattr(os, "POSIX_FADV_DONTNEED", 4))
        finally:
            os.close(fd)


def _sha256(path: Path) -> str:
    _drop_cache(path)
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- run lock ----------------------------------------------------------------------------------


_LOCK_OFFSET = 1 << 20  # Windows: lock one byte far beyond the holder text so that text stays readable


def _epoch() -> float:
    return time.time()


@dataclass(frozen=True)
class Holder:
    """Who wrote the lock file last: the process id and its start time (epoch seconds)."""

    pid: int
    started: float


def read_holder(path: Path) -> Holder | None:
    """The holder recorded in the lock file, or ``None`` if it is missing, empty or unreadable."""
    try:
        pid_text, start_text = path.read_text(encoding="ascii").split()[:2]
        holder = Holder(int(pid_text), float(start_text))
    except (OSError, ValueError, UnicodeError):
        return None
    return holder if holder.pid > 0 else None


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill would TERMINATE the process on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Exclusive, non-blocking, process-lifetime lock on a file (released when the process dies).

    The holder writes ``"<pid> <start epoch>"`` into the file so a second run can tell a healthy
    holder from a dead or stuck one (see ``run``)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> bool:
        fh = self._path.open("a+b")
        try:
            if sys.platform == "win32":
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        with contextlib.suppress(OSError):  # best effort: the lock itself is what protects the run
            fh.truncate(0)
            fh.write(f"{os.getpid()} {_epoch():.3f}\n".encode("ascii"))
            fh.flush()
        return True

    def take_over(self) -> bool:
        """The recorded holder is dead but the lock is still held (an orphaned descendant kept the
        descriptor): drop the file so a fresh one (new inode) can be locked."""
        try:
            self._path.unlink()
        except OSError:
            return False
        return self.acquire()

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                self._fh.seek(_LOCK_OFFSET)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


# --- sqlite helpers ----------------------------------------------------------------------------


def _parse_day(name: str) -> str | None:
    """``YYYY-MM-DD`` from a day-file name, or ``None`` if it is not one or the date is invalid."""
    match = _NAME_RE.match(name)
    if match is None:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return match.group(1)


def _sidecars(db: Path) -> list[Path]:
    return [db.with_name(db.name + s) for s in ("-wal", "-shm")]


def _last_write(db: Path) -> float:
    """Newest mtime of the database and its ``-wal``: in WAL mode recent writes live in the ``-wal``."""
    times = [db.stat().st_mtime]
    wal = _sidecars(db)[0]
    if wal.exists():
        times.append(wal.stat().st_mtime)
    return max(times)


def is_closed(db: Path, now: datetime) -> bool:
    day = _parse_day(db.name)
    if day is None:
        return False
    day_end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return now >= day_end + GRACE and _last_write(db) <= (now - GRACE).timestamp()


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)


@contextlib.contextmanager
def _corruption_is_integrity(label: str) -> Iterator[None]:
    """A malformed / not-a-database error is a verification failure (exit 2), not a plain failure."""
    try:
        yield
    except sqlite3.OperationalError:  # locked, I/O error, disk full: an operational failure
        raise
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(f"{label}: {type(exc).__name__}: {exc}") from exc


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}


def _check(conn: sqlite3.Connection, label: str) -> None:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise IntegrityError(f"{label}: integrity_check returned {rows[:3]}")


def _source_counts(source: Path) -> dict[str, int]:
    with _corruption_is_integrity(source.name), contextlib.closing(_ro(source)) as conn:
        return _row_counts(conn)


def snapshot(source: Path, tmp: Path) -> dict[str, int]:
    """Online copy of ``source`` into one self-contained, integrity-checked, fsynced file; returns counts."""
    tmp.unlink(missing_ok=True)
    try:
        with (
            _corruption_is_integrity(source.name),
            contextlib.closing(_ro(source)) as src,
            contextlib.closing(sqlite3.connect(str(tmp))) as dst,
        ):
            src.backup(dst, pages=-1)
            dst.execute("PRAGMA journal_mode=DELETE")
            _check(dst, source.name)
            counts = _row_counts(dst)
        _fsync_file(tmp)
        return counts
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a partial copy behind
        raise


def archive_counts(archive: Path, scratch: Path, codec: Codec) -> dict[str, int]:
    """Decompress ``archive`` to ``scratch``, integrity-check it and return its row counts."""
    try:
        _drop_cache(archive)
        try:
            codec.decompress(archive, scratch)
        except subprocess.CalledProcessError as exc:
            raise IntegrityError(f"{archive.name}: decompression failed ({exc.returncode})") from exc
        with (
            _corruption_is_integrity(archive.name),
            contextlib.closing(sqlite3.connect(str(scratch))) as conn,
        ):
            _check(conn, archive.name)
            return _row_counts(conn)
    finally:
        scratch.unlink(missing_ok=True)


def _copy_verified(src: Path, dest: Path, mode: int) -> None:
    """Copy via a unique temp file: fsync, sha256 compare, atomic rename, fsync of the directory."""
    tmp = _tmp_for(dest)
    try:
        shutil.copyfile(src, tmp)
        _fsync_file(tmp)
        if _sha256(tmp) != _sha256(src):
            raise IntegrityError(f"{dest.name}: copy differs from source")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)
    with contextlib.suppress(OSError):
        dest.chmod(mode)


def _match_owner(path: Path, like: Path) -> None:
    if hasattr(os, "chown"):
        st = like.stat()
        with contextlib.suppress(OSError):
            os.chown(path, st.st_uid, st.st_gid)


# --- the operations ----------------------------------------------------------------------------


def publish_snapshot(source: Path, dest: Path, *, require_complete: bool) -> None:
    """Plain, integrity-checked online copy of ``source`` at ``dest`` (atomic, durable).

    ``require_complete`` (a closed file: no writer left) also demands the copy's row counts equal
    the source's, so the result is a full copy and never a partial one."""
    mtime = _last_write(source)  # before the copy: a write during the copy forces a re-copy next run
    tmp = _tmp_for(dest)
    try:
        counts = snapshot(source, tmp)
        if require_complete and _source_counts(source) != counts:
            raise BackupError(f"{source.name}: source changed while it was being copied")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)
    os.utime(dest, (mtime, mtime))
    with contextlib.suppress(OSError):
        dest.chmod(0o640)


def backup_open_day(source: Path, dest: Path) -> None:
    publish_snapshot(source, dest, require_complete=False)


def _delete_closed_source(source: Path) -> None:
    """Remove the day file, then its sidecars (a stray sidecar is harmless; a lone database is not)."""
    for path in (source, *_sidecars(source)):
        path.unlink(missing_ok=True)
    _fsync_dir(source.parent)


def _finish(
    source: Path,
    archive: Path,
    dest_dir: Path,
    staging: Path,
    codec: Codec,
    counts: dict[str, int],
    write_mark: float,
) -> None:
    """Steps 4-6: back the archive up, make everything durable, re-verify from disk, then delete."""
    backup_copy = dest_dir / archive.name
    _copy_verified(archive, backup_copy, 0o640)
    _fsync_file(archive)
    _fsync_dir(archive.parent)
    _sync_all()  # durability barrier: nothing below may read a page that is not on disk yet
    if _sha256(archive) != _sha256(backup_copy):
        raise IntegrityError(f"{archive.name}: archive and backup copy differ after sync")
    verify = staging / f"{archive.name}.{_token()}.verify"
    if archive_counts(backup_copy, verify, codec) != counts:
        raise IntegrityError(f"{backup_copy.name}: backup copy rows differ from the source after sync")
    if _source_counts(source) != counts or _last_write(source) != write_mark:
        raise BackupError(f"{source.name}: source changed after it was archived; both kept")
    (dest_dir / source.name).unlink(missing_ok=True)  # last night's partial plain copy of the then-open day
    _delete_closed_source(source)


def archive_closed_day(source: Path, dest_dir: Path, staging: Path, codec: Codec) -> Path:
    """Steps 1-6 of the module docstring; returns the archive path. Raises before deleting anything."""
    archive = source.with_name(source.name + codec.suffix)
    token = _token()
    plain, check = staging / f"{source.name}.{token}.plain", staging / f"{source.name}.{token}.verify"
    pending = archive.with_name(f"{archive.name}.{token}.tmp")
    write_mark = _last_write(source)
    try:
        counts = snapshot(source, plain)
        if _source_counts(source) != counts:
            raise IntegrityError(f"{source.name}: snapshot row counts differ from the source")
        codec.compress(plain, pending)
        _fsync_file(pending)
        if archive_counts(pending, check, codec) != counts:
            raise IntegrityError(f"{archive.name}: decompressed row counts differ from the source")
        pending.replace(archive)
        _fsync_dir(archive.parent)
        _match_owner(archive, source)
        with contextlib.suppress(OSError):
            archive.chmod(0o444)
    finally:
        for leftover in (plain, check, pending):
            leftover.unlink(missing_ok=True)
    _finish(source, archive, dest_dir, staging, codec, counts, write_mark)
    return archive


def resume_or_conflict(source: Path, archive: Path, dest_dir: Path, staging: Path, codec: Codec) -> bool:
    """Both the plain file and its archive exist. If the archive holds exactly the source's rows, an
    earlier run was interrupted after the archive was made: finish it (same gates as a fresh run) and
    return True. Otherwise (a writer after closing) keep both and return False -- never overwrite an
    archive."""
    write_mark = _last_write(source)
    source_counts = _source_counts(source)
    if archive_counts(archive, staging / f"{archive.name}.{_token()}.verify", codec) != source_counts:
        return False
    _finish(source, archive, dest_dir, staging, codec, source_counts, write_mark)
    return True


# --- driver ------------------------------------------------------------------------------------


@dataclass
class Summary:
    archived: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    def attempt(self, label: str, action: Callable[[], object]) -> bool:
        try:
            action()
        except IntegrityError as exc:
            self.corrupt.append(str(exc))
        except BackupError as exc:
            self.failed.append(str(exc))
        except (sqlite3.Error, OSError, subprocess.CalledProcessError) as exc:
            self.failed.append(f"{label}: {type(exc).__name__}: {exc}")
        else:
            return True
        return False


def _needs_copy(source: Path, dest: Path) -> bool:
    return not dest.exists() or int(_last_write(source)) > int(dest.stat().st_mtime)


def _copy_archive(archive: Path, dest_dir: Path, out: Summary) -> None:
    _copy_verified(archive, dest_dir / archive.name, 0o640)
    out.copied.append(archive.name)


def _resume(
    source: Path, archive: Path, dest_dir: Path, staging: Path, codec: Codec, now: datetime, out: Summary
) -> None:
    if is_closed(source, now) and resume_or_conflict(source, archive, dest_dir, staging, codec):
        out.archived.append(archive.name)
    else:
        raise BackupError(f"{source.name}: conflict, archive {archive.name} already exists; both kept")


def _dest_is_current_copy(source: Path, dest: Path, source_counts: dict[str, int]) -> bool:
    """True only if ``dest`` is a readable copy with the source's exact row counts. A missing, stale,
    truncated or corrupt ``dest`` (any error reading it) simply means it must be copied again."""
    try:
        return not _needs_copy(source, dest) and _source_counts(dest) == source_counts
    except (sqlite3.Error, OSError, IntegrityError):
        return False


def _safeguard(source: Path, dest_dir: Path, out: Summary) -> None:
    """Archiving (or resuming) a closed day failed: make sure a full, verified plain copy of it is in
    ``dest_dir``."""
    dest = dest_dir / source.name

    def full_copy() -> None:
        if not _dest_is_current_copy(source, dest, _source_counts(source)):
            publish_snapshot(source, dest, require_complete=True)
        out.copied.append(f"{source.name} (full copy, archive failed)")

    if not out.attempt(source.name, full_copy):
        out.failed.append(f"{source.name}: NO complete backup of this closed day exists in {dest_dir}")


def _handle_day(
    source: Path, dest_dir: Path, staging: Path, codec: Codec, now: datetime, out: Summary
) -> None:
    archive = source.with_name(source.name + codec.suffix)

    def resume() -> None:
        _resume(source, archive, dest_dir, staging, codec, now, out)

    def archive_it() -> None:
        out.archived.append(archive_closed_day(source, dest_dir, staging, codec).name)

    def copy_open() -> None:
        backup_open_day(source, dest_dir / source.name)
        out.copied.append(source.name)

    if archive.exists():
        if not out.attempt(source.name, resume):  # failed or conflict: the data must still be backed up
            _safeguard(source, dest_dir, out)
    elif is_closed(source, now):
        if not out.attempt(source.name, archive_it):
            _safeguard(source, dest_dir, out)
    elif _needs_copy(source, dest_dir / source.name):
        out.attempt(source.name, copy_open)
    else:
        out.skipped += 1


def _clean_stale_temp(source_dir: Path, dest_dir: Path, staging: Path) -> None:
    """With the run lock held no other run exists: leftovers of a crashed run are garbage."""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(exist_ok=True)
    for pattern, folder in (("*.tmp", dest_dir), ("lbank-*.tmp", source_dir)):
        for stale in folder.glob(pattern):
            stale.unlink(missing_ok=True)
    for sidecar in source_dir.glob("lbank-*.sqlite-???"):  # orphans of an interrupted delete
        db = sidecar.with_name(sidecar.name.rsplit("-", 1)[0])
        if not db.exists() and sidecar.name.endswith(("-wal", "-shm")):
            sidecar.unlink(missing_ok=True)


def _pending_days(source_dir: Path, dest_dir: Path, codec: Codec, now: datetime) -> list[str]:
    """Closed days whose source is still there, or whose archive has no copy in ``dest_dir``."""
    pending: set[str] = set()
    for path in source_dir.iterdir():
        day = _parse_day(path.name)
        if day is not None:
            with contextlib.suppress(OSError):
                if is_closed(path, now):
                    pending.add(day)
    for archive in source_dir.glob(f"lbank-*.sqlite{codec.suffix}"):
        if not (dest_dir / archive.name).exists():
            pending.add(archive.name.removeprefix("lbank-")[:10])
    return sorted(pending)


def _write_status(dest_dir: Path, payload: dict[str, object]) -> None:
    path = dest_dir / "status.json"
    tmp = _tmp_for(path)
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _fsync_file(tmp)
        tmp.replace(path)
        _fsync_dir(dest_dir)
        with contextlib.suppress(OSError):
            path.chmod(0o644)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"lbank_backup: could not write status.json: {exc}", file=sys.stderr)


def _exit_code(out: Summary) -> int:
    if out.corrupt:
        return EXIT_CORRUPT
    return EXIT_FAILED if (out.failed or out.invalid) else EXIT_OK


def _message(out: Summary, code: int) -> str:
    if code == EXIT_OK:
        return f"ok: archived {len(out.archived)}, copied {len(out.copied)}, skipped {out.skipped}"
    problems = [*out.corrupt, *out.failed, *(f"invalid file name {n}" for n in out.invalid)]
    return "; ".join(problems)[:_MESSAGE_MAX]


def _process(source_dir: Path, dest_dir: Path, codec: Codec, now: datetime, out: Summary) -> None:
    staging = dest_dir / ".staging"
    _clean_stale_temp(source_dir, dest_dir, staging)
    for source in sorted(p for p in source_dir.iterdir() if _NAME_RE.match(p.name)):
        if _parse_day(source.name) is None:
            out.invalid.append(source.name)
            continue
        _handle_day(source, dest_dir, staging, codec, now, out)
    for archive in sorted(source_dir.glob(f"lbank-*.sqlite{codec.suffix}")):
        plain_still_there = archive.with_name(archive.name.removesuffix(codec.suffix)).exists()
        # An earlier night's copy failed: retry it. Not while the plain file still exists: then the
        # archive is unverified (the resume path judges it) and must not be copied as if it were good.
        if not plain_still_there and not (dest_dir / archive.name).exists():
            out.attempt(archive.name, partial(_copy_archive, archive, dest_dir, out))


def _lock_busy(lock: RunLock, lock_path: Path) -> tuple[bool, int, str]:
    """The lock is held. Returns ``(took_over, exit_code, message)``.

    * holder pid dead (an orphan kept the descriptor): take the lock over and run;
    * holder alive and older than ``STUCK_AFTER_S``: it is stuck, exit 4 (not a success code);
    * otherwise a normal run is in progress: exit 3."""
    holder = read_holder(lock_path)
    if holder is not None and not _pid_alive(holder.pid):
        if lock.take_over():
            return True, EXIT_OK, f"took over the lock of dead pid {holder.pid}"
        return (
            False,
            EXIT_STUCK,
            f"lock held, recorded holder pid {holder.pid} is dead and cannot be replaced",
        )
    if holder is not None and _epoch() - holder.started > STUCK_AFTER_S:
        age = int(_epoch() - holder.started)
        return (
            False,
            EXIT_STUCK,
            f"lock holder pid {holder.pid} has been running {age} s (> {int(STUCK_AFTER_S)})",
        )
    return False, EXIT_LOCKED, "another run holds the lock"


def _status_payload(started_ts: str, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "ok": False,
        "in_progress": False,
        "started_ts": started_ts,
        "exit_code": None,
        "days_archived": [],
        "days_pending": [],
        "message": "",
        "integrity_ok": None,
    }
    return {**base, **fields}


def run(source_dir: Path, dest_dir: Path, *, codec: Codec | None = None, now: datetime | None = None) -> int:
    codec = codec or ZstdCli()
    now = now or datetime.now(UTC)
    dest_dir.mkdir(parents=True, exist_ok=True)
    lock_path = dest_dir / ".lock"
    lock = RunLock(lock_path)
    if not lock.acquire():
        took_over, code, message = _lock_busy(lock, lock_path)
        line = {"event": "lbank_backup", "exit_code": code, "message": message}
        print(json.dumps(line), file=sys.stderr)
        if not took_over:
            return code  # status.json is left as the holder wrote it (in_progress, ok=false)
    try:
        started, out = time.monotonic(), Summary()
        started_ts = datetime.now(UTC).isoformat()
        # A run that is killed or times out must not leave the previous night's OK behind.
        _write_status(
            dest_dir,
            _status_payload(started_ts, ts=started_ts, in_progress=True, message="run in progress"),
        )
        pending: list[str] = []
        try:
            _process(source_dir, dest_dir, codec, now, out)
            pending = _pending_days(source_dir, dest_dir, codec, now)
        except Exception as exc:  # last resort: still report through status.json and the exit code
            out.failed.append(f"unexpected {type(exc).__name__}: {exc}")
        code = _exit_code(out)
        integrity_ok = False if out.corrupt else (True if (out.archived or out.copied) else None)
        _write_status(
            dest_dir,
            _status_payload(
                started_ts,
                ok=code == EXIT_OK,
                exit_code=code,
                days_archived=sorted(n.removeprefix("lbank-")[:10] for n in out.archived),
                days_pending=pending,
                message=_message(out, code),
                integrity_ok=integrity_ok,
            ),
        )
        summary = {"event": "lbank_backup", **out.__dict__, "seconds": round(time.monotonic() - started, 2)}
        print(json.dumps(summary), file=sys.stdout if code == EXIT_OK else sys.stderr)
        return code
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--dest-dir", type=Path, default=Path("/var/backups/tbot/lbank"))
    args = parser.parse_args(argv)
    return run(args.source_dir, args.dest_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
