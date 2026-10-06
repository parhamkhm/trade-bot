"""Single source of truth for the order-code guardrail (CLAUDE.md section 3.6 / SPEC D-052).

Both the ``PreToolUse`` hook (``block_order_code.py``) and the pytest audit test
(``tests/test_order_code_audit.py``) import *this* module and call :func:`find_matches` so the two
enforcement points can never drift apart. Everything here is stdlib-only (``re``) on purpose:

* The hook is sometimes handed a bare code *fragment* (an ``Edit``/``MultiEdit`` ``new_string`` or
  ``old_string``), which is almost never valid standalone Python (wrong indentation, a dangling
  ``if`` with no body, ...). ``ast.parse``/``tokenize`` would raise on most such fragments, and
  "fail closed on any exception for an in-scope path" would then block nearly every ordinary edit,
  not just order-code ones. A plain, forgiving regex pass on raw text never raises, so it works
  identically on a full file (``Write``) and on a two-line fragment (``Edit``).
* Using the exact same masking + pattern code for full files (the audit test reads whole ``*.py``
  files from disk) and fragments (the hook) is what "same source of truth" has to mean in practice:
  a single AST-based masker for files and a different regex-based one for fragments would be two
  rule sets wearing one name.

Known, accepted limitation: masking blanks out *every* triple-quoted string (not just true
docstrings -- distinguishing the two needs a real parser, which fragments don't allow, see above).
Real order-placement code deliberately hidden inside a triple-quoted string literal would therefore
evade both the hook and the audit test. This is a defense-in-depth tool for catching ordinary/
accidental additions, not an adversarial sandbox; CLAUDE.md section 9 keeps a human (Parham) in the
loop before phase 5b regardless, and nothing in src/ or scripts/ does this today (see the module
docstring of ``tabdeal_client.py``, which explains the client is deliberately kept read-only).
"""

from __future__ import annotations

import re

__all__ = [
    "EXACT_STRING_ALLOWLIST",
    "FORBIDDEN_PATTERNS",
    "Finding",
    "find_matches",
    "mask_text",
]

# ---------------------------------------------------------------------------
# Forbidden patterns
# ---------------------------------------------------------------------------
# Every pattern is case-insensitive (`(?i)`) and deliberately does NOT include the bare word
# "order" -- "OrderRequest", "OrderType", "OrderAck", "OrderStatus", "OrderState",
# "client_order_id" (core/types.py contracts) and "order book" / "orderbook" / "order_book"
# (data/tabdeal_recorder.py, data/depth.py, data/candles.py) all pass *by construction*: none of
# them contain a "/" next to "order", none of them are one of the specific camelCase endpoint
# names below, and none of them are an HTTP-verb call or a *ClientOrderId parameter name.
#
# `\boco\b` (not a bare `oco` substring) matters for a very concrete reason: "Protocol" and
# "protocol" both contain the letters "oco" (pr-OTO-col -> ...t-OCO-l), and `Protocol` is used
# throughout core/types.py (`Strategy(Protocol)`, `Broker(Protocol)`, ...). A bare substring match
# would flag every `Protocol` base class in the codebase.
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("path:/order", re.compile(r"(?i)/order(?:/|\b)")),
    ("path:order/", re.compile(r"(?i)\border/")),
    ("endpoint:openOrders", re.compile(r"(?i)\bopenorders\b")),
    ("endpoint:allOrders", re.compile(r"(?i)\ballorders\b")),
    ("endpoint:orderList", re.compile(r"(?i)\borderlist\b")),
    ("endpoint:paginatedOpenOrders", re.compile(r"(?i)\bpaginatedopenorders\b")),
    ("endpoint:nonExpiredAllOrders", re.compile(r"(?i)\bnonexpiredallorders\b")),
    ("endpoint:oco", re.compile(r"(?i)\boco\b")),
    ("path:/margin", re.compile(r"(?i)/margin\b")),
    ("action:withdraw", re.compile(r"(?i)withdraw")),
    ("stream:userDataStream", re.compile(r"(?i)userdatastream")),
    ("stream:listenKey", re.compile(r"(?i)listenkey")),
    ("http:post-call", re.compile(r"(?i)\.post\s*\(")),
    ("http:delete-call", re.compile(r"(?i)\.delete\s*\(")),
    ("http:put-call", re.compile(r"(?i)\.put\s*\(")),
    ("http:method-kwarg", re.compile(r'(?i)method\s*=\s*["\'](?:post|delete|put)["\']')),
    ("http:httpx-verb", re.compile(r"(?i)httpx\.(?:post|delete|put)\b")),
    ("http:requests-verb", re.compile(r"(?i)requests\.(?:post|delete|put)\b")),
    ("param:newClientOrderId", re.compile(r"(?i)newclientorderid")),
    ("param:origClientOrderId", re.compile(r"(?i)origclientorderid")),
    ("param:listClientOrderId", re.compile(r"(?i)listclientorderid")),
    ("param:stopClientOrderId", re.compile(r"(?i)stopclientorderid")),
    ("param:limitClientOrderId", re.compile(r"(?i)limitclientorderid")),
)

