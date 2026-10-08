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
import re
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


_SCANNED_DIRS: tuple[str, ...] = ("src", "scripts", "deploy", "config")
# round 3 (NIT): added Dockerfile (and a `Dockerfile.*` variant, e.g. a future
# `Dockerfile.recorder`), `*.service`/`*.timer` (systemd units -- `deploy/systemd/` ships a
# `.service` today and could grow a `.timer`), `*.ipynb` (research notebooks can land under
# `scripts/`) and `*.toml` (a tool config under `config/` or `deploy/`). None of these were
# scanned before even though the hook's `is_in_scope` treats ANY file under `src/scripts/deploy`
# (now also `config`) as in-scope regardless of extension -- the audit's extension list was
# narrower than the hook's real scope, which is exactly the kind of drift section 3.6 says the
# audit must not have relative to the hook.
_SCANNED_GLOBS: tuple[str, ...] = (
    "*.py",
    "*.sh",
    "*.yml",
    "*.yaml",
    "*.toml",
    "*.ipynb",
    "*.service",
    "*.timer",
    "Dockerfile",
    "Dockerfile.*",
)


def _scanned_files() -> list[Path]:
    """MINOR-2: exact twin of the hook's directory scope (`src/`, `scripts/`, `deploy/`,
    `config/`).

    The hook treats ANY file under those four directories as in-scope (it only ever looks at the
    one file a single Write/Edit/MultiEdit/NotebookEdit call names, so there is no tree-glob cost
    to worry about); this audit has to enumerate actual files on disk, so it is restricted to the
    extensions/filenames the pattern module understands text-scanning for (see `_SCANNED_GLOBS`).
    """
    files: list[Path] = []
    for sub in _SCANNED_DIRS:
        base = _REPO_ROOT / sub
        for glob in _SCANNED_GLOBS:
            files.extend(sorted(base.rglob(glob)))
    return sorted(set(files))


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
        "deploy/Dockerfile",
        "deploy/systemd/tbot-recorder.service",
        "config/default.yaml",
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
        # --- round 3 (reviewer-verified false positives of the round-2 pattern set) ---------------
        # `literal:withdraw` (round 2) matched any quote-withdraw*-quote, including a plain
        # ledger-entry-type constant with no "/" and nothing exchange-shaped about it at all.
        'WITHDRAWAL = "withdrawal"\n',
        # `literal:order` (round 2) matched any quote-order(s)-quote; all three of these are a
        # pandas sort key, a local SQL/variable table name, and a local CSV path -- none of them
        # calls anything or builds a URL. Round 3's `literal:order` only fires when the literal is
        # an argument to a `*.join(`/`*_url(`/`*urljoin(` call (see `order_code_patterns.py`).
        'df.sort_values("order")\n',
        'table = "orders"\n',
        # `path:/order` (round 2) matched a bare `/orders?` word-boundary regardless of context, so
        # a local file path and a division expression both tripped it just because the substring
        # "/order" happens to share the same boundary shape as a real endpoint. Round 3 additionally
        # requires the literal token `api` on the same line (every real Tabdeal endpoint in this
        # codebase is `/api/v1/...` or `/r/api/v1/...`; see CLAUDE.md section 6).
        'from pathlib import Path\nPath("data/orders.csv")\n',
        'avg = total/orders\n',
        # `http:verb-call`/`http:withdraw-call` (round 2) used `\b(?:http|client|session|httpx|\n`
        # `requests|_http)\w*\.` as the receiver prefix -- `\w*` after the base word let a RECEIVER
        # that merely *starts with* one of those words (but means something unrelated) through:
        # a cache/store/queue keyed by client/session/request objects, not an HTTP client itself.
        "client_cache.put(k, v)\n",
        "session_store.put(x, y)\n",
        "requests_seen.put(rid)\n",
        # --- round 4 (MAJOR-C fix + m-c/m-e review): false positives the round-4 fix set must
        # NOT reopen ------------------------------------------------------------------------------
        # `path:/order` dropped the "api on the same line" requirement; re-verify its round-3
        # false positives still pass under the new endpoint-shaped-literal pattern.
        'from pathlib import Path\nPath("data/orders.csv")\n',
        'avg = total/orders\n',
        'df.sort_values("order")\n',
        'table = "orders"\n',
        # `path:/order-bare` (new, YAML/TOML-shaped) must not fire on a type-annotated assignment
        # or a bare division expression that merely happens to contain a colon elsewhere.
        'ratio: float = total/orders\n',
        # The real `tabdeal_client.py` house-style f-string reads -- round 4 stops blanking an
        # f-string's literal text for "contains whitespace" (that whitespace lives inside the
        # `{...}` expression, not the literal tail), so these must stay clean on their own merit,
        # not because the whole string got blanked.
        'self._get(f"{prefix or self._read_prefix}/trades", lambda: {"symbol": symbol})\n',
        'return self._get(f"{prefix or self._read_prefix}/depth", lambda: {"symbol": symbol})\n',
        'return self._get(f"{prefix or self._read_prefix}/exchangeInfo", lambda: {})\n',
        'return self._get(f"{prefix or self._read_prefix}/ping", lambda: {})\n',
        'return self._get(f"{prefix or self._read_prefix}/time", lambda: {})\n',
        'return self._get(f"{prefix or self._read_prefix}/account", _build, signed=True)\n',
        # The widened `_RECEIVER_RE` (round 4 adds api/rest/transport/aiohttp) must not reopen the
        # round-3 "receiver that merely starts with a base word" gap for the new words either.
        "api_client.get(x)\n",
        "rest_store.put(x, y)\n",
        "transportation.post(x)\n",
        # `get_order` (core/types.py's Broker protocol) must still pass -- round 4 deliberately did
        # NOT add it to `sdk:order-method-call` even though `delete_order`/`cancel_replace` were
        # added for the same pattern shape.
        "def get_order(self, client_order_id: str) -> OrderStatus: ...\n",
        'class Broker(Protocol):\n    def get_order(self, client_order_id: str) -> OrderStatus: ...\n',
    ],
)
def test_known_safe_snippets_produce_no_matches(safe_snippet: str) -> None:
    assert find_matches(safe_snippet) == []


