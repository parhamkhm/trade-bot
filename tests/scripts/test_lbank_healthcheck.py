from __future__ import annotations

from typing import Any

from scripts.lbank_healthcheck import STREAM_LIMITS_S, evaluate

NOW = 1_791_637_448_000


def _payload(**overrides: dict[str, Any]) -> dict[str, Any]:
    streams = {
        name: {"last_ok_ms": NOW - 5_000, "last_attempt_ms": NOW - 5_000, "consecutive_errors": 0}
        for name in STREAM_LIMITS_S
    }
    streams.update(overrides)
    return {"written_ms": NOW - 2_000, "streams": streams}


def test_all_streams_fresh_is_healthy() -> None:
    assert evaluate(_payload(), NOW) == (True, "ok")


def test_stale_heartbeat_is_unhealthy() -> None:
    payload = _payload()
    payload["written_ms"] = NOW - 120_000
    assert evaluate(payload, NOW)[0] is False


def test_stale_depth_stream_is_unhealthy() -> None:
    ok, reason = evaluate(_payload(depth={"last_ok_ms": NOW - 200_000, "consecutive_errors": 7}), NOW)
    assert not ok and "depth" in reason


def test_never_succeeded_after_repeated_errors_is_unhealthy() -> None:
    ok, reason = evaluate(
        _payload(trades={"last_ok_ms": None, "last_attempt_ms": NOW, "consecutive_errors": 4}), NOW
    )
    assert not ok and "trades" in reason


def test_just_started_stream_is_healthy() -> None:
    ok, _ = evaluate(
        _payload(kline_day1={"last_ok_ms": None, "last_attempt_ms": NOW - 1_000, "consecutive_errors": 0}),
        NOW,
    )
    assert ok
