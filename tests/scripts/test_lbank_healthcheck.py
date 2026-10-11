from __future__ import annotations

from typing import Any

from scripts.lbank_healthcheck import STREAM_LIMITS_S, disk_problem, evaluate

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


def _trades(**fields: Any) -> dict[str, Any]:
    base = {
        "last_ok_ms": NOW - 5_000,
        "last_attempt_ms": NOW - 5_000,
        "consecutive_errors": 0,
        "unparsed_total": 0,
        "consecutive_bad_polls": 0,
        "last_new_rows_ms": NOW - 30_000,
    }
    return {**base, **fields}


def test_old_heartbeat_without_the_new_fields_stays_healthy() -> None:
    assert evaluate(_payload(), NOW) == (True, "ok")


def test_polls_that_succeed_but_parse_nothing_degrade_the_health() -> None:
    ok, reason = evaluate(_payload(trades=_trades(consecutive_bad_polls=3, unparsed_total=1800)), NOW)
    assert not ok and "trades" in reason and "unparseable" in reason and "1800" in reason
    assert evaluate(_payload(trades=_trades(consecutive_bad_polls=2)), NOW)[0] is True


def test_bad_poll_threshold_is_configurable() -> None:
    payload = _payload(trades=_trades(consecutive_bad_polls=2))
    assert evaluate(payload, NOW, bad_polls_max=2)[0] is False


def test_no_new_trades_within_the_window_is_unhealthy() -> None:
    stale = _trades(last_new_rows_ms=NOW - 1_000_000)
    ok, reason = evaluate(_payload(trades=stale), NOW)
    assert not ok and "no new trade stored" in reason
    assert evaluate(_payload(trades=stale), NOW, trades_idle_s=2_000)[0] is True


def test_no_trade_ever_stored_is_judged_from_process_start() -> None:
    payload = _payload(trades=_trades(last_new_rows_ms=None))
    payload["started_ms"] = NOW - 60_000
    assert evaluate(payload, NOW)[0] is True  # still within the window after a fresh start
    payload["started_ms"] = NOW - 3_600_000
    ok, reason = evaluate(payload, NOW)
    assert not ok and "since start" in reason


def test_disk_alarm_at_80_percent() -> None:
    assert disk_problem(79, 100, 80.0) is None
    problem = disk_problem(80, 100, 80.0)
    assert problem is not None and "80.0 %" in problem
