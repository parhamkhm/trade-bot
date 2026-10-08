#!/usr/bin/env python3
"""Claude Code ``PreToolUse`` hook: order-code guardrail (CLAUDE.md section 3.6 / SPEC D-052).

Reads a single JSON payload from stdin (the shape Claude Code's ``PreToolUse`` hook protocol
sends for ``Write``/``Edit``/``MultiEdit``/``NotebookEdit``), decides whether the tool call should
be allowed, and communicates the decision purely through the process exit code:

* ``0``  -- allow the tool call.
* ``2``  -- block it. Claude Code feeds stderr back to the model, so the message on stderr is
            written for Claude (and, through it, for Parham) to read, not for a human terminal.

Scope
-----
Only two things ever get blocked:

1. **Self-protection** (always on, regardless of ``TBOT_ALLOW_ORDER_CODE``): edits to this file,
   to ``order_code_patterns.py``, to ``.claude/settings.json``/``.claude/settings.local.json``, to
   ``tests/test_order_code_audit.py``, or to ``.github/workflows/ci.yml``. Nobody -- not even with
   the unlock env var set -- gets to disarm the guardrail through Claude; only Parham, editing
   those files by hand outside of Claude, can do that. See ``_PROTECTED_SUFFIXES`` below for why
   the comparison is a casefolded path-segment SUFFIX match rather than an exact relative-path
   lookup: it has to survive being edited from a different worktree, a differently-cased drive
   letter, or a Git-Bash-style path, not just the one shape a payload "should" arrive in.
2. **Order-code patterns** (see ``order_code_patterns.py``) newly introduced into a file whose
   normalized path has ``src``, ``scripts`` or ``deploy`` as one of its directory segments (see
   ``is_in_scope``). "Newly introduced" matters for ``Edit``/``MultiEdit``: the new text
   (``new_string``) is compared against the old text (``old_string``) for the same edit, and only
   a match that is NOT already present in the old text blocks the call -- editing an unrelated
   line of a file is never blocked by pre-existing text (there is none today; see the audit test).
   ``Write`` and ``NotebookEdit`` have no "old" counterpart in the payload, so their full new
   content is scanned directly.

Everything else -- a path with none of those three segments, a tool other than
``Write``/``Edit``/``MultiEdit``/``NotebookEdit``, or ``TBOT_ALLOW_ORDER_CODE=1`` for a
non-self-protected path -- is allowed (exit 0).

Fail-closed policy
-------------------
Any exception while handling a payload for a path that turns out to be in scope (self-protected or
under src/scripts/deploy), any unreadable/non-UTF-8/non-JSON stdin, or a recognized tool whose
``tool_input`` does not have the shape this hook expects, all exit 2 with a clear stderr message --
"we could not prove this is safe" blocks rather than silently allowing. A payload this hook does
not recognize as one of the tools it cares about, or whose ``file_path``/``notebook_path``
resolves out of scope, is the only path that exits 0 without full analysis.

Known limitation: this hook only ever sees edits made through Claude Code's own
Write/Edit/MultiEdit/NotebookEdit tools. A shell command (``echo >> file``, ``sed -i``, a script
Claude runs via Bash) bypasses it completely. That is exactly why ``tests/test_order_code_audit.py``
plus the CI step that runs it are the binding enforcement (CLAUDE.md section 3.6); this hook is a
fast, local, best-effort nudge on top.

Timeout and fail-open, by design
---------------------------------
``.claude/settings.json`` wires this hook with a 60-second timeout. Claude Code treats a hook that
exceeds its timeout as **non-blocking** (fail open) -- a slow or hung hook process does not stop the
tool call. 60s is generous specifically because fail-open is the alternative to a timeout: this
hook (``uv run --no-project ... python ...``) normally completes in well under a second once uv's
environment is warm, but the very first invocation in a cold environment (resolving the Python
interpreter, no cached venv) can be meaningfully slower, and a guardrail that times out and
silently allows the very thing it exists to block is worse than a guardrail that is merely slow.
Generous-timeout-over-fail-open is the deliberate trade-off; it does not change the fail-**closed**
policy above, which only governs this process's own exit code once it actually runs.

Future narrowing (documentation only -- no change here)
---------------------------------------------------------
Phase 5b (CLAUDE.md section 9) is when order code is deliberately introduced (``TabdealBroker``,
OMS). At that point this blanket src/scripts/deploy-wide guardrail should be narrowed to an
explicit allow-list of the specific, reviewed files phase 5b adds (e.g.
``src/tbot/execution/tabdeal_broker.py``, ``src/tbot/execution/oms.py``) rather than disabled
wholesale via ``TBOT_ALLOW_ORDER_CODE=1`` -- that env var is a blunt, temporary unlock for Parham's
own local iteration, not the intended steady state once order code is a permanent, reviewed part
of the codebase. No allow-list exists yet; this paragraph is a note for whoever does that work.

Phase 7a (manual live, CLAUDE.md section 9) needs a *read-only* signed query -- confirming a stop
Parham placed by hand on the Tabdeal UI is actually resting on the book, via signed GET
``/r/api/v1/openOrders`` (and possibly ``/r/api/v1/allOrders`` for a closed/filled stop) -- before
any order-*placement* or order-*cancellation* code exists (that is still phase 5b). This guardrail
does **not** allow that today: ``endpoint:openOrders`` / ``endpoint:allOrders`` (and ``path:/order``
once ``api`` is also on the line) block it exactly like a real order-placement call, with no
distinction for "read-only" vs "write" -- correctly, since this regex-based tool cannot verify a
call site is actually a GET (see the module's masking limitations) and a wrong allow-list entry
would quietly reopen the order-write surface it exists to close.
Round 3 (this review) deliberately does **not** add that allow-list: it would need (a) a pattern
that matches *only* a signed GET to exactly ``/r/api/v1/openOrders``/``/r/api/v1/allOrders`` and
nothing else shaped like it, and (b) a reviewed, narrow reconciliation module to use it in (neither
exists yet, and inventing the shape of either before the module exists risks over-fitting the
allow-list to guesses). The intended steady state is a short-lived, explicitly reviewed allow-list
entry added here by Parham, by hand, immediately before phase 7a starts -- scoped to the specific
reconciliation call site(s) that need it, not a blanket unblock of the ``openOrders``/``allOrders``
patterns.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# MINOR-4: never create a __pycache__ entry for this hook or its sibling pattern module -- it runs
# on every single Write/Edit/MultiEdit/NotebookEdit call, so a stray .pyc would otherwise be
# written into a version-controlled directory on every edit. Must be set before the
# order_code_patterns import below, which is the one import this process performs that would
# otherwise trigger bytecode compilation.
sys.dont_write_bytecode = True

# Importable whether invoked as `python block_order_code.py` (script dir on sys.path by default)
# or from an odd cwd -- make sure the sibling module is reachable either way.
_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from order_code_patterns import Finding, find_matches  # noqa: E402

# M-2: never unlocked, even with TBOT_ALLOW_ORDER_CODE=1. Checked as a casefolded path-segment
# SUFFIX match (see `is_self_protected`), not an exact relative-path lookup, so every worktree /
# differently-cased-drive / Git-Bash-style copy of these paths is covered, not just the one the
# repo root happens to resolve to in this process's own CLAUDE_PROJECT_DIR.
#
# round 3: kept explicitly (even though `_is_under_hooks_dir` below already covers the two
# ``.claude/hooks/...`` entries as a side effect of protecting the whole directory) as the
# documented, reviewed list of exactly-why-each-one-matters; `is_self_protected` ORs this suffix
# check with the whole-directory check, not one or the other.
_PROTECTED_SUFFIXES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/hooks/block_order_code.py",
    ".claude/hooks/order_code_patterns.py",
    "tests/test_order_code_audit.py",
    ".github/workflows/ci.yml",
)

# round 3 (MINOR): protect every file under `.claude/hooks/`, not just the two named above.
# Concretely, this closes the ``.claude/hooks/json.py`` shadowing bypass a reviewer found (see
# ``main``'s `-P` note below) -- a NEW file dropped into this directory under any name is just as
# capable of subverting the hook (shadow a stdlib module it imports, or simply replace its behavior
# outright if Claude Code's hook invocation ever changes) as editing the two files named above
# directly. ``_HOOKS_DIR_SEGMENTS`` is checked as two *consecutive* path segments (not two
# independent substring checks), so a coincidentally-named `.claude/hooksarchive/x.py` (a different
# directory) is not caught by accident.
_HOOKS_DIR_SEGMENTS: tuple[str, str] = (".claude", "hooks")

# M-1/MINOR-2: "in scope" is "this path has one of these as a directory segment, anywhere" -- not
# "this path starts with one of these prefixes relative to CLAUDE_PROJECT_DIR". A prefix check is
# exactly what a sibling worktree (`trade-bot-design/src/...` "starts with" `trade-bot` only by
# string-literal accident, not by actually being under this project) defeats; a segment check does
# not care where the repo root is, only that *some* ancestor directory in the normalized path is
# named one of these four. Audit test (`tests/test_order_code_audit.py`) scans the same four
# directories for the same extensions (MINOR-2): the hook additionally treats any file under them
# as in-scope regardless of extension, since a single edited file's extension is already known and
# there is no "glob a tree" cost to worry about the way there is for the audit.
# round 3: `config` was added -- `config/default.yaml` already holds the Tabdeal endpoint prefixes
# (`read_prefix`/`write_prefix`) and could grow an order-shaped value (a reviewer-found gap: neither
# this hook nor the audit test looked at it before).
_IN_SCOPE_SEGMENTS: frozenset[str] = frozenset({"src", "scripts", "deploy", "config"})

_EXTENDED_LENGTH_PREFIX = "\\\\?\\"
_MSYS_ABS_RE = re.compile(r"^/([A-Za-z])(/.*)?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")

_GUARDRAIL_REFERENCE = "CLAUDE.md section 3.6 / SPEC D-052"


def _to_posix(path: str) -> str:
    return path.replace("\\", "/")


def _strip_extended_length_prefix(path: str) -> str:
    """Strip Windows' ``\\\\?\\`` extended-length-path prefix, if present."""
    if path.startswith(_EXTENDED_LENGTH_PREFIX):
        return path[len(_EXTENDED_LENGTH_PREFIX) :]
    # The prefix can also arrive already-forward-slashed (e.g. after a prior _to_posix pass).
    if path.startswith("//?/"):
        return path[4:]
    return path


