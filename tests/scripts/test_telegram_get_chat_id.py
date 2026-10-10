"""Tests for scripts/telegram_get_chat_id.py (fake token, fake Telegram, nothing printed from env)."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from typing import Any

import pytest

from scripts import telegram_get_chat_id as tool
from tbot.monitoring import health_monitor as hm

FAKE_TOKEN = "123456789:AAFakeTokenFakeTokenFakeToken_xyz"
ENV = {hm.ENV_TOKEN: FAKE_TOKEN}


def run(
    env: dict[str, str], fetch: Any = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(env, fetch, out, err)
    return code, out.getvalue(), err.getvalue()


def updates(*chats: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": [{"update_id": i, "message": {"chat": c}} for i, c in enumerate(chats)]}


def test_prints_chat_id_type_and_name() -> None:
    envelope = updates({"id": 424242, "type": "private", "first_name": "Parham"})
    code, out, err = run(ENV, lambda _token: envelope)
    assert code == 0
    assert "chat_id=424242" in out
    assert "type=private" in out
    assert "Parham" in out
    assert err == ""


def test_lists_each_chat_once_including_groups() -> None:
    envelope = updates(
        {"id": 424242, "type": "private", "first_name": "P"},
        {"id": 424242, "type": "private", "first_name": "P"},
        {"id": -100123, "type": "supergroup", "title": "alerts"},
    )
    code, out, _ = run(ENV, lambda _token: envelope)
    assert code == 0
    assert out.count("chat_id=424242") == 1
    assert "chat_id=-100123  type=supergroup  name=alerts" in out


def test_no_chats_gives_clear_instructions() -> None:
    code, out, _ = run(ENV, lambda _token: {"ok": True, "result": []})
    assert code == 1
    assert "No chats found" in out
    assert "Press Start" in out


def test_missing_token_exits_2_and_does_not_call_telegram() -> None:
    def fetch(token: str) -> dict[str, Any]:
        raise AssertionError("must not be called")

    code, _, err = run({}, fetch)
    assert code == 2
    assert hm.ENV_TOKEN in err


def test_telegram_error_is_reported_without_the_token() -> None:
    def fetch(token: str) -> dict[str, Any]:
        raise hm.TelegramError("http error", 401)

    code, out, err = run(ENV, fetch)
    assert code == 1
    assert "HTTP 401" in err
    assert "wrong or was revoked" in err
    assert FAKE_TOKEN not in out + err


def test_token_is_never_printed_even_if_an_error_contains_it() -> None:
    def fetch(token: str) -> dict[str, Any]:
        raise hm.TelegramError(f"weird failure for {token}")

    code, out, err = run(ENV, fetch)
    assert code == 1
    assert FAKE_TOKEN not in out + err


def test_real_fetch_path_uses_get_and_hides_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 0.0) -> Any:
        seen.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    code, out, err = run(ENV)
    assert code == 1
    assert len(seen) == 1
    assert seen[0].get_method() == "GET"
    assert "/getUpdates?" in seen[0].full_url
    assert "offset" not in seen[0].full_url  # updates are not acknowledged / consumed
    assert FAKE_TOKEN not in out + err


def test_argv_is_not_used_for_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["telegram_get_chat_id.py", FAKE_TOKEN])
    code, _, err = run({})
    assert code == 2
    assert FAKE_TOKEN not in err
