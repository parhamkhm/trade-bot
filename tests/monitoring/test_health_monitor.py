"""Tests for the host-side Telegram health monitor. Only fake values and fake edges are used:
no real token, no network, no docker, no systemctl, no real env file."""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import subprocess
import threading
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tbot.monitoring import health_monitor as hm
from tbot.monitoring.health_monitor import (
    CmdResult,
    ConfigError,
    DiskUsage,
    Monitor,
    Probes,
    Problem,
    Settings,
    StateStore,
    Target,
    TelegramError,
    TelegramSender,
)

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
FAKE_TOKEN = "123456789:AAFakeTokenFakeTokenFakeToken_xyz"
FAKE_CHAT = "424242"

TABDEAL = hm.DEFAULT_TARGETS[0]
LBANK = hm.DEFAULT_TARGETS[1]
ROOT = Path("/")
DOCKER_DIR = Path("/var/lib/docker")
BACKUP_DIR = Path("/var/backups/tbot")

GIB = 1024**3


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def container_state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "Status": "running",
        "Restarting": False,
        "ExitCode": 0,
        "StartedAt": "2026-10-09T00:00:00.123456789Z",
        "Health": {"Status": "healthy", "Log": [{"Output": "ok"}]},
    }
    state.update(over)
    return state


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Host:
    """A fake server: containers, volumes, files, directories, units and disks, all mutable."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.states: dict[str, Any] = {
            "tbot-recorder": container_state(),
            "tbot-lbank-recorder": container_state(),
        }
        self.restarts: dict[str, int] = {}
        self.memory = hm.Memory()
        self.mounts = {"tbot-data": "/v/tbot-data", "tbot-lbank-data": "/v/tbot-lbank-data"}
        self.files: dict[Path, str | Exception | Callable[[], str]] = {}
        self.dirs: dict[Path, list[str] | Exception] = {}
        self.failed_units: set[str] = set()
        self.disks: dict[Path, DiskUsage | None] = {
            ROOT: DiskUsage(50 * GIB, 50 * GIB, 1),
            DOCKER_DIR: DiskUsage(50 * GIB, 50 * GIB, 1),
            BACKUP_DIR: DiskUsage(10 * GIB, 90 * GIB, 2),
        }
        self.tool_missing: set[str] = set()
        self.docker_down = False
        self.calls: list[tuple[str, ...]] = []
        self.heartbeat_ok()
        self.lbank_status()
        self.trades_backup()

    # -- scenario helpers -------------------------------------------------------------------

    def heartbeat_ok(self, age_s: float = 10.0) -> None:
        """Heartbeats that stay `age_s` old however far the fake clock advances (a live recorder)."""

        def tabdeal() -> str:
            return json.dumps({"last_poll_ts": iso(self.clock() - timedelta(seconds=age_s))})

        def lbank() -> str:
            when = self.clock() - timedelta(seconds=age_s)
            return json.dumps({"written_ms": int(when.timestamp() * 1000)})

        self.files[Path("/v/tbot-data") / "tabdeal/heartbeat.json"] = tabdeal
        self.files[Path("/v/tbot-lbank-data") / "lbank/heartbeat.json"] = lbank

    def lbank_status(self, **over: Any) -> None:
        data: dict[str, Any] = {
            "ts": iso(self.clock() - timedelta(hours=3)),
            "ok": True,
            "exit_code": 0,
            "days_archived": [],
            "days_pending": [],
            "message": "ok",
            "integrity_ok": True,
        }
        data.update(over)
        self.files[hm.LBANK_STATUS_FILE] = json.dumps(data)

    def trades_backup(self, age_h: float = 5.0) -> None:
        stamp = (self.clock() - timedelta(hours=age_h)).strftime("%Y%m%dT%H%M%SZ")
        self.dirs[hm.TRADES_BACKUP_DIR] = [f"trades-{stamp}.sqlite", "notes.txt"]

    # -- injected edges ---------------------------------------------------------------------

    def run(self, args: Sequence[str]) -> CmdResult:
        argv = tuple(args)
        self.calls.append(argv)
        if argv[0] in self.tool_missing:
            raise hm.ToolError(argv[0], "not installed / not on PATH")
        if argv[0] == "systemctl":
            failed = argv[2] in self.failed_units
            return CmdResult(0 if failed else 1, "failed\n" if failed else "active\n", "")
        if self.docker_down:
            return CmdResult(1, "", "Cannot connect to the Docker daemon")
        if argv[:2] == ("docker", "inspect"):
            state = self.states.get(argv[-1])
            if state is None:
                return CmdResult(1, "", f"Error: No such object: {argv[-1]}")
            if isinstance(state, str):  # raw, possibly garbage, output
                return CmdResult(0, state, "")
            doc = {"state": state, "restarts": self.restarts.get(argv[-1], 0)}
            return CmdResult(0, json.dumps(doc), "")
        if argv[:3] == ("docker", "volume", "inspect"):
            mount = self.mounts.get(argv[3])
            return CmdResult(0, f"{mount}\n", "") if mount else CmdResult(1, "", "no such volume")
        raise AssertionError(f"unexpected command {argv}")

    def read_text(self, path: Path) -> str:
        item = self.files.get(path)
        if item is None:
            raise FileNotFoundError(path.name)
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    def list_names(self, path: Path) -> list[str]:
        item = self.dirs.get(path)
        if item is None:
            raise FileNotFoundError(path.name)
        if isinstance(item, Exception):
            raise item
        return item

    def disk(self, path: Path) -> DiskUsage | None:
        return self.disks.get(path)

    def probes(self) -> Probes:
        return Probes(
            now=self.clock,
            run=self.run,
            read_text=self.read_text,
            list_names=self.list_names,
            disk=self.disk,
            memory=self.memory,
        )


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    return Settings(state_dir=tmp_path / "state", hostname="testhost", **over)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def host(clock: Clock) -> Host:
    return Host(clock)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


class Outbox:
    """Collects sent messages; can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.calls = 0
        self.fail: Exception | None = None

    def __call__(self, text: str) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.sent.append(text)


def make_monitor(
    settings: Settings, host: Host, out: Outbox, store: StateStore | None = None
) -> Monitor:
    return Monitor(settings, host.probes(), out, store or StateStore(settings.state_dir / "state.json"))


def keys(problems: dict[str, Problem] | list[Problem]) -> set[str]:
    items = problems.values() if isinstance(problems, dict) else problems
    return {p.key for p in items}


# ---------------------------------------------------------------------------------------------
# Healthy baseline
# ---------------------------------------------------------------------------------------------


def test_healthy_host_has_no_problems(host: Host, settings: Settings) -> None:
    assert hm.collect_problems(settings, host.probes()) == {}


# ---------------------------------------------------------------------------------------------
# Class 3: disks
# ---------------------------------------------------------------------------------------------


def test_disk_above_limit_alerts_and_equal_does_not(host: Host, settings: Settings) -> None:
    host.disks[BACKUP_DIR] = DiskUsage(81 * GIB, 19 * GIB, 2)
    found = hm.check_disks(settings, host.probes())
    assert keys(found) == {"disk:" + str(BACKUP_DIR)}
    assert "81.0%" in found[0].summary

    host.disks[BACKUP_DIR] = DiskUsage(80 * GIB, 20 * GIB, 2)
    assert hm.check_disks(settings, host.probes()) == []


def test_disk_reports_one_problem_per_filesystem(host: Host, settings: Settings) -> None:
    full = DiskUsage(95 * GIB, 5 * GIB, 7)
    host.disks[ROOT] = full
    host.disks[DOCKER_DIR] = full  # same device id as "/": one filesystem, one alert
    assert keys(hm.check_disks(settings, host.probes())) == {"disk:" + str(ROOT)}


def test_disk_path_that_cannot_be_read_raises_no_alert_but_is_unknown(host: Host, settings: Settings) -> None:
    host.disks[DOCKER_DIR] = None
    found = hm.check_disks(settings, host.probes())
    assert [(p.key, p.silent) for p in found] == [("disk:" + str(DOCKER_DIR), True)]
    assert hm.collect(settings, host.probes()).problems == {}