def _msys_to_drive(path: str) -> str:
    """Convert a Git-Bash/MSYS absolute path (``/f/projects/...``) to ``f:/projects/...``.

    Git for Windows' bash (and any MSYS2-derived shell) represents ``F:\\projects`` as
    ``/f/projects`` internally; a command built from ``$CLAUDE_PROJECT_DIR``-style substitution in
    such a shell can hand this hook a path in that form even though the rest of Claude Code, and
    the filesystem underneath, think in drive letters. Only a *single* lower/upper-case ASCII
    letter directly after the leading ``/`` is treated as a drive letter -- a genuine POSIX path
    whose first segment happens to be one letter long (rare) is misread as a Windows path by this
    heuristic, which is an accepted false-positive-toward-blocking trade-off, not a bypass.
    """
    m = _MSYS_ABS_RE.match(path)
    if m:
        drive, rest = m.group(1), m.group(2) or "/"
        return f"{drive}:{rest}"
    return path


def _is_drive_path(path: str) -> bool:
    return bool(_DRIVE_RE.match(path))


def _clean_windows_segment(segment: str) -> str:
    """Strip an NTFS alternate-data-stream suffix and trailing dot/space cruft from one Windows
    path segment (round 3, MINOR).

    Windows silently collapses a trailing ``.``/`` `` (one or more, any mix) off a file or
    directory name when resolving it -- ``settings.json.`` and ``settings.json `` both name the
    exact same file as ``settings.json`` on disk, and ``src./`` / ``src /`` both name the same
    directory as ``src``. It separately lets a filename carry a ``:streamname`` (or the default
    ``::$DATA``) alternate-data-stream suffix that still refers to the *same base file* for every
    purpose this guardrail cares about (self-protection, in-scope-ness). Neither of these is
    POSIX/Linux filesystem behaviour (a trailing dot or a literal ``:`` is just an ordinary,
    distinct character in a filename there), which is why this is applied only to a path already
    identified as a Windows drive-letter path, not to a genuine POSIX path -- same asymmetry as
    the whole-path casefold a few lines below in ``normalize_path``.

    ``.`` and ``..`` are passed through unchanged: they are resolved as relative-path operators
    by the caller, not cleaned as filename cruft (a bare ``.`` has no trailing dot to strip once
    the single dot itself is the whole segment, and ``..``'s dots are not "trailing" cruft either).
    """
    if segment in (".", ".."):
        return segment
    colon = segment.find(":")
    if colon != -1:
        segment = segment[:colon]
    return segment.rstrip(". ")