def test_known_safe_snippet_op_delete_is_accepted_as_still_blocked() -> None:
    """Round 3 (reviewer-verified, deliberately NOT fixed): a bare ``op = "DELETE"`` assignment,
    with no receiver and no other exchange context at all, still matches ``http:verb-literal``.

    Decision (documented, not a bug): nothing under ``src/``, ``scripts/``, ``deploy/`` or
    ``config/`` has a legitimate reason to assign the literal string ``"DELETE"``/``"POST"``/
    ``"PUT"`` before phase 5b, and narrowing `http:verb-literal` to require more surrounding
    context (e.g. a nearby call) would reopen the receiver-free verb-literal bypass it exists to
    catch in the first place (`client.request("DELETE", ...)`, `client.stream("POST", url)` --
    see `test_known_unsafe_snippets_are_detected`). If a genuinely unrelated, legitimate use of a
    bare ``"DELETE"``/``"POST"``/``"PUT"`` literal shows up before phase 5b, Parham decides then
    whether to narrow this pattern or just use ``TBOT_ALLOW_ORDER_CODE=1`` for that one edit.
    """
    findings = find_matches('op = "DELETE"\n')
    assert findings
    assert any("verb-literal" in name for name, _, _ in findings)


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
        # --- round 3 additions ------------------------------------------------------------------
        # Receiver-prefix gap: `\b` does not fire before `self._client.post(` etc. because `_` is
        # a word character -- only the hardcoded `_http` alternative matched a leading underscore
        # receiver before. See `order_code_patterns.py`'s `_RECEIVER_RE`.
        ('self._client.post(url, json=payload)', "verb-call"),
        ('self._session.delete(url)', "verb-call"),
        ('self._client.withdraw(amount)', "withdraw-call"),
        # SDK-style method calls with no post/delete/put verb and no http-ish receiver at all --
        # the pre-round-3 pattern set matched nothing for any of these.
        ('client.new_order(symbol="BTCUSDT", side="BUY")', "order-method"),
        ('exchange.create_order(symbol, qty)', "order-method"),
        ('self.cancel_order(client_order_id)', "order-method"),
        ('self._client.cancel_all_orders(symbol="BTCUSDT")', "order-method"),
        ('self.cancel_open_orders(symbol)', "order-method"),
        ('self.place_order(req)', "order-method"),
        ('client.new_oco_order(**params)', "order-method"),
        ('client.create_stop_limit_order(**params)', "order-method"),
        # --- round 4 additions (MAJOR-C fix + m-c/m-e review) ------------------------------------
        # `path:/order` regression: a bare module-level constant with no `api` token anywhere on
        # the line (round 3's bypass).
        ('ORDER_PATH = "/order"', "path:/order"),
        ('BASE = "https://api1.tabdeal.org"\nurl = BASE + "/order"', "path:/order"),
        # The YAML/TOML-shaped bare (unquoted) path value.
        ('order_path: /order', "path:/order-bare"),
        # f-string masking regression: round 3's whitespace-exemption blanked the WHOLE f-string
        # (including the literal `/openOrders`/`/order` tail) just because the `{...}` expression
        # contained spaces.
        (
            'self._get(f"{prefix or self._read_prefix}/openOrders", lambda: {})',
            "openOrders",
        ),
        (
            'return self._request(HTTPMethod.DELETE, f"{prefix or self._write_prefix}/order", '
            'lambda: {"symbol": symbol, "orderId": order_id}, signed=True)',
            "path:/order",
        ),
        # `HTTPMethod.POST`/`.DELETE`/`.PUT`/`.PATCH` enum-member verb spelling.
        ('self._request(HTTPMethod.DELETE, url)', "verb-enum"),
        ('self._request(HTTPMethod.POST, url)', "verb-enum"),
        ('self._request(HTTPMethod.PUT, url)', "verb-enum"),
        ('self._request(HTTPMethod.PATCH, url)', "verb-enum"),
        # Lowercase/mixed-case positional verb literal (round 3 made `http:verb-literal`
        # case-sensitive-uppercase-only, which missed this).
        ('client.request("post", url)', "verb-literal"),
        ('client.request("Post", url)', "verb-literal"),
        ('client.stream("delete", url)', "verb-literal"),
        # Widened receivers: `_api`, `_rest`, `transport`, `_aiohttp`.
        ('self._api.post(url, json=payload)', "verb-call"),
        ('self._rest.post(url)', "verb-call"),
        ('transport.post(url)', "verb-call"),
        ('self._aiohttp.post(url)', "verb-call"),
        # New SDK method names.
        ('self.delete_order(client_order_id)', "order-method"),
        ('self._client.cancel_replace(client_order_id)', "order-method"),
        # `.delete_order(` with a plain unprefixed receiver name, exactly as a reviewer wrote it.
        ('order_client.delete_order(coid)', "order-method"),
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
# 2b. round 4 (MAJOR-C): the two exact reviewer-reported regression snippets, verbatim.
# ---------------------------------------------------------------------------

