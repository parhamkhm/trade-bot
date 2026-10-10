"""Print the chat id(s) of chats that have messaged your bot (one getUpdates call, read-only).

Setup: create the bot in BotFather, open a chat with it in Telegram and send it any message
(press Start), then run, on the server:

    set -a; . /opt/tbot/health-monitor.env; set +a
    python3 scripts/telegram_get_chat_id.py

The bot token is read from the environment variable TBOT_TELEGRAM_BOT_TOKEN only (never argv)
and is never printed. Only chat id, type and a title/username are shown. No offset is passed, so
the pending updates are not acknowledged and nothing is consumed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

try:
    from tbot.monitoring.health_monitor import ENV_TOKEN, TelegramError, redact, register_secret, telegram_get
except ImportError:  # started straight from a checkout, without uv / PYTHONPATH
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from tbot.monitoring.health_monitor import ENV_TOKEN, TelegramError, redact, register_secret, telegram_get

Fetch = Callable[[str], dict[str, Any]]

NO_CHATS_HELP = """\
No chats found. Do this, then run this script again:
  1. Open Telegram and search for your bot by its @username (the one BotFather gave you).
  2. Press Start (or send it any message, e.g. "hi").
  3. Run this script again. (Updates are kept by Telegram for about 24 hours.)
"""


def _fetch_updates(token: str) -> dict[str, Any]:
    return telegram_get(token, "getUpdates", {"limit": "100", "timeout": "0"})


def extract_chats(envelope: Mapping[str, Any]) -> dict[int, tuple[str, str]]:
    """Map chat id -> (type, title/username/first name) for every chat seen in the updates."""
    chats: dict[int, tuple[str, str]] = {}
    result = envelope.get("result")
    if not isinstance(result, list):
        return chats
    for update in result:
        if not isinstance(update, dict):
            continue
        for part in update.values():
            chat = part.get("chat") if isinstance(part, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                chats[chat["id"]] = (str(chat.get("type", "?")), str(label))
    return chats


def main(
    env: Mapping[str, str] | None = None,
    fetch: Fetch | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    environ = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    token = environ.get(ENV_TOKEN, "").strip()
    if not token:
        print(f"{ENV_TOKEN} is not set in the environment (see deploy/health_monitor.env.example).", file=err)
        return 2
    register_secret(token)
    try:
        envelope = (fetch or _fetch_updates)(token)
    except TelegramError as exc:
        print(redact(f"getUpdates failed: {exc}"), file=err)
        if exc.status == 401:
            print("HTTP 401 means the bot token is wrong or was revoked.", file=err)
        return 1
    chats = extract_chats(envelope)
    if not chats:
        print(NO_CHATS_HELP, file=out, end="")
        return 1
    print("Chats that messaged this bot (use the id as TBOT_TELEGRAM_CHAT_ID):", file=out)
    for chat_id, (chat_type, label) in sorted(chats.items()):
        print(f"  chat_id={chat_id}  type={chat_type}  name={label or '-'}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