def _normpath_posix(path: str) -> str:
    """``posixpath.normpath`` equivalent that treats the string purely as POSIX, no OS lookup."""
    is_abs = path.startswith("/")
    drive = ""
    is_windows = bool(_DRIVE_RE.match(path))
    if is_windows:
        drive, path = path[:2], path[2:]
        is_abs = True
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if is_windows:
        # round 3: Windows-only segment cleanup (trailing dot/space, ADS suffix) -- see
        # `_clean_windows_segment`. Re-filter afterwards: a segment that was ONLY cruft (e.g. a
        # lone trailing space, rare but possible) can collapse to "" and must drop out exactly
        # like an original "" or "." segment does above.
        parts = [p for p in (_clean_windows_segment(p) for p in parts) if p not in ("", ".")]
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


def normalize_path(raw_path: str) -> str:
    """Canonicalize ``raw_path`` to a forward-slash form for scope/self-protection checks.

    Handles (M-1): backslash -> forward slash, the Windows extended-length ``\\\\?\\`` prefix,
    the Git-Bash/MSYS absolute form, and lexical ``..`` resolution. Casefolds the WHOLE result
    when (after the above) it looks like a Windows drive-letter path -- Windows filesystems
    (including a drive accessed via a different-case drive letter, or a directory someone typed
    in a different case) are case-insensitive, so ``F:\\Projects\\...`` and ``f:/projects/...`` and
    ``F:\\PROJECTS\\...`` must normalize identically. A genuine POSIX absolute path (no drive --
    i.e. the real case on the Ubuntu server and in Linux CI) is left case-sensitive, matching the
    actual filesystem semantics there.
    """
    path = _strip_extended_length_prefix(raw_path)
    path = _to_posix(path)
    path = _msys_to_drive(path)
    path = _normpath_posix(path)
    if _is_drive_path(path):
        path = path.casefold()
    return path


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or _is_drive_path(path)