def test_disk_clear_margin_keeps_an_open_alert_until_well_below_the_limit(
    host: Host, settings: Settings
) -> None:
    key = "disk:" + str(BACKUP_DIR)
    probes = host.probes()
    host.disks[BACKUP_DIR] = DiskUsage(79 * GIB, 21 * GIB, 2)  # 79%: not open yet, below the 80% limit
    assert hm.check_disks(settings, probes) == []
    probes.memory.open_keys = {key}  # simulate: this alert is already open
    found = hm.check_disks(settings, probes)
    assert keys(found) == {key}  # still above 80 - 2 = 78%, so it does not clear
    assert "clears below 78%" in found[0].summary
    host.disks[BACKUP_DIR] = DiskUsage(77 * GIB, 23 * GIB, 2)  # 77%: now below the clear margin
    assert hm.check_disks(settings, probes) == []
    host.disks[BACKUP_DIR] = DiskUsage(85 * GIB, 15 * GIB, 2)  # unrelated: a fresh alert still needs >80%
    probes.memory.open_keys = set()
    assert keys(hm.check_disks(settings, probes)) == {key}


# ---------------------------------------------------------------------------------------------
# Class 4: containers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "needle"),
    [
        (container_state(Status="exited", ExitCode=137), "exited"),
        (container_state(Status="restarting", Restarting=True, ExitCode=1), "restarting"),
        (container_state(Status="dead"), "dead"),
        (container_state(Status="paused"), "paused"),
    ],
)
def test_container_not_running_alerts(
    host: Host, settings: Settings, state: dict[str, Any], needle: str
) -> None:
    host.states["tbot-recorder"] = state
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:tabdeal"}
    assert needle in found[0].summary


def test_missing_container_alerts(host: Host, settings: Settings) -> None:
    del host.states["tbot-lbank-recorder"]
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:lbank"}
    assert "does not exist" in found[0].summary


def test_unreadable_docker_state_alerts(host: Host, settings: Settings) -> None:
    host.states["tbot-recorder"] = "this is not json"
    assert keys(hm.check_recorders(settings, host.probes())) == {"recorder:tabdeal"}


def test_unhealthy_container_is_a_stream_problem_with_reason(host: Host, settings: Settings) -> None:
    host.states["tbot-lbank-recorder"] = container_state(
        Health={"Status": "unhealthy", "Log": [{"Output": "book stream stale: 412s"}]}
    )
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:lbank"}
    assert "book stream stale: 412s" in found[0].summary


def test_starting_health_is_tolerated_only_for_the_grace_period(host: Host, settings: Settings) -> None:
    fresh = iso(NOW - timedelta(minutes=2))
    host.states["tbot-recorder"] = container_state(StartedAt=fresh, Health={"Status": "starting"})
    assert hm.check_recorders(settings, host.probes()) == []
    old = iso(NOW - timedelta(minutes=30))
    host.states["tbot-recorder"] = container_state(StartedAt=old, Health={"Status": "starting"})
    assert keys(hm.check_recorders(settings, host.probes())) == {"recorder:tabdeal"}


def test_container_without_healthcheck_status_alerts(host: Host, settings: Settings) -> None:
    host.states["tbot-recorder"] = container_state(Health=None)
    assert keys(hm.check_recorders(settings, host.probes())) == {"recorder:tabdeal"}


def test_restart_count_increase_is_folded_into_the_recorder_alert(host: Host, settings: Settings) -> None:
    """A crash loop (RestartCount climbing) is a container-state signal; it shares the recorder's
    one key rather than minting a separate one (review MAJOR 2/3 unification; MINOR 10)."""
    host.restarts["tbot-lbank-recorder"] = 1
    assert hm.check_recorders(settings, host.probes()) == []  # first sighting: nothing to compare yet
    host.restarts["tbot-lbank-recorder"] = 3
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:lbank"}
    assert "restarted 2 time(s)" in found[0].summary
    assert "total 3" in found[0].summary


def test_restart_count_note_is_appended_to_an_existing_container_problem(
    host: Host, settings: Settings
) -> None:
    host.restarts["tbot-lbank-recorder"] = 1
    hm.check_recorders(settings, host.probes())  # first sighting
    host.restarts["tbot-lbank-recorder"] = 2
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:lbank"}  # still one key, not two
    assert "exited" in found[0].summary
    assert "restarted 1 time(s)" in found[0].summary


def test_container_and_stream_problems_share_one_key_and_never_falsely_clear_each_other(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """Review MAJOR 3: one recorder used to be two keys (container:x, stream:x); a container
    coming back healthy must not RESOLVE a key that is still open for a stale heartbeat."""
    out = Outbox()
    monitor = make_monitor(make_settings(tmp_path, confirm_cycles=1), host, out)
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]
    host.states["tbot-lbank-recorder"] = container_state()  # container is healthy again...
    stale = Path("/v/tbot-lbank-data") / "lbank/heartbeat.json"
    host.files[stale] = json.dumps({"written_ms": int((clock() - timedelta(seconds=900)).timestamp() * 1000)})
    # ...but the stream is now stale: same key, must stay open
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # no RESOLVED: the key's problem just changed shape
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []
    host.files[stale] = json.dumps({"written_ms": int((clock() - timedelta(seconds=10)).timestamp() * 1000)})
    # now genuinely healthy
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []
    clock.advance(minutes=1)
    assert [(e.kind, e.key) for e in monitor.run_cycle()] == [("resolved", "recorder:lbank")]


def test_confirm_cycles_requires_consecutive_bad_cycles_before_alerting(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    monitor = make_monitor(make_settings(tmp_path, confirm_cycles=3), host, Outbox())
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    assert monitor.run_cycle() == []  # bad cycle 1 of 3: not yet
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # bad cycle 2 of 3: still not yet
    clock.advance(minutes=1)
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]  # bad cycle 3 of 3: now it alerts


def test_confirm_cycles_streak_resets_on_a_good_cycle(host: Host, tmp_path: Path, clock: Clock) -> None:
    monitor = make_monitor(make_settings(tmp_path, confirm_cycles=2), host, Outbox())
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    assert monitor.run_cycle() == []  # bad cycle 1 of 2
    host.states["tbot-lbank-recorder"] = container_state()  # a good cycle breaks the streak
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # bad again, but this is only cycle 1 of a NEW streak
    clock.advance(minutes=1)
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]  # cycle 2 of the new streak


def test_docker_down_freezes_an_open_recorder_alert_instead_of_resolving_it(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """Review MAJOR 2: when a whole check fails (docker unreachable), every alert it owns must stay
    open -- the monitor cannot tell if the underlying problem cleared, so it must not claim it did."""
    settings = make_settings(tmp_path, confirm_cycles=1)
    monitor = make_monitor(settings, host, Outbox())
    host.states["tbot-lbank-recorder"] = container_state(Status="exited", ExitCode=1)
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]

    host.docker_down = True
    for _ in range(3):  # however long docker stays down, the open alert is neither resolved nor repeated
        clock.advance(hours=1)
        events = monitor.run_cycle()
        assert "recorder:lbank" not in {e.key for e in events if e.kind == "resolved"}

    host.docker_down = False
    host.states["tbot-lbank-recorder"] = container_state()  # healthy again once docker answers
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # resolve_cycles=2: first clean cycle
    clock.advance(minutes=1)
    assert ("resolved", "recorder:lbank") in [(e.kind, e.key) for e in monitor.run_cycle()]


# ---------------------------------------------------------------------------------------------
# Class 1: stale or erroring stream (independent heartbeat read)
# ---------------------------------------------------------------------------------------------


def test_stale_heartbeat_alerts_for_both_formats(host: Host, settings: Settings) -> None:
    host.heartbeat_ok(age_s=900)
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:tabdeal", "recorder:lbank"}
    assert all("900s old" in p.summary for p in found)


def test_heartbeat_just_inside_limit_is_fine(host: Host, settings: Settings) -> None:
    host.heartbeat_ok(age_s=290)
    assert hm.check_recorders(settings, host.probes()) == []


def test_unreadable_or_invalid_heartbeat_alerts(host: Host, settings: Settings) -> None:
    host.files[Path("/v/tbot-data") / "tabdeal/heartbeat.json"] = "{not json"
    host.files[Path("/v/tbot-lbank-data") / "lbank/heartbeat.json"] = json.dumps({"written_ms": "yesterday"})
    assert keys(hm.check_recorders(settings, host.probes())) == {"recorder:tabdeal", "recorder:lbank"}
    del host.files[Path("/v/tbot-data") / "tabdeal/heartbeat.json"]
    host.files[Path("/v/tbot-lbank-data") / "lbank/heartbeat.json"] = PermissionError("denied")
    assert keys(hm.check_recorders(settings, host.probes())) == {"recorder:tabdeal", "recorder:lbank"}


