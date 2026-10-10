"""Docker HEALTHCHECK for the LBank recorder: exit 0 healthy, 1 unhealthy (stdlib only).

Unhealthy when the heartbeat file is missing or stale, or when any stream has not completed a
successful poll within its own limit (order book 120 s, trades 300 s, 1m klines 300 s, funding 600 s,
1h klines 1800 s, 1d klines 7200 s), counted from the later of the last success and process start,
or when the data filesystem is at or above ``TBOT_LBANK_DISK_MAX_PCT`` (default 80 %) used. The disk
alarm only marks the container unhealthy; it never stops recording.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HEARTBEAT_MAX_AGE_S = 60.0
DISK_MAX_PCT_DEFAULT = 80.0
STREAM_LIMITS_S = {
    "depth": 120.0,
    "trades": 300.0,
    "kline_minute1": 300.0,
    "funding": 600.0,
    "kline_hour1": 1800.0,
    "kline_day1": 7200.0,
}


def evaluate(payload: dict[str, Any], now_ms: int) -> tuple[bool, str]:
    written = payload.get("written_ms")
    if not isinstance(written, int) or (now_ms - written) / 1000 > HEARTBEAT_MAX_AGE_S:
        return False, "heartbeat stale"
    streams = payload.get("streams", {})
    problems = []
    for name, limit in STREAM_LIMITS_S.items():
        status = streams.get(name)
        if not isinstance(status, dict):
            problems.append(f"{name}: missing")
            continue
        last_ok = status.get("last_ok_ms")
        reference = last_ok if isinstance(last_ok, int) else status.get("last_attempt_ms")
        if not isinstance(reference, int) or (last_ok is None and status.get("consecutive_errors", 0) > 3):
            problems.append(f"{name}: no successful poll")
        elif (now_ms - reference) / 1000 > limit:
            problems.append(f"{name}: last ok {int((now_ms - reference) / 1000)} s ago")
    return (not problems), ("ok" if not problems else "; ".join(problems))


def disk_problem(used: int, total: int, max_pct: float) -> str | None:
    pct = 100.0 * used / total if total else 100.0
    return f"disk {pct:.1f} % used (alarm at {max_pct:g} %)" if pct >= max_pct else None


def main() -> int:
    path = Path(os.environ.get("TBOT_LBANK_HEARTBEAT_FILE", "/app/data/lbank/heartbeat.json"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"unhealthy: cannot read heartbeat: {exc}")
        return 1
    ok, reason = evaluate(payload, int(time.time() * 1000))
    usage = shutil.disk_usage(path.parent)
    disk = disk_problem(
        usage.used, usage.total, float(os.environ.get("TBOT_LBANK_DISK_MAX_PCT", DISK_MAX_PCT_DEFAULT))
    )
    if disk:
        ok, reason = False, disk if reason == "ok" else f"{reason}; {disk}"
    print(("healthy: " if ok else "unhealthy: ") + reason)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