def resolve_full_path(file_path: str, project_dir: str) -> str:
    """Return a normalized, absolute-when-possible POSIX-style path for ``file_path``.

    If ``file_path`` is relative, it is joined onto ``project_dir`` first (both normalized the
    same way beforehand). The combined path is re-normalized so a drive-ness introduced only by
    the join (a relative ``file_path`` joined onto a drive-letter ``project_dir``) still gets
    casefolded. No OS lookup and no symlink resolution -- purely lexical, which is the safe
    direction to err for a guardrail (it can only make scope detection broader, never narrower,
    relative to what the real filesystem would resolve to).
    """
    norm_file = normalize_path(file_path)
    if _is_absolute(norm_file):
        return norm_file
    norm_project = normalize_path(project_dir).rstrip("/")
    return normalize_path(norm_project + "/" + norm_file)


def _path_segments(path: str) -> list[str]:
    stripped = path[2:] if _is_drive_path(path) else path
    return [p for p in stripped.split("/") if p]


def is_in_scope(full_path: str) -> bool:
    return any(segment in _IN_SCOPE_SEGMENTS for segment in _path_segments(full_path))


def _is_under_hooks_dir(full_path: str) -> bool:
    """True if ``.claude`` and ``hooks`` appear as two consecutive segments, anywhere (round 3)."""
    segments = [s.casefold() for s in _path_segments(full_path)]
    target = tuple(s.casefold() for s in _HOOKS_DIR_SEGMENTS)
    return any(tuple(segments[i : i + 2]) == target for i in range(len(segments) - 1))


