"""Tests for deploy/healthcheck.py: the Docker HEALTHCHECK for the `recorder` service.

Reviewer fix-round finding M5: the previous healthcheck only checked the heartbeat file's
mtime, so a recorder whose every poll cycle was failing (but which still rewrites a
fresh-looking heartbeat file every cycle -- see `tbot.data.tabdeal_recorder.
TabdealRecorderService.process_once`) was reported "healthy" by Docker forever. These tests
drive `evaluate_heartbeat` (the pure decision function) and `main` (the CLI entry point,
via a real heartbeat file on disk) directly, without Docker.

`deploy/healthcheck.py` is not part of the `tbot` package (it ships standalone into the
image, deliberately dependency-free), so it is imported here by file path.
"""

from __future__ import annotations

import importlib.util
import json
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

_HEALTHCHECK_PATH = Path(__file__).resolve().parents[2] / "deploy" / "healthcheck.py"


def _load_healthcheck_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_healthcheck", _HEALTHCHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


healthcheck = _load_healthcheck_module()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "TBOT_HEARTBEAT_FILE",
        "TBOT_HEARTBEAT_MAX_AGE_SECONDS",
        "TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS",
        "TBOT_HEARTBEAT_MAX_TRADE_STALENESS_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _payload(
    *,
    last_poll_ts: str | None = "2026-01-01T00:00:00+00:00",
    consecutive_errors: object = 0,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {"consecutive_errors": consecutive_errors}
    if last_poll_ts is not None:
        payload["last_poll_ts"] = last_poll_ts
    payload.update(extra)
    return payload


# --- evaluate_heartbeat: the pure decision function --------------------------------


def test_fresh_heartbeat_with_no_errors_is_healthy() -> None:
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0)

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is True
    assert reason == "ok"


def test_stale_last_poll_ts_is_unhealthy() -> None:
    """A heartbeat file that stopped being rewritten (process dead/wedged)."""
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)  # 1 hour after last_poll_ts
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0)

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False
    assert "old" in reason


def test_high_consecutive_errors_is_unhealthy_even_with_fresh_timestamp() -> None:
    """Reviewer's exact M5 scenario: every poll is failing, but the file is still being
    rewritten every cycle, so its mtime/last_poll_ts stays fresh. Must still fail.
    """
    now = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=42)

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False
    assert "consecutive_errors" in reason


def test_consecutive_errors_exactly_at_threshold_is_unhealthy() -> None:
    now = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=10)

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False


def test_consecutive_errors_just_under_threshold_is_healthy() -> None:
    now = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=9)

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is True


@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        None,
        [],
        42,
    ],
)
def test_non_dict_payload_is_unhealthy_not_a_crash(payload: object) -> None:
    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=datetime.now(UTC), max_age_seconds=180, max_consecutive_errors=10
    )
    assert healthy is False
    assert reason


def test_missing_last_poll_ts_is_unhealthy_not_a_crash() -> None:
    healthy, reason = healthcheck.evaluate_heartbeat(
        {"consecutive_errors": 0},
        now=datetime.now(UTC),
        max_age_seconds=180,
        max_consecutive_errors=10,
    )
    assert healthy is False
    assert "last_poll_ts" in reason


def test_malformed_last_poll_ts_is_unhealthy_not_a_crash() -> None:
    healthy, reason = healthcheck.evaluate_heartbeat(
        _payload(last_poll_ts="not-a-timestamp"),
        now=datetime.now(UTC),
        max_age_seconds=180,
        max_consecutive_errors=10,
    )
    assert healthy is False
    assert "last_poll_ts" in reason


def test_missing_consecutive_errors_is_unhealthy_not_a_crash() -> None:
    healthy, reason = healthcheck.evaluate_heartbeat(
        {"last_poll_ts": datetime.now(UTC).isoformat()},
        now=datetime.now(UTC),
        max_age_seconds=180,
        max_consecutive_errors=10,
    )
    assert healthy is False
    assert "consecutive_errors" in reason


def test_bool_consecutive_errors_is_unhealthy_not_a_crash() -> None:
    """bool is a subclass of int in Python; explicitly reject it so a stray `true`/`false`
    in the JSON (e.g. from a future schema change) can't silently coerce into 0 or 1.
    """
    now = datetime.now(UTC)
    healthy, reason = healthcheck.evaluate_heartbeat(
        _payload(last_poll_ts=now.isoformat(), consecutive_errors=True),
        now=now,
        max_age_seconds=180,
        max_consecutive_errors=10,
    )
    assert healthy is False
    assert "consecutive_errors" in reason


