"""Contract tests: the health monitor is fed output from the REAL writers (the backup script and
the two recorders' heartbeat code), not hand-built fixtures. These catch drift between the monitor's
assumptions (field names, types, the status.json lifecycle) and what actually gets written on the
server. All data is synthetic/local; nothing here talks to a real exchange, docker or Telegram.
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tbot.data.lbank_recorder import LBankRecorder, RecorderConfig
from tbot.data.tabdeal_recorder import HeartbeatState, write_heartbeat
from tbot.monitoring import health_monitor as hm
from tbot.monitoring.health_monitor import CmdResult, Memory, Monitor, Probes, Settings, StateStore

REPO = Path(__file__).resolve().parents[2]


def _load_backup_lbank() -> ModuleType:
    """Load deploy/backup_lbank.py the same way tests/deploy/test_backup_lbank.py does, under a
    distinct module name so the two test modules never fight over ``sys.modules``."""
    path = REPO / "deploy" / "backup_lbank.py"
    spec = importlib.util.spec_from_file_location("deploy_backup_lbank_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup_lbank = _load_backup_lbank()


class _GzipCodec:
    """Same contract as backup_lbank.ZstdCli, without needing the zstd binary on the test machine."""

    suffix = ".zst"

    def __init__(self, corrupt: bool = False) -> None:
        self.corrupt = corrupt

    def compress(self, src: Path, dst: Path) -> None:
        data = src.read_bytes()
        if self.corrupt:
            data = data[: len(data) // 2]
        dst.write_bytes(gzip.compress(data))

    def decompress(self, src: Path, dst: Path) -> None:
        dst.write_bytes(gzip.decompress(src.read_bytes()))


# Last write of a closed test day file, fixed relative to the tests' `now` (2026-10-12 04:00 UTC) so the
# backup's "untouched for GRACE" rule never depends on the real clock.
_DAY_WRITTEN = datetime(2026, 10, 12, 0, 30, tzinfo=UTC).timestamp()


def _make_day(path: Path, rows: int) -> None:
    import os
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()
    os.utime(path, (_DAY_WRITTEN, _DAY_WRITTEN))


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    return Settings(state_dir=tmp_path / "state", hostname="testhost", **over)


def fixed_probes(now: datetime, status_dest: Path) -> Probes:
    """Every check except the LBank backup one must see a healthy host (no real docker/systemctl)."""

    def run(args: Sequence[str]) -> CmdResult:
        if args[:2] == ["docker", "inspect"]:
            return CmdResult(0, '{"state":{"Status":"running","Health":{"Status":"healthy"}},'
                              '"restarts":0}', "")
        if args[:3] == ["docker", "volume", "inspect"]:
            return CmdResult(0, str(status_dest) + "\n", "")
        if args[:1] == ["systemctl"]:
            return CmdResult(0, "active\n", "")
        raise AssertionError(f"unexpected command in contract test: {args}")

    return Probes(now=lambda: now, run=run, read_text=hm._read_text, list_names=hm._list_names,
                  disk=lambda _p: hm.DiskUsage(used=0, free=10 * 1024**3, device=1), memory=Memory())


# ---------------------------------------------------------------------------------------------
# Contract: deploy/backup_lbank.py's real status.json, fed straight into the Monitor
# ---------------------------------------------------------------------------------------------


def test_contract_real_ok_backup_run_is_quiet_through_the_monitor(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    now = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    _make_day(src / "lbank-2026-10-11.sqlite", 5)
    assert backup_lbank.run(src, dest, codec=_GzipCodec(), now=now) == 0

    settings = make_settings(tmp_path, lbank_status_file=dest / "status.json")
    sent: list[str] = []
    monitor = Monitor(
        settings, fixed_probes(now + timedelta(seconds=5), dest), sent.append,
        StateStore(settings.state_dir / "state.json"),
    )
    events = monitor.run_cycle()
    assert [e for e in events if e.key.startswith("backup:lbank")] == []


def test_contract_real_corrupt_backup_run_alerts_through_the_monitor(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    now = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    _make_day(src / "lbank-2026-10-11.sqlite", 500)
    assert backup_lbank.run(src, dest, codec=_GzipCodec(corrupt=True), now=now) == 2

    settings = make_settings(tmp_path, lbank_status_file=dest / "status.json")
    sent: list[str] = []
    monitor = Monitor(
        settings, fixed_probes(now + timedelta(seconds=5), dest), sent.append,
        StateStore(settings.state_dir / "state.json"),
    )
    events = monitor.run_cycle()
    alerts = [e for e in events if e.key == "backup:lbank"]
    assert len(alerts) == 1
    assert alerts[0].kind == "alert"
    assert "backup failed" in alerts[0].summary


def test_contract_real_stale_status_alerts(tmp_path: Path) -> None:
    """A real ok=true status.json whose 'ts' has simply aged past backup_max_age_hours."""
    src, dest = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    now = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    _make_day(src / "lbank-2026-10-11.sqlite", 3)
    assert backup_lbank.run(src, dest, codec=_GzipCodec(), now=now) == 0

    settings = make_settings(tmp_path, lbank_status_file=dest / "status.json")
    later = now + timedelta(hours=40)
    found = hm.check_lbank_backup(settings, fixed_probes(later, dest))
    assert {p.key for p in found} == {"backup:lbank"}
    assert "h old" in found[0].summary


def test_contract_real_in_progress_write_within_the_grace_period_is_quiet(tmp_path: Path) -> None:
    """The writer's very first status.json write of a run sets in_progress=true, ok=false: the
    monitor must not treat that as a failure while the run is still young."""
    dest = tmp_path / "dst"
    dest.mkdir()
    now = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    started_ts = now.isoformat()
    backup_lbank._write_status(
        dest, backup_lbank._status_payload(started_ts, ts=started_ts, in_progress=True,
                                            message="run in progress"),
    )
    settings = make_settings(tmp_path, lbank_status_file=dest / "status.json")
    found = hm.check_lbank_backup(settings, fixed_probes(now + timedelta(minutes=10), dest))
    assert len(found) == 1
    assert found[0].key == "backup:lbank"
    assert found[0].silent is True  # never a visible alert while the run is still young
    assert found[0].freeze == ("backup:lbank",)  # but also never read as "cleared"


# ---------------------------------------------------------------------------------------------
# Contract: the two recorders' real heartbeat writers
# ---------------------------------------------------------------------------------------------


def test_contract_real_tabdeal_heartbeat_is_understood(tmp_path: Path) -> None:
    target = hm.DEFAULT_TARGETS[0]
    heartbeat_path = tmp_path / target.heartbeat_rel
    now = datetime.now(UTC)
    write_heartbeat(
        heartbeat_path,
        HeartbeatState(last_poll_ts=now, last_trade_id=42, n_trades_total=100, consecutive_errors=0),
    )
    settings = make_settings(tmp_path)
    probes = fixed_probes(now, tmp_path)  # docker volume inspect in fixed_probes returns tmp_path
    assert hm.check_heartbeat(target, settings, probes) == []

    # and a stale real heartbeat does alert
    write_heartbeat(
        heartbeat_path,
        HeartbeatState(
            last_poll_ts=now - timedelta(seconds=900), last_trade_id=42, n_trades_total=100,
            consecutive_errors=3,
        ),
    )
    found = hm.check_heartbeat(target, settings, fixed_probes(now, tmp_path))
    assert {p.key for p in found} == {"recorder:tabdeal"}


def test_contract_real_lbank_heartbeat_is_understood(tmp_path: Path) -> None:
    target = hm.DEFAULT_TARGETS[1]
    data_dir = tmp_path / "lbank"
    data_dir.mkdir(parents=True)  # normally done by LBankRecorder.start(), which we don't run here
    recorder = LBankRecorder(RecorderConfig(data_dir=data_dir))
    recorder.write_heartbeat()  # fresh: written_ms is "now" by the real wall clock
    settings = make_settings(tmp_path)
    probes = fixed_probes(datetime.now(UTC), tmp_path)
    assert hm.check_heartbeat(target, settings, probes) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
