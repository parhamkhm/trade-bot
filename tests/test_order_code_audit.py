"""Order-code audit: the REAL enforcement for CLAUDE.md section 3.6 / SPEC D-052.

The `.claude/hooks/block_order_code.py` PreToolUse hook only sees edits Claude Code makes through
its own Write/Edit/MultiEdit/NotebookEdit tools -- a shell command bypasses it completely. This
test (and the "Order-code audit" CI step that runs it, see `.github/workflows/ci.yml`) is what
actually keeps exchange order/cancel/OCO/margin/withdrawal code out of `src/`, `scripts/` and
`deploy/`: it scans every matching file on disk with the exact same pattern module the hook uses
(`.claude/hooks/order_code_patterns.py`), so the two can never disagree about what counts as
order code.

This module is added to ``sys.path`` manually (rather than imported as a package) because
``.claude/hooks`` deliberately has no ``__init__.py`` -- it is not part of the ``tbot`` package,
it is tooling for Claude Code itself (see the hook's own module docstring for why it is pure
stdlib and has no dependency on anything under ``src/``).

Skipped entirely when ``TBOT_ALLOW_ORDER_CODE=1`` **and** ``CI`` is not set -- the one-shot unlock
Parham sets himself for phase 5b (CLAUDE.md section 9), on his own machine. The ``CI`` half of
that condition is the actual fix for a real bypass a reviewer found: GitHub Actions sets
``CI=true`` in every job automatically, so if ``TBOT_ALLOW_ORDER_CODE=1`` ever reached a CI run
(e.g. a workflow misconfiguration, or someone setting it as a repo/organization secret), the old
"skip whenever the env var is 1" condition would have silently skipped the one test that is
supposed to be the binding enforcement. With the ``CI`` check, this module's tests never skip in
CI, period -- see ``.github/workflows/ci.yml``'s step, which additionally runs with
``env -u TBOT_ALLOW_ORDER_CODE`` and separately fails the build if anything in this file is
reported as skipped, as defense in depth on top of this predicate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO_ROOT / ".claude" / "hooks"
_HOOK_SCRIPT = _HOOKS_DIR / "block_order_code.py"
_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from order_code_patterns import find_matches  # noqa: E402


def _should_skip_audit(env: Mapping[str, str]) -> bool:
    """The module skip predicate, factored out so it can be unit-tested directly.

    Skips only on a developer's own machine where Parham deliberately set
    ``TBOT_ALLOW_ORDER_CODE=1`` *and* ``CI`` is not set. GitHub Actions (and most other CI
    systems) set ``CI`` unconditionally, so this is false in CI even if ``TBOT_ALLOW_ORDER_CODE``
    were somehow set there too.
    """
    return env.get("TBOT_ALLOW_ORDER_CODE") == "1" and not env.get("CI")


pytestmark = pytest.mark.skipif(
    _should_skip_audit(os.environ),
    reason="TBOT_ALLOW_ORDER_CODE=1 locally (CI not set): Parham has unlocked order code on his "
    "own machine (phase 5b); this never applies in CI, see .github/workflows/ci.yml",
)


_SCANNED_DIRS: tuple[str, ...] = ("src", "scripts", "deploy")
_SCANNED_GLOBS: tuple[str, ...] = ("*.py", "*.sh", "*.yml", "*.yaml")


def _scanned_files() -> list[Path]:
    """MINOR-2: exact twin of the hook's directory scope (`src/`, `scripts/`, `deploy/`).

    The hook treats ANY file under those three directories as in-scope (it only ever looks at the
    one file a single Write/Edit/MultiEdit/NotebookEdit call names, so there is no tree-glob cost
    to worry about); this audit has to enumerate actual files on disk, so it is restricted to the
    extensions the pattern module understands text-scanning for.
    """
    files: list[Path] = []
    for sub in _SCANNED_DIRS:
        base = _REPO_ROOT / sub
        for glob in _SCANNED_GLOBS:
            files.extend(sorted(base.rglob(glob)))
    return files


# ---------------------------------------------------------------------------
# 1. The audit itself: zero forbidden-pattern matches anywhere under src/, scripts/ or deploy/.
# ---------------------------------------------------------------------------


def test_no_order_code_under_src_scripts_or_deploy() -> None:
    files = _scanned_files()
    assert files, "expected at least one scanned file under src/, scripts/ or deploy/ -- audit found none"

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

    Guards against a future refactor silently narrowing `_scanned_files` to the point where it
    scans nothing (or drops a whole directory/extension) and the test above passes for the wrong
    reason.
    """
    relnames = {p.relative_to(_REPO_ROOT).as_posix() for p in _scanned_files()}
    for expected in (
        "src/tbot/core/types.py",
        "src/tbot/execution/tabdeal_client.py",
        "src/tbot/data/tabdeal_recorder.py",
        "scripts/tabdeal_probe.py",
        "deploy/docker-compose.yml",
        "deploy/healthcheck.py",
    ):
        assert expected in relnames, f"expected {expected} to be scanned by the order-code audit"


