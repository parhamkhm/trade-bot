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
* Using the exact same masking + pattern code for full files (the audit test reads whole files from
  disk) and fragments (the hook) is what "same source of truth" has to mean in practice: a single
  AST-based masker for files and a different regex-based one for fragments would be two rule sets
  wearing one name.

Known, accepted limitations (defense-in-depth tool, not an adversarial sandbox; CLAUDE.md section
9/12 keeps a human, Parham, in the loop before phase 5b regardless):

1. Masking blanks out *every* triple-quoted string (not just true docstrings -- distinguishing the
   two needs a real parser, which fragments don't allow, see above). Real order-placement code
   deliberately hidden inside a triple-quoted string literal would therefore evade both the hook and
   the audit test. Nothing in src/ or scripts/ does this today (see the module docstring of
   ``tabdeal_client.py``, which explains the client is deliberately kept read-only).
2. (MINOR-5) **f-string same-quote-reuse bypass.** Python 3.12 (PEP 701) lets an f-string reuse its
   own delimiter quote character inside the ``{...}`` expression part, e.g. ``f"{"#"}"`` is valid
   syntax. ``_strip_one_line_comment`` below is a naive, line-local quote tracker: it has no concept
   of an f-string's ``{``/``}`` expression boundaries, so it just toggles "inside a string" on every
   occurrence of the line's active quote character, in source order. A line that mixes a PEP-701
   same-quote f-string with real code can desynchronize that toggle for the rest of the line --
   either harmlessly over-masking real code as "still inside a string" (a missed detection: a
   bypass), or under-masking a comment as real code (harmless: at worst a spurious finding). A real
   tokenizer would resolve this correctly but, as above, cannot run on a bare fragment. Accepted gap,
   same category as (1); not hardened here because a correct fix needs real lexing, which conflicts
   with "must never raise on a fragment".
3. (MINOR-6) **Fragment-masking is an approximation, not equivalent to a full-file parse.** An
   ``Edit``/``MultiEdit`` ``old_string``/``new_string`` fragment can open a triple-quoted string or an
   f-string on one line with no syntactic "close" anywhere in the fragment (the real close lives
   outside the edited region, in surrounding file text the hook never sees), or vice versa -- a
   fragment can *look* like it closes a string that, in the full file, was never opened. Both
   directions are possible: the masker can treat real code as "inside a string" (a potential bypass)
   or treat part of a string literal as real code (a potential false positive). The audit test does
   not have this problem (it always reads the whole file), which is one more reason it -- not the
   hook -- is the binding enforcement; see the hook's module docstring.
4. A deliberately space-padded fake endpoint (``"/api/v1/ order"``) would also be allowed through by
   the allow-listed-string-literal rule below; same category of intentional-evasion gap as (1).

Known limitation specific to the pattern list (not masking): every pattern here is a plain regex
scoped as tightly as the known bypasses to date require (see the git history of this file and of
``tests/test_order_code_audit.py`` for the concrete repro cases each pattern/false-positive-fix was
added for). A new, cleverer bypass is always possible; this module is reviewed whenever a reviewer
finds one, not treated as complete.
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
# Every pattern is case-insensitive (`(?i)`) UNLESS noted otherwise, and deliberately does NOT
# include the bare word "order" -- "OrderRequest", "OrderType", "OrderAck", "OrderStatus",
# "OrderState", "client_order_id" (core/types.py contracts) and "order book" / "orderbook" /
# "order_book" (data/tabdeal_recorder.py, data/depth.py, data/candles.py) all pass *by
# construction*: none of them contain a "/" next to "order", none of them are one of the specific
# camelCase endpoint names below, and none of them are an HTTP-verb call or a *ClientOrderId
# parameter name.
#
# `\boco\b` (not a bare `oco` substring) matters for a very concrete reason: "Protocol" and
# "protocol" both contain the letters "oco" (pr-OTO-col -> ...t-OCO-l), and `Protocol` is used
# throughout core/types.py (`Strategy(Protocol)`, `Broker(Protocol)`, ...). A bare substring match
# would flag every `Protocol` base class in the codebase.
#
# `http:verb-call` / `http:withdraw-call` are deliberately scoped to receivers that *look* like an
# HTTP client (`http`, `client`, `session`, `httpx`, `requests`, optionally prefixed with `_` and/or
# suffixed with exactly `_client`/`_session` -- e.g. `client`, `_client`, `http_client`, `httpx`,
# `self._session`, `_http`) so that `self._events.put(event)`, `await queue.put(x)`,
# `ledger.post(entry)`, `portfolio.record_withdrawal(...)` AND round-3's reviewer-found false
# positives `client_cache.put(k, v)`, `session_store.put(...)`, `requests_seen.put(...)` all pass --
# none of those receivers is an exact `client`/`_client`/`http_client`/... token, they just happen to
# start with one as a substring. See `_RECEIVER_RE` below for why a plain `\b...\w*\.` prefix (the
# round-2 fix) is not tight enough: `\b` does not fire before `self._client.post(` because `_` is a
# word character, so only the explicitly-hardcoded `_http` alternative in round 2 ever matched a
# leading-underscore receiver -- `self._client.post(`, `self._session.delete(` and
# `self._client.withdraw(` all slipped through. The fix is a negative lookbehind for "preceding char
# is alphanumeric" (so a `.` or `_` or start-of-string immediately before the receiver is fine, but a
# receiver that is itself a *suffix* of a longer identifier, e.g. the `client` inside `fooclient`, is
# not) PLUS restricting what may follow the base word to exactly `_client`/`_session` (not an
# arbitrary `\w*`), which is what excludes `client_cache`/`session_store`/`requests_seen`.
#
# Coverage for a *generic* verb-call regardless of receiver name (e.g. `client.request("POST", ...)`)
# comes from `http:verb-literal` instead. SDK-style method calls that do not go through a
# post/delete/put verb at all (e.g. `client.new_order(...)`, `exchange.create_order(...)`,
# `cancel_order(...)`) are covered by `sdk:order-method-call`, which is deliberately receiver-agnostic
# (phase 5b's real `TabdealBroker`/OMS can call these through any object name) -- unlike
# `http:verb-call`, false positives here are not a concern because the method names themselves
# (`new_order`, `cancel_all_orders`, `new_oco_order`, ...) are not realistic names for anything other
# than placing/cancelling exchange orders.
_RECEIVER_RE = r"(?<![A-Za-z0-9])_?(?:http|client|session|httpx|requests)(?:_?client|_?session)?"

# `path:/order` additionally requires the literal token `api` to appear somewhere on the same
# (masked) line (case-insensitive, word-bounded) -- round 3's reviewer-found false positives
# `Path("data/orders.csv")` and `avg = total/orders` both contain a bare `/order(s)` substring with
# the SAME word-boundary shape as a real endpoint (`/api/v1/orders`), so the boundary check alone
# (round 2's fix) cannot tell a URL path apart from a local file path or a division expression.
# Every real Tabdeal endpoint in this codebase is reached through `/api/v1/...` or `/r/api/v1/...`
# (CLAUDE.md section 6 / `config/default.yaml`'s `read_prefix`/`write_prefix`) -- "the line also says
# api somewhere" is therefore a cheap, well-justified proxy for "this is a URL path, not a local file
# path or an arithmetic expression". Accepted gap (documented, same category as the module
# docstring's others): an endpoint literally split across `BASE = "https://api1.tabdeal.org"` on one
# line and `PATH = "/v1/order"` on another, with no `api` token on the second line, would bypass this
# specific pattern -- `http:verb-call`/`http:verb-literal`/`sdk:order-method-call` still catch the
# call itself once those two pieces are actually used to make a request.
#
# `literal:order` is scoped to a bare `"order"`/`"orders"` string literal passed as an argument to a
# path-building call (`*.join(...)`, `*_url(...)`, `*urljoin(...)`) -- not a blanket
# quote-order-quote match (round 2's version) -- because `df.sort_values("order")`,
# `table = "orders"` and the `"order"` inside `Path("data/orders.csv")` are indistinguishable from a
# real endpoint fragment at the plain-substring level; only the *call context* (joining it onto a
# path/URL) tells them apart. `self._http.build_request("POST", self._url("order"))` (the original
# M-3 repro) is still blocked -- via `http:verb-literal` matching the `"POST"` literal in the same
# call, not via this pattern; see the regression test for that snippet.
#
# `literal:withdraw` requires the quoted content to also contain a `/` (an endpoint-shaped literal
# like `"/withdraw"` or `"/api/v1/withdraw"`) -- round 3's reviewer-found false positive
# `WITHDRAWAL = "withdrawal"` (a ledger-entry-type constant, not a call to anything) has no `/` and
# so no longer matches.
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("path:/order", re.compile(r"(?i)^(?=.*\bapi\b)(?=.*/orders?(?:/|\b)).*$")),
    ("path:order/", re.compile(r"(?i)\border/")),
    (
        "literal:order",
        re.compile(r"(?i)\b(?:join|_url|urljoin)\w*\s*\([^()\n]*?([\"'])orders?\1"),
    ),
    ("endpoint:openOrders", re.compile(r"(?i)\bopenorders\b")),
    ("endpoint:allOrders", re.compile(r"(?i)\ballorders\b")),
    ("endpoint:orderList", re.compile(r"(?i)\borderlist\b")),
    ("endpoint:paginatedOpenOrders", re.compile(r"(?i)\bpaginatedopenorders\b")),
    ("endpoint:nonExpiredAllOrders", re.compile(r"(?i)\bnonexpiredallorders\b")),
    ("endpoint:oco", re.compile(r"(?i)\boco\b")),
    ("path:/margin", re.compile(r"(?i)/margin\b")),
    ("path:/withdraw", re.compile(r"(?i)/withdraw")),
    (
        "literal:withdraw",
        re.compile(r'(?i)([\"\'])(?=[^"\'\n]*/)[^"\'\n]*withdraw\w*[^"\'\n]*\1'),
    ),
    ("stream:userDataStream", re.compile(r"(?i)userdatastream")),
    ("stream:listenKey", re.compile(r"(?i)listenkey")),
    (
        "http:verb-call",
        re.compile(rf"(?i){_RECEIVER_RE}\.(?:post|delete|put)\s*\("),
    ),
    (
        "http:withdraw-call",
        re.compile(rf"(?i){_RECEIVER_RE}\.withdraw\w*\s*\("),
    ),
    ("http:method-kwarg", re.compile(r'(?i)method\s*=\s*["\'](?:post|delete|put)["\']')),
    # Case-SENSITIVE by design (no `(?i)`): a bare quoted HTTP-verb literal passed positionally,
    # e.g. `client.request("POST", ORDER_PATH)` or `client.stream("POST", url)`. Exact uppercase
    # only, matching how Binance-style exchange APIs spell the verb; "GET" is deliberately not in
    # the alternation (`tabdeal_client.py` uses `method="GET"` throughout and must stay allowed).
    # NIT (round 3, documented, not fixed): a bare `op = "DELETE"` assignment with no receiver and
    # no exchange context at all also matches this pattern -- accepted as still-blocked-on-purpose:
    # nothing under src/scripts/deploy/config has a legitimate reason to assign the literal string
    # "DELETE"/"POST"/"PUT" before phase 5b, and narrowing this pattern to require more context would
    # reopen exactly the kind of receiver-free verb-literal bypass (`client.request("DELETE", ...)`)
    # it exists to catch. See `test_known_safe_snippet_op_delete_is_accepted_as_still_blocked`.
    ("http:verb-literal", re.compile(r"([\"'])(?:POST|DELETE|PUT)\1")),
    (
        "sdk:order-method-call",
        re.compile(
            r"(?i)\.(?:new_order|create_order|cancel_order|cancel_all_orders|cancel_open_orders|"
            r"place_order|new_oco_order|create_\w*_order|withdraw)\s*\("
        ),
    ),
    ("param:newClientOrderId", re.compile(r"(?i)newclientorderid")),
    ("param:origClientOrderId", re.compile(r"(?i)origclientorderid")),
    ("param:listClientOrderId", re.compile(r"(?i)listclientorderid")),
    ("param:stopClientOrderId", re.compile(r"(?i)stopclientorderid")),
    ("param:limitClientOrderId", re.compile(r"(?i)limitclientorderid")),
)

# Exact string-literal allow-list. A quoted string literal (single/double, NOT triple-quoted --
# those are already blanked wholesale, see `mask_text`) whose content is EXACTLY one of these is
# never flagged, even though its text matches `literal:withdraw` / `path:/withdraw`.
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
    would raise on a bare code fragment; see the module docstring). See the module docstring
    (MINOR-5) for the specific, accepted f-string same-quote-reuse bypass this approximation has.
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