# Exact string-literal allow-list. A quoted string literal (single/double, NOT triple-quoted --
# those are already blanked wholesale, see `mask_text`) whose content is EXACTLY one of these is
# never flagged, even though its text matches `action:withdraw`.
#
# Why these three and only these three: `scripts/tabdeal_probe.py::classify_key_permissions`
# reads the Tabdeal/Binance-style account-info response to confirm the API key has NO withdraw
# permission (CLAUDE.md section 3.6 requires this check before phase 5b). That function reads
# `account_body["canWithdraw"]` and checks `"WITHDRAW"` / `"WITHDRAWALS"` against the account's
# `permissions` list -- i.e. it is read-only *verification that withdrawal is impossible*, never a
# call to a withdrawal endpoint. Adding a new entry here requires the same justification: the
# string must be a field/value name read from an exchange *response*, never a URL, a method name,
# or anything passed as a *request* parameter.
EXACT_STRING_ALLOWLIST: frozenset[str] = frozenset({"canWithdraw", "WITHDRAW", "WITHDRAWALS"})

# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

_TRIPLE_QUOTED_RE = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')
_QUOTED_STRING_RE = re.compile(
    r'"([^"\\\n]*(?:\\.[^"\\\n]*)*)"' r"|'([^'\\\n]*(?:\\.[^'\\\n]*)*)'"
)


def _blank_preserving_newlines(match: re.Match[str]) -> str:
    return "".join(ch if ch == "\n" else " " for ch in match.group(0))


def _strip_one_line_comment(line: str) -> str:
    """Blank a trailing ``# ...`` comment, tracking simple quote state.

    Approximate on purpose (no handling of raw strings, f-string braces, or backslash edge cases
    beyond a single backslash-escape check) -- good enough to stop real comments like
    ``# ... withdraw permission ...`` from being scanned, without needing a real tokenizer (which
    would raise on a bare code fragment; see the module docstring).
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote and line[i - 1] != "\\":
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#":
            return line[:i] + " " * (len(line) - i)
    return line


def _strip_line_comments(text: str) -> str:
    return "\n".join(_strip_one_line_comment(line) for line in text.split("\n"))


def _blank_allowlisted_string_literals(text: str) -> str:
    """Blank quoted string-literal content that cannot be exchange code.

    Two independent reasons a quoted literal is allowed through unscanned:

    1. Exact match against :data:`EXACT_STRING_ALLOWLIST` (specific known-safe field/value names,
       see its docstring).
    2. The content contains a whitespace character. Every real Tabdeal endpoint path, HTTP verb,
       and order-placement parameter name in CLAUDE.md section 6 / the forbidden-pattern list
       above is a single token -- "/order", "openOrders", "POST", "WITHDRAW", "newClientOrderId".
       None of them contain a space. A quoted literal with a space in it is a human-readable
       message (a log line, a CLI warning, an error string) -- e.g.
       ``"...verify MANUALLY in the Tabdeal UI that this key has NO trade and NO withdrawal
       permission..."`` in ``scripts/tabdeal_probe.py`` -- not a value ever sent to the exchange.
       Accepted trade-off: a deliberately space-padded fake endpoint (``"/api/v1/ order"``) would
       also be allowed through; this is the same category of intentional-evasion gap as the
       triple-quoted-string exclusion documented in the module docstring.
    """

    def repl(match: re.Match[str]) -> str:
        content = match.group(1) if match.group(1) is not None else match.group(2)
        if content in EXACT_STRING_ALLOWLIST or any(ch.isspace() for ch in content):
            return " " * len(match.group(0))
        return match.group(0)

    return _QUOTED_STRING_RE.sub(repl, text)


def mask_text(text: str) -> str:
    """Blank out triple-quoted strings, line comments and allow-listed string literals.

    Line count and newline positions are preserved exactly, so line numbers reported by
    :func:`find_matches` against the masked text stay valid against the original text.
    """
    masked = _TRIPLE_QUOTED_RE.sub(_blank_preserving_newlines, text)
    masked = _strip_line_comments(masked)
    masked = _blank_allowlisted_string_literals(masked)
    return masked


# (pattern_name, line_number, original_line_text)
Finding = tuple[str, int, str]


def find_matches(text: str) -> list[Finding]:
    """Scan ``text`` (a full file OR a bare code fragment) for forbidden order-code patterns.

    Returns one :data:`Finding` per (pattern, occurrence), against the ORIGINAL (unmasked) line
    text so callers can report a readable snippet, but matched only against the masked copy.
    """
    masked = mask_text(text)
    masked_lines = masked.split("\n")
    original_lines = text.split("\n")
    findings: list[Finding] = []
    for lineno, masked_line in enumerate(masked_lines, start=1):
        has_original_line = lineno - 1 < len(original_lines)
        original_line = original_lines[lineno - 1] if has_original_line else masked_line
        for name, pattern in FORBIDDEN_PATTERNS:
            for _ in pattern.finditer(masked_line):
                findings.append((name, lineno, original_line))
    return findings
