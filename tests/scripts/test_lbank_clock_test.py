"""Tests for scripts/lbank_clock_test.py -- offline only (httpx.MockTransport, fake clock + sleeper).

No test touches the network (tests/conftest.py blocks real sockets). The fake LBank answers
``timestamp.do`` with a server time computed from the fake host clock plus a chosen true offset, and
advances the fake clock by a chosen RTT, so offsets and RTTs are exact integers of milliseconds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.lbank_probe import OutputPaths

from scripts import lbank_clock_test as ct

BASE_NS = 1_791_000_000_000 * 10**6  # whole-millisecond epoch so offsets come out exact
PASS_WORD = re.compile(r"\bPASS\b")


@pytest.fixture(autouse=True)
def _fixed_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TBOT_GIT_COMMIT", "abc1234")


# ---------------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------------


def rec(
    index: int,
    rtt_ms: int,
    offset_ms: int,
    *,
    status: int = 200,
    reason: str | None = None,
) -> ct.CallRecord:
    """A call whose RTT / offset are exact: server time = midpoint + offset (rtt_ms must be even)."""
    t_before = BASE_NS + index * 30 * 10**9
    t_after = t_before + rtt_ms * 10**6
    server_ms = (t_before + t_after) // 2 // 10**6 + offset_ms
    return ct.CallRecord(
        index=index,
        due_s=index * 30.0,
        start_s=index * 30.0,
        t_before_ns=t_before,
        t_after_ns=t_after,
        mono_rtt_ms=float(rtt_ms),
        status=status,
        error=None,
        server_ms=server_ms if status == 200 else None,
        invalid_reason=reason if reason else (None if status == 200 else f"http_{status}"),
    )


def test_offset_and_rtt_arithmetic_and_sign_convention() -> None:
    ahead = rec(0, 400, 250)  # server clock is 250 ms ahead of the host
    behind = rec(1, 400, -141)
    assert ahead.rtt_ms == 400.0
    assert ahead.offset_ms == 250.0
    assert behind.offset_ms == -141.0
    # by hand: t_before=1000 ms, t_after=1600 ms, server=1900 ms -> midpoint 1300 -> offset +600
    manual = ct.CallRecord(
        index=0, due_s=0, start_s=0, t_before_ns=1000 * 10**6, t_after_ns=1600 * 10**6, mono_rtt_ms=600.0,
        status=200, error=None, server_ms=1900, invalid_reason=None,
    )  # fmt: skip
    assert manual.rtt_ms == 600.0
    assert manual.offset_ms == 600.0


def test_estimate_uses_median_of_fastest_twelve_and_smallest_valid_rtt() -> None:
    calls = [rec(i, 400 + 2 * i, offset_ms=100 + i) for i in range(120)]
    est = ct.estimate_clock(calls, 120)
    assert est is not None
    assert [c.index for c in est.fastest] == list(range(12))  # fastest 12 by RTT
    # offsets 100..111 -> median of an even count is the mean of the two middle ones
    assert est.offset_ms == 105.5
    assert est.min_rtt_ms == 400.0
    assert est.bound_ms == 105.5 + 200.0
    assert est.criterion_met


def test_min_rtt_ignores_fast_invalid_calls() -> None:
    calls = [rec(i, 400, 100) for i in range(12)] + [rec(12, 4, 0, status=502)]
    est = ct.estimate_clock(calls, 120)
    assert est is not None
    assert est.min_rtt_ms == 400.0


def test_fastest_selection_ties_break_by_call_index() -> None:
    calls = [rec(i, 400, offset_ms=i) for i in range(20)]  # 20 identical RTTs
    chosen = ct.select_fastest(list(reversed(calls)), 12)
    assert [c.index for c in chosen] == list(range(12))
    mixed = [rec(0, 600, 0), rec(1, 400, 0), rec(2, 400, 0), rec(3, 500, 0)]
    assert [c.index for c in ct.select_fastest(mixed, 3)] == [1, 2, 3]


def test_fastest_count_and_min_valid_are_integer_exact() -> None:
    assert ct.fastest_count(120) == 12
    assert ct.min_valid_calls(120) == 108
    assert ct.fastest_count(1) == 1
    assert ct.min_valid_calls(5) == 5  # ceil(4.5)


@pytest.mark.parametrize(
    ("offset", "min_rtt", "expected"),
    [
        (300.0, 399.8, True),  # 499.9
        (300.0, 400.0, False),  # exactly 500.0
        (-300.0, 400.0, False),  # |offset|: sign does not help
        (-300.0, 399.8, True),
        (0.0, 999.8, True),
        (0.0, 1000.0, False),
    ],
)
def test_criterion_boundary_is_strict_at_500_ms(offset: float, min_rtt: float, expected: bool) -> None:
    assert ct.criterion_met(offset, min_rtt) is expected


# ---------------------------------------------------------------------------------
# fake exchange + fake clock
# ---------------------------------------------------------------------------------


@dataclass
class Step:
    rtt_ms: int = 400
    offset_ms: int = 100
    status: int = 200
    data: Any = "auto"  # "auto" = server time from the fake clock
    raises: Exception | None = None
    headers: dict[str, str] | None = None


class World:
    """Fake host clock (integer ns) + fake LBank. ``plan(n)`` describes the n-th request (0-based)."""

    def __init__(self, plan: Callable[[int], Step] | None = None) -> None:
        self.now_ns = BASE_NS
        self.plan = plan or (lambda _n: Step())
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.interrupt_on_sleep: int | None = None
        self.clock = ct.Clock(wall_ns=lambda: self.now_ns, monotonic=self._mono, sleep=self._sleep)
        self.transport = httpx.MockTransport(self._handle)

    def _mono(self) -> float:
        return (self.now_ns - BASE_NS) / 1e9

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.interrupt_on_sleep is not None and len(self.sleeps) >= self.interrupt_on_sleep:
            raise KeyboardInterrupt
        self.now_ns += round(max(0.0, seconds) * 1e9)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        step = self.plan(len(self.requests))
        self.requests.append(request)
        half = step.rtt_ms * 10**6 // 2
        self.now_ns += half
        server_ms = self.now_ns // 10**6 + step.offset_ms
        self.now_ns += half
        if step.raises is not None:
            raise step.raises
        data = server_ms if step.data == "auto" else step.data
        body = {"result": "true", "data": data, "error_code": 0, "ts": server_ms}
        return httpx.Response(step.status, json=body, headers=step.headers)


def run(
    world: World,
    tmp_path: Path,
    *,
    calls: int = ct.REGISTERED_CALLS,
    spacing: float = ct.REGISTERED_SPACING_S,
) -> dict[str, Any]:
    paths = OutputPaths.for_run(tmp_path, "clock_test", "20261012T080000Z")
    prov = ct.make_provenance(world.clock, ["x"], "turkey-vps")
    with httpx.Client(transport=world.transport, timeout=ct.REQUEST_TIMEOUT_S) as client:
        return ct.run_clock_test(
            client, world.clock, calls=calls, spacing_s=spacing, paths=paths, provenance=prov
        )


def read_jsonl(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "clock_test_20261012T080000Z.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------------
# whole-run behaviour
# ---------------------------------------------------------------------------------


def test_registered_run_passes_and_writes_all_outputs(tmp_path: Path) -> None:
    world = World()  # rtt 400, offset +100 -> bound 300 ms
    summary = run(world, tmp_path)
    assert summary["verdict"] == "PASS"
    assert summary["registered_run"] is True
    assert len(world.requests) == 120
    est = summary["estimate"]
    assert est["offset_ms"] == 100.0
    assert est["min_rtt_ms"] == 400.0
    assert est["bound_ms"] == 300.0
    assert est["offset_minus_half_min_rtt_ms"] == -100.0
    assert est["offset_plus_half_min_rtt_ms"] == 300.0
    assert len(est["fastest_calls"]) == 12
    assert summary["counts"]["valid"] == 120
    assert ct.exit_code(summary, sigterm=False) == 0
    md = (tmp_path / "clock_test_20261012T080000Z.md").read_text(encoding="utf-8")
    assert "Verdict: PASS" in md
    assert "Fastest 12 calls by RTT" in md
    assert md.count("\n| ") > 120  # per-call table
    saved = json.loads((tmp_path / "clock_test_20261012T080000Z.summary.json").read_text(encoding="utf-8"))
    assert saved["verdict"] == "PASS"
    kinds = [r["kind"] for r in read_jsonl(tmp_path)]
    assert kinds[0] == "provenance"
    assert kinds[-1] == "end"
    assert kinds.count("call") == 120


def test_exact_boundary_end_to_end(tmp_path: Path) -> None:
    # offset +300, min rtt 400 -> exactly 500.0 -> FAIL; min rtt 398 -> 499.0 -> PASS
    fail = run(World(lambda _n: Step(rtt_ms=400, offset_ms=300)), tmp_path / "a")
    assert fail["estimate"]["bound_ms"] == 500.0
    assert fail["verdict"] == "FAIL"
    ok = run(World(lambda _n: Step(rtt_ms=398, offset_ms=300)), tmp_path / "b")
    assert ok["estimate"]["bound_ms"] == 499.0
    assert ok["verdict"] == "PASS"


def test_429_at_call_k_makes_no_further_request_and_fails(tmp_path: Path) -> None:
    k = 7
    world = World(lambda n: Step(status=429) if n == k - 1 else Step())
    summary = run(world, tmp_path)
    assert len(world.requests) == k
    assert summary["verdict"] == "FAIL"
    assert summary["stopped_on_ban"] == {"reason": "HTTP 429", "call": k - 1}
    assert ct.exit_code(summary, sigterm=False) == 2
    assert [r["kind"] for r in read_jsonl(tmp_path)][-2:] == ["stopped_on_ban", "end"]


@pytest.mark.parametrize("status", [403, 418])
def test_other_ban_statuses_stop_immediately(tmp_path: Path, status: int) -> None:
    world = World(lambda n: Step(status=status) if n == 2 else Step())
    summary = run(world, tmp_path)
    assert len(world.requests) == 3
    assert summary["verdict"] == "FAIL"


def test_fewer_than_108_valid_calls_fails_but_108_does_not(tmp_path: Path) -> None:
    bad_107 = World(lambda n: Step(status=500) if n < 13 else Step())  # 13 bad -> 107 valid
    s107 = run(bad_107, tmp_path / "a")
    assert len(bad_107.requests) == 120  # 5xx is recorded, not a ban
    assert s107["counts"]["valid"] == 107
    assert s107["verdict"] == "FAIL"
    assert "107" in s107["verdict_reason"]
    bad_12 = World(lambda n: Step(status=500) if n < 12 else Step())  # 12 bad -> 108 valid
    s108 = run(bad_12, tmp_path / "b")
    assert s108["counts"]["valid"] == 108
    assert s108["verdict"] == "PASS"
    assert s108["counts"]["http_status"]["500"] == 12


def test_timeouts_and_transport_errors_are_recorded_not_fatal(tmp_path: Path) -> None:
    def plan(n: int) -> Step:
        if n == 3:
            return Step(raises=httpx.ReadTimeout("slow"))
        if n == 4:
            return Step(raises=httpx.ConnectError("down"))
        return Step()

    summary = run(World(plan), tmp_path)
    assert summary["counts"]["invalid_reasons"] == {"timeout": 1, "ConnectError": 1}
    assert summary["counts"]["valid"] == 118
    assert summary["verdict"] == "PASS"


def test_server_time_in_seconds_or_garbage_is_not_a_valid_sample(tmp_path: Path) -> None:
    def plan(n: int) -> Step:
        if n == 0:
            return Step(data=1_791_000_000)  # seconds
        if n == 1:
            return Step(data="not-a-number")
        return Step()

    summary = run(World(plan), tmp_path)
    assert summary["counts"]["invalid_reasons"] == {"server_time_not_in_ms": 1, "server_time_unparseable": 1}


def test_rate_limit_headers_are_kept_and_secrets_are_not(tmp_path: Path) -> None:
    world = World(
        lambda n: Step(status=429, headers={"Retry-After": "60", "Set-Cookie": "s=1"}) if n == 0 else Step()
    )
    run(world, tmp_path)
    call = next(r for r in read_jsonl(tmp_path) if r["kind"] == "call")
    assert call["headers"] == {"retry-after": "60"}
    assert "Set-Cookie" not in json.dumps(call)


# ---------------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------------


def test_calls_start_exactly_on_the_grid(tmp_path: Path) -> None:
    world = World()
    run(world, tmp_path, calls=10, spacing=30.0)
    starts = [r["start_s"] for r in read_jsonl(tmp_path) if r["kind"] == "call"]
    assert starts == pytest.approx([30.0 * i for i in range(10)], abs=1e-6)


def test_slow_call_never_shifts_later_starts_or_causes_a_burst(tmp_path: Path) -> None:
    # call 1 (starts at 30) takes 90 s: slots 2, 3 (due 60, 90) are skipped, slot 4 (due 120) is on the grid
    world = World(lambda n: Step(rtt_ms=90_000) if n == 1 else Step())
    summary = run(world, tmp_path, calls=8, spacing=30.0)
    recs = [r for r in read_jsonl(tmp_path) if r["kind"] == "call"]
    starts = {r["index"]: r["start_s"] for r in recs}
    assert summary["counts"]["skipped_slot_indexes"] == [2, 3]
    assert starts[4] == pytest.approx(120.0, abs=1e-6)  # not shifted by the slow call
    assert starts[7] == pytest.approx(210.0, abs=1e-6)
    ordered = sorted(starts.values())
    gaps = [b - a for a, b in pairwise(ordered)]
    assert min(gaps) >= 30.0 - 5.0 - 1e-6  # never closer than spacing minus the late-start tolerance
    assert len(world.requests) == 6  # 8 slots - 2 skipped: no catch-up requests
    assert any(r["kind"] == "skipped_slot" for r in read_jsonl(tmp_path))


def test_slightly_late_call_starts_immediately_and_next_stays_on_grid(tmp_path: Path) -> None:
    world = World(lambda n: Step(rtt_ms=32_000) if n == 0 else Step())  # 2 s past the next slot
    run(world, tmp_path, calls=4, spacing=30.0)
    starts = {r["index"]: r["start_s"] for r in read_jsonl(tmp_path) if r["kind"] == "call"}
    assert starts[1] == pytest.approx(32.0, abs=1e-6)
    assert starts[2] == pytest.approx(60.0, abs=1e-6)


# ---------------------------------------------------------------------------------
# request whitelist
# ---------------------------------------------------------------------------------


def test_only_get_timestamp_do_is_ever_requested(tmp_path: Path) -> None:
    world = World()
    run(world, tmp_path, calls=5, spacing=30.0)
    assert len(world.requests) == 5
    for req in world.requests:
        assert req.method == "GET"
        assert req.url.scheme == "https"
        assert req.url.host == "api.lbkex.com"
        assert req.url.path == "/v2/timestamp.do"
        assert req.url.query == b""
        assert req.content == b""
        lowered = {k.lower() for k in req.headers}
        assert not lowered & {"authorization", "x-mbx-apikey", "cookie"}
        assert "signature" not in str(req.url).lower()


def test_url_comes_from_the_recorders_spot_base() -> None:
    from scripts.lbank_probe import ENDPOINTS

    from tbot.data.lbank_recorder import SPOT_BASE_URL

    spec = ENDPOINTS["timestamp"]
    assert spec.base == SPOT_BASE_URL
    assert spec.path == "/v2/timestamp.do"


# ---------------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------------


def test_provenance_is_complete(tmp_path: Path) -> None:
    summary = run(World(), tmp_path, calls=3, spacing=30.0)
    prov = summary["provenance"]
    assert prov["hostname"] == "turkey-vps"
    assert prov["git_commit"] == "abc1234"
    assert prov["script"] == "scripts/lbank_clock_test.py"
    assert prov["data_source"] == "live"
    assert prov["utc_start"].endswith("+00:00")
    assert prov["utc_end"].endswith("+00:00")
    assert prov["measured_from"].startswith("measured from turkey-vps at ")
    assert "NOT checked" in prov["local_clock_sync"]
    assert "operator records" in summary["host_clock_sync"]
    md = (tmp_path / "clock_test_20261012T080000Z.md").read_text(encoding="utf-8")
    assert "measured from turkey-vps at " in md
    assert "operator records" in md
    first = read_jsonl(tmp_path)[0]
    assert first["kind"] == "provenance"
    assert first["hostname"] == "turkey-vps"


# ---------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------


def test_cli_defaults_equal_the_registered_values() -> None:
    args = ct._build_parser().parse_args([])
    assert args.calls == 120
    assert args.spacing == 30.0
    assert ct.REQUEST_TIMEOUT_S == 10.0
    assert ct.THRESHOLD_MS == 500.0


def test_main_registered_run_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = World()
    code = ct.main(
        ["--out-dir", str(tmp_path), "--host-label", "turkey-vps"],
        clock=world.clock,
        transport=world.transport,
    )
    assert code == 0
    assert "verdict=PASS" in capsys.readouterr().out
    assert len(world.requests) == 120
    assert len(list(tmp_path.glob("lbank_clock_test_*.jsonl"))) == 1


@pytest.mark.parametrize(("calls", "spacing"), [(5, "30"), (120, "29"), (3, "2")])
def test_non_default_counts_are_never_labelled_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], calls: int, spacing: str
) -> None:
    world = World()  # would comfortably meet the criterion
    code = ct.main(
        ["--calls", str(calls), "--spacing", spacing, "--out-dir", str(tmp_path), "--host-label", "h"],
        clock=world.clock,
        transport=world.transport,
    )
    captured = capsys.readouterr()
    assert code == 4
    texts = [captured.out, captured.err] + [p.read_text(encoding="utf-8") for p in tmp_path.iterdir()]
    for text in texts:
        assert not PASS_WORD.search(text)
    summary = json.loads(next(tmp_path.glob("*.summary.json")).read_text(encoding="utf-8"))
    assert summary["verdict"] == "NOT_REGISTERED_RUN"
    assert summary["registered_run"] is False
    md = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "NOT the registered run" in md
    assert "NOT the registered run" in captured.err


def test_non_default_run_that_hits_a_ban_still_never_says_pass(tmp_path: Path) -> None:
    world = World(lambda n: Step(status=429) if n == 1 else Step())
    summary = run(world, tmp_path, calls=5, spacing=30.0)
    assert summary["verdict"] == "NOT_REGISTERED_RUN"
    assert len(world.requests) == 2
    assert ct.exit_code(summary, sigterm=False) == 4


def test_ctrl_c_writes_partial_output_and_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.interrupt_on_sleep = 5  # KeyboardInterrupt while waiting for call index 5
    code = ct.main(
        ["--out-dir", str(tmp_path), "--host-label", "h"], clock=world.clock, transport=world.transport
    )
    assert code == 130
    assert len(world.requests) == 5
    summary = json.loads(next(tmp_path.glob("*.summary.json")).read_text(encoding="utf-8"))
    assert summary["verdict"] == "INCOMPLETE"
    assert summary["interrupted"] is True
    assert summary["counts"]["valid"] == 5
    assert "verdict=INCOMPLETE" in capsys.readouterr().out
    assert next(tmp_path.glob("*.md")).exists()
    assert [r["kind"] for r in read_jsonl_path(next(tmp_path.glob("*.jsonl")))][-1] == "end"


def read_jsonl_path(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("argv", [["--spacing", "0.5"], ["--calls", "0"]])
def test_cli_rejects_unsafe_overrides(tmp_path: Path, argv: list[str]) -> None:
    world = World()
    with pytest.raises(SystemExit):
        ct.main([*argv, "--out-dir", str(tmp_path)], clock=world.clock, transport=world.transport)
    assert world.requests == []