def is_self_protected(full_path: str) -> bool:
    """Casefolded path-segment SUFFIX match against ``_PROTECTED_SUFFIXES`` (M-1/M-2), OR anywhere
    under ``.claude/hooks/`` (round 3, see ``_HOOKS_DIR_SEGMENTS``).

    A plain ``str.endswith`` on an un-casefolded path is NOT enough here for two reasons:

    1. Case: ``F:\\projects\\trade-bot\\.claude\\Settings.json`` (capital ``S``) must still be
       recognized as ``.claude/settings.json``.
    2. Segment alignment: ``endswith`` is a raw substring check, so a path like
       ``.../notatests/test_order_code_audit.py`` would incorrectly match the suffix
       ``tests/test_order_code_audit.py`` (its last characters happen to spell the same string)
       even though ``notatests`` is a different directory from ``tests``. Requiring the character
       immediately before the suffix to be a ``/`` (or the suffix to be the entire path) fixes
       that boundary.
    """
    cf_path = full_path.casefold()
    for suffix in _PROTECTED_SUFFIXES:
        cf_suffix = suffix.casefold()
        if cf_path == cf_suffix or cf_path.endswith("/" + cf_suffix):
            return True
    return _is_under_hooks_dir(full_path)


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

    if tool_name == "NotebookEdit":
        # NotebookEdit identifies the target cell (by id, or by edit_mode insert/delete) rather
        # than supplying the old cell source, so -- like Write -- there is no "old" counterpart to
        # diff against; the full new_source is scanned directly. A "delete" edit may carry no
        # new_source at all, which is simply nothing to scan (not a payload-shape error).
        new_source = tool_input.get("new_source")
        if new_source is None:
            return [("", "")]
        if not isinstance(new_source, str):
            raise ValueError("NotebookEdit tool_input.new_source must be a string when present")
        return [("", new_source)]

    raise ValueError(f"unsupported tool_name for an in-scope path: {tool_name!r}")


def _newly_introduced_matches(old_text: str, new_text: str) -> list[Finding]:
    """Findings in ``new_text`` beyond what the same pattern already matched in ``old_text``.

    Counts matches per pattern name (not exact (name, line) pairs, since a line's number can
    shift between old_string and new_string) -- if ``new_text`` has strictly more occurrences of
    a given pattern than ``old_text`` did, the extra occurrences are "newly introduced" and block
    the call. ``old_text`` is always ``""`` for ``Write``/``NotebookEdit`` (see module docstring),
    so every match in the new content counts as newly introduced, matching the spec's "for Write
    the full new content" rule.
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

    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return 0  # not a tool this guardrail cares about
    if not isinstance(tool_input, dict):
        return _block(f"tool_input must be an object for {tool_name}, got {type(tool_input).__name__}")

    path_key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    file_path = tool_input.get(path_key)
    if not isinstance(file_path, str) or not file_path:
        return _block(f"tool_input.{path_key} is missing or not a string")

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
    full_path = resolve_full_path(file_path, project_dir)

    if is_self_protected(full_path):
        return _block(
            f"BLOCKED - {file_path} is a guardrail file (hook / pattern module / settings / audit "
            f"test / CI workflow) and cannot be edited by Claude ({_GUARDRAIL_REFERENCE}). Parham "
            f"may edit it by hand outside Claude Code."
        )

    if not is_in_scope(full_path):
        return 0

    try:
        text_pairs = _extract_text_pairs(str(tool_name), tool_input)
    except ValueError as exc:
        return _block(f"{exc} (path in scope: {file_path})")

    if os.environ.get("TBOT_ALLOW_ORDER_CODE") == "1":
        return 0  # unlocked by Parham; self-protection above still applied unconditionally

    for old_text, new_text in text_pairs:
        introduced = _newly_introduced_matches(old_text, new_text)
        if introduced:
            name, lineno, line = introduced[0]
            snippet = line.strip()[:120]
            return _block(
                f"BLOCKED {file_path}:{lineno} - matched '{name}' ({snippet!r}). Exchange "
                f"order/cancel/OCO/margin/withdrawal code is not allowed before phase 5b "
                f"({_GUARDRAIL_REFERENCE}). Unlock: Parham sets TBOT_ALLOW_ORDER_CODE=1 himself."
            )

    return 0


def main() -> int:
    # MINOR-13: read stdin as bytes and decode explicitly, rather than `sys.stdin.read()` (which
    # relies on the platform/locale-dependent default text encoding and can silently replace or
    # mangle invalid bytes instead of failing). A decode error is exactly the kind of "cannot prove
    # this payload is safe" situation this hook fails closed on.
    try:
        raw_bytes = sys.stdin.buffer.read()
    except Exception as exc:  # fail closed on any stdin read error
        return _block(f"failed to read stdin: {exc}")

    try:
        raw_stdin = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _block(f"stdin is not valid UTF-8: {exc}")

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