def test_skip_predicate_never_skips_in_ci() -> None:
    """Unit test for the real CI-bypass fix: TBOT_ALLOW_ORDER_CODE=1 reaching CI must not skip."""
    assert _should_skip_audit({"TBOT_ALLOW_ORDER_CODE": "1", "CI": "true"}) is False
    assert _should_skip_audit({"TBOT_ALLOW_ORDER_CODE": "1", "CI": "1"}) is False
    assert _should_skip_audit({"TBOT_ALLOW_ORDER_CODE": "1"}) is True
    assert _should_skip_audit({}) is False
    assert _should_skip_audit({"CI": "true"}) is False


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
        # MINOR-1 false positives (reviewer repro): generic `.put(`/`.post(` on a non-HTTP receiver.
        "self._events.put(event)\n",
        "await queue.put(x)\n",
        "ledger.post(entry)\n",
        "portfolio.record_withdrawal(amount)\n",
        # "GET" must stay allowed (tabdeal_client.py uses method="GET" throughout).
        'method = "GET"\nrequest = self._http.build_request(method, url)\n',
        # Prose words ("withdraw", "order/") inside a docstring/comment Edit fragment must pass --
        # triple-quoted text is blanked wholesale, line comments are stripped.
        '"""\nThis explains the withdraw process and order/ handling in prose.\n"""\n',
        '# see the withdraw and order/ sections of the docs for background\nx = 1\n',
    ],
)
def test_known_safe_snippets_produce_no_matches(safe_snippet: str) -> None:
    assert find_matches(safe_snippet) == []


@pytest.mark.parametrize(
    "unsafe_snippet,expected_pattern_substring",
    [
        ('httpx.post("https://api1.tabdeal.org/api/v1/order", json={})', "verb-call"),
        ('requests.delete(f"/api/v1/order/{client_order_id}")', "verb-call"),
        ('resp = session.put(url, json=payload)\nmethod = "PUT"', "verb-call"),
        ('endpoint = "/api/v1/openOrders"', "openOrders"),
        ('endpoint = "/api/v1/allOrders"', "allOrders"),
        ('url = base + "/order/oco"', "oco"),
        ('path = "/margin/borrow"', "/margin"),
        ('resp = client.post("/api/v1/withdraw")', "withdraw"),
        ('stream_url = "wss://api1.tabdeal.org/stream/" + userDataStreamKey', "userDataStream"),
        ('params = {"listenKey": key}', "listenKey"),
        ('params["newClientOrderId"] = coid', "newClientOrderId"),
        ('params["origClientOrderId"] = coid', "origClientOrderId"),
        # M-3 additions (reviewer-found bypasses of the pre-fix pattern set):
        ('self._http.build_request("POST", self._url("order"))', "order"),
        ('client.request("DELETE", ORDER_PATH)', "verb-literal"),
        ('client.stream("POST", url)', "verb-literal"),
        ('posixpath.join(prefix, "order")', "order"),
        ('path = "/api/v1/orders"', "order"),
        ('self._http.withdraw(amount)', "withdraw"),
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


# --- M-2: the full protected set is never unlocked, even with TBOT_ALLOW_ORDER_CODE=1. ---------


def test_hook_blocks_settings_local_json_disable_all_hooks_even_unlocked() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": ".claude/settings.local.json",
            "content": '{"disableAllHooks": true}',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT, env_extra={"TBOT_ALLOW_ORDER_CODE": "1"})
    assert result.returncode == 2


def test_hook_blocks_audit_test_skip_edit_even_unlocked() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "tests/test_order_code_audit.py",
            "content": "import pytest\npytest.skip('nope')\n",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT, env_extra={"TBOT_ALLOW_ORDER_CODE": "1"})
    assert result.returncode == 2


