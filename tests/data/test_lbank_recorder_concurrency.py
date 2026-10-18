"""Tests for the depth-stream scheduler fix (G1b coverage, docs/reports/tabdeal-shutdown-and-g1b-2026-10-18.md
§3): a slow or timed-out call must not make `_run_stream` skip the following slots.

`_run_concurrent_stream` fires a new depth call every period even while earlier ones are still in
flight, bounded to `max_concurrent` calls at once; a slot due while every worker is busy is skipped
and counted rather than queued, so the average request rate never exceeds one per period and a run
of slow calls never produces a catch-up burst once they return.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from tbot.data import lbank_recorder as lr
from tbot.data.lbank_recorder import LBankRecorder, RecorderConfig
from tests.data.test_lbank_recorder import DEPTH, FIXED_NOW_MS, _rows

_DEADLINE_S = 10.0  # generous: CI/Windows thread start-up can be slow, per test_lbank_recorder.py


def _wait_until(predicate: Callable[[], bool], timeout: float = _DEADLINE_S, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def test_slots_keep_firing_through_a_hang_and_in_flight_is_capped(tmp_path: Path) -> None:
    hang_event = threading.Event()
    lock = threading.Lock()
    state = {"calls": 0, "active": 0, "max_active": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            state["calls"] += 1
            call_no = state["calls"]
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        if call_no == 1:
            hang_event.wait(timeout=5.0)  # the first call hangs well past the 10 ms period
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    rec = LBankRecorder(
        RecorderConfig(data_dir=tmp_path, depth_interval_s=0.01, depth_max_concurrent=3, http_timeout_s=5.0),
        transport=transport,
        now_ms=lambda: FIXED_NOW_MS,
    )
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_concurrent_stream,
        args=("depth", 0.01, rec._poll_depth, 3, 5.0),
        daemon=True,
    )
    thread.start()
    try:
        # while call #1 hangs, at least 2 more rows must still land (slots are not skipped just
        # because one earlier call has not returned)
        assert _wait_until(lambda: rec.status["depth"].rows_total >= 2)
        with lock:
            assert state["max_active"] <= 3  # never more than depth_max_concurrent in flight
    finally:
        hang_event.set()
        rec._stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert rec.status["depth"].rows_total == len(_rows(tmp_path, "SELECT * FROM depth_snapshots"))


def test_skip_is_counted_and_logged_when_every_slot_is_hung(tmp_path: Path) -> None:
    release_event = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        release_event.wait(timeout=5.0)
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    rec = LBankRecorder(
        RecorderConfig(data_dir=tmp_path, depth_interval_s=0.01, depth_max_concurrent=3, http_timeout_s=5.0),
        transport=transport,
        now_ms=lambda: FIXED_NOW_MS,
    )
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_concurrent_stream,
        args=("depth", 0.01, rec._poll_depth, 3, 5.0),
        daemon=True,
    )
    with capture_logs() as logs:
        thread.start()
        try:
            assert _wait_until(lambda: rec.status["depth"].skipped_total >= 1)
            skipped = rec.status["depth"].skipped_total
        finally:
            release_event.set()
            rec._stop.set()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert skipped >= 1
    warnings = [e for e in logs if e["event"] == "lbank_recorder.slot_skipped"]
    assert warnings and warnings[0]["log_level"] == "warning" and warnings[0]["stream"] == "depth"
    assert warnings[0]["skipped_total"] >= 1


def test_rate_never_exceeds_one_dispatch_per_period_after_a_hang_clears(tmp_path: Path) -> None:
    """After the one slow call finally returns, later slots fire at the normal cadence -- not in a burst."""
    hang_event = threading.Event()
    call_times: list[float] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            call_times.append(time.monotonic())
            call_no = len(call_times)
        if call_no <= 3:  # fill all 3 workers so the 4th scheduled slot has to be skipped
            hang_event.wait(timeout=5.0)
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    period = 0.05
    cfg = RecorderConfig(
        data_dir=tmp_path, depth_interval_s=period, depth_max_concurrent=3, http_timeout_s=5.0
    )
    rec = LBankRecorder(cfg, transport=transport, now_ms=lambda: FIXED_NOW_MS)
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_concurrent_stream,
        args=("depth", period, rec._poll_depth, 3, 5.0),
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(lambda: rec.status["depth"].skipped_total >= 1)  # 2 free slots filled, then full
        hang_event.set()  # release the stuck call; scheduling must resume at `period`, not catch up
        assert _wait_until(lambda: rec.status["depth"].rows_total >= 5)
    finally:
        rec._stop.set()
        thread.join(timeout=5)
    with lock:
        times = list(call_times)
    # at most one new call is dispatched per period on average once the pipe clears -- no instant burst
    later = [t for t in times if t > times[2]]
    if len(later) >= 2:
        avg_gap = (later[-1] - later[0]) / (len(later) - 1)
        assert avg_gap >= period * 0.4


def test_depth_max_concurrent_one_falls_back_to_the_sequential_single_call_path(tmp_path: Path) -> None:
    """`max_concurrent <= 1` must behave exactly like the pre-existing single-call `_run_stream` path."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    ticks = itertools.count()
    rec = LBankRecorder(
        RecorderConfig(data_dir=tmp_path, depth_interval_s=0.02, depth_max_concurrent=1, http_timeout_s=5.0),
        transport=transport,
        now_ms=lambda: FIXED_NOW_MS + next(ticks),
    )
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_stream,
        args=("depth", 0.02, rec._poll_depth, 1, 5.0),
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(lambda: rec.status["depth"].rows_total >= 2)
    finally:
        rec._stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert rec.status["depth"].skipped_total == 0  # the sequential path never skips via `_note_skip`