# Exactly as the third T6 review reported it: a bare path constant with no `api` token nearby,
# then a receiver (`self._api`) round 3's `_RECEIVER_RE` did not recognize at all.
_SNIPPET_B = (
    'ORDER_PATH = "/order"\n'
    "resp = self._api.post(self._prefix + ORDER_PATH, data={\"symbol\": symbol, \"side\": \"BUY\", "
    '"type": "MARKET", "quantity": qty})\n'
)

# Exactly as the third T6 review reported it: `tabdeal_client.py`'s own house style
# (`self._request(HTTPMethod.<VERB>, f"{prefix or self._write_prefix}/order", ...)`), which round
# 3 missed in both rounds because the f-string's `/order` tail was blanked wholesale (MINOR-5
# whitespace exemption) and the verb was an enum member, not a string literal.
_SNIPPET_A = (
    "request = self._http.build_request(method, target, params=build())\n"
    'return self._request(HTTPMethod.DELETE, f"{prefix or self._write_prefix}/order", '
    'lambda: {"symbol": symbol, "orderId": order_id}, signed=True)\n'
    'return self._request(HTTPMethod.POST, f"{prefix or self._write_prefix}/order", '
    'lambda: {"quoteOrderQty": q}, signed=True)\n'
)


def test_snippet_b_receiver_and_bare_path_constant_is_blocked() -> None:
    findings = find_matches(_SNIPPET_B)
    names = {name for name, _, _ in findings}
    assert "path:/order" in names, findings
    assert any("verb-call" in name for name in names), findings


def test_snippet_a_house_style_http_method_enum_and_fstring_tail_is_blocked() -> None:
    findings = find_matches(_SNIPPET_A)
    names = {name for name, _, _ in findings}
    assert "path:/order" in names, findings
    assert any("verb-enum" in name for name in names), findings


