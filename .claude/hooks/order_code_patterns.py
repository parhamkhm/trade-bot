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
#
# round 4: widened the base-word alternation to also cover `api`, `rest`, `transport` and
# `aiohttp` receivers (reviewer repro: `self._api.post(...)`, `self._rest.post(...)`,
# `transport.post(...)`, `self._aiohttp.post(...)` -- a house-style receiver name that is not
# literally `http`/`client`/`session`/`httpx`/`requests` but is just as much an HTTP client).
# Same negative-lookbehind + "must be followed immediately by `.`" shape as before, so this does
# NOT reopen the round-3 false positives: `api_client.get(...)` still fails to match `http:verb-call`
# because `api` must be followed immediately by `.` (not `_client`), exactly like `client_cache.put`
# before it.
_RECEIVER_RE = (
    r"(?<![A-Za-z0-9])_?(?:http|client|session|httpx|requests|api|rest|transport|aiohttp)"
    r"(?:_?client|_?session)?"
)

# `path:/order` (round 4, MAJOR-C fix): round 3's "`api` must also appear on the same line" proxy
# was itself a bypass -- `ORDER_PATH = "/order"` on its own line has no `api` token anywhere near
# it, so the round-3 pattern missed it outright, and the reviewer additionally found that an
# f-string tail like `f"{prefix or self._write_prefix}/order"` never even reached the "same line"
# check because the whole f-string got blanked first (see MINOR-5 / `_blank_fstring_braces` below).
# Round 4 drops the `api`-same-line requirement and instead matches the *shape* of an endpoint
# literal directly: a quoted literal whose content ends with `/order`/`/orders`, or contains
# `/order/`/`/orders/` as a substring (so `"/order"`, `"/api/v1/orders"` and `"/order/oco"` all
# match), plus the equivalent "f-string tail" shape -- text starting right after a blanked `{...}`
# replacement field's closing `}` and ending the same way, since an f-string's literal portion is no
# longer blanked away by the whitespace-exemption (round 4 fix #2). This still correctly rejects
# round 3's false positives: `Path("data/orders.csv")` ends with `.csv`, not `/order`/`/orders`, and
# `avg = total/orders` has no quotes (and no `/` immediately before `order` inside quotes) at all.
# A second, narrower alternative (`path:/order-bare`) covers the unquoted YAML/TOML shape
# `order_path: /order` -- a bare scalar value with no quotes at all, which the quoted-literal
# pattern cannot see. Every real Tabdeal endpoint in this codebase is `/api/v1/...` or
# `/r/api/v1/...` (CLAUDE.md section 6); neither alternative requires the literal token `api`
# anymore, since that requirement is exactly what round 3's bypass exploited.
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
    (
        "path:/order",
        re.compile(
            r"(?i)([\"'])[^\"'\s]*/orders?(?:/[^\"'\s]*)?\1"
            r"|\}[^\"'\s{}]*/orders?(?:/[^\"'\s]*)?\b"
        ),
    ),
    # round 4: the YAML/TOML shape -- a bare (unquoted) scalar value, e.g. `order_path: /order` --
    # which the quoted-literal alternative above cannot see at all (there are no quote characters
    # on that line). Scoped to "right after a colon" so it does not fire on an unrelated division
    # expression or a type-annotated assignment (`ratio: float = total/orders` has text between the
    # colon and the `/`, which this pattern -- anchored at `:\s*` -- does not skip over).
    ("path:/order-bare", re.compile(r"(?i):\s*[\"']?/[\w/]*orders?\b")),
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
    # A bare quoted HTTP-verb literal passed positionally, e.g. `client.request("POST", ORDER_PATH)`,
    # `client.stream("POST", url)` or `client.request("post", url)`. Round 3 made this
    # case-SENSITIVE (uppercase only) specifically so "GET" could never match -- but a reviewer
    # found that was over-tight: it also meant a lowercase/mixed-case positional verb literal
    # (`client.request("post", url)`) was never caught at all. Round 4 makes the match
    # case-INSENSITIVE instead (matching every other pattern in this module); "GET" still can
    # never match because it is simply not one of the three alternatives, in any case -- the
    # original safety property came from the alternation's contents, not from case-sensitivity.
    # NIT (round 3, documented, not fixed): a bare `op = "DELETE"` assignment with no receiver and
    # no exchange context at all also matches this pattern -- accepted as still-blocked-on-purpose:
    # nothing under src/scripts/deploy/config has a legitimate reason to assign the literal string
    # "DELETE"/"POST"/"PUT" before phase 5b, and narrowing this pattern to require more context would
    # reopen exactly the kind of receiver-free verb-literal bypass (`client.request("DELETE", ...)`)
    # it exists to catch. See `test_known_safe_snippet_op_delete_is_accepted_as_still_blocked`.
    ("http:verb-literal", re.compile(r"(?i)([\"'])(?:POST|DELETE|PUT)\1")),
    # round 4: a Binance-style SDK (or this codebase's own house style) can spell the verb as an
    # enum member instead of a string literal -- `HTTPMethod.POST`/`.DELETE`/`.PUT`/`.PATCH`
    # (`http.HTTPMethod` is stdlib since Python 3.11). `GET` is deliberately not in the
    # alternation, same reasoning as `http:verb-literal` above.
    ("http:verb-enum", re.compile(r"\bHTTPMethod\.(?:POST|DELETE|PUT|PATCH)\b")),
    (
        "sdk:order-method-call",
        re.compile(
            r"(?i)\.(?:new_order|create_order|cancel_order|cancel_all_orders|cancel_open_orders|"
            r"place_order|new_oco_order|create_\w*_order|withdraw|delete_order|cancel_replace)\s*\("
        ),
    ),
    # round 4: `delete_order`/`cancel_replace` added above (reviewer-found missed SDK method
    # names). Deliberately did NOT add `get_order` or `query_order`: `src/tbot/core/types.py`'s
    # `Broker` protocol already defines `get_order(self, client_order_id: str) -> OrderStatus`
    # (a *read*, not a placement/cancellation -- CLAUDE.md section 5) -- adding it here would
    # make the guardrail block its own pre-approved contract. `query_order` was checked
    # (`grep -rn` over src/scripts/deploy/config) and does not appear anywhere today; it is left
    # out rather than guessed at, consistent with this module's "add a pattern when a concrete
    # bypass is found, not speculatively" approach (see the module docstring's closing paragraph).
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
# round 4 (MAJOR-C, part 2): the prefix group (0-2 letters, e.g. `f`, `F`, `rf`, `Rf`) is captured
# so `repl` below can tell an f-string literal apart from a plain one -- see
# `_blank_allowlisted_string_literals`. It only captures when those letters sit *immediately*
# before the quote with nothing in between (how every real string prefix in Python source looks);
# `foo("bar")` cannot accidentally have "oo" read as a prefix for `"bar"` because the `(` between
# them breaks that adjacency, and `re.finditer`'s normal leftmost-first scanning still finds the
# correct (empty-prefix) match starting at the quote itself.
_QUOTED_STRING_RE = re.compile(
    r'(?P<pfx_dq>[A-Za-z]{0,2})"(?P<dq>[^"\\\n]*(?:\\.[^"\\\n]*)*)"'
    r"|(?P<pfx_sq>[A-Za-z]{0,2})'(?P<sq>[^'\\\n]*(?:\\.[^'\\\n]*)*)'"
)

