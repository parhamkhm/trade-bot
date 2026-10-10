"""Nightly online, integrity-checked backup of the LBank recorder's per-day SQLite files (stdlib only).

For every ``lbank-YYYY-MM-DD.sqlite`` in ``--source-dir`` whose backup is missing or older than the
source (by mtime), copy it with SQLite's online backup API (read-only source, one step -- the recorder
uses WAL, so a single read snapshot never blocks its writers), switch the copy to ``journal_mode=DELETE``
(one self-contained file), run ``PRAGMA integrity_check`` on the temporary file, then atomically rename
it into ``--dest-dir`` and set its mtime to the source's. Closed days are therefore backed up once
(re-copied only if they change); today's file is re-copied every night. Nothing is ever deleted here.

Exit codes: 0 all ok, 1 a backup failed, 2 an integrity check failed. One JSON line per run on stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

_NAME_RE = re.compile(r"^lbank-\d{4}-\d{2}-\d{2}\.sqlite$")


class IntegrityError(Exception):
    pass


def _source_mtime(source: Path) -> float:
    """Newest mtime of the database and its ``-wal`` file: in WAL mode recent writes live in the
    ``-wal`` file and the main file's mtime can lag until the next checkpoint."""
    times = [source.stat().st_mtime]
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        times.append(wal.stat().st_mtime)
    return max(times)


def _needs_backup(source: Path, dest: Path) -> bool:
    return not dest.exists() or int(_source_mtime(source)) > int(dest.stat().st_mtime)


def _copy_and_check(source: Path, tmp: Path) -> list[tuple[object, ...]]:
    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst, pages=-1)
            dst.execute("PRAGMA journal_mode=DELETE")
            return dst.execute("PRAGMA integrity_check").fetchall()
        finally:
            dst.close()
    finally:
        src.close()


def backup_one(source: Path, dest: Path) -> None:
    mtime = _source_mtime(source)  # before the copy: a write during the copy forces a re-copy next run
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        rows = _copy_and_check(source, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a partial copy behind
        raise
    if rows != [("ok",)]:
        tmp.unlink(missing_ok=True)
        raise IntegrityError(f"{source.name}: integrity_check returned {rows[:3]}")
    tmp.replace(dest)
    os.utime(dest, (mtime, mtime))
    with contextlib.suppress(OSError):
        dest.chmod(0o640)


def run(source_dir: Path, dest_dir: Path) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    copied, skipped, failed, corrupt = [], 0, [], []
    for source in sorted(p for p in source_dir.iterdir() if _NAME_RE.match(p.name)):
        dest = dest_dir / source.name
        if not _needs_backup(source, dest):
            skipped += 1
            continue
        try:
            backup_one(source, dest)
            copied.append(source.name)
        except IntegrityError as exc:
            corrupt.append(str(exc))
        except (sqlite3.Error, OSError) as exc:
            failed.append(f"{source.name}: {type(exc).__name__}: {exc}")
    summary = {
        "event": "lbank_backup",
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "corrupt": corrupt,
        "seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(summary), file=sys.stderr if (failed or corrupt) else sys.stdout)
    return 2 if corrupt else 1 if failed else 0


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
