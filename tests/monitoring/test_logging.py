"""Tests for tbot.monitoring.logging: JSON rendering and secret redaction.

CLAUDE.md section 3.6: secrets must never appear in logs. The critical test
here (`test_secret_value_never_reaches_rendered_output`) proves that end to
end: configure real JSON logging, log a field holding a raw secret string
under a sensitive key name, capture the actual bytes written to stdout, and
assert the secret substring is absent from them.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
import structlog
from pytest import CaptureFixture

from tbot.monitoring import logging as tbot_logging
from tbot.monitoring.logging import (
    REDACTED,
    configure_logging,
    redact_secrets,
    register_secret,
    register_secrets_for_logging,
)


class _FakeSecretStr:
    """Duck-types pydantic's SecretStr without depending on pydantic in this test."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        # pydantic's own SecretStr already hides the value in repr(); mirror that so
        # a bug that only fixed redact_secrets but not __repr__ would still be caught
        # if something were to str()/repr() the object directly instead of going
        # through get_secret_value().
        return "FakeSecretStr('**********')"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Each test gets a clean structlog/stdlib logging global state.

    MINOR-10 (third fix round): this used to just `.clear()` the registry on the way out,
    which happens to be equivalent to save/restore only because every test in *this* module
    starts from an empty registry. That stopped being true in general once production code
    started calling `register_secrets_for_logging` from more places (the whole point of the
    M-C fix) -- a genuine save/restore, snapshotting whatever was registered before this test
    ran and restoring exactly that afterwards, is what actually prevents one test's
    registered secret from leaking into another test's assertions (which could silently mask
    a real redaction failure) regardless of what was registered before this test started.
    """
    original_excepthook = sys.excepthook
    secret_values_before = tbot_logging._SECRET_VALUES
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    sys.excepthook = original_excepthook
    tbot_logging._SECRET_VALUES = secret_values_before


# --- redact_secrets processor, called directly (no I/O) ----------------------------


def test_sensitive_key_names_are_redacted() -> None:
    event_dict: dict[str, Any] = {
        "event": "placed order",
        "api_key": "AKIA-super-secret",
        "TABDEAL_API_SECRET": "another-secret",
        "auth_token": "tok_123",
        "x-signature": "sig_abc",
        "password": "p@ss",
        "symbol": "BTCUSDT",
    }
    out = redact_secrets(None, "info", dict(event_dict))

    assert out["api_key"] == REDACTED
    assert out["TABDEAL_API_SECRET"] == REDACTED
    assert out["auth_token"] == REDACTED
    assert out["x-signature"] == REDACTED
    assert out["password"] == REDACTED
    # Non-sensitive fields pass through unchanged.
    assert out["event"] == "placed order"
    assert out["symbol"] == "BTCUSDT"


def test_secret_container_is_redacted_regardless_of_key_name() -> None:
    """A SecretStr-shaped value must be masked even under an innocuous key name."""
    event_dict = {"event": "loaded config", "field_named_anything": _FakeSecretStr("raw-secret-value")}
    out = redact_secrets(None, "info", event_dict)
    assert out["field_named_anything"] == REDACTED


def test_redaction_recurses_into_nested_dicts_and_lists() -> None:
    event_dict = {
        "event": "http request",
        "headers": {"X-MBX-APIKEY": "leaked-if-not-redacted", "Content-Type": "application/json"},
        "attempts": [
            {"api_secret": "leaked-1"},
            {"ok": True},
        ],
    }
    out = redact_secrets(None, "info", event_dict)

    assert out["headers"]["X-MBX-APIKEY"] == REDACTED
    assert out["headers"]["Content-Type"] == "application/json"
    assert out["attempts"][0]["api_secret"] == REDACTED
    assert out["attempts"][1]["ok"] is True


def test_redaction_is_case_insensitive_and_matches_substrings() -> None:
    event_dict = {"TabdealApiKey": "x", "myToken": "y", "unrelated_field": "z"}
    out = redact_secrets(None, "info", event_dict)
    assert out["TabdealApiKey"] == REDACTED
    assert out["myToken"] == REDACTED
    assert out["unrelated_field"] == "z"


# --- configure_logging -------------------------------------------------------------


def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        configure_logging("NOT_A_LEVEL")


def test_configure_logging_produces_one_json_object_per_line(capsys: CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = structlog.get_logger("test")
    log.info("heartbeat written", symbol="BTCUSDT", n_trades=3)

    captured = capsys.readouterr().out.strip()
    lines = captured.splitlines()
    assert len(lines) == 1

    parsed = json.loads(lines[0])
    assert parsed["event"] == "heartbeat written"
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["n_trades"] == 3
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def test_configure_logging_respects_level_filtering(capsys: CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    log = structlog.get_logger("test")
    log.info("should be dropped")
    log.warning("should appear")

    captured = capsys.readouterr().out.strip()
    lines = [line for line in captured.splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should appear"


# --- fix-round regressions: minor finding 4 (redaction holes) ----------------------


def test_secret_used_as_dict_key_is_redacted() -> None:
    """Minor finding 4a: a raw secret used AS a dict key (not a value) must not survive
    verbatim, even though no field is named "api_key"/"secret"/etc. — the sensitive-key-name
    rule only ever looked at values, never at the key text itself.
    """
    event_dict: dict[Any, Any] = {"event": "probe.key_as_value", "sk-live-LEAKED-123": "present"}
    out = redact_secrets(None, "info", event_dict)

    assert "sk-live-LEAKED-123" not in out
    assert REDACTED in out
    assert out[REDACTED] == "present"
    assert out["event"] == "probe.key_as_value"


def test_set_and_frozenset_values_are_redacted() -> None:
    """Minor finding 4b: set/frozenset values were not walked at all -- only dict/list/tuple
    were recursed into, so a secret inside a set reached the renderer untouched.
    """
    event_dict = {
        "event": "probe.set",
        "tags": {"api_key=leaked-set-value", "ok"},
        "frozen_tags": frozenset({"token=leaked-frozen-value"}),
    }
    out = redact_secrets(None, "info", dict(event_dict))

    assert isinstance(out["tags"], set)
    assert "api_key=leaked-set-value" not in out["tags"]
    assert any(REDACTED in item for item in out["tags"])
    assert "ok" in out["tags"]

    assert isinstance(out["frozen_tags"], frozenset)
    assert "token=leaked-frozen-value" not in out["frozen_tags"]
    assert any(REDACTED in item for item in out["frozen_tags"])


class _Point(NamedTuple):
    x: int
    y: str


def test_namedtuple_value_does_not_crash_and_is_redacted() -> None:
    """Minor finding 4c: rebuilding a sequence value via `type(value)(generator)` raises
    TypeError on a namedtuple ("NT.__new__() missing 1 required positional argument"),
    which crashed the log call outright instead of just failing to redact.
    """
    event_dict = {"event": "probe.namedtuple", "point": _Point(x=1, y="api_key=leaked-nt-value")}

    out = redact_secrets(None, "info", dict(event_dict))  # must not raise

    assert isinstance(out["point"], _Point)
    assert out["point"].x == 1
    assert "leaked-nt-value" not in out["point"].y
    assert REDACTED in out["point"].y


# --- fix-round regressions: MAJOR M2 (processor ordering / embedded secrets) -------


def test_exception_traceback_is_redacted(capsys: CaptureFixture[str]) -> None:
    """Reviewer probe M2 #1: a traceback rendered by format_exc_info must still be scanned.

    Before the processor-ordering fix, redact_secrets ran BEFORE StackInfoRenderer/
    format_exc_info, so the `exception` field didn't exist yet when redaction ran and the
    raw secret embedded in the traceback message reached stdout untouched:
        {"event":"probe.exc_info", ..., "exception":"... ValueError: signing failed for
        key sk-live-LEAKED-123"}
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "sk-live-LEAKED-123"
    try:
        raise ValueError(f"signing failed for key {secret_value}")
    except ValueError:
        log.exception("probe.exc_info")

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered

    parsed = json.loads(rendered.strip())
    assert parsed["event"] == "probe.exc_info"
    assert secret_value not in parsed["exception"]
    assert REDACTED in parsed["exception"]


def test_signed_url_value_is_redacted(capsys: CaptureFixture[str]) -> None:
    """Reviewer probe M2 #2: a signed URL under an innocuous field name ("url" is not itself
    a sensitive key name) must still have its `signature=` parameter scrubbed:
        {"url":"https://api1.tabdeal.org/r/api/v1/account?timestamp=1&signature=sk-live-
        LEAKED-123", ...}
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "sk-live-LEAKED-123"
    log.info(
        "probe.signed_url",
        url=f"https://api1.tabdeal.org/r/api/v1/account?timestamp=1&signature={secret_value}",
    )

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered

    parsed = json.loads(rendered.strip())
    assert secret_value not in parsed["url"]
    assert f"signature={REDACTED}" in parsed["url"]


# --- the end-to-end secret-safety guarantee ----------------------------------------


def test_secret_value_never_reaches_rendered_output(capsys: CaptureFixture[str]) -> None:
    """A raw secret, logged exactly as a careless call site might, must not leak.

    This exercises the full pipeline configure_logging() wires up (not just the
    processor in isolation): the rendered stdout bytes are inspected directly.
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "sk-live-TOTALLY-REAL-SECRET-abc123"
    log.info(
        "tabdeal request signed",
        api_key=secret_value,
        api_secret=_FakeSecretStr(secret_value + "-secret-form"),
        recv_window_ms=5000,
    )

    rendered = capsys.readouterr().out

    assert secret_value not in rendered
    assert "secret-form" not in rendered
    assert REDACTED in rendered

    parsed = json.loads(rendered.strip())
    assert parsed["api_key"] == REDACTED
    assert parsed["api_secret"] == REDACTED
    assert parsed["recv_window_ms"] == 5000


# --- second fix round: MAJOR-3 (value-based redaction for free-text secrets) -------


def test_registered_secret_with_no_prefix_is_redacted_as_a_plain_string_field(
    capsys: CaptureFixture[str],
) -> None:
    """Reviewer's exact MAJOR-3 probe: a bare alphanumeric secret (no sk-/AKIA/ghp_-style
    prefix, no sensitive key name) was emitted verbatim before `register_secret` existed:
        log.info("note", note="9f8e7d6c5b4a39281706")  ->  emitted verbatim
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "9f8e7d6c5b4a39281706"
    register_secret(secret_value)
    log.info("note", note=secret_value)

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered
    assert json.loads(rendered.strip())["note"] == REDACTED


def test_registered_secret_is_redacted_inside_a_traceback(capsys: CaptureFixture[str]) -> None:
    """MAJOR-3: `raise RuntimeError(f"auth failed for key {secret}")` must not leak through
    the `exception` field just because the secret has no recognizable prefix.
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "9f8e7d6c5b4a39281706"
    register_secret(secret_value)
    try:
        raise RuntimeError(f"auth failed for key {secret_value}")
    except RuntimeError:
        log.exception("probe.registered_secret_traceback")

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered
    parsed = json.loads(rendered.strip())
    assert secret_value not in parsed["exception"]
    assert REDACTED in parsed["exception"]


# --- second fix round: MINOR-9 (redaction recurses into repr()/bytes via registry) --


class _SecretHolder:
    """An arbitrary object -- not a dict/list/SecretStr -- holding a raw secret.

    structlog's JSON fallback renders unrecognized objects via repr(), which happens at
    render time, after `redact_secrets` has already run -- so this can only be caught by
    the final rendered-line scrub, not by recursing into the object's attributes.
    """

    def __init__(self, api_secret: str) -> None:
        self.api_secret = api_secret

    def __repr__(self) -> str:
        return f"_SecretHolder(api_secret={self.api_secret!r})"


def test_registered_secret_inside_arbitrary_object_repr_is_redacted(
    capsys: CaptureFixture[str],
) -> None:
    """Reviewer's exact MINOR-9 probe:
        log.info("d", obj=Holder(api_secret="9f8e...")) -> "Holder(api_secret='9f8e...')"
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "9f8e7d6c5b4a39281706"
    register_secret(secret_value)
    log.info("probe.object_repr", obj=_SecretHolder(secret_value))

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered


def test_registered_secret_inside_bytes_value_is_redacted(capsys: CaptureFixture[str]) -> None:
    """Reviewer's exact MINOR-9 probe:
        payload=b"signature=9f8e..." -> "b'signature=9f8e...'"
    """
    configure_logging("INFO")
    log = structlog.get_logger("test")

    secret_value = "9f8e7d6c5b4a39281706"
    register_secret(secret_value)
    log.info("probe.bytes_value", payload=f"signature={secret_value}".encode())

    rendered = capsys.readouterr().out
    assert secret_value not in rendered
    assert REDACTED in rendered


def test_secret_container_value_is_automatically_registered() -> None:
    """A SecretStr-like container's raw value is fed into the registry as a side effect of
    `_is_secret_container`, so the same secret is still caught if it later leaks elsewhere
    as a bare string with no container around it at all.
    """
    secret_value = "auto-registered-raw-value-123"
    redact_secrets(None, "info", {"api_secret": _FakeSecretStr(secret_value)})

    out = redact_secrets(None, "info", {"note": f"copied value: {secret_value}"})
    assert secret_value not in out["note"]
    assert REDACTED in out["note"]


# --- second fix round: MAJOR-4 (stdlib/foreign loggers and unhandled exceptions) ----


def test_foreign_stdlib_logger_is_redacted(capsys: CaptureFixture[str]) -> None:
    """MAJOR-4: before routing stdlib logging through structlog's ProcessorFormatter, a
    third-party logger's record reached stdout completely unscanned -- reviewer's exact
    scenario was `httpx`/python-telegram-bot logging a signed URL at DEBUG.
    """
    configure_logging("INFO")

    foreign_logger = logging.getLogger("tbot.test.third_party_dependency")
    foreign_logger.info("GET https://api/x?signature=abcdef1234")

    rendered = capsys.readouterr().out
    assert "abcdef1234" not in rendered
    assert REDACTED in rendered
    parsed = json.loads(rendered.strip())
    assert f"signature={REDACTED}" in parsed["event"]


def test_foreign_stdlib_logger_respects_configured_level(capsys: CaptureFixture[str]) -> None:
    """The unified pipeline must still honour level filtering for foreign loggers, not just
    structlog-originated ones.
    """
    configure_logging("WARNING")

    foreign_logger = logging.getLogger("tbot.test.third_party_level")
    foreign_logger.info("should be dropped")
    foreign_logger.warning("should appear")

    captured = capsys.readouterr().out.strip()
    lines = [line for line in captured.splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should appear"


def test_sys_excepthook_scrubs_registered_secret_from_unhandled_exception(
    capsys: CaptureFixture[str],
) -> None:
    """MAJOR-4: an unhandled exception bypasses `logging`/`structlog` entirely -- the
    default excepthook writes the traceback straight to stderr with no redaction at all.
    """
    configure_logging("INFO")

    secret_value = "9f8e7d6c5b4a39281706"
    register_secret(secret_value)
    try:
        raise RuntimeError(f"boom, leaked key {secret_value}")
    except RuntimeError:
        exc_type, exc_value, exc_tb = sys.exc_info()
        assert exc_type is not None
        assert exc_value is not None
        sys.excepthook(exc_type, exc_value, exc_tb)

    captured_err = capsys.readouterr().err
    assert secret_value not in captured_err
    assert REDACTED in captured_err


# --- third fix round: MAJOR M-C (production wiring for the value registry) ---------


def test_register_secrets_for_logging_registers_every_secretstr_field() -> None:
    """MAJOR M-C: production code is expected to call this once, right after constructing
    `tbot.core.config.Secrets()`. It must register every live credential on the object --
    duck-typed exactly like `_is_secret_container`, so this leaf module never needs to
    import pydantic or `tbot.core` -- and must skip `None`/plain-`str` fields without error.
    """

    class _FakeSecrets:
        def __init__(self) -> None:
            self.tabdeal_api_key = _FakeSecretStr("tabdeal-key-abcdef123456")
            self.tabdeal_api_secret = _FakeSecretStr("tabdeal-secret-ghijkl789012")
            self.telegram_bot_token = None  # not configured -- must not crash
            self.telegram_chat_id = "123456789"  # plain str, not a secret container
            self.live_trading = False

    register_secrets_for_logging(_FakeSecrets())

    out = redact_secrets(
        None,
        "info",
        {"note": "leaked tabdeal-key-abcdef123456 and tabdeal-secret-ghijkl789012 both"},
    )
    assert "tabdeal-key-abcdef123456" not in out["note"]
    assert "tabdeal-secret-ghijkl789012" not in out["note"]
    assert out["note"].count(REDACTED) == 2


def test_register_secrets_for_logging_is_a_no_op_for_objects_without_dict() -> None:
    """A startup-time logging-hygiene call must never be able to crash a service's startup,
    however it is misused."""
    register_secrets_for_logging(42)
    register_secrets_for_logging(None)
    register_secrets_for_logging(object())


# --- third fix round: MINOR-1 (registry cannot corrupt valid JSON) -----------------


def test_register_secret_ignores_too_short_values_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer's exact MINOR-1 probe: `register_secret("1")` must not turn every bare "1"
    anywhere in a log line -- including inside valid JSON like `"price": "61234.1"` -- into
    redaction noise.
    """
    monkeypatch.setattr(tbot_logging, "_warned_invalid_secret", False)
    before = tbot_logging._SECRET_VALUES

    with pytest.warns(RuntimeWarning, match="ignored a value"):
        register_secret("1")

    assert before == tbot_logging._SECRET_VALUES

    out = redact_secrets(None, "info", {"price": "61234.1"})
    assert out["price"] == "61234.1"


def test_register_secret_ignores_values_with_characters_outside_the_safe_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer's exact MINOR-1 probe: `register_secret('"')` must not be able to produce
    output `json.loads` rejects. A bare `"` is also shorter than 8 characters, so this also
    exercises a value that is long enough but still unsafe (contains a space and a quote).
    """
    monkeypatch.setattr(tbot_logging, "_warned_invalid_secret", False)
    before = tbot_logging._SECRET_VALUES

    with pytest.warns(RuntimeWarning, match="ignored a value"):
        register_secret('"')
        register_secret("this has spaces and \"quotes\"")

    assert before == tbot_logging._SECRET_VALUES


def test_registering_a_short_numeric_value_does_not_corrupt_rendered_json(
    capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end version of the MINOR-1 probe through the real pipeline: the rendered line
    must stay valid JSON, and an ordinary numeric field must survive untouched.
    """
    monkeypatch.setattr(tbot_logging, "_warned_invalid_secret", False)
    configure_logging("INFO")
    log = structlog.get_logger("test")

    with pytest.warns(RuntimeWarning, match="ignored a value"):
        register_secret("1")
    log.info("tick", price="61234.1")

    rendered = capsys.readouterr().out
    parsed = json.loads(rendered.strip())  # must not raise -- JSON must stay well-formed
    assert parsed["price"] == "61234.1"


def test_embedded_param_pattern_does_not_eat_trailing_angle_bracket() -> None:
    """MINOR-1 tidy-up: `<Obj secret=SECRET>` must keep its closing ">" -- the value capture
    group used to swallow it along with the secret, rendering `<Obj secret=***REDACTED***`
    with no closing bracket at all.
    """
    out = redact_secrets(None, "info", {"event": "<Obj secret=SECRET>"})
    assert out["event"] == f"<Obj secret={REDACTED}>"


# --- third fix round: MINOR-2 (registry thread-safety) -----------------------------


def test_registry_is_thread_safe_under_concurrent_register_and_read() -> None:
    """Reviewer reproduced `RuntimeError: Set changed size during iteration` thrown from
    inside a log call because one thread registered a secret (e.g. via auto-registration
    inside `_is_secret_container`) while another thread was redacting a log line and
    iterating the registry at the same moment. Phase 5 adds Telegram and OMS threads, so
    this interleaving is a real production scenario, not a theoretical one. The fix
    (immutable tuple, rebound atomically under a lock; lock-free reads) must never raise,
    regardless of how the two kinds of thread interleave.
    """
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(i: int) -> None:
        for j in range(500):
            register_secret(f"thread-{i}-secret-value-{j:05d}")

    def reader() -> None:
        while not stop.is_set():
            try:
                redact_secrets(
                    None, "info", {"note": "scanning thread-0-secret-value-00000 for leaks"}
                )
            except BaseException as exc:
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in readers:
        t.start()
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join(timeout=5)

    assert errors == []


# --- third fix round: MINOR-3 (scrub the JSON-escaped form of a registered secret) -


def test_scrub_rendered_line_matches_json_escaped_form_of_registered_secret() -> None:
    """MINOR-3: the final rendered-line pass (`_scrub_rendered_line`, which runs *after*
    `JSONRenderer`) must match a secret's **JSON-escaped** form too, not just its raw bytes.

    This targets `_scrub_rendered_line` directly with an already-JSON-rendered line, so the
    earlier pre-render `redact_secrets` scrub (which sees the plain, unescaped Python string
    and would already catch the raw value before it is ever escaped) cannot mask the bug --
    exactly the gap the reviewer described: a secret containing `"` or `\\` surviving in
    escaped form inside the one pass that scrubs text structlog itself already rendered.
    """
    # Registered directly (bypassing register_secret's own MINOR-1 charset guard, which
    # would otherwise reject a value containing a bare `"`) to isolate this escaping concern
    # from the input-validation concern.
    secret_value = 'sec"ret-with-quote-123456'
    tbot_logging._SECRET_VALUES = (*tbot_logging._SECRET_VALUES, secret_value)

    rendered_line = json.dumps({"note": f"leaked: {secret_value}"})
    # `_scrub_rendered_line` is typed against the generic (dict-shaped) Processor signature
    # (see its docstring) but, at its actual position in the chain -- after JSONRenderer --
    # always receives the rendered `str` at runtime; mypy only sees the declared type.
    scrubbed = tbot_logging._scrub_rendered_line(None, "info", rendered_line)  # type: ignore[arg-type]

    assert isinstance(scrubbed, str)
    assert json.loads(scrubbed)["note"] == f"leaked: {REDACTED}"


# --- third fix round: MINOR-9 (foreign extra={...} fields must not be dropped) -----


def test_extra_adder_merges_foreign_logger_extra_fields(capsys: CaptureFixture[str]) -> None:
    """A foreign stdlib logger's `extra={...}` fields must reach the rendered line instead of
    being silently dropped -- `shared_processors` had no `ExtraAdder` before this fix.
    """
    configure_logging("INFO")
    foreign_logger = logging.getLogger("tbot.test.extra_fields")
    foreign_logger.info("order filled", extra={"order_id": "abc-123"})

    rendered = capsys.readouterr().out
    parsed = json.loads(rendered.strip())
    assert parsed["order_id"] == "abc-123"
