"""Nightly archive + backup of the LBank recorder's per-day SQLite files (stdlib + the ``zstd`` CLI).

Runs on the host as root from ``tbot-lbank-backup.timer`` (~02:40 UTC). Plan §4, approved 2026-10-10.

**Closed days** (the UTC day has ended at least ``GRACE`` ago and neither the database nor its ``-wal``
has been written for ``GRACE``): every recorder stream switches to the new day file on its first
write after midnight, the slowest within an hour, so a closed file has no writer left. Each one is

1. copied with SQLite's online backup API from a read-only connection into a staging file, switched
   to ``journal_mode=DELETE`` and checked with ``PRAGMA integrity_check``;
2. compressed with zstd (content checksum on) into ``lbank-YYYY-MM-DD.sqlite.zst`` next to the source;
3. **verified before anything is removed**: the archive is decompressed again, integrity-checked, and
   every table's row count must equal the source's;
4. copied to ``--dest-dir`` (sha256 compared after the copy), replacing last night's plain hot copy;
5. only then is the uncompressed source (``.sqlite``, ``-wal``, ``-shm``) deleted. The archive is
   left read-only.

**The open day** (today, or a day that still has a recent write) gets a plain online copy in
``--dest-dir`` as before, re-copied whenever it changed, so at most one night of data is unbacked.

An archive whose plain file reappears (a writer after closing: should never happen) is reported as a
conflict and both files are kept. Nothing is ever deleted before step 3 has passed.

Exit codes: 0 ok, 1 an operation failed, 2 an integrity/verification check failed. One JSON line per
run on stdout (stderr when not ok).
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Protocol

_NAME_RE = re.compile(r"^lbank-(\d{4}-\d{2}-\d{2})\.sqlite$")
GRACE = timedelta(hours=2)


class IntegrityError(Exception):
    pass


class Codec(Protocol):
    suffix: str

    def compress(self, src: Path, dst: Path) -> None: ...

    def decompress(self, src: Path, dst: Path) -> None: ...


class ZstdCli:
    """The system ``zstd`` binary (``apt install zstd``; present on Ubuntu 26.04 by default)."""

    suffix = ".zst"

    def compress(self, src: Path, dst: Path) -> None:
        subprocess.run(["zstd", "-q", "-19", "-T0", "--check", "-f", "-o", str(dst), str(src)], check=True)
        subprocess.run(["zstd", "-q", "-t", str(dst)], check=True)

    def decompress(self, src: Path, dst: Path) -> None:
        subprocess.run(["zstd", "-q", "-d", "-f", "-o", str(dst), str(src)], check=True)


# --- helpers ---------------------------------------------------------------------------------


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
    match = _NAME_RE.match(db.name)
    if match is None:
        return False
    day_end = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return now >= day_end + GRACE and _last_write(db) <= (now - GRACE).timestamp()


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}


def _check(conn: sqlite3.Connection, label: str) -> None:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise IntegrityError(f"{label}: integrity_check returned {rows[:3]}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(source: Path, tmp: Path) -> dict[str, int]:
    """Online copy of ``source`` into one self-contained, integrity-checked file; returns row counts."""
    tmp.unlink(missing_ok=True)
    try:
        src = _ro(source)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst, pages=-1)
                dst.execute("PRAGMA journal_mode=DELETE")
                _check(dst, source.name)
                return _row_counts(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a partial copy behind
        raise


def _copy_verified(src: Path, dest: Path, mode: int) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(src, tmp)
    if _sha256(tmp) != _sha256(src):
        tmp.unlink(missing_ok=True)
        raise IntegrityError(f"{dest.name}: copy differs from source")
    tmp.replace(dest)
    with contextlib.suppress(OSError):
        dest.chmod(mode)


def _match_owner(path: Path, like: Path) -> None:
    if hasattr(os, "chown"):
        st = like.stat()
        with contextlib.suppress(OSError):
            os.chown(path, st.st_uid, st.st_gid)


# --- the two operations ----------------------------------------------------------------------


def backup_open_day(source: Path, dest: Path) -> None:
    mtime = _last_write(source)  # before the copy: a write during the copy forces a re-copy next run
    tmp = dest.with_name(dest.name + ".tmp")
    snapshot(source, tmp)
    tmp.replace(dest)
    os.utime(dest, (mtime, mtime))
    with contextlib.suppress(OSError):
        dest.chmod(0o640)


def archive_closed_day(source: Path, dest_dir: Path, staging: Path, codec: Codec) -> Path:
    """Steps 1-5 of the module docstring; returns the archive path. Raises before deleting anything."""
    archive = source.with_name(source.name + codec.suffix)
    plain, check = staging / source.name, staging / (source.name + ".verify")
    try:
        counts = snapshot(source, plain)
        with contextlib.closing(_ro(source)) as conn:
            if _row_counts(conn) != counts:
                raise IntegrityError(f"{source.name}: snapshot row counts differ from the source")
        pending = archive.with_name(archive.name + ".tmp")
        codec.compress(plain, pending)
        if archive_counts(pending, check, codec) != counts:
            raise IntegrityError(f"{archive.name}: decompressed row counts differ from the source")
        pending.replace(archive)
        _match_owner(archive, source)
        with contextlib.suppress(OSError):
            archive.chmod(0o444)
    finally:
        for leftover in (plain, check, archive.with_name(archive.name + ".tmp")):
            leftover.unlink(missing_ok=True)
    _finish(source, archive, dest_dir)
    return archive


def archive_counts(archive: Path, scratch: Path, codec: Codec) -> dict[str, int]:
    """Decompress ``archive`` to ``scratch``, integrity-check it and return its row counts."""
    try:
        codec.decompress(archive, scratch)
        conn = sqlite3.connect(str(scratch))
        try:
            _check(conn, archive.name)
            return _row_counts(conn)
        finally:
            conn.close()
    finally:
        scratch.unlink(missing_ok=True)


def _finish(source: Path, archive: Path, dest_dir: Path) -> None:
    """Steps 4-5: back the verified archive up, then remove the uncompressed source."""
    _copy_verified(archive, dest_dir / archive.name, 0o640)
    (dest_dir / source.name).unlink(missing_ok=True)  # last night's plain copy of the then-open day
    for path in (source, *_sidecars(source)):
        path.unlink(missing_ok=True)


def resume_or_conflict(source: Path, archive: Path, dest_dir: Path, staging: Path, codec: Codec) -> bool:
    """Both the plain file and its archive exist. If the archive holds exactly the source's rows, an
    earlier run was interrupted after step 3: finish it and return True. Otherwise (a writer after
    closing) keep both and return False -- never overwrite an archive."""
    with contextlib.closing(_ro(source)) as conn:
        source_counts = _row_counts(conn)
    if archive_counts(archive, staging / (archive.name + ".verify"), codec) != source_counts:
        return False
    _finish(source, archive, dest_dir)
    return True


# --- driver ----------------------------------------------------------------------------------


@dataclass
class Summary:
    archived: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)

    def attempt(self, label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except IntegrityError as exc:
            self.corrupt.append(str(exc))
        except (sqlite3.Error, OSError, subprocess.CalledProcessError) as exc:
            self.failed.append(f"{label}: {type(exc).__name__}: {exc}")


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
        out.failed.append(f"{source.name}: conflict, archive {archive.name} already exists; both kept")


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
        out.attempt(source.name, resume)
    elif is_closed(source, now):
        out.attempt(source.name, archive_it)
    elif _needs_copy(source, dest_dir / source.name):
        out.attempt(source.name, copy_open)
    else:
        out.skipped += 1


def run(source_dir: Path, dest_dir: Path, *, codec: Codec | None = None, now: datetime | None = None) -> int:
    codec = codec or ZstdCli()
    now = now or datetime.now(UTC)
    dest_dir.mkdir(parents=True, exist_ok=True)
    staging = dest_dir / ".staging"
    staging.mkdir(exist_ok=True)
    started, out = time.monotonic(), Summary()
    for source in sorted(p for p in source_dir.iterdir() if _NAME_RE.match(p.name)):
        _handle_day(source, dest_dir, staging, codec, now, out)
    for archive in sorted(source_dir.glob(f"lbank-*.sqlite{codec.suffix}")):
        if not (dest_dir / archive.name).exists():  # an earlier night's copy failed: retry it
            out.attempt(archive.name, partial(_copy_archive, archive, dest_dir, out))
    summary = {"event": "lbank_backup", **out.__dict__, "seconds": round(time.monotonic() - started, 2)}
    print(json.dumps(summary), file=sys.stderr if (out.failed or out.corrupt) else sys.stdout)
    return 2 if out.corrupt else 1 if out.failed else 0


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