def test_concurrent_writers_to_the_same_day_file_do_not_corrupt_it(tmp_path: Path) -> None:
    """3 worker threads writing depth rows to the same day file stay safe under WAL + busy_timeout."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    rec = LBankRecorder(
        RecorderConfig(data_dir=tmp_path, depth_interval_s=0.01, depth_max_concurrent=3, http_timeout_s=5.0),
        transport=transport,
        now_ms=lambda: FIXED_NOW_MS,
    )
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_concurrent_stream,
        args=("depth", 0.01, rec._poll_depth, 3, 5.0),
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_until(lambda: rec.status["depth"].rows_total >= 20)
    finally:
        rec._stop.set()
        thread.join(timeout=5)
    paths = sorted(tmp_path.glob("lbank-*.sqlite"))
    assert len(paths) == 1
    conn = sqlite3.connect(paths[0])
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    count = conn.execute("SELECT COUNT(*) FROM depth_snapshots").fetchone()[0]
    conn.close()
    assert count == rec.status["depth"].rows_total


def test_stop_joins_cleanly_while_a_call_is_still_in_flight(tmp_path: Path) -> None:
    release_event = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        release_event.wait(timeout=2.0)
        return httpx.Response(200, json=DEPTH)

    transport = httpx.MockTransport(handler)
    rec = LBankRecorder(
        RecorderConfig(data_dir=tmp_path, depth_interval_s=0.01, depth_max_concurrent=3, http_timeout_s=5.0),
        transport=transport,
        now_ms=lambda: FIXED_NOW_MS,
    )
    rec.status["depth"] = lr.StreamStatus()
    thread = threading.Thread(
        target=rec._run_concurrent_stream,
        args=("depth", 0.01, rec._poll_depth, 3, 5.0),
        daemon=True,
    )
    thread.start()
    assert _wait_until(lambda: thread.is_alive())
    release_event.set()  # let in-flight calls finish so stop() + join() do not hang for ~2 s each
    rec._stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()  # the thread's own executor.shutdown(wait=True) + client.close() returned


@pytest.mark.parametrize("max_concurrent", [3])
def test_full_recorder_start_stop_is_clean_with_concurrent_depth(tmp_path: Path, max_concurrent: int) -> None:
    """End-to-end: `start()`/`stop()`/`join()` through the real scheduler wiring, all streams together."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "depth.do" in url:
            return httpx.Response(200, json=DEPTH)
        if "trades.do" in url:
            return httpx.Response(200, json={"result": "true", "error_code": 0, "data": []})
        if "kline.do" in url:
            return httpx.Response(
                200,
                json={"result": "true", "error_code": 0, "data": [[1791637200, 1, 1, 1, 1, 1]]},
            )
        return httpx.Response(
            200, json={"result": "true", "data": [{"symbol": "BTCUSDT", "fundingRate": "0.0001"}]}
        )

    ticks = itertools.count()
    rec = LBankRecorder(
        RecorderConfig(
            data_dir=tmp_path,
            depth_interval_s=0.02,
            depth_max_concurrent=max_concurrent,
            trades_interval_s=0.05,
            trades_timeout_s=0.5,
            funding_interval_s=0.05,
            kline_interval_s={"minute1": 0.05},
            kline_lookback_bars={"minute1": 2},
            heartbeat_interval_s=0.02,
            http_timeout_s=0.5,
        ),
        transport=httpx.MockTransport(handler),
        now_ms=lambda: FIXED_NOW_MS + next(ticks),
    )
    rec.start()
    try:
        assert _wait_until(lambda: rec.status["depth"].rows_total >= 5)
    finally:
        rec.stop()
        rec.join(timeout=10)
    assert all(not t.is_alive() for t in rec._threads)
    assert rec.status["depth"].skipped_total == 0  # plenty of capacity (3) for a 20 ms period here