def test_naive_last_poll_ts_is_treated_as_utc() -> None:
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00")  # no tzinfo

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is True


def test_last_poll_ts_far_in_the_future_is_unhealthy() -> None:
    """Large positive skew (clock jump, bad write) should not be silently accepted."""
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T01:00:00+00:00")  # 1h in the future

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False
    assert "future" in reason


# --- evaluate_heartbeat: last_new_trade_ts_ms (m3, fourth fix round) ---------------


def test_stale_last_new_trade_ts_ms_is_unhealthy_even_with_fresh_poll() -> None:
    """m3's whole point: the process is polling fine (fresh last_poll_ts, zero
    consecutive_errors -- both checks above would pass) but the exchange's own trade feed has
    gone stale. Neither of the pre-existing checks can see that; this one must.
    """
    now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
    stale_trade_ms = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)  # 2h old
    payload = _payload(
        last_poll_ts=now.isoformat(), consecutive_errors=0, last_new_trade_ts_ms=stale_trade_ms
    )

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10, max_trade_staleness_seconds=3600
    )

    assert healthy is False
    assert "last_new_trade_ts_ms" in reason


def test_fresh_last_new_trade_ts_ms_is_healthy() -> None:
    now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
    fresh_trade_ms = int(datetime(2026, 1, 1, 1, 59, 30, tzinfo=UTC).timestamp() * 1000)  # 30s old
    payload = _payload(
        last_poll_ts=now.isoformat(), consecutive_errors=0, last_new_trade_ts_ms=fresh_trade_ms
    )

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10, max_trade_staleness_seconds=3600
    )

    assert healthy is True


def test_missing_last_new_trade_ts_ms_field_is_healthy_treated_as_unknown() -> None:
    """A heartbeat payload from before this fix round (or any payload that simply omits the
    field) must not be penalised for a field it never had."""
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0)
    assert "last_new_trade_ts_ms" not in payload

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is True


def test_null_last_new_trade_ts_ms_is_healthy_treated_as_unknown() -> None:
    """Present but ``null`` (no trade has ever been recorded yet) is likewise not itself a
    failure -- ``last_poll_ts``'s own staleness check already covers total silence."""
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(
        last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0, last_new_trade_ts_ms=None
    )

    healthy, _ = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is True


def test_malformed_last_new_trade_ts_ms_is_unhealthy_not_a_crash() -> None:
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(
        last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0, last_new_trade_ts_ms="not-an-int"
    )

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False
    assert "last_new_trade_ts_ms" in reason


def test_bool_last_new_trade_ts_ms_is_unhealthy_not_a_crash() -> None:
    """bool is a subclass of int; a stray true/false must not silently coerce into 0/1 ms."""
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    payload = _payload(
        last_poll_ts="2026-01-01T00:00:00+00:00", consecutive_errors=0, last_new_trade_ts_ms=True
    )

    healthy, reason = healthcheck.evaluate_heartbeat(
        payload, now=now, max_age_seconds=180, max_consecutive_errors=10
    )

    assert healthy is False
    assert "last_new_trade_ts_ms" in reason


# --- main(): file I/O + env var parsing, robust to a missing/malformed file --------


def test_main_is_unhealthy_when_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(tmp_path / "does-not-exist.json"))
    assert healthcheck.main([]) == 1


def test_main_is_unhealthy_when_file_is_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    assert healthcheck.main([]) == 1  # must not raise


def test_main_is_unhealthy_when_file_is_valid_json_but_not_an_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    assert healthcheck.main([]) == 1