def test_unknown_docker_volume_alerts(host: Host, settings: Settings) -> None:
    del host.mounts["tbot-data"]
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"recorder:tabdeal"}
    assert "tbot-data" in found[0].summary


# ---------------------------------------------------------------------------------------------
# Class 2: backups
# ---------------------------------------------------------------------------------------------


def lbank(host: Host, settings: Settings) -> list[Problem]:
    return hm.check_lbank_backup(settings, host.probes())


def test_lbank_status_ok(host: Host, settings: Settings) -> None:
    assert lbank(host, settings) == []


def test_lbank_status_ok_false_alerts_with_exit_code_and_message(host: Host, settings: Settings) -> None:
    host.lbank_status(ok=False, exit_code=1, message="disk full")
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank"}
    assert "exit 1" in found[0].summary
    assert "disk full" in found[0].summary


def test_lbank_integrity_failure_alerts(host: Host, settings: Settings) -> None:
    host.lbank_status(ok=False, exit_code=2, integrity_ok=False, message="corrupt")
    found = lbank(host, settings)
    assert "integrity check FAILED" in found[0].summary
    host.lbank_status(ok=True, integrity_ok=False)  # integrity alone is enough
    assert "integrity check FAILED" in lbank(host, settings)[0].summary


def test_lbank_integrity_null_is_not_an_alert(host: Host, settings: Settings) -> None:
    host.lbank_status(integrity_ok=None)
    assert lbank(host, settings) == []


def test_lbank_in_progress_within_the_grace_period_is_a_silent_frozen_problem_not_nothing(
    host: Host, settings: Settings
) -> None:
    """The writer sets in_progress=true, ok=false, exit_code=null at the START of every run (so a
    killed run never leaves last night's OK behind); a normal run must not ALERT while it works.
    But this must come back as a silent, frozen Problem -- not an empty list -- so that a Monitor
    does not read "no problems" as "the previous open alert cleared" (see the Monitor-level test
    below, test_lbank_in_progress_freezes_an_open_alert_instead_of_resolving_it)."""
    host.lbank_status(
        in_progress=True,
        ok=False,
        exit_code=None,
        integrity_ok=None,
        ts=iso(NOW - timedelta(minutes=5)),
        started_ts=iso(NOW - timedelta(minutes=5)),
    )
    found = lbank(host, settings)
    assert len(found) == 1
    assert found[0].key == "backup:lbank"
    assert found[0].silent is True
    assert found[0].freeze == ("backup:lbank",)
    assert hm.collect(settings, host.probes()).problems == {}  # never surfaces as a visible problem


def test_lbank_in_progress_freezes_an_open_alert_instead_of_resolving_it(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """Monitor-level: an open backup:lbank alert must not be RESOLVED just because a new run started
    (in_progress=true is written over whatever last night's status said, per deploy/backup_lbank.py).
    Without the freeze, resolve_cycles=2 clean-looking cycles below would wrongly send RESOLVED."""
    settings = make_settings(tmp_path)
    host.lbank_status(ok=False, exit_code=1, message="disk full")
    monitor = make_monitor(settings, host, Outbox())
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]  # backup:lbank is open

    clock.advance(hours=23)
    started_ts = iso(clock())
    host.lbank_status(
        in_progress=True, ok=False, exit_code=None, integrity_ok=None, ts=started_ts, started_ts=started_ts
    )
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # cycle 1 "clean-looking": must not count towards resolving
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # cycle 2: still must not resolve (would have, unfrozen)


def test_lbank_in_progress_past_one_hour_alerts_as_stuck(host: Host, settings: Settings) -> None:
    host.lbank_status(
        in_progress=True,
        ok=False,
        exit_code=None,
        integrity_ok=None,
        ts=iso(NOW - timedelta(hours=2)),
        started_ts=iso(NOW - timedelta(hours=2)),
    )
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank"}
    assert "in_progress" in found[0].summary
    assert "2.0 h" in found[0].summary
    assert "killed or is stuck" in found[0].summary


def test_lbank_old_status_without_in_progress_field_behaves_as_before(host: Host, settings: Settings) -> None:
    """Status files written before the in_progress/started_ts fields existed must not be misread."""
    host.lbank_status()  # no 'in_progress' key at all
    assert lbank(host, settings) == []


def test_lbank_status_older_than_36h_alerts(host: Host, settings: Settings) -> None:
    host.lbank_status(ts=iso(NOW - timedelta(hours=37)))
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank"}
    assert "37 h old" in found[0].summary
    host.lbank_status(ts=iso(NOW - timedelta(hours=35)))
    assert lbank(host, settings) == []


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        json.dumps({"ts": iso(NOW)}),  # no "ok"
        json.dumps({"ts": "not-a-time", "ok": True}),
        json.dumps({"ok": True}),
    ],
)
def test_lbank_corrupt_status_alerts(host: Host, settings: Settings, content: str) -> None:
    host.files[hm.LBANK_STATUS_FILE] = content
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank"}
    assert "corrupt" in found[0].summary


def test_lbank_unreadable_status_alerts_under_its_own_key(host: Host, settings: Settings) -> None:
    host.files[hm.LBANK_STATUS_FILE] = PermissionError("denied")
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank:unreadable"}
    assert "unreadable (PermissionError)" in found[0].summary


def test_lbank_status_not_created_yet_is_quiet_during_the_grace_period(
    host: Host, settings: Settings, clock: Clock
) -> None:
    """The writer may not be deployed yet: no alert at once (and never an 'unreadable' alert)..."""
    del host.files[hm.LBANK_STATUS_FILE]
    assert lbank(host, settings) == []  # first sighting starts the clock
    clock.advance(hours=35)
    assert lbank(host, settings) == []


def test_lbank_status_never_appearing_alerts_after_the_max_age(
    host: Host, settings: Settings, clock: Clock
) -> None:
    """...but a backup that never reports must not pass silently either."""
    del host.files[hm.LBANK_STATUS_FILE]
    lbank(host, settings)
    clock.advance(hours=37)
    found = lbank(host, settings)
    assert keys(found) == {"backup:lbank:nostatus"}
    assert "never reported" in found[0].summary
    assert "37 h" in found[0].summary


def test_lbank_status_that_vanishes_is_measured_from_the_last_time_it_was_seen(
    host: Host, settings: Settings, clock: Clock
) -> None:
    assert lbank(host, settings) == []  # status present: remembered
    del host.files[hm.LBANK_STATUS_FILE]
    clock.advance(hours=30)
    assert lbank(host, settings) == []
    clock.advance(hours=7)
    assert keys(lbank(host, settings)) == {"backup:lbank:nostatus"}
    host.lbank_status()  # the writer runs again with a fresh status
    assert lbank(host, settings) == []


