#!/usr/bin/env python3
"""Claude Code ``PreToolUse`` hook: order-code guardrail (CLAUDE.md section 3.6 / SPEC D-052).

Reads a single JSON payload from stdin (the shape Claude Code's ``PreToolUse`` hook protocol
sends for ``Write``/``Edit``/``MultiEdit``), decides whether the tool call should be allowed, and
communicates the decision purely through the process exit code:

* ``0``  -- allow the tool call.
* ``2``  -- block it. Claude Code feeds stderr back to the model, so the message on stderr is
            written for Claude (and, through it, for Parham) to read, not for a human terminal.

Scope
-----
Only two things ever get blocked:

1. **Self-protection** (always on, regardless of ``TBOT_ALLOW_ORDER_CODE``): edits to this file,
   to ``order_code_patterns.py``, or to ``.claude/settings.json``. Nobody -- not even with the
   unlock env var set -- gets to disarm the guardrail through Claude; only Parham, editing those
   files by hand outside of Claude, can do that.
2. **Order-code patterns** (see ``order_code_patterns.py``) newly introduced into a file under
   ``src/`` or ``scripts/``. "Newly introduced" matters for ``Edit``/``MultiEdit``: the new text
   (``new_string``) is compared against the old text (``old_string``) for the same edit, and only
   a match that is NOT already present in the old text blocks the call -- editing an unrelated
   line of a file is never blocked by pre-existing text (there is none today; see the audit test).
   ``Write`` has no "old" counterpart in the payload, so its full new content is scanned directly.

Everything else -- a path outside ``src/``/``scripts/``, a tool other than
``Write``/``Edit``/``MultiEdit``, or ``TBOT_ALLOW_ORDER_CODE=1`` for a non-self-protected path --
is allowed (exit 0).

Fail-closed policy
-------------------
Any exception while handling a payload for a path that turns out to be in scope (self-protected or
under src/scripts), any unreadable/non-JSON stdin, or a recognized tool whose ``tool_input`` does
not have the shape this hook expects, all exit 2 with a clear stderr message -- "we could not prove
this is safe" blocks rather than silently allowing. A payload this hook does not recognize as
one of the three tools it cares about, or whose ``file_path`` resolves out of scope, is the only
path that exits 0 without full analysis.

Known limitation: this hook only ever sees edits made through Claude Code's own
Write/Edit/MultiEdit tools. A shell command (``echo >> file``, ``sed -i``, a script Claude runs
via Bash) bypasses it completely. That is exactly why ``tests/test_order_code_audit.py`` plus the
CI step that runs it are the binding enforcement (CLAUDE.md section 3.6); this hook is a fast,
local, best-effort nudge on top.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Importable whether invoked as `python block_order_code.py` (script dir on sys.path by default)
# or from an odd cwd -- make sure the sibling module is reachable either way.
_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from order_code_patterns import Finding, find_matches  # noqa: E402

_SELF_PROTECTED_RELPATHS: frozenset[str] = frozenset(
    {
        ".claude/hooks/block_order_code.py",
        ".claude/hooks/order_code_patterns.py",
        ".claude/settings.json",
    }
)

_IN_SCOPE_PREFIXES: tuple[str, ...] = ("src/", "scripts/")

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")

_GUARDRAIL_REFERENCE = "CLAUDE.md section 3.6 / SPEC D-052"


def _to_posix(path: str) -> str:
    return path.replace("\\", "/")


def _is_absolute_posix(path: str) -> bool:
    return path.startswith("/") or bool(_WINDOWS_DRIVE_RE.match(path))


def _starts_with(path: str, prefix: str) -> bool:
    """Prefix check, case-insensitive only for a Windows drive-letter path.

    Windows drive letters are case-insensitive (``F:\\...`` and ``f:\\...`` are the same path);
    everything else in this repo lives on a case-sensitive filesystem (the Ubuntu server, and
    Linux CI), so the rest of the path is compared exactly.
    """
    if _WINDOWS_DRIVE_RE.match(path) and _WINDOWS_DRIVE_RE.match(prefix):
        drive_len = 2  # "C:"
        if path[:drive_len].lower() != prefix[:drive_len].lower():
            return False
        path, prefix = path[drive_len:], prefix[drive_len:]
    return path == prefix.rstrip("/") or path.startswith(prefix)


def resolve_relative_path(file_path: str, project_dir: str) -> str:
    """Return a POSIX-style path relative to ``project_dir``, with ``..`` resolved lexically.

    Handles Windows backslashes, Windows drive-letter absolute paths, POSIX absolute paths, and
    relative paths. If ``file_path`` cannot be placed under ``project_dir`` (e.g. it names a
    completely different drive), the best-effort lexically-normalized path is returned -- it will
    simply fail every scope/self-protection check below, which is the safe outcome.
    """
    posix_file = _to_posix(file_path)
    posix_project = _to_posix(project_dir).rstrip("/")

    candidate = posix_file if _is_absolute_posix(posix_file) else posix_project + "/" + posix_file

    normalized = _normpath_posix(candidate)

    if _starts_with(normalized, posix_project):
        return normalized[len(posix_project) :].lstrip("/")
    return normalized.lstrip("/")


def _normpath_posix(path: str) -> str:
    """``posixpath.normpath`` equivalent that treats the string purely as POSIX, no OS lookup."""
    is_abs = path.startswith("/")
    drive = ""
    if _WINDOWS_DRIVE_RE.match(path):
        drive, path = path[:2], path[2:]
        is_abs = True
    parts = [p for p in path.split("/") if p not in ("", ".")]
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved and resolved[-1] != "..":
                resolved.pop()
            elif not is_abs:
                resolved.append(part)
        else:
            resolved.append(part)
    result = "/".join(resolved)
    if is_abs:
        result = "/" + result
    return drive + result


def is_self_protected(rel_path: str) -> bool:
    return rel_path in _SELF_PROTECTED_RELPATHS


def is_in_scope(rel_path: str) -> bool:
    return rel_path.startswith(_IN_SCOPE_PREFIXES)


def _extract_text_pairs(tool_name: str, tool_input: dict[str, object]) -> list[tuple[str, str]]:
    """Return a list of ``(old_text, new_text)`` pairs to scan.

    Raises ``ValueError`` for a payload shape this hook does not recognize -- the caller treats
    that as "unknown tool payload for an in-scope path" and fails closed (exit 2).
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise ValueError("Write tool_input.content must be a string")
        return [("", content)]

    if tool_name == "Edit":
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise ValueError("Edit tool_input requires string old_string/new_string")
        return [(old_string, new_string)]

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("MultiEdit tool_input.edits must be a non-empty list")
        pairs: list[tuple[str, str]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("MultiEdit edits entries must be objects")
            old_string = edit.get("old_string")
            new_string = edit.get("new_string")
            if not isinstance(old_string, str) or not isinstance(new_string, str):
                raise ValueError("MultiEdit edit requires string old_string/new_string")
            pairs.append((old_string, new_string))
        return pairs

    raise ValueError(f"unsupported tool_name for an in-scope path: {tool_name!r}")


def _newly_introduced_matches(old_text: str, new_text: str) -> list[Finding]:
    """Findings in ``new_text`` beyond what the same pattern already matched in ``old_text``.

    Counts matches per pattern name (not exact (name, line) pairs, since a line's number can
    shift between old_string and new_string) -- if ``new_text`` has strictly more occurrences of
    a given pattern than ``old_text`` did, the extra occurrences are "newly introduced" and block
    the call. ``old_text`` is always ``""`` for ``Write`` (see module docstring), so every match
    in the new content counts as newly introduced, matching the spec's "for Write the full new
    content" rule.
    """
    old_counts = Counter(name for name, _, _ in find_matches(old_text))
    new_matches = find_matches(new_text)
    seen_counts: dict[str, int] = {}
    introduced: list[Finding] = []
    for name, lineno, line in new_matches:
        seen_counts[name] = seen_counts.get(name, 0) + 1
        if seen_counts[name] > old_counts.get(name, 0):
            introduced.append((name, lineno, line))
    return introduced


def _block(message: str) -> int:
    print(f"block_order_code: {message}", file=sys.stderr)
    return 2


def handle_payload(payload: dict[str, object]) -> int:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")

    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0  # not a tool this guardrail cares about
    if not isinstance(tool_input, dict):
        return _block(f"tool_input must be an object for {tool_name}, got {type(tool_input).__name__}")

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return _block("tool_input.file_path is missing or not a string")

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
    rel_path = resolve_relative_path(file_path, project_dir)

    if is_self_protected(rel_path):
        return _block(
            f"BLOCKED - {rel_path} is a guardrail file (hook / pattern module / settings) and "
            f"cannot be edited by Claude ({_GUARDRAIL_REFERENCE}). Parham may edit it by hand "
            f"outside Claude Code."
        )

    if not is_in_scope(rel_path):
        return 0

    try:
        text_pairs = _extract_text_pairs(str(tool_name), tool_input)
    except ValueError as exc:
        return _block(f"{exc} (path in scope: {rel_path})")

    if os.environ.get("TBOT_ALLOW_ORDER_CODE") == "1":
        return 0  # unlocked by Parham; self-protection above still applied unconditionally

    for old_text, new_text in text_pairs:
        introduced = _newly_introduced_matches(old_text, new_text)
        if introduced:
            name, lineno, line = introduced[0]
            snippet = line.strip()[:120]
            return _block(
                f"BLOCKED {rel_path}:{lineno} - matched '{name}' ({snippet!r}). Exchange "
                f"order/cancel/OCO/margin/withdrawal code is not allowed before phase 5b "
                f"({_GUARDRAIL_REFERENCE}). Unlock: Parham sets TBOT_ALLOW_ORDER_CODE=1 himself."
            )

    return 0


def main() -> int:
    try:
        raw_stdin = sys.stdin.read()
    except Exception as exc:  # fail closed on any stdin read error
        return _block(f"failed to read stdin: {exc}")

    try:
        payload = json.loads(raw_stdin)
    except Exception as exc:  # fail closed on unparseable JSON
        return _block(f"invalid JSON on stdin: {exc}")

    if not isinstance(payload, dict):
        return _block(f"top-level JSON payload must be an object, got {type(payload).__name__}")

    try:
        return handle_payload(payload)
    except Exception as exc:  # fail closed on any unexpected error
        return _block(f"unexpected error: {exc}")


if __name__ == "__main__":
    sys.exit(main())