def test_hook_blocks_ci_workflow_edit_even_unlocked() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": ".github/workflows/ci.yml",
            "old_string": "a",
            "new_string": "b",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT, env_extra={"TBOT_ALLOW_ORDER_CODE": "1"})
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


def test_hook_invalid_utf8_stdin_fails_closed() -> None:
    """MINOR-13: stdin is read as bytes and decoded explicitly; bad bytes must fail closed."""
    result = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=b"\xff\xfe not valid utf-8 \x80\x81",
        capture_output=True,
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


# --- NotebookEdit support (NIT) -------------------------------------------------------------


def test_hook_blocks_notebook_edit_with_order_code() -> None:
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": "scripts/evil.ipynb",
            "cell_id": "abc123",
            "new_source": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_allows_clean_notebook_edit() -> None:
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": "scripts/clean.ipynb",
            "cell_id": "abc123",
            "new_source": "x = 1\n",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_notebook_edit_delete_mode_without_new_source_is_allowed() -> None:
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": "scripts/clean.ipynb",
            "cell_id": "abc123",
            "edit_mode": "delete",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 0


def test_hook_blocks_notebook_edit_of_self_protected_path() -> None:
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": ".claude/settings.json",
            "cell_id": "abc123",
            "new_source": "x = 1\n",
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


# --- M-1: path-normalization bypasses the reviewer found (sibling worktree, case, MSYS, UNC) ----


def test_hook_sibling_worktree_path_is_in_scope() -> None:
    """``trade-bot-design`` "starts with" ``trade-bot`` only as a string; a prefix-only scope
    check (the pre-fix bug) would wrongly treat it as outside the project and allow the edit."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\projects\\trade-bot-design\\src\\tbot\\execution\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_uppercase_drive_segment_case_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\projects\\trade-bot\\SRC\\tbot\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_differently_cased_path_segment_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\Projects\\trade-bot\\src\\tbot\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_msys_git_bash_style_path_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/f/projects/trade-bot/src/tbot/execution/x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_extended_length_unc_style_path_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "\\\\?\\F:\\projects\\trade-bot\\src\\tbot\\execution\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_self_protection_suffix_match_is_case_insensitive() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "F:\\projects\\trade-bot\\.claude\\Settings.json", "content": "{}"},
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_self_protection_suffix_does_not_match_unrelated_dir_name() -> None:
    """``notatests/test_order_code_audit.py`` must NOT be treated as the protected
    ``tests/test_order_code_audit.py`` -- a naive (non-segment-aligned) suffix check would
    incorrectly match here because the characters happen to overlap at the ``tests/`` boundary."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "notatests/test_order_code_audit.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    # Not self-protected, and not in scope (no src/scripts/deploy segment) -- allowed.
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# 4. Exact-settings-command subprocess test (NIT): the literal command line from settings.json.
# ---------------------------------------------------------------------------

_BASH_AND_UV_AVAILABLE = shutil.which("bash") is not None and shutil.which("uv") is not None


@pytest.mark.skipif(not _BASH_AND_UV_AVAILABLE, reason="bash and/or uv not found on PATH")
def test_settings_json_hook_command_blocks_and_allows_end_to_end() -> None:
    """Runs the EXACT command string configured in `.claude/settings.json`'s PreToolUse hook,
    through bash, for a blocked and an allowed payload -- not a reimplementation of it."""
    settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(_REPO_ROOT)
    env.pop("TBOT_ALLOW_ORDER_CODE", None)

    blocked_payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/tbot/execution/evil.py",
                "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
            },
        }
    )
    result = subprocess.run(
        ["bash", "-c", command], input=blocked_payload, capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 2, result.stderr

    allowed_payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/tbot/data/clean.py",
                "content": "def f() -> int:\n    return 1\n",
            },
        }
    )
    result = subprocess.run(
        ["bash", "-c", command], input=allowed_payload, capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