def test_lbank_real_failure_after_the_nostatus_alert_is_a_new_alert_not_a_reminder(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    out = Outbox()
    monitor = make_monitor(make_settings(tmp_path), host, out)
    del host.files[hm.LBANK_STATUS_FILE]
    monitor.run_cycle()
    clock.advance(hours=37)
    host.trades_backup()  # keep the unrelated trades backup fresh
    assert [(e.kind, e.key) for e in monitor.run_cycle()] == [("alert", "backup:lbank:nostatus")]
    clock.advance(minutes=1)
    host.lbank_status(ok=False, exit_code=1, message="disk full")  # the writer finally runs, and fails
    assert [(e.kind, e.key) for e in monitor.run_cycle()] == [("alert", "backup:lbank")]
    assert "ALERT backup:lbank\n" in out.sent[-1]
    assert "disk full" in out.sent[-1]


def test_trades_backup_fresh_ok_and_stale_alerts(host: Host, settings: Settings) -> None:
    assert hm.check_trades_backup(settings, host.probes()) == []
    host.trades_backup(age_h=40)
    found = hm.check_trades_backup(settings, host.probes())
    assert keys(found) == {"backup:trades"}
    assert "40 h old" in found[0].summary


def test_trades_backup_uses_the_newest_file(host: Host, settings: Settings) -> None:
    old = (NOW - timedelta(hours=100)).strftime("%Y%m%dT%H%M%SZ")
    new = (NOW - timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
    host.dirs[hm.TRADES_BACKUP_DIR] = [f"trades-{old}.sqlite", f"trades-{new}.sqlite"]
    assert hm.check_trades_backup(settings, host.probes()) == []


def test_trades_backup_missing_dir_or_no_files_alerts(host: Host, settings: Settings) -> None:
    host.dirs[hm.TRADES_BACKUP_DIR] = ["README", "trades-garbage.sqlite"]
    assert "no trades backup file" in hm.check_trades_backup(settings, host.probes())[0].summary
    del host.dirs[hm.TRADES_BACKUP_DIR]
    assert keys(hm.check_trades_backup(settings, host.probes())) == {"backup:trades"}


def test_failed_backup_units_alert(host: Host, settings: Settings) -> None:
    host.failed_units = {"tbot-trades-backup.service", "tbot-lbank-backup.service"}
    found = hm.check_units(settings, host.probes())
    assert keys(found) == {
        "backup:unit:tbot-trades-backup.service",
        "backup:unit:tbot-lbank-backup.service",
    }
    assert ("systemctl", "is-failed", "tbot-lbank-backup.service") in host.calls


def test_inactive_units_do_not_alert(host: Host, settings: Settings) -> None:
    assert hm.check_units(settings, host.probes()) == []


@pytest.mark.parametrize(
    "state", ["active", "inactive", "activating", "deactivating", "reloading", "maintenance"]
)
def test_every_documented_clear_state_does_not_alert(host: Host, settings: Settings, state: str) -> None:
    def clear_run(_args: Sequence[str], _s: str = state) -> CmdResult:
        return CmdResult(1, f"{_s}\n", "")

    probes = replace(host.probes(), run=clear_run)
    assert hm.check_units(settings, probes) == []


@pytest.mark.parametrize(
    "stdout",
    ["", "unknown", "Failed to connect to bus: No such file or directory", "\n", "garbage output"],
)
def test_ambiguous_systemctl_output_is_a_silent_frozen_problem_not_clear(
    host: Host, settings: Settings, stdout: str
) -> None:
    """Review MAJOR: check_units may treat a unit as clear only when the output is one of the known
    states. Anything else (empty stdout, "unknown", a bus-connection failure, garbage) must freeze
    that one unit's key rather than being silently read as "not failed"."""
    base_run = host.run

    def ambiguous_run(args: Sequence[str], _o: str = stdout) -> CmdResult:
        return CmdResult(1, _o, "") if args[0] == "systemctl" else base_run(args)

    probes = replace(host.probes(), run=ambiguous_run)
    found = hm.check_units(settings, probes)
    assert len(found) == len(hm.BACKUP_UNITS)
    for problem, unit in zip(found, hm.BACKUP_UNITS, strict=True):
        assert problem.key == f"backup:unit:{unit}"
        assert problem.silent is True
        assert problem.freeze == (problem.key,)
    assert hm.collect(settings, probes).problems == {}  # never surfaces as a visible problem


def test_systemctl_ambiguous_output_freezes_that_units_alert_until_resolved(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """Monitor-level: an open unit alert must stay open -- neither RESOLVED nor repeated -- for as
    long as systemctl answers ambiguously, then resolve normally once it is confirmed healthy again.
    Removing the freeze=(key,) added to check_units's ambiguous-output branch makes this fail."""
    settings = make_settings(tmp_path)
    host.failed_units = {"tbot-trades-backup.service"}
    base_run = host.run
    ambiguous = {"on": False}

    def flaky_run(args: Sequence[str]) -> CmdResult:
        if ambiguous["on"] and tuple(args) == ("systemctl", "is-failed", "tbot-trades-backup.service"):
            return CmdResult(3, "", "Failed to connect to bus: No such file or directory")
        return base_run(args)

    probes = replace(host.probes(), run=flaky_run)
    out = Outbox()
    monitor = Monitor(settings, probes, out, StateStore(settings.state_dir / "state.json"))
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]  # the unit is genuinely failed

    host.failed_units = set()  # the unit actually recovered...
    ambiguous["on"] = True  # ...but systemctl itself answers ambiguously right now
    clock.advance(hours=1)
    assert monitor.run_cycle() == []  # neither RESOLVED nor a reminder: inconclusive, not healthy
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []

    ambiguous["on"] = False
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # resolve_cycles=2: first genuinely clean cycle
    clock.advance(minutes=1)
    assert [(e.kind, e.key) for e in monitor.run_cycle()] == [
        ("resolved", "backup:unit:tbot-trades-backup.service")
    ]


def test_disks_check_crashing_freezes_open_disk_alerts_instead_of_resolving_them(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """Monitor-level: if the named 'disks' check itself crashes, its open alerts must stay open --
    the monitor cannot tell whether the underlying disk problem cleared. Removing "disks": ("disk:",)
    from CHECK_KEY_PREFIXES (or the freeze handling in collect()) makes this fail."""
    settings = make_settings(tmp_path, confirm_cycles=1)
    host.disks[BACKUP_DIR] = DiskUsage(95 * GIB, 5 * GIB, 2)
    monitor = make_monitor(settings, host, Outbox())
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]  # disk:<BACKUP_DIR> is open

    class BoomDisk:
        def __call__(self, _path: Path) -> DiskUsage | None:
            raise RuntimeError("disk probe exploded")

    monitor._probes = replace(monitor._probes, disk=BoomDisk())
    for _ in range(3):  # however long the check keeps crashing, the open alert is never resolved
        clock.advance(hours=1)
        events = monitor.run_cycle()
        assert "disk:" + str(BACKUP_DIR) not in {e.key for e in events if e.kind == "resolved"}

    monitor._probes = replace(monitor._probes, disk=host.disk)
    host.disks[BACKUP_DIR] = DiskUsage(10 * GIB, 90 * GIB, 2)  # genuinely healthy again
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # resolve_cycles=2: first clean cycle
    clock.advance(minutes=1)
    assert ("resolved", "disk:" + str(BACKUP_DIR)) in [(e.kind, e.key) for e in monitor.run_cycle()]


# ---------------------------------------------------------------------------------------------
# Missing or broken docker / systemctl are alerts, not crashes
# ---------------------------------------------------------------------------------------------


def test_missing_docker_is_one_tool_problem(host: Host, settings: Settings) -> None:
    host.tool_missing = {"docker"}
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"tool:docker"}
    assert "not installed" in found[0].summary


def test_docker_daemon_down_is_one_tool_problem(host: Host, settings: Settings) -> None:
    host.docker_down = True
    found = hm.check_recorders(settings, host.probes())
    assert keys(found) == {"tool:docker"}
    assert "Cannot connect" in found[0].summary


def test_missing_systemctl_is_a_tool_problem(host: Host, settings: Settings) -> None:
    host.tool_missing = {"systemctl"}
    assert keys(hm.check_units(settings, host.probes())) == {"tool:systemctl"}


def test_missing_tools_do_not_stop_the_other_checks(host: Host, settings: Settings) -> None:
    host.tool_missing = {"docker", "systemctl"}
    host.disks[BACKUP_DIR] = DiskUsage(95 * GIB, 5 * GIB, 2)
    found = hm.collect_problems(settings, host.probes())
    assert keys(found) == {"tool:docker", "tool:systemctl", "disk:" + str(BACKUP_DIR)}


def test_a_crashing_check_becomes_a_monitor_problem(host: Host, settings: Settings) -> None:
    def boom(_s: Settings, _p: Probes) -> list[Problem]:
        raise RuntimeError("secret-ish detail that must not leak")

    found = hm.collect_problems(settings, host.probes(), checks=(("boom", boom), ("disks", hm.check_disks)))
    assert keys(found) == {"monitor:boom"}
    assert "RuntimeError" in found["monitor:boom"].summary
    assert "secret-ish" not in found["monitor:boom"].summary


def test_run_command_missing_binary_raises_tool_error() -> None:
    with pytest.raises(hm.ToolError) as info:
        hm.run_command(["definitely-not-an-installed-binary-xyz"])
    assert "not installed" in info.value.reason


# ---------------------------------------------------------------------------------------------
# No secrets reach docker / systemctl: the scrubbed child environment
# ---------------------------------------------------------------------------------------------


def test_child_env_drops_everything_except_the_allow_list() -> None:
    fake_environ = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "TBOT_TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TBOT_TELEGRAM_CHAT_ID": FAKE_CHAT,
        "TBOT_DEADMAN_URL": "https://example.com/ping/secret",
        "SOME_RANDOM_VAR": "x",
    }
    env = hm._child_env(fake_environ)
    assert env == {"PATH": "/usr/bin", "HOME": "/root"}