def test_snippet_a_and_b_hook_subprocess_blocks_write() -> None:
    """End-to-end (not just the shared pattern module): the real hook, run as a subprocess against
    a synthetic Write payload, blocks both exact regression snippets."""
    for snippet in (_SNIPPET_A, _SNIPPET_B):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "src/tbot/execution/evil.py", "content": snippet},
        }
        result = _run_hook(payload, project_dir=_REPO_ROOT)
        assert result.returncode == 2, (snippet, result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# 2c. round 4: unit coverage for the new f-string-aware masking mechanism itself.
# ---------------------------------------------------------------------------


def test_fstring_brace_content_is_blanked_but_literal_tail_survives() -> None:
    from order_code_patterns import mask_text

    masked = mask_text('url = f"{prefix or self._read_prefix}/openOrders"\n')
    # the expression content must be gone...
    assert "self._read_prefix" not in masked
    # ...but the literal tail must survive untouched.
    assert "/openOrders" in masked
    # and the overall line length (hence every later line-number computation) is unchanged.
    assert len(masked.splitlines()[0]) == len('url = f"{prefix or self._read_prefix}/openOrders"')


def test_plain_non_fstring_whitespace_literal_is_still_blanked_wholesale() -> None:
    """The round-4 fix is scoped to f-strings only -- a PLAIN string with a space in it (a log
    line, a CLI warning) must still be blanked wholesale, exactly as before round 4."""
    from order_code_patterns import mask_text

    masked = mask_text('print("verify NO withdrawal permission for /order endpoints")\n')
    assert "withdrawal" not in masked.lower()
    assert "/order" not in masked


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


# --- round 3: config/ joins the in-scope set ------------------------------------------------


def test_hook_config_dir_is_in_scope() -> None:
    """``config/default.yaml`` already holds the Tabdeal endpoint prefixes (CLAUDE.md section 6)
    -- round 3 adds `config` to `_IN_SCOPE_SEGMENTS` so an edit introducing an order-shaped value
    there is caught the same way `src/scripts/deploy` already are."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "config/default.yaml",
            "old_string": "exchange:",
            "new_string": 'exchange:\n  order_endpoint: "/api/v1/order"',
        },
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


# --- round 3: whole-`.claude/hooks/` self-protection (not just the two named files) -----------


def test_hook_blocks_edit_of_any_new_file_under_hooks_dir() -> None:
    """A brand-new file under ``.claude/hooks/`` (not one of the two explicitly named in
    ``_PROTECTED_SUFFIXES``) must still be self-protected -- round 3 protects the whole
    directory, closing the ``.claude/hooks/json.py`` stdlib-shadowing gap a reviewer found (see
    ``test_hook_command_with_dash_p_defeats_json_shadow_attack`` below) and any similar future
    sibling-module trick."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": ".claude/hooks/helpers.py", "content": "x = 1\n"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    assert result.returncode == 2


def test_hook_does_not_protect_unrelated_dir_named_hooksy() -> None:
    """``.claude/hooksy/x.py`` is a different directory from ``.claude/hooks/`` -- the
    consecutive-segment check must not fire on a mere prefix match."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": ".claude/hooksy/x.py", "content": "x = 1\n"},
    }
    result = _run_hook(payload, project_dir=_REPO_ROOT)
    # Not self-protected, and not in scope either (no src/scripts/deploy/config segment).
    assert result.returncode == 0


# --- round 3: Windows trailing dot/space and NTFS alternate-data-stream path forms ------------


def test_hook_settings_json_trailing_dot_is_self_protected() -> None:
    """Windows collapses a trailing ``.`` when resolving a filename -- ``settings.json.`` IS
    ``settings.json`` on disk."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "F:\\projects\\trade-bot\\.claude\\settings.json.", "content": "{}"},
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_settings_json_trailing_space_is_self_protected() -> None:
    """Windows also collapses a trailing space -- ``settings.json `` IS ``settings.json``."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "F:\\projects\\trade-bot\\.claude\\settings.json ", "content": "{}"},
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_settings_json_ads_suffix_is_self_protected() -> None:
    """An NTFS alternate-data-stream suffix (``::$DATA`` is the default, unnamed stream) still
    names the same base file for every purpose this guardrail cares about."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\projects\\trade-bot\\.claude\\settings.json::$DATA",
            "content": "{}",
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_src_trailing_dot_segment_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\projects\\trade-bot\\src.\\tbot\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_src_trailing_space_segment_is_in_scope() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "F:\\projects\\trade-bot\\src \\tbot\\x.py",
            "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
        },
    }
    result = _run_hook(payload, project_dir=Path("F:\\projects\\trade-bot"))
    assert result.returncode == 2


