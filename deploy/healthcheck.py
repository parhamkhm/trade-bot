"""Docker HEALTHCHECK entry point for the `recorder` service.

Invoked by `deploy/Dockerfile`'s `HEALTHCHECK` instruction as `python /app/healthcheck.py`
(no extra dependencies -- stdlib only -- so it works inside the slim runtime image without
needing the project's own virtualenv to be importable from a healthcheck context).

Fix-round context (reviewer finding M5): the previous healthcheck only `stat()`-ed the
heartbeat file's mtime. `TabdealRecorderService.process_once()`
(`src/tbot/data/tabdeal_recorder.py`) rewrites the heartbeat file on *every* poll cycle --
including a cycle where the poll itself failed (`PollOutcome.ok=False`) -- so a recorder
whose every single poll is failing (bad credentials, Tabdeal down, a bug) still produces a
fresh-looking heartbeat file forever. `docker ps` / `docker compose ps` would report
"healthy" indefinitely while nothing is actually being recorded, and with no Telegram
alerting until phase 5, that failure could run for an entire G1b week completely unnoticed.

The heartbeat payload is a fixed, already-implemented contract
(`tbot.data.tabdeal_recorder.HeartbeatState`/`write_heartbeat`):

    {"last_poll_ts": "<ISO-8601 UTC>", "last_trade_id": int | null,
     "n_trades_total": int, "consecutive_errors": int}

This script now additionally fails when `consecutive_errors` has crossed a threshold (the
recorder is up but every poll is failing) or when `last_poll_ts` itself is older than
expected (the process is wedged/dead even though the file still exists from before). Both
thresholds are env-overridable so an operator can tune them without rebuilding the image.

Every failure mode here -- missing file, unreadable file, malformed JSON, missing/
malformed fields, a clock that looks wrong -- resolves to a normal "unhealthy" result, never
an uncaught exception: a healthcheck command that itself crashes is a worse failure mode
than one that reports unhealthy, because some Docker versions do not distinguish "command
errored" from "command reported unhealthy" in a way that's easy to diagnose from the
outside, and a traceback printed by `docker inspect` is a poor substitute for "polling is
failing" or "file missing" as a diagnosis.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["DEFAULT_MAX_CONSECUTIVE_ERRORS", "evaluate_heartbeat", "main"]

# Matches the ENV default in deploy/Dockerfile; kept here too so this module has a sane
# default even if invoked without that ENV var set (e.g. directly, in a test).
DEFAULT_MAX_CONSECUTIVE_ERRORS = 10

# Small forward-clock-skew allowance: a `last_poll_ts` a few seconds in the future (container
# clock vs. host clock jitter) should not itself be treated as a failure; anything beyond this
# is suspicious enough to flag rather than silently accept.
_MAX_CLOCK_SKEW_SECONDS = 5.0


def evaluate_heartbeat(
    payload: object,
    *,
    now: datetime,
    max_age_seconds: float,
    max_consecutive_errors: int,
) -> tuple[bool, str]:
    """Pure decision function: (healthy, reason). Never raises.

    Kept separate from file I/O / env parsing so it can be unit tested directly against
    arbitrary (including malformed) payloads without touching the filesystem.
    """
    if not isinstance(payload, dict):
        return False, "heartbeat payload is not a JSON object"

    last_poll_ts_raw = payload.get("last_poll_ts")
    if not isinstance(last_poll_ts_raw, str):
        return False, "heartbeat payload missing string field 'last_poll_ts'"
    try:
        last_poll_ts = datetime.fromisoformat(last_poll_ts_raw)
    except ValueError as exc:
        return False, f"'last_poll_ts' is not a valid ISO-8601 timestamp: {exc}"
    if last_poll_ts.tzinfo is None:
        last_poll_ts = last_poll_ts.replace(tzinfo=UTC)

    age_seconds = (now - last_poll_ts).total_seconds()
    if age_seconds < -_MAX_CLOCK_SKEW_SECONDS:
        return False, f"'last_poll_ts' is {-age_seconds:.0f}s in the future"
    if age_seconds > max_age_seconds:
        return False, f"last_poll_ts is {age_seconds:.0f}s old (max {max_age_seconds:.0f}s)"

    consecutive_errors = payload.get("consecutive_errors")
    if not isinstance(consecutive_errors, int) or isinstance(consecutive_errors, bool):
        return False, "heartbeat payload missing integer field 'consecutive_errors'"
    if consecutive_errors >= max_consecutive_errors:
        return (
            False,
            f"consecutive_errors={consecutive_errors} >= max {max_consecutive_errors} "
            "(the process is alive but every poll cycle is failing)",
        )

    return True, "ok"


class _PayloadReadError(Exception):
    """Raised by `_read_payload` on any I/O/parse failure; never escapes `main()`.

    Fix-round finding (minor #7): a plain string return used as an error sentinel meant a
    heartbeat file that happened to contain a bare JSON string (e.g. `"some status"`, valid
    JSON, just not an object) was indistinguishable from `_read_payload`'s own error
    message -- `main()` would print that string to stderr as though it were the diagnosis.
    A dedicated exception type removes the ambiguity: `_read_payload` either returns a
    parsed JSON value (of *any* shape -- `evaluate_heartbeat` is what decides whether that
    shape is usable) or raises, never both via the same `object`-typed return.
    """


def _read_payload(path: Path) -> object:
    """Returns the parsed JSON payload (of any shape), or raises `_PayloadReadError`.

    Callers must catch `_PayloadReadError` -- never let it propagate -- so every I/O/parse
    failure mode (missing file, permission error, not valid JSON, ...) resolves to
    "unhealthy", never a traceback.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _PayloadReadError(f"cannot read heartbeat file {path!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _PayloadReadError(f"heartbeat file {path!r} is not valid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - argv kept for test symmetry
    """CLI entry point. Exit 0 = healthy, 1 = unhealthy. Never raises."""
    path = Path(os.environ.get("TBOT_HEARTBEAT_FILE", "/app/data/tabdeal/heartbeat.json"))
    try:
        max_age_seconds = float(os.environ.get("TBOT_HEARTBEAT_MAX_AGE_SECONDS", "180"))
    except ValueError:
        max_age_seconds = 180.0
    try:
        max_consecutive_errors = int(
            os.environ.get(
                "TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS", str(DEFAULT_MAX_CONSECUTIVE_ERRORS)
            )
        )
    except ValueError:
        max_consecutive_errors = DEFAULT_MAX_CONSECUTIVE_ERRORS

    if not path.is_file():
        print(f"heartbeat file not found: {path}", file=sys.stderr)
        return 1

    try:
        payload = _read_payload(path)
    except _PayloadReadError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    healthy, reason = evaluate_heartbeat(
        payload,
        now=datetime.now(UTC),
        max_age_seconds=max_age_seconds,
        max_consecutive_errors=max_consecutive_errors,
    )
    if not healthy:
        print(reason, file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    # Local sanity check without Docker: TBOT_HEARTBEAT_FILE=... python deploy/healthcheck.py
    raise SystemExit(main(sys.argv[1:]))