def test_child_env_falls_back_to_a_default_path_when_unset() -> None:
    env = hm._child_env({})
    assert env["PATH"] == hm._DEFAULT_PATH


def test_run_command_passes_only_the_scrubbed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)

        class Done:
            returncode, stdout, stderr = 0, "", ""

        return Done()

    monkeypatch.setattr(os, "environ", {"PATH": "/bin", hm.ENV_TOKEN: FAKE_TOKEN})
    monkeypatch.setattr(subprocess, "run", fake_run)
    hm.run_command(["docker", "ps"])
    assert seen["env"] == {"PATH": "/bin"}
    assert FAKE_TOKEN not in str(seen["env"])


# ---------------------------------------------------------------------------------------------
# _read_text: O_NOFOLLOW, regular-file check, size cap
# ---------------------------------------------------------------------------------------------


def test_read_text_reads_a_small_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "small.json"
    path.write_text("hello", encoding="utf-8")
    assert hm._read_text(path) == "hello"


def test_read_text_refuses_a_file_above_the_size_cap(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_text("x" * 10, encoding="utf-8")
    with pytest.raises(OSError):
        hm._read_text(path, max_bytes=5)


def test_read_text_refuses_a_symlink_as_the_final_component(tmp_path: Path) -> None:
    if not hasattr(os, "symlink") or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform has no symlink / O_NOFOLLOW support for this check")
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("creating a symlink is not permitted in this environment")
    with pytest.raises(OSError):
        hm._read_text(link)


# ---------------------------------------------------------------------------------------------
# Alert hygiene: dedupe, reminder, resolved
# ---------------------------------------------------------------------------------------------


def test_new_problem_alerts_once_and_is_deduplicated(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-trades-backup.service"}
    first = monitor.run_cycle()
    assert [e.kind for e in first] == ["alert"]
    for _ in range(5):
        clock.advance(minutes=1)
        assert monitor.run_cycle() == []
    assert len(out.sent) == 1
    text = out.sent[0]
    assert "ALERT backup:unit:tbot-trades-backup.service" in text
    assert text.startswith("[testhost]")
    assert text.rstrip().endswith("UTC")
    assert "2026-10-10 12:00:00 UTC" in text


def test_reminder_every_repeat_hours_not_before(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-lbank-backup.service"}
    monitor.run_cycle()
    clock.advance(hours=5, minutes=59)
    assert monitor.run_cycle() == []
    clock.advance(minutes=2)
    assert [e.kind for e in monitor.run_cycle()] == ["reminder"]
    assert "REMINDER" in out.sent[-1]
    clock.advance(hours=3)
    assert monitor.run_cycle() == []
    clock.advance(hours=3)
    assert [e.kind for e in monitor.run_cycle()] == ["reminder"]
    assert len(out.sent) == 3


def test_resolved_after_the_problem_clears(host: Host, tmp_path: Path, clock: Clock) -> None:
    out = Outbox()
    monitor = make_monitor(make_settings(tmp_path, confirm_cycles=1), host, out)
    host.disks[BACKUP_DIR] = DiskUsage(90 * GIB, 10 * GIB, 2)
    monitor.run_cycle()
    host.disks[BACKUP_DIR] = DiskUsage(10 * GIB, 90 * GIB, 2)
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # first clear cycle: wait (flap guard)
    clock.advance(minutes=1)
    assert [e.kind for e in monitor.run_cycle()] == ["resolved"]
    assert "RESOLVED disk:" in out.sent[-1]
    assert "after 2 min" in out.sent[-1]
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # and it is not announced again
    assert len(out.sent) == 2


def test_flapping_problem_does_not_resolve_or_realert(host: Host, tmp_path: Path, clock: Clock) -> None:
    out = Outbox()
    monitor = make_monitor(make_settings(tmp_path, confirm_cycles=1), host, out)
    bad, good = DiskUsage(90 * GIB, 10 * GIB, 2), DiskUsage(10 * GIB, 90 * GIB, 2)
    host.disks[BACKUP_DIR] = bad
    monitor.run_cycle()
    for disk in (good, bad, good, bad):
        host.disks[BACKUP_DIR] = disk
        clock.advance(minutes=1)
        assert monitor.run_cycle() == []
    assert len(out.sent) == 1


def test_each_problem_key_is_tracked_separately(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-trades-backup.service"}
    monitor.run_cycle()
    host.failed_units.add("tbot-lbank-backup.service")
    clock.advance(minutes=1)
    sent = monitor.run_cycle()
    assert [e.key for e in sent] == ["backup:unit:tbot-lbank-backup.service"]


def test_state_survives_a_restart_without_realerting(host: Host, settings: Settings, clock: Clock) -> None:
    host.failed_units = {"tbot-trades-backup.service"}
    out = Outbox()
    make_monitor(settings, host, out).run_cycle()
    assert len(out.sent) == 1
    clock.advance(minutes=10)
    out2 = Outbox()
    restarted = make_monitor(settings, host, out2)
    assert restarted.run_cycle() == []
    assert out2.sent == []
    host.failed_units = set()  # and the restarted instance still announces the recovery
    for _ in range(2):
        clock.advance(minutes=1)
        restarted.run_cycle()
    assert "RESOLVED" in out2.sent[-1]


def test_restart_counts_persist_across_a_process_restart(
    host: Host, settings: Settings, clock: Clock
) -> None:
    """A real restart replaces the whole process (fresh Memory); only the persisted state survives.
    The crash-loop count must come from there, not be lost on every restart."""
    host.restarts["tbot-lbank-recorder"] = 1
    make_monitor(settings, host, Outbox()).run_cycle()  # first sighting: remembered, no note yet
    clock.advance(minutes=1)
    fresh_host = Host(clock)  # a brand-new Host/Memory, as if the monitor process restarted
    fresh_host.restarts["tbot-lbank-recorder"] = 2
    out = Outbox()
    events = make_monitor(settings, fresh_host, out).run_cycle()
    assert [(e.kind, e.key) for e in events] == [("alert", "recorder:lbank")]
    assert "restarted 1 time(s)" in out.sent[-1]
    assert "total 2" in out.sent[-1]


def test_lbank_nostatus_grace_anchor_persists_across_a_process_restart(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    settings = make_settings(tmp_path)
    del host.files[hm.LBANK_STATUS_FILE]
    make_monitor(settings, host, Outbox()).run_cycle()  # first sighting: anchor persisted at NOW
    clock.advance(hours=35)
    fresh_host = Host(clock)
    del fresh_host.files[hm.LBANK_STATUS_FILE]
    out = Outbox()
    # a fresh process at 35 h must still be quiet (the grace clock did not restart with the process)
    assert make_monitor(settings, fresh_host, out).run_cycle() == []
    clock.advance(hours=2)
    out2 = Outbox()
    events = make_monitor(settings, fresh_host, out2).run_cycle()
    assert [(e.kind, e.key) for e in events] == [("alert", "backup:lbank:nostatus")]


def test_at_most_eight_messages_per_cycle(host: Host, tmp_path: Path, clock: Clock) -> None:
    paths = tuple(Path(f"/mnt/d{i}") for i in range(12))
    for i, path in enumerate(paths):
        host.disks[path] = DiskUsage(99 * GIB, 1 * GIB, 100 + i)
    settings = make_settings(tmp_path, disk_paths=paths, confirm_cycles=1)
    out = Outbox()
    monitor = make_monitor(settings, host, out)
    assert len(monitor.run_cycle()) == 8
    assert len(monitor.run_cycle()) == 4
    assert monitor.run_cycle() == []
    assert len(out.sent) == 12


def test_restart_note_survives_a_telegram_backoff_until_actually_sent(
    host: Host, tmp_path: Path, clock: Clock
) -> None:
    """The note must not be "consumed" just because a cycle observed the new RestartCount: it has
    to survive until a message mentioning it is actually delivered (review MINOR, restart-note
    pending-until-sent)."""
    settings = make_settings(tmp_path)
    host.restarts["tbot-lbank-recorder"] = 1
    make_monitor(settings, host, Outbox()).run_cycle()  # bootstrap: baseline=1, no note possible yet

    out = Outbox()
    out.fail = TelegramError("timeout")
    store = StateStore(settings.state_dir / "state.json")
    monitor = Monitor(settings, host.probes(), out, store)
    host.restarts["tbot-lbank-recorder"] = 2
    clock.advance(minutes=1)
    assert monitor.run_cycle() == []  # alert attempted, send failed: nothing committed

    out.fail = None
    clock.advance(seconds=70)  # past the back-off window
    host.restarts["tbot-lbank-recorder"] = 3  # a second restart while the first note was still pending
    events = monitor.run_cycle()
    assert [(e.kind, e.key) for e in events] == [("alert", "recorder:lbank")]
    assert "restarted 2 time(s)" in out.sent[-1]  # cumulative: 1 -> 3, the first tick was not lost
    assert "total 3" in out.sent[-1]


def test_restart_note_survives_the_per_cycle_message_cap(host: Host, tmp_path: Path, clock: Clock) -> None:
    settings = make_settings(tmp_path, confirm_cycles=1)
    host.restarts["tbot-lbank-recorder"] = 1
    make_monitor(settings, host, Outbox()).run_cycle()  # bootstrap: baseline=1, no note, no other alerts

    paths = tuple(Path(f"/mnt/d{i}") for i in range(8))
    for i, path in enumerate(paths):
        host.disks[path] = DiskUsage(99 * GIB, 1 * GIB, 100 + i)
    settings2 = make_settings(tmp_path, disk_paths=paths, confirm_cycles=1)  # same state_dir: continuity

    host.restarts["tbot-lbank-recorder"] = 2
    out = Outbox()
    monitor = Monitor(settings2, host.probes(), out, StateStore(settings2.state_dir / "state.json"))
    clock.advance(minutes=1)
    first = monitor.run_cycle()
    assert len(first) == 8  # the 8 disk alerts fill the cap; recorder:lbank's alert is dropped
    assert "recorder:lbank" not in {e.key for e in first}

    host.restarts["tbot-lbank-recorder"] = 3  # a second restart while the first note was still pending
    clock.advance(minutes=1)
    second = monitor.run_cycle()
    assert [(e.kind, e.key) for e in second] == [("alert", "recorder:lbank")]
    assert "restarted 2 time(s)" in out.sent[-1]  # cumulative: 1 -> 3, the capped tick was not lost
    assert "total 3" in out.sent[-1]


# ---------------------------------------------------------------------------------------------
# Telegram failures never crash the loop
# ---------------------------------------------------------------------------------------------


def test_telegram_failure_keeps_the_alert_pending_and_backs_off(
    host: Host, settings: Settings, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    out = Outbox()
    out.fail = TelegramError("http error", 502)
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-trades-backup.service"}
    with caplog.at_level(logging.ERROR, logger="tbot.health_monitor"):
        assert monitor.run_cycle() == []
    assert "telegram send failed" in caplog.text
    assert "HTTP 502" in caplog.text
    assert out.calls == 1

    clock.advance(seconds=30)  # inside the back-off window: no new attempt
    assert monitor.run_cycle() == []
    assert out.calls == 1

    out.fail = None  # Telegram is back; the next attempt after the back-off delivers the alert
    clock.advance(seconds=40)
    assert [e.kind for e in monitor.run_cycle()] == ["alert"]
    assert len(out.sent) == 1


def test_backoff_doubles_and_is_capped(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    out.fail = TelegramError("timeout")
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-trades-backup.service"}
    gaps = []
    for _ in range(8):
        before = out.calls
        monitor.run_cycle()
        assert out.calls == before + 1
        gap = (monitor._next_send_at - clock()).total_seconds()  # type: ignore[operator]
        gaps.append(gap)
        clock.advance(seconds=gap)
    assert gaps[:4] == [60.0, 120.0, 240.0, 480.0]
    assert max(gaps) == hm._MAX_BACKOFF_S


def test_unexpected_sender_exception_does_not_crash(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    out.fail = RuntimeError("anything")
    monitor = make_monitor(settings, host, out)
    host.failed_units = {"tbot-trades-backup.service"}
    assert monitor.run_cycle() == []


def test_failed_alert_is_not_persisted_as_sent(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    out.fail = TelegramError("timeout")
    host.failed_units = {"tbot-trades-backup.service"}
    make_monitor(settings, host, out).run_cycle()
    out2 = Outbox()
    clock.advance(minutes=5)
    assert [e.kind for e in make_monitor(settings, host, out2).run_cycle()] == ["alert"]


def test_run_forever_survives_a_crashing_cycle() -> None:
    stop = threading.Event()

    class Flaky:
        def __init__(self) -> None:
            self.n = 0

        def run_cycle(self) -> list[Any]:
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            stop.set()
            return []

    flaky = Flaky()
    hm.run_forever(flaky, 0.0, stop)  # type: ignore[arg-type]
    assert flaky.n == 2


# ---------------------------------------------------------------------------------------------
# Optional dead-man ping: never logs the URL, never fatal, only after a clean cycle
# ---------------------------------------------------------------------------------------------


_DEADMAN_URL = "https://hc-ping.com/super-secret-id"


def test_deadman_pinger_sends_a_get_and_the_url_never_appears_in_repr_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0.0) -> FakeResponse:
        seen.append(request)
        return FakeResponse(200, b"OK")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pinger = hm.DeadmanPinger(_DEADMAN_URL)
    assert _DEADMAN_URL not in repr(pinger)
    with caplog.at_level(logging.DEBUG):
        assert pinger() is True
    assert len(seen) == 1
    assert seen[0].get_method() == "GET"
    assert seen[0].full_url == _DEADMAN_URL
    assert _DEADMAN_URL not in caplog.text


def test_deadman_pinger_ignores_failures_and_never_logs_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: float = 0.0) -> FakeResponse:
        raise TimeoutError(f"timed out talking to {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pinger = hm.DeadmanPinger(_DEADMAN_URL)
    with caplog.at_level(logging.WARNING):
        assert pinger() is False  # ignored: does not raise
    assert _DEADMAN_URL not in caplog.text
    assert "dead-man ping failed" in caplog.text


def test_run_forever_pings_only_after_a_clean_cycle_and_never_lets_a_broken_ping_stop_it() -> None:
    stop = threading.Event()
    calls = {"cycles": 0, "pings": 0}

    class OnceThenStop:
        def run_cycle(self) -> list[Any]:
            calls["cycles"] += 1
            if calls["cycles"] == 2:
                stop.set()
                raise RuntimeError("the cycle itself crashed: no ping should follow")
            return []

    def ping() -> bool:
        calls["pings"] += 1
        if calls["pings"] == 1:
            raise RuntimeError("a broken ping must not stop the loop")
        return True

    hm.run_forever(OnceThenStop(), 0.0, stop, ping)  # type: ignore[arg-type]
    assert calls["cycles"] == 2  # a crashing cycle still happened, but got no ping afterwards
    assert calls["pings"] == 1  # only after the one clean cycle


# ---------------------------------------------------------------------------------------------
# Start notice
# ---------------------------------------------------------------------------------------------


def test_start_notice_sent_once(host: Host, settings: Settings) -> None:
    out = Outbox()
    make_monitor(settings, host, out).start()
    assert len(out.sent) == 1
    assert "INFO monitor started" in out.sent[0]
    assert "[testhost]" in out.sent[0]


def test_start_notice_suppressed_in_rapid_restart_loop(host: Host, settings: Settings, clock: Clock) -> None:
    out = Outbox()
    make_monitor(settings, host, out).start()
    clock.advance(seconds=60)
    make_monitor(settings, host, out).start()
    assert len(out.sent) == 1
    clock.advance(minutes=10)
    make_monitor(settings, host, out).start()
    assert len(out.sent) == 2


def test_start_notice_failure_is_not_fatal_and_not_recorded(host: Host, settings: Settings) -> None:
    out = Outbox()
    out.fail = TelegramError("connect error")
    make_monitor(settings, host, out).start()
    out.fail = None
    make_monitor(settings, host, out).start()
    assert len(out.sent) == 1


# ---------------------------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------------------------


def test_state_store_round_trip_and_atomic_write(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "sub" / "state.json")
    state = hm.MonitorState({"k": hm.Tracked("a", "b", "summary", 1)}, "2026-01-01T00:00:00+00:00")
    store.save(state)
    loaded = store.load()
    assert loaded.tracked["k"].summary == "summary"
    assert loaded.tracked["k"].clear_count == 1
    assert loaded.last_start_notice_ts == "2026-01-01T00:00:00+00:00"
    assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["state.json"]  # no temp leftovers


def test_state_store_missing_or_corrupt_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert StateStore(path).load().tracked == {}
    path.write_text("{ truncated", encoding="utf-8")
    assert StateStore(path).load().tracked == {}
    path.write_text(json.dumps({"tracked": {"k": {"nope": 1}}}), encoding="utf-8")
    assert StateStore(path).load().tracked == {}


def test_state_store_write_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    StateStore(blocker / "state.json").save(hm.MonitorState())  # parent is a file -> OSError, logged


# ---------------------------------------------------------------------------------------------
# Settings and credentials
# ---------------------------------------------------------------------------------------------


def test_load_settings_defaults() -> None:
    s = hm.load_settings({}, hostname="h")
    assert (s.interval_s, s.disk_max_pct, s.repeat_hours, s.backup_max_age_hours) == (60.0, 80.0, 6.0, 36.0)
    assert s.resolve_cycles == 2
    assert s.confirm_cycles == 2
    assert s.disk_clear_margin == 2.0
    assert s.deadman_url == ""
    assert s.state_dir == Path("/var/lib/tbot-health-monitor")
    assert s.lbank_status_file == Path("/var/backups/tbot/lbank/status.json")
    assert s.hostname == "h"


def test_load_settings_confirm_cycles_and_disk_clear_margin() -> None:
    s = hm.load_settings({"TBOT_MONITOR_CONFIRM_CYCLES": "4", "TBOT_MONITOR_DISK_CLEAR_MARGIN": "5"})
    assert s.confirm_cycles == 4
    assert s.disk_clear_margin == 5.0


def test_load_settings_deadman_url_accepts_http_and_https_only() -> None:
    s = hm.load_settings({"TBOT_DEADMAN_URL": "https://hc-ping.com/abc-123"})
    assert s.deadman_url == "https://hc-ping.com/abc-123"
    assert "abc-123" not in repr(s)  # the field is repr=False: never printed by accident


def test_load_settings_rejects_a_non_http_deadman_url_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        s = hm.load_settings({"TBOT_DEADMAN_URL": "ftp://example.com/x"})
    assert s.deadman_url == ""
    assert "not an http" in caplog.text


def test_load_settings_overrides_and_bad_values() -> None:
    s = hm.load_settings(
        {
            "TBOT_MONITOR_DISK_MAX_PCT": "70",
            "TBOT_MONITOR_REPEAT_HOURS": "2",
            "TBOT_MONITOR_INTERVAL_S": "abc",  # ignored -> default
            "TBOT_MONITOR_HEARTBEAT_MAX_AGE_S": "1",  # below minimum -> default
            "TBOT_MONITOR_DISK_PATHS": "/, /data ,",
            "TBOT_MONITOR_STATE_DIR": "/tmp/x",
        }
    )
    assert s.disk_max_pct == 70.0
    assert s.repeat_hours == 2.0
    assert s.interval_s == 60.0
    assert s.heartbeat_max_age_s == 300.0
    assert s.disk_paths == (Path("/"), Path("/data"))
    assert s.state_dir == Path("/tmp/x")


def test_load_credentials_ok() -> None:
    assert hm.load_credentials({hm.ENV_TOKEN: FAKE_TOKEN, hm.ENV_CHAT_ID: "-1001234567890"}) == (
        FAKE_TOKEN,
        "-1001234567890",
    )


@pytest.mark.parametrize(
    "env",
    [
        {},
        {hm.ENV_TOKEN: FAKE_TOKEN},
        {hm.ENV_CHAT_ID: FAKE_CHAT},
        {hm.ENV_TOKEN: "not-a-token-FAKE", hm.ENV_CHAT_ID: FAKE_CHAT},
        {hm.ENV_TOKEN: FAKE_TOKEN, hm.ENV_CHAT_ID: "@channelname"},
    ],
)
def test_load_credentials_rejects_bad_input_without_echoing_it(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as info:
        hm.load_credentials(env)
    for value in env.values():
        assert value not in str(info.value)


# ---------------------------------------------------------------------------------------------
# Secret handling: the bot token never reaches logs, exceptions, repr or tracebacks
# ---------------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _n: int = -1) -> bytes:
        return self._body


def _http_error(request: urllib.request.Request) -> Exception:
    return urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"nope"))  # type: ignore[arg-type]


def _timeout(request: urllib.request.Request) -> Exception:
    return TimeoutError(f"timed out talking to {request.full_url}")


def _url_error_timeout(request: urllib.request.Request) -> Exception:
    return urllib.error.URLError(TimeoutError(f"read timed out {request.full_url}"))


def _connect_error(request: urllib.request.Request) -> Exception:
    return urllib.error.URLError(socket.gaierror(f"cannot resolve host for {request.full_url}"))


def _os_error(request: urllib.request.Request) -> Exception:
    return ConnectionResetError(f"reset while fetching {request.full_url}")


FAILURES = {
    "http_error": (_http_error, "HTTP 401"),
    "timeout": (_timeout, "timeout"),
    "url_error_timeout": (_url_error_timeout, "timeout"),
    "connect_error": (_connect_error, "connect error"),
    "os_error": (_os_error, "ConnectionResetError"),
}


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> list[urllib.request.Request]:
    seen: list[urllib.request.Request] = []

    def fake(request: urllib.request.Request, timeout: float = 0.0) -> FakeResponse:
        seen.append(request)
        result = behaviour(request) if callable(behaviour) else behaviour
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FakeResponse)
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Raw log text plus every record rendered through the production JSON formatter."""
    formatter = hm.JsonFormatter()
    rendered = "\n".join(formatter.format(r) for r in caplog.records)
    return caplog.text + "\n" + rendered


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_token_never_appears_in_telegram_error(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    make_exc, expected = FAILURES[name]
    patch_urlopen(monkeypatch, make_exc)
    with pytest.raises(TelegramError) as info:
        TelegramSender(FAKE_TOKEN, FAKE_CHAT)("hello")
    exc = info.value
    rendered = "\n".join(
        [str(exc), repr(exc), "".join(traceback.format_exception(exc)), exc.reason, str(exc.args)]
    )
    assert FAKE_TOKEN not in rendered
    assert "bot123456789" not in rendered
    assert expected in rendered
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_token_never_in_telegram_error_for_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    for body in (b"<html>gateway</html>", b'{"ok": false, "description": "Unauthorized"}', b"[1, 2]"):
        patch_urlopen(monkeypatch, FakeResponse(200, body))
        with pytest.raises(TelegramError) as info:
            TelegramSender(FAKE_TOKEN, FAKE_CHAT)("hello")
        rendered = str(info.value) + repr(info.value) + "".join(traceback.format_exception(info.value))
        assert FAKE_TOKEN not in rendered
        assert "HTTP 200" in rendered


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_token_never_appears_in_logs_when_sending_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    host: Host,
    settings: Settings,
    name: str,
) -> None:
    make_exc, expected = FAILURES[name]
    patch_urlopen(monkeypatch, make_exc)
    monitor = Monitor(
        settings,
        host.probes(),
        TelegramSender(FAKE_TOKEN, FAKE_CHAT),
        StateStore(settings.state_dir / "state.json"),
    )
    host.failed_units = {"tbot-trades-backup.service"}
    with caplog.at_level(logging.DEBUG):
        monitor.start()
        monitor.run_cycle()
    text = all_log_text(caplog)
    assert "telegram send failed" in text
    assert expected in text
    assert FAKE_TOKEN not in text
    assert "api.telegram.org" not in text
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out + captured.err


def test_token_never_appears_in_logs_for_malformed_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, host: Host, settings: Settings
) -> None:
    patch_urlopen(monkeypatch, FakeResponse(200, b"<html>not json</html>"))
    monitor = Monitor(
        settings,
        host.probes(),
        TelegramSender(FAKE_TOKEN, FAKE_CHAT),
        StateStore(settings.state_dir / "state.json"),
    )
    host.failed_units = {"tbot-trades-backup.service"}
    with caplog.at_level(logging.DEBUG):
        monitor.run_cycle()
    text = all_log_text(caplog)
    assert "malformed response" in text
    assert FAKE_TOKEN not in text


def test_successful_send_uses_get_with_params_and_logs_no_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, host: Host, settings: Settings
) -> None:
    seen = patch_urlopen(monkeypatch, FakeResponse(200, b'{"ok": true, "result": {}}'))
    monitor = Monitor(
        settings,
        host.probes(),
        TelegramSender(FAKE_TOKEN, FAKE_CHAT),
        StateStore(settings.state_dir / "state.json"),
    )
    host.failed_units = {"tbot-trades-backup.service"}
    with caplog.at_level(logging.DEBUG):
        monitor.start()
        sent = monitor.run_cycle()
    assert len(sent) == 1
    assert len(seen) == 2
    request = seen[1]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url.startswith(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage?")
    query = request.full_url.split("?", 1)[1]
    assert f"chat_id={FAKE_CHAT}" in query
    assert "disable_notification=false" in query
    assert "text=" in query
    assert FAKE_TOKEN not in all_log_text(caplog)


def test_message_text_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = patch_urlopen(monkeypatch, FakeResponse(200, b'{"ok": true}'))
    TelegramSender(FAKE_TOKEN, FAKE_CHAT)("x" * 5000)
    assert len(seen[0].full_url) < 1500


def test_sender_repr_is_masked() -> None:
    sender = TelegramSender(FAKE_TOKEN, FAKE_CHAT)
    for text in (repr(sender), str(sender), f"{sender!r}", f"{sender}"):
        assert FAKE_TOKEN not in text
        assert FAKE_CHAT not in text
        assert "***" in text


def test_redact_removes_registered_tokens_url_segments_and_token_shapes() -> None:
    hm.register_secret(FAKE_TOKEN)
    assert hm.redact(f"failed for {FAKE_TOKEN} here") == "failed for *** here"
    assert FAKE_TOKEN not in hm.redact(f"GET https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage?x=1")
    assert "/bot***/sendMessage" in hm.redact("https://api.telegram.org/botANYTHING-HERE/sendMessage")
    other = "987654321:BBotherTokenotherTokenother12"
    assert other not in hm.redact(f"token {other} leaked")


def test_json_formatter_never_renders_tracebacks(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("tbot.health_monitor")
    try:
        raise ValueError(f"url was https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    except ValueError:
        with caplog.at_level(logging.ERROR, logger="tbot.health_monitor"):
            logger.exception("something failed")
    record = caplog.records[-1]
    line = hm.JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["exc_type"] == "ValueError"
    assert payload["msg"] == "something failed"
    assert FAKE_TOKEN not in line
    assert "Traceback" not in line


def test_setup_logging_quietens_http_libraries_and_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "urllib3")}
    try:
        hm.setup_logging()
        for name in saved_levels:
            assert logging.getLogger(name).level == logging.WARNING
        logging.getLogger("tbot.health_monitor").info("hello %s", "world")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(line)["msg"] == "hello world"
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


# ---------------------------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, host: Host) -> Host:
    monkeypatch.setattr(hm, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(hm, "Probes", host.probes)
    return host


def test_main_without_credentials_exits_2_and_never_prints_values(
    cli: Host, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        code = hm.main([], env={hm.ENV_TOKEN: "garbage-value-FAKE", hm.ENV_CHAT_ID: FAKE_CHAT})
    assert code == 2
    assert "garbage-value-FAKE" not in caplog.text + capsys.readouterr().out


def test_main_check_prints_problems_and_sends_nothing(
    cli: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = patch_urlopen(monkeypatch, FakeResponse(200, b'{"ok": true}'))
    cli.failed_units = {"tbot-trades-backup.service"}
    assert hm.main(["--check"], env={}) == 0
    out = capsys.readouterr().out
    assert "backup:unit:tbot-trades-backup.service" in out
    assert seen == []


def test_main_send_test_sends_one_message(
    cli: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = patch_urlopen(monkeypatch, FakeResponse(200, b'{"ok": true}'))
    env = {hm.ENV_TOKEN: FAKE_TOKEN, hm.ENV_CHAT_ID: FAKE_CHAT}
    assert hm.main(["--send-test"], env=env) == 0
    assert len(seen) == 1
    assert "TEST" in seen[0].full_url
    captured = capsys.readouterr()
    assert "test message sent" in captured.out
    assert FAKE_TOKEN not in captured.out + captured.err


def test_main_send_test_failure_exits_1_without_leaking_the_token(
    cli: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_urlopen(monkeypatch, _http_error)
    env = {hm.ENV_TOKEN: FAKE_TOKEN, hm.ENV_CHAT_ID: FAKE_CHAT}
    assert hm.main(["--send-test"], env=env) == 1
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert FAKE_TOKEN not in captured.out + captured.err


# ---------------------------------------------------------------------------------------------
# Deployment artifacts
# ---------------------------------------------------------------------------------------------


def test_env_example_has_exactly_two_empty_secrets() -> None:
    lines = (REPO / "deploy" / "health_monitor.env.example").read_text(encoding="utf-8").splitlines()
    active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert active == ["TBOT_TELEGRAM_BOT_TOKEN=", "TBOT_TELEGRAM_CHAT_ID="]


def test_env_example_is_not_ignored_but_the_real_file_is() -> None:
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/health_monitor.env" in ignore
    assert "!.env.example" in ignore


def test_systemd_unit_is_hardened_and_uses_the_root_only_env_file() -> None:
    text = (REPO / "deploy" / "systemd" / "tbot-health-monitor.service").read_text(encoding="utf-8")
    required = [
        "EnvironmentFile=/opt/tbot/health-monitor.env",
        "Type=simple",
        "User=root",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/tbot-health-monitor",
        "PrivateTmp=yes",
        "ProtectHome=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "Restart=always",
        "RestartSec=10",
        "WantedBy=multi-user.target",
        # pinned, root-owned copy (same pattern as /opt/tbot/lbank-run) -- never the branch-switched
        # main checkout
        "Environment=PYTHONPATH=/opt/tbot/monitor-run/src",
        "WorkingDirectory=/opt/tbot/monitor-run",
        "ExecStart=/usr/bin/python3 /opt/tbot/monitor-run/scripts/run_health_monitor.py",
        # extra hardening (review MINOR 5)
        "CapabilityBoundingSet=CAP_DAC_READ_SEARCH",
        "SystemCallFilter=@system-service",
        "PrivateDevices=yes",
        "RestrictNamespaces=yes",
        "ProtectKernelLogs=yes",
        "ProtectClock=yes",
        "MemoryDenyWriteExecute=yes",
        "UMask=0077",
    ]
    active = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for line in required:
        assert line in active
    assert "docker.sock" not in text
    assert "Requires=docker.service" not in active  # must keep running (and alert) when docker is down
    # must run from the pinned copy, not the branch-switched main checkout (comments may still mention
    # /opt/tbot/trade-bot as the one-time copy source)
    assert not any("/opt/tbot/trade-bot" in ln for ln in active)


def test_run_script_delegates_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import run_health_monitor

    calls: list[Sequence[str] | None] = []
    def fake_main(argv: Sequence[str] | None = None, env: Any = None) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(hm, "main", fake_main)
    monkeypatch.setattr("sys.argv", ["run_health_monitor.py", "--check"])
    assert run_health_monitor._run() == 0
    assert calls == [["--check"]]


def test_default_targets_match_the_compose_container_names() -> None:
    assert isinstance(TABDEAL, Target)
    assert TABDEAL.container == "tbot-recorder"
    assert LBANK.container == "tbot-lbank-recorder"
    compose = (REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "container_name: tbot-recorder" in compose
    assert f"name: {TABDEAL.volume}" in compose
    lbank_compose = (REPO / "deploy" / "docker-compose.lbank.yml").read_text(encoding="utf-8")
    assert f"container_name: {LBANK.container}" in lbank_compose
    assert f"name: {LBANK.volume}" in lbank_compose