def test_main_prints_evaluate_heartbeat_reason_not_the_payload_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fix-round finding minor #7: a heartbeat file containing a bare JSON string (valid
    JSON, just not an object) used to be indistinguishable from `_read_payload`'s own
    error-string sentinel -- `main()` printed that string to stderr as though it were the
    diagnosis, instead of the real "payload is not a JSON object" reason from
    `evaluate_heartbeat`. `_read_payload` now raises `_PayloadReadError` on an actual
    read/parse failure instead of returning a string, so a bare-string *payload* reaches
    `evaluate_heartbeat` like any other non-dict payload and gets the real diagnosis.
    """
    path = tmp_path / "heartbeat.json"
    path.write_text('"some status"', encoding="utf-8")  # valid JSON, a bare string
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))

    assert healthcheck.main([]) == 1

    stderr = capsys.readouterr().err
    assert "some status" not in stderr
    assert "not a JSON object" in stderr


def test_read_payload_raises_dedicated_error_type_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(healthcheck._PayloadReadError, match="not valid JSON"):
        healthcheck._read_payload(path)


def test_read_payload_returns_bare_string_payload_without_raising(tmp_path: Path) -> None:
    """A bare JSON string is a successfully-parsed (if useless) payload, not a read error --
    `_read_payload` must return it, not raise, so `evaluate_heartbeat` is what decides it's
    unusable.
    """
    path = tmp_path / "heartbeat.json"
    path.write_text('"some status"', encoding="utf-8")
    assert healthcheck._read_payload(path) == "some status"


def test_main_is_healthy_for_a_fresh_low_error_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "heartbeat.json"
    payload = _payload(last_poll_ts=datetime.now(UTC).isoformat(), consecutive_errors=0)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    assert healthcheck.main([]) == 0


def test_main_is_unhealthy_when_consecutive_errors_crosses_env_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """Reviewer's exact M5 scenario end to end: every poll failing, file still fresh."""
    path = tmp_path / "heartbeat.json"
    payload = _payload(last_poll_ts=datetime.now(UTC).isoformat(), consecutive_errors=10)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS", "10")
    assert healthcheck.main([]) == 1


def test_main_respects_overridden_max_age_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "heartbeat.json"
    stale_ts = datetime.now(UTC) - timedelta(seconds=30)
    payload = _payload(last_poll_ts=stale_ts.isoformat(), consecutive_errors=0)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_AGE_SECONDS", "10")  # stricter than the 30s-old file
    assert healthcheck.main([]) == 1


def test_main_falls_back_to_defaults_on_malformed_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """A malformed TBOT_HEARTBEAT_MAX_AGE_SECONDS/...MAX_CONSECUTIVE_ERRORS must not crash
    the healthcheck -- it should fall back to the built-in defaults.
    """
    path = tmp_path / "heartbeat.json"
    payload = _payload(last_poll_ts=datetime.now(UTC).isoformat(), consecutive_errors=0)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_AGE_SECONDS", "not-a-number")
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_CONSECUTIVE_ERRORS", "also-not-a-number")
    assert healthcheck.main([]) == 0


def test_main_is_unhealthy_when_trade_staleness_crosses_env_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """m3 end to end: polling itself looks perfectly healthy, but the trade feed is stale per a
    tightened TBOT_HEARTBEAT_MAX_TRADE_STALENESS_SECONDS."""
    path = tmp_path / "heartbeat.json"
    now = datetime.now(UTC)
    stale_trade_ms = int((now - timedelta(seconds=120)).timestamp() * 1000)
    payload = _payload(
        last_poll_ts=now.isoformat(), consecutive_errors=0, last_new_trade_ts_ms=stale_trade_ms
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_TRADE_STALENESS_SECONDS", "60")  # stricter than 120s old
    assert healthcheck.main([]) == 1


def test_main_uses_default_trade_staleness_when_env_var_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """Without an override, the built-in default (7200s, sized against BTCUSDT's measured
    inter-trade gap -- see the module docstring) must not false-alarm on an ordinary quiet
    period."""
    path = tmp_path / "heartbeat.json"
    now = datetime.now(UTC)
    quiet_but_fine_ms = int((now - timedelta(seconds=2000)).timestamp() * 1000)  # > old 1800s default
    payload = _payload(
        last_poll_ts=now.isoformat(), consecutive_errors=0, last_new_trade_ts_ms=quiet_but_fine_ms
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    assert healthcheck.main([]) == 0


def test_main_falls_back_to_default_trade_staleness_on_malformed_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "heartbeat.json"
    payload = _payload(last_poll_ts=datetime.now(UTC).isoformat(), consecutive_errors=0)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TBOT_HEARTBEAT_FILE", str(path))
    monkeypatch.setenv("TBOT_HEARTBEAT_MAX_TRADE_STALENESS_SECONDS", "not-a-number")
    assert healthcheck.main([]) == 0  # must not crash; falls back to the default


def test_module_is_self_contained_stdlib_only() -> None:
    """The image ships this file standalone (deploy/Dockerfile COPYs it in, no project
    venv assumed importable from a HEALTHCHECK context) -- its own source must not
    reference `tbot` or any third-party package, only the standard library.
    """
    source = _HEALTHCHECK_PATH.read_text(encoding="utf-8")
    for forbidden in ("import tbot", "from tbot", "import structlog", "import pydantic"):
        assert forbidden not in source
    assert isinstance(healthcheck, types.ModuleType)
