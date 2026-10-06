"""Order-code audit: the REAL enforcement for CLAUDE.md section 3.6 / SPEC D-052.

The `.claude/hooks/block_order_code.py` PreToolUse hook only sees edits Claude Code makes through
its own Write/Edit/MultiEdit tools -- a shell command bypasses it completely. This test (and the
"Order-code audit" CI step that runs it, see `.github/workflows/ci.yml`) is what actually keeps
exchange order/cancel/OCO/margin/withdrawal code out of `src/` and `scripts/`: it scans every
``*.py`` file on disk with the exact same pattern module the hook uses
(`.claude/hooks/order_code_patterns.py`), so the two can never disagree about what counts as
order code.

This module is added to ``sys.path`` manually (rather than imported as a package) because
``.claude/hooks`` deliberately has no ``__init__.py`` -- it is not part of the ``tbot`` package,
it is tooling for Claude Code itself (see the hook's own module docstring for why it is pure
stdlib and has no dependency on anything under ``src/``).

Skipped entirely when ``TBOT_ALLOW_ORDER_CODE=1`` -- the one-shot unlock Parham sets himself for
phase 5b (CLAUDE.md section 9). CI never sets this environment variable (see
``.github/workflows/ci.yml``), so in CI this test always runs for real; a skip here can only ever
happen on a developer's own machine, after Parham has deliberately unlocked order code locally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO_ROOT / ".claude" / "hooks"
_HOOK_SCRIPT = _HOOKS_DIR / "block_order_code.py"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from order_code_patterns import find_matches  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("TBOT_ALLOW_ORDER_CODE") == "1",
    reason="TBOT_ALLOW_ORDER_CODE=1: Parham has unlocked order code locally (phase 5b); "
    "CI never sets this variable, see .github/workflows/ci.yml",
)


def _scanned_python_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("src", "scripts"):
        files.extend(sorted((_REPO_ROOT / sub).rglob("*.py")))
    return files


# ---------------------------------------------------------------------------
# 1. The audit itself: zero forbidden-pattern matches anywhere under src/ or scripts/.
# ---------------------------------------------------------------------------


def test_no_order_code_under_src_or_scripts() -> None:
    files = _scanned_python_files()
    assert files, "expected at least one *.py file under src/ or scripts/ -- audit found none"

    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name, lineno, line in find_matches(text):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            violations.append(f"{rel}:{lineno}: [{name}] {line.strip()[:120]}")

    assert not violations, (
        "Order-code guardrail violation(s) -- exchange order/cancel/OCO/margin/withdrawal code "
        "is not allowed before phase 5b (CLAUDE.md section 3.6 / SPEC D-052). "
        "Unlock: Parham sets TBOT_ALLOW_ORDER_CODE=1 himself.\n" + "\n".join(violations)
    )


def test_scanned_tree_covers_known_safe_modules() -> None:
    """Sanity check that the audit is actually looking at the files it should be.

    Guards against a future refactor silently narrowing `_scanned_python_files` to the point
    where it scans nothing and the test above passes for the wrong reason.
    """
    relnames = {p.relative_to(_REPO_ROOT).as_posix() for p in _scanned_python_files()}
    for expected in (
        "src/tbot/core/types.py",
        "src/tbot/execution/tabdeal_client.py",
        "src/tbot/data/tabdeal_recorder.py",
        "scripts/tabdeal_probe.py",
    ):
        assert expected in relnames, f"expected {expected} to be scanned by the order-code audit"


# ---------------------------------------------------------------------------
# 2. Allow-list / false-positive regression checks (unit-level, on the shared pattern module).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "safe_snippet",
    [
        'class Strategy(Protocol):\n    """A trading strategy."""\n',
        "@dataclass(frozen=True)\nclass OrderRequest:\n    client_order_id: str\n",
        "class OrderType(StrEnum):\n    MARKET = 'MARKET'\n",
        "class OrderAck: ...\n",
        "class OrderStatus: ...\n",
        "class OrderState(StrEnum): ...\n",
        "def open_orders(self, symbol: str) -> list[OrderStatus]: ...\n",
        'CREATE TABLE IF NOT EXISTS orderbook (\n    ts_ms INTEGER NOT NULL\n);\n',
        "# order book snapshot poller\ndef poll_orderbook_once() -> None: ...\n",
        '_PERMISSION_INDICATOR_KEYS = ("canTrade", "canWithdraw", "permissions")\n',
        'if upper & {"TRADE", "WITHDRAW", "WITHDRAWALS"}:\n    unsafe = True\n',
        '"""\nOCO, ``userDataStream`` or withdrawal surface are not implemented here.\n"""\n',
        '# m9b: withdraw permission check, not a withdrawal call\nx = 1\n',
        'print(\n    "WARNING: verify that this key has NO trade and NO withdrawal permission"\n)\n',
    ],
)
def test_known_safe_snippets_produce_no_matches(safe_snippet: str) -> None:
    assert find_matches(safe_snippet) == []


@pytest.mark.parametrize(
    "unsafe_snippet,expected_pattern_substring",
    [
        ('httpx.post("https://api1.tabdeal.org/api/v1/order", json={})', "post-call"),
        ('requests.delete(f"/api/v1/order/{client_order_id}")', "delete-call"),
        ('resp = session.put(url, json=payload)\nmethod = "PUT"', "put-call"),
        ('endpoint = "/api/v1/openOrders"', "openOrders"),
        ('endpoint = "/api/v1/allOrders"', "allOrders"),
        ('url = base + "/order/oco"', "oco"),
        ('path = "/margin/borrow"', "/margin"),
        ('resp = client.post(withdraw_url)', "withdraw"),
        ('stream_url = "wss://api1.tabdeal.org/stream/" + userDataStreamKey', "userDataStream"),
        ('params = {"listenKey": key}', "listenKey"),
        ('params["newClientOrderId"] = coid', "newClientOrderId"),
        ('params["origClientOrderId"] = coid', "origClientOrderId"),
    ],
)
def test_known_unsafe_snippets_are_detected(unsafe_snippet: str, expected_pattern_substring: str) -> None:
    findings = find_matches(unsafe_snippet)
    assert findings, f"expected a match for: {unsafe_snippet!r}"
    assert any(expected_pattern_substring.lower() in name.lower() for name, _, _ in findings), (
        f"expected a finding whose pattern name mentions {expected_pattern_substring!r}, "
        f"got {[n for n, _, _ in findings]}"
    )


# ---------------------------------------------------------------------------
# 3. Hook subprocess tests: synthetic PreToolUse payloads, exactly as Claude Code would send them.
# ---------------------------------------------------------------------------


def _run_hook(
    payload: Mapping[str, object],
    *,
    project_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("TBOT_ALLOW_ORDER_CODE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_hook_blocks_write_with_order_code() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/tbot/execution/evil.py",
            "content": 'import httpx\nhttpx.post("https://api1.tabdeal.org/api/v1/order")\n',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2
    assert "evil.py" in result.stderr
    assert "path:/order" in result.stderr


def test_hook_allows_clean_write() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "src/tbot/data/clean.py", "content": "def f() -> int:\n    return 1\n"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_allows_edit_out_of_scope_even_with_order_code() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "docs/SPEC.md",
            "old_string": "a",
            "new_string": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_blocks_edit_of_settings_json() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": ".claude/settings.json", "old_string": "a", "new_string": "b"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2
    assert "settings.json" in result.stderr


def test_hook_blocks_edit_of_hook_itself() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": ".claude/hooks/block_order_code.py",
            "old_string": "a",
            "new_string": "b",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_blocks_edit_of_pattern_module_itself() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": ".claude/hooks/order_code_patterns.py",
            "old_string": "a",
            "new_string": "b",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_unlock_env_allows_order_code_but_not_self_edit() -> None:
    order_code_payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/tbot/execution/evil.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(order_code_payload, project_dir=_REPO_ROOT, env_extra={"TBOT_ALLOW_ORDER_CODE": "1"})
    assert result.returncode == 0

    settings_payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": ".claude/settings.json", "content": "{}"},
    }
    result = _run_hook(settings_payload, project_dir=_REPO_ROOT, env_extra={"TBOT_ALLOW_ORDER_CODE": "1"})
    assert result.returncode == 2


def test_hook_multiedit_blocks_if_any_edit_introduces_order_code() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": "src/tbot/execution/foo.py",
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 2"},
                {"old_string": "y = 1", "new_string": 'requests.delete("/order/123")'},
            ],
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2
    assert "foo.py" in result.stderr


def test_hook_multiedit_allows_clean_edits() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": "src/tbot/execution/foo.py",
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 2"},
                {"old_string": "y = 1", "new_string": "y = 2"},
            ],
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_edit_does_not_block_unrelated_line_with_preexisting_match() -> None:
    """A pre-existing (unchanged) match in old_string must not block editing another line."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "src/tbot/execution/foo.py",
            "old_string": 'url = "https://api1.tabdeal.org/api/v1/order"\nx = 1',
            "new_string": 'url = "https://api1.tabdeal.org/api/v1/order"\nx = 2',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_windows_style_path_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src\\tbot\\execution\\evil.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_absolute_windows_path_is_in_scope() -> None:
    abs_path = str(_REPO_ROOT / "src" / "tbot" / "execution" / "evil.py")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": abs_path, "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")'},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_absolute_path_is_self_protected() -> None:
    abs_path = str(_HOOK_SCRIPT)
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": abs_path, "old_string": "a", "new_string": "b"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_path_traversal_resolves_to_self_protected_settings() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "src/../.claude/settings.json", "content": "{}"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2
    assert "settings.json" in result.stderr


def test_hook_garbage_stdin_fails_closed() -> None:
    result = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input="not json at all {{{",
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(_REPO_ROOT)},
        timeout=30,
    )
    assert result.returncode == 2


def test_hook_unknown_tool_name_is_allowed() -> None:
    payload = {"tool_name": "Read", "tool_input": {"file_path": "src/tbot/execution/foo.py"}}
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_missing_file_path_fails_closed() -> None:
    payload = {"tool_name": "Write", "tool_input": {"content": "x = 1"}}
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2
