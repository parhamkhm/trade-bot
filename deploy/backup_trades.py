"""Nightly online, integrity-checked backup of the Tabdeal recorder's ``trades.sqlite``.

Run by ``deploy/systemd/tbot-trades-backup.service`` (installed as a daily timer, see
``deploy/systemd/tbot-trades-backup.timer`` and the "Nightly backup of trades.sqlite" section of
``deploy/SERVER_SETUP.md``). Not part of the ``tbot`` package and deliberately stdlib-only
(CLAUDE.md section 7 / the HEALTHCHECK script in this same directory follows the same rule) --
this runs as root, outside the recorder's own virtualenv, reading a Docker volume path resolved
at runtime.

Why this exists at all: CLAUDE.md section 3 item 8 ("Raw data first") -- Tabdeal's ``/trades``
endpoint returns only ~29h of history (SPEC section 5.4), so the raw ``trades`` table in
``trades.sqlite`` is the one dataset in this whole project that cannot be re-downloaded if lost.
Everything derived from it (candles, Parquet, reports) can be rebuilt from the raw trades, but
not the other way around. A nightly, verified, off-volume copy is the mitigation.

Method: SQLite's own *online backup* API (``sqlite3.Connection.backup()``), not a plain file
copy. The recorder (``src/tbot/data/tabdeal_recorder.py``) opens short ``BEGIN IMMEDIATE``
write transactions roughly every 5 seconds; a plain ``cp``/``shutil.copy`` while a transaction is
mid-flight can copy a torn, inconsistent file (worse with the default rollback-journal mode,
where the "real" data briefly lives in the ``-journal`` side file, not the main file). The online
backup API instead copies the database page-by-page through SQLite's own B-tree layer, which
understands transaction boundaries, so an engine-level write never produces a torn page in the
copy. The source connection is opened read-only (``file:...?mode=ro``) so this script itself can
never corrupt or write to the live database.

MINOR-1 (PR2 followups): ``backup()`` is called differently depending on the source's own
``PRAGMA journal_mode`` (``_source_journal_mode``). When the source is WAL -- the recorder's own
mode, see ``RecorderStore.__init__`` -- the copy runs in a SINGLE step (``pages=-1``): one call
takes one consistent read snapshot of the source and does not block the recorder's writers (WAL's
whole point is that readers and writers do not block each other), so there is nothing to sleep
between chunks for. A STEPWISE WAL backup (``pages=N > 0``, sleeping between each chunk) does not
merely risk holding a lock a little longer, as the previous version of this comment claimed --
SQLite's own backup implementation explicitly restarts a stepwise backup from page 1 whenever the
source is modified between two steps. Against a writer that commits roughly as often as the sleep
between steps (the recorder commits every ~5s), a large enough source database can restart on
essentially every single step and never complete at all (reproduced: a 43MB+ WAL source with a
writer committing every ~0.05-1s never finished a stepwise backup within generous retry budgets;
the same source copied in one step in under 2s). For a non-WAL (rollback-journal) source, the
stepwise form is kept, with its bounded retry/backoff below -- a rollback-journal writer's
exclusive lock already serializes with this read, so the restart-on-modification behaviour above
is far less likely to repeat indefinitely, and bounding each attempt's wall time still protects
against a persistently locked source. The whole call is wrapped in an outer attempt loop with
exponential backoff, bounded by both an attempt count (``_MAX_ATTEMPTS``) and a per-attempt
wall-clock budget (``_MAX_ATTEMPT_SECONDS``, enforced via the ``progress`` callback), so a
persistently locked (or, for the one-step WAL path, persistently failing) source fails this run
loudly (exit 1) rather than hanging the timer indefinitely.

Exit codes: 0 ok, 1 backup failed (could not produce a verified copy), 2 integrity check failed
(a copy was produced but ``PRAGMA integrity_check`` did not return a single ``ok`` row -- the bad
copy is deleted before exiting, it is never left on disk for a later run to trip over).

A one-line JSON object is printed to stdout on success (row counts, duration, files rotated away)
and to stderr on failure -- never anything from the environment, even though nothing here is
actually secret (CLAUDE.md section 3.6 / section 6 of this docstring's reasoning: a backup script
that reads credentials by habit is a bad habit to have, even when today's env has none).
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

__all__ = [
    "BackupError",
    "BackupResult",
    "IntegrityCheckError",
    "main",
    "parse_args",
    "perform_backup",
    "rotate_backups",
    "run",
]

# --- tunables ------------------------------------------------------------------------

_DEFAULT_KEEP_DAYS = 14

# "trades-20260101T023000Z.sqlite" -- the exact, grep-able pattern rotation matches on.
_FILENAME_STRFTIME = "trades-%Y%m%dT%H%M%SZ.sqlite"
_FILENAME_RE = re.compile(r"^trades-(\d{8}T\d{6}Z)\.sqlite$")
# NIT (backup sidecars, PR2 followups): matches a leftover -wal/-shm sidecar of a backup copy,
# for `rotate_backups`'s orphan sweep -- see that function.
_SIDECAR_RE = re.compile(r"^trades-\d{8}T\d{6}Z\.sqlite-(?:wal|shm)$")

_PAGES_PER_STEP = 100  # sqlite3.Connection.backup()'s own `pages=` -- see module docstring.
_SLEEP_PER_STEP_SECONDS = 0.05  # ...and its own `sleep=` between chunks.
_MAX_ATTEMPT_SECONDS = 60.0  # abort one attempt (via the progress callback) past this wall time.
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0

# Requirement 3: backups live outside the Docker volume; root-owned 0750 dir, files 0640.
# Applied best-effort (see `_try_chmod`) -- this script may run on a filesystem/OS that does
# not support POSIX permission bits (e.g. during local/CI testing on Windows), in which case
# the backup itself must still succeed.
_DEST_DIR_MODE = 0o750
_DEST_FILE_MODE = 0o640


class BackupError(Exception):
    """The online backup could not be completed after all retries. Caller exits 1."""


class IntegrityCheckError(Exception):
    """A copy was produced but failed ``PRAGMA integrity_check``. Caller exits 2."""


@dataclass(frozen=True)
class BackupResult:
    dest_path: Path
    source_trade_count: int | None
    copy_trade_count: int | None
    duration_seconds: float
    attempts: int


# --- online backup ---------------------------------------------------------------------


def _source_uri(path: Path) -> str:
    """A ``file:...?mode=ro`` URI for `path`, usable on both POSIX and Windows.

    SQLite's own URI parser wants forward slashes even for a Windows drive-letter path
    (e.g. ``file:C:/data/trades.sqlite?mode=ro``); ``Path.as_posix()`` does exactly that
    conversion.
    """
    return f"file:{path.resolve().as_posix()}?mode=ro"


def _open_source_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_source_uri(path), uri=True, timeout=5.0)


def _source_journal_mode(source: Path) -> str:
    """The source database's own ``PRAGMA journal_mode`` (lowercased, e.g. ``"wal"`` or
    ``"delete"``) -- MINOR-1 (PR2 followups): decides whether ``_backup_attempt`` copies it in
    one step or stepwise. Read on a fresh read-only connection, not the attempt's own
    ``source_conn``, so this check never consumes any of `max_attempt_seconds`.
    """
    conn = _open_source_readonly(source)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row is not None else ""
    finally:
        conn.close()


def _backup_attempt(
    source: Path, tmp_dest: Path, *, pages: int, sleep: float, max_attempt_seconds: float
) -> None:
    """One online-backup attempt: copy `source` into a fresh `tmp_dest` file.

    `pages`/`sleep` are ``sqlite3.Connection.backup()``'s own parameters -- the caller
    (`perform_backup`) picks them based on the source's journal mode; see the module docstring's
    MINOR-1 note for why a WAL source must use a single step (``pages=-1``).

    Raises ``sqlite3.DatabaseError`` (e.g. the source stays locked, or is not a valid database
    at all) or ``TimeoutError`` (the attempt made no useful progress within
    `max_attempt_seconds`, enforced by the `progress` callback) on failure -- both are retried
    by the caller.

    MINOR-1 (PR2 followups), NIT (backup sidecars): once the copy itself completes, the
    destination is switched to ``journal_mode=DELETE`` before this connection closes -- a copy
    produced from a WAL source would otherwise persist that WAL flag inside the file, and ANY
    later read of it (including this very script's own `_integrity_check_ok`) can then create
    fresh, orphaned ``-wal``/``-shm`` sidecars next to it that are never rotated away (observed:
    exactly this, from `_integrity_check_ok`/`_count_trades_readonly` opening the finished copy).
    A plain ``DELETE``-mode file is self-contained -- a single file, no sidecars, ever.
    """
    with contextlib.suppress(OSError):
        tmp_dest.unlink()

    started_at = time.monotonic()

    def _progress(_status: int, remaining: int, total: int) -> None:
        if time.monotonic() - started_at > max_attempt_seconds:
            raise TimeoutError(
                f"backup attempt exceeded {max_attempt_seconds:.0f}s "
                f"with {remaining}/{total} pages remaining"
            )

    source_conn = _open_source_readonly(source)
    try:
        dest_conn = sqlite3.connect(str(tmp_dest))
        try:
            source_conn.backup(dest_conn, pages=pages, sleep=sleep, progress=_progress)
            dest_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def _fsync_fd_best_effort(fd: int) -> None:
    # fsync is a durability improvement, not a correctness requirement here -- the atomic
    # rename below is what guarantees readers never see a partial file under the final name.
    # Some filesystems/platforms refuse fsync on certain handles; never let that fail the
    # backup that already succeeded.
    with contextlib.suppress(OSError):
        os.fsync(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        _fsync_fd_best_effort(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_fd_best_effort(fd)
    finally:
        os.close(fd)


def _try_chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):
        path.chmod(mode)


def _integrity_check_ok(path: Path) -> tuple[bool, list[str]]:
    conn = sqlite3.connect(_source_uri(path), uri=True, timeout=5.0)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    results = [str(row[0]) for row in rows]
    return results == ["ok"], results


def _count_trades_readonly(path: Path) -> int | None:
    conn = sqlite3.connect(_source_uri(path), uri=True, timeout=5.0)
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row[0])
    finally:
        conn.close()


def _unlink_with_sidecars(path: Path) -> None:
    """Delete `path` plus any ``-wal``/``-shm`` sidecar next to it, best-effort.

    NIT (backup sidecars, PR2 followups): an attempt that fails partway through
    ``_backup_attempt`` -- before the destination is converted to ``journal_mode=DELETE`` -- can
    leave the destination in WAL mode with its own ``<name>-wal``/``<name>-shm`` sidecars; a
    bare ``path.unlink()`` of the main file alone would orphan them.
    """
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        with contextlib.suppress(OSError):
            candidate.unlink()


def _pick_pages_and_sleep(source: Path) -> tuple[int, float]:
    """MINOR-1 (PR2 followups): see the module docstring and ``_backup_attempt`` -- a WAL source
    (the recorder's own mode) must be copied in one step; a non-WAL source keeps the stepwise,
    retry-friendly form.
    """
    try:
        source_journal_mode = _source_journal_mode(source)
    except sqlite3.DatabaseError:
        # NIT (error handling, PR2 followups): a corrupt/non-database source fails here too,
        # before the retry loop even starts -- default to the stepwise path (not a real choice
        # for a source this broken, but it defers to the retry loop's own, already-correct
        # handling of `sqlite3.DatabaseError`, so this failure is classified in one place only).
        source_journal_mode = ""
    if source_journal_mode == "wal":
        return -1, 0.0
    return _PAGES_PER_STEP, _SLEEP_PER_STEP_SECONDS


def _run_backup_attempts(source: Path, tmp_path: Path, *, pages: int, sleep: float) -> int:
    """Retry ``_backup_attempt`` up to ``_MAX_ATTEMPTS`` times with exponential backoff. Returns
    the number of attempts made; raises `BackupError` if every attempt failed.
    """
    attempt = 0
    last_error: Exception | None = None
    while attempt < _MAX_ATTEMPTS:
        attempt += 1
        try:
            _backup_attempt(
                source, tmp_path, pages=pages, sleep=sleep, max_attempt_seconds=_MAX_ATTEMPT_SECONDS
            )
            return attempt
        # NIT (error handling, PR2 followups): sqlite3.OperationalError is itself a
        # sqlite3.DatabaseError subclass; catching the broader type here also covers a corrupt/
        # non-database source ("file is not a database", raised as a plain DatabaseError, not
        # OperationalError) -- previously uncaught here, it propagated all the way out of `run()`
        # as a raw traceback, with no backoff/cleanup, leaving `tmp_path` on disk forever.
        except (sqlite3.DatabaseError, TimeoutError) as exc:
            last_error = exc
            _unlink_with_sidecars(tmp_path)
            if attempt < _MAX_ATTEMPTS:
                backoff = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_MAX_SECONDS)
                time.sleep(backoff)
    raise BackupError(
        f"online backup of {source} failed after {attempt} attempt(s): {last_error}"
    ) from last_error


def _verify_and_finalize(tmp_path: Path, final_path: Path, dest_dir: Path) -> None:
    """Integrity-check `tmp_path` and, only if it passes, atomically rename it to `final_path`.

    NIT (verify before rename, PR2 followups): the check runs on the ``.tmp`` file BEFORE the
    atomic rename, not after -- a process killed between the old rename and its integrity check
    could leave an UNVERIFIED file sitting under the final, "looks done" name forever (nothing
    re-checks a file that already has the right name). Checking first means the only way a file
    ever appears under the final name is already known-good at that moment.
    """
    _fsync_file(tmp_path)
    ok, results = _integrity_check_ok(tmp_path)
    if not ok:
        _unlink_with_sidecars(tmp_path)
        raise IntegrityCheckError(
            f"integrity_check on {tmp_path} returned {results!r}, not ['ok'] -- copy deleted"
        )
    tmp_path.replace(final_path)
    _fsync_dir(dest_dir)
    _try_chmod(final_path, _DEST_FILE_MODE)


def perform_backup(source: Path, dest_dir: Path, *, now: datetime | None = None) -> BackupResult:
    """Produce one verified, timestamped online backup of `source` inside `dest_dir`.

    Raises `BackupError` if no attempt completed the online copy, or `IntegrityCheckError`
    (after deleting the bad copy) if a copy was produced but did not pass
    ``PRAGMA integrity_check``. Never leaves a partially-written file under the final name
    (write to ``.tmp``, fsync, verify, THEN atomic rename) nor a failed integrity copy on disk.
    """
    if now is None:
        now = datetime.now(timezone.utc)  # noqa: UP017 -- needs 3.10+, see module docstring

    dest_dir.mkdir(parents=True, exist_ok=True)
    _try_chmod(dest_dir, _DEST_DIR_MODE)

    filename = now.strftime(_FILENAME_STRFTIME)
    final_path = dest_dir / filename
    tmp_path = dest_dir / f"{filename}.tmp"

    overall_start = time.monotonic()
    pages, sleep = _pick_pages_and_sleep(source)
    attempts = _run_backup_attempts(source, tmp_path, pages=pages, sleep=sleep)
    _verify_and_finalize(tmp_path, final_path, dest_dir)

    copy_count = _count_trades_readonly(final_path)
    source_count = _count_trades_readonly(source)
    duration = time.monotonic() - overall_start

    return BackupResult(
        dest_path=final_path,
        source_trade_count=source_count,
        copy_trade_count=copy_count,
        duration_seconds=duration,
        attempts=attempts,
    )


# --- rotation --------------------------------------------------------------------------


def _parse_backup_timestamp(filename: str) -> datetime | None:
    match = _FILENAME_RE.match(filename)
    if match is None:
        return None
    try:
        # datetime.UTC needs 3.11+; timezone.utc works on 3.10+ -- see module docstring.
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)  # noqa: UP017
    except ValueError:
        return None


def _rotate_orphan_sidecars(dest_dir: Path) -> list[Path]:
    """Delete any ``-wal``/``-shm`` sidecar whose matching ``trades-<ts>.sqlite`` main file no
    longer exists in `dest_dir`.

    NIT (backup sidecars, PR2 followups): a backup copy is always written as a plain,
    ``journal_mode=DELETE`` single file now (see ``_backup_attempt``), but a sidecar can still
    be left behind by a copy made before this fix, or by any read of such an older copy (e.g.
    this script's own `_integrity_check_ok`/`_count_trades_readonly`, run against a pre-fix
    WAL-mode copy) -- these never get scanned or rotated by `rotate_backups`'s own loop, which
    only looks at `trades-<ts>.sqlite` files, never their sidecars. Returns the deleted paths.
    """
    deleted: list[Path] = []
    for entry in dest_dir.iterdir():
        if not entry.is_file() or _SIDECAR_RE.match(entry.name) is None:
            continue
        main_file = entry.with_name(entry.name.rsplit("-", 1)[0])
        if main_file.exists():
            continue
        with contextlib.suppress(OSError):
            entry.unlink()
        deleted.append(entry)
    return deleted


def rotate_backups(dest_dir: Path, *, keep_days: int, now: datetime | None = None) -> list[Path]:
    """Delete backup files in `dest_dir` older than `keep_days`, matching the exact naming
    pattern this script writes (``trades-<UTC timestamp>.sqlite``) -- never a file that does
    not match it. The single newest matching file is always kept, even if it is itself older
    than `keep_days` (e.g. the recorder/backup job has been broken for longer than the
    retention window -- losing the only remaining copy would defeat the point of this script).

    NIT (backup sidecars, PR2 followups): deleting a matching copy also deletes its own
    ``-wal``/``-shm`` sidecars, if any (``_unlink_with_sidecars``), and this also sweeps for any
    already-orphaned sidecar left by an older copy (``_rotate_orphan_sidecars``) -- see that
    function. Returns the list of every path deleted (main files and sidecars alike).
    """
    if now is None:
        now = datetime.now(timezone.utc)  # noqa: UP017 -- needs 3.10+, see module docstring
    cutoff = now - timedelta(days=keep_days)

    candidates: list[tuple[datetime, Path]] = []
    for entry in dest_dir.iterdir():
        if not entry.is_file():
            continue
        ts = _parse_backup_timestamp(entry.name)
        if ts is not None:
            candidates.append((ts, entry))

    deleted: list[Path] = []
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        newest_path = candidates[0][1]
        for ts, path in candidates[1:]:
            if path == newest_path:
                continue  # defensive: duplicate timestamps should never both be "the newest"
            if ts < cutoff:
                _unlink_with_sidecars(path)
                deleted.append(path)

    deleted.extend(_rotate_orphan_sidecars(dest_dir))
    return deleted


# --- CLI ---------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Path to the live trades.sqlite")
    parser.add_argument(
        "--dest-dir", required=True, type=Path, help="Directory to write verified backups into"
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=_DEFAULT_KEEP_DAYS,
        help=f"Delete older matching backups past this many days (default: {_DEFAULT_KEEP_DAYS})",
    )
    return parser.parse_args(argv)


def _print_json(stream: IO[str], payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), file=stream)


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        result = perform_backup(args.source, args.dest_dir)
    except IntegrityCheckError as exc:
        _print_json(sys.stderr, {"status": "error", "stage": "integrity_check", "error": str(exc)})
        return 2
    except (BackupError, OSError) as exc:
        _print_json(sys.stderr, {"status": "error", "stage": "backup", "error": str(exc)})
        return 1

    try:
        deleted = rotate_backups(args.dest_dir, keep_days=args.keep_days)
    except OSError as exc:
        # The new backup is already verified and on disk; a rotation failure must not mask
        # that success -- log it as a warning and still report overall success (exit 0).
        _print_json(sys.stderr, {"status": "warning", "stage": "rotation", "error": str(exc)})
        deleted = []

    _print_json(
        sys.stdout,
        {
            "status": "ok",
            "dest": str(result.dest_path),
            "source_trade_count": result.source_trade_count,
            "copy_trade_count": result.copy_trade_count,
            "duration_seconds": round(result.duration_seconds, 3),
            "attempts": result.attempts,
            "rotated_deleted": [str(path) for path in deleted],
        },
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    # Local sanity check without systemd:
    #   python deploy/backup_trades.py --source ... --dest-dir ... [--keep-days N]
    raise SystemExit(main(sys.argv[1:]))