def test_hook_posix_path_trailing_dot_is_not_cleaned() -> None:
    """The Windows trailing-dot/space/ADS cleanup must NOT apply to a genuine POSIX path (the
    real case on the Ubuntu server and in Linux CI) -- a literal trailing dot or colon is just an
    ordinary character in a filename there, not cruft to strip."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/home/parham/trade-bot/notes.txt.", "content": "hello\n"},
    }
    result = _run_hook(payload, project_dir=Path("/home/parham/trade-bot"))
    # Not self-protected, not in scope -- allowed, and specifically NOT because "notes.txt." got
    # cleaned to something that coincidentally matched; it is simply an ordinary out-of-scope file.
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


# ---------------------------------------------------------------------------
# 5. round 3 (M-2 bypass): `.claude/hooks/json.py` shadowing stdlib `json`, and the `-P` fix.
# ---------------------------------------------------------------------------
#
# A reviewer found that `block_order_code.py`'s own directory sits first on `sys.path` for a
# plain `python block_order_code.py` invocation (CPython prepends the script's directory). A
# `.claude/hooks/json.py` dropped next to the hook -- itself writable via a Write/Edit call
# BEFORE round 3 (see `test_hook_blocks_edit_of_any_new_file_under_hooks_dir` above for that half
# of the fix) -- would shadow the stdlib `json` module the hook's `import json` resolves, letting
# a malicious sibling module make `json.loads` return anything (e.g. an empty payload, which
# `handle_payload` treats as "not a recognized tool" and allows unconditionally). Python's `-P`
# flag ("safe path") stops the interpreter from prepending the script's own directory to
# `sys.path` at startup, so the hook's `import json` (at the very top of the module, before its
# own `sys.path.insert` for `order_code_patterns`) resolves the real stdlib module regardless of
# what sits next to it on disk. `.claude/settings.json`'s hook command now runs
# `python -P ...block_order_code.py`.


def _make_hooks_copy_with_malicious_json(tmp_path: Path) -> Path:
    """Copy the real `.claude/hooks/` dir into `tmp_path` and drop in a `json.py` that defeats
    JSON parsing (so a shadowed `import json` would make the hook allow everything)."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(_HOOKS_DIR, copy_dir)
    (copy_dir / "json.py").write_text(
        "def loads(*_args, **_kwargs):\n"
        "    return {}\n"  # no tool_name -> handle_payload treats it as unrecognized -> allow (0)
        "\n"
        "def dumps(*_args, **_kwargs):\n"
        "    return '{}'\n",
        encoding="utf-8",
    )
    return copy_dir / "block_order_code.py"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="python -P requires 3.11+")
def test_hook_command_with_dash_p_defeats_json_shadow_attack(tmp_path: Path) -> None:
    """With `-P`, a malicious sibling `json.py` next to the hook does NOT shadow stdlib `json` --
    the hook still blocks real order code even with the malicious module present."""
    hook_copy = _make_hooks_copy_with_malicious_json(tmp_path)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(_REPO_ROOT)
    env.pop("TBOT_ALLOW_ORDER_CODE", None)
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/tbot/execution/evil.py",
                "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
            },
        }
    )
    result = subprocess.run(
        [sys.executable, "-P", str(hook_copy)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 2, (
        f"expected -P to defeat the json.py shadow and still block; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_hook_command_without_dash_p_is_vulnerable_to_json_shadow_attack(tmp_path: Path) -> None:
    """Documents the actual vulnerability `-P` fixes: WITHOUT `-P`, the same malicious sibling
    `json.py` DOES shadow stdlib `json` and the hook allows the order-code write through (exit 0)
    -- this is exactly why `.claude/settings.json` must invoke `python -P`, not plain `python`."""
    hook_copy = _make_hooks_copy_with_malicious_json(tmp_path)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(_REPO_ROOT)
    env.pop("TBOT_ALLOW_ORDER_CODE", None)
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/tbot/execution/evil.py",
                "content": 'httpx.post("https://api1.tabdeal.org/api/v1/order")',
            },
        }
    )
    result = subprocess.run(
        [sys.executable, str(hook_copy)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, (
        "expected the json.py shadow to defeat the hook WITHOUT -P (documenting the bug -P "
        f"fixes); stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_settings_json_hook_command_uses_dash_p() -> None:
    """`.claude/settings.json`'s PreToolUse command must invoke `python -P` (not plain `python`)
    -- the actual fix for the json.py-shadow attack the two tests above demonstrate."""
    settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert re.search(r"\bpython\s+-P\b", command), (
        f"expected the hook command to invoke 'python -P ...', got: {command!r}"
    )