# round 4: matches one *non-nested* `{...}` replacement field inside an f-string's content. Nested
# braces (a dict literal inside an f-string expression, e.g. `f"{ {1: 2} }"`) are not handled --
# same "forgiving regex, not a parser" trade-off as the rest of this module (see the module
# docstring); nothing in src/scripts/deploy/config does this today.
_FSTRING_BRACE_RE = re.compile(r"\{[^{}\n]*\}")


def _blank_fstring_braces(content: str) -> str:
    """Blank the inside of every ``{...}`` replacement field in f-string ``content``, keeping the
    braces themselves and all literal text outside them untouched (same length in, same length
    out, so the overall match length -- and therefore line positions -- never changes).
    """

    def _r(m: re.Match[str]) -> str:
        inner = m.group(0)
        return "{" + " " * (len(inner) - 2) + "}"

    return _FSTRING_BRACE_RE.sub(_r, content)


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

    Two independent reasons a *plain* (non-f) quoted literal is allowed through unscanned:

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

    round 4 (MAJOR-C): rule 2 above must NEVER apply to an f-string (a prefix containing ``f``/``F``,
    e.g. ``f"..."``, ``rf"..."``, ``Rf"..."``). A reviewer found that the house-style read call
    ``self._get(f"{prefix or self._read_prefix}/openOrders", ...)`` was being allowed through
    *wholesale* by rule 2 -- the f-string's content as a whole contains spaces (inside the
    ``{prefix or self._read_prefix}`` expression), so the old code treated the entire literal,
    ``/openOrders`` tail included, as "a human-readable message" and blanked it completely. That is
    backwards for an f-string: the *expression* part (inside ``{...}``) is the part that is never a
    literal endpoint fragment by itself (it is a Python expression, evaluated at runtime), while the
    *literal text* around it (``/openOrders`` here) is exactly the kind of fixed endpoint fragment
    this guardrail exists to catch, whitespace inside the expression notwithstanding. Fix: for an
    f-string, never blank for whitespace -- instead blank only the inside of each ``{...}``
    replacement field (:func:`_blank_fstring_braces`) and leave every other character, including the
    literal text immediately after a closing ``}``, visible to every pattern below exactly as if it
    were a plain string with no internal whitespace at all.
    """

    def repl(match: re.Match[str]) -> str:
        if match.group("dq") is not None:
            prefix, content, quote = match.group("pfx_dq"), match.group("dq"), '"'
        else:
            prefix, content, quote = match.group("pfx_sq"), match.group("sq"), "'"
        if "f" in prefix.lower():
            return prefix + quote + _blank_fstring_braces(content) + quote
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
