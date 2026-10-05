"""Structured JSON logging for tbot services.

Configures `structlog` so every log line is a single JSON object on stdout —
the shape Docker's `json-file` log driver expects (see
`deploy/docker-compose.yml`'s `max-size`/`max-file` rotation) and the shape
any future log shipper can parse without a custom grammar.

Secrets must never reach a log line (CLAUDE.md section 3.6: "Secrets only in
`.env` on the server (never in the repo, logs, tracebacks, chat or
Telegram)"). Redaction runs through several complementary mechanisms, in the
order they are applied to an event dict / rendered line:

1. Any value that *is* a secret container — duck-typed as "has a callable
   `get_secret_value` attribute", which matches pydantic's `SecretStr` /
   `SecretBytes` (and therefore every field on `tbot.core.config.Secrets`)
   without this module needing to import `pydantic` or `tbot.core` — is
   replaced, regardless of which key holds it. As a side effect, the raw
   value behind the container is also fed into the value registry (3) below,
   so that if the same raw secret later leaks into a *different* field as a
   bare string (e.g. interpolated into an exception message), it is still
   caught even though that occurrence carries no container and no
   recognizable key name or prefix.
2. Any value whose **key name** matches `api[_-]?key`, `secret`, `token`,
   `signature` or `password` (case-insensitive) is replaced, regardless of
   its type — this catches a plain `str` secret passed under an obviously
   sensitive field name, e.g. `log.info("auth", api_key=raw_key)`.
3. **Value-based redaction (the primary defense against a leak in free
   text).** `register_secret(value)` adds a raw secret string to a
   module-level registry; every string that passes through `_scrub_string`
   (a top-level field, something nested, a rendered `exception`/`stack`
   traceback, or — via the final rendered-line pass installed by
   `configure_logging` — the fully-rendered JSON line itself, which is what
   catches a secret embedded in an arbitrary object's `repr()` or inside a
   `bytes` value, since structlog's JSON fallback renders those as text only
   at render time) has every registered value replaced before the
   pattern-based rules below run. This is the only mechanism that works
   regardless of key name, prefix, nesting or container type — a bare
   Tabdeal API key/secret is just an alphanumeric string with none of the
   recognizable prefixes rule (4) looks for, so (4) alone cannot catch
   `raise RuntimeError(f"auth failed for key {secret}")`.
4. Any **string value** (wherever it sits) is additionally scanned for
   `key=value`-shaped secrets embedded in free text, e.g. a signed URL
   (`...?signature=abc123`), and for bare tokens carrying a recognizable
   credential prefix (`sk-...`, `AKIA...`, `ghp_...`, etc.) — defense in
   depth for a secret that was never registered.
5. Any **dict key** that is itself a secret container, a string carrying a
   registered value, or a string carrying one of the prefixes from (4) is
   replaced too — this catches the inverted mistake of using the secret
   *as* a key (`log.info("probe", **{leaked_key: "present"})`).

Rules (1)/(2)/(5) walk dicts, lists, tuples, **sets and frozensets**
recursively, so a secret nested inside a structured field (e.g.
`log.info("req", headers={"X-MBX-APIKEY": "..."})`) is caught too, and a
`namedtuple` value is rebuilt field-by-field rather than via the generic
`type(value)(iterable)` constructor that plain tuples accept (a `namedtuple`
needs positional args, not a single iterable).

Processor ordering matters: `redact_secrets` must run strictly *after*
`StackInfoRenderer`/`format_exc_info` in `configure_logging`'s pipeline, since
those are what create the `exception`/`stack` string fields in the first
place — redacting before they exist means the traceback text is never
scanned at all.

**All log output — structlog-originated and stdlib (foreign) — flows through
one pipeline.** `configure_logging` installs `structlog.stdlib.LoggerFactory`
plus a `structlog.stdlib.ProcessorFormatter` on the root stdlib handler, with
`redact_secrets` in `foreign_pre_chain`. Before this, a third-party logger
(httpx, python-telegram-bot's bot-token-bearing DEBUG lines in phase 5, ...)
wrote through a bare `basicConfig` handler, completely bypassing redaction;
muting one known leaker (`logging.getLogger("httpx").setLevel(WARNING)` in
`tbot.execution.tabdeal_client`) was a point fix for that one logger, not a
general guarantee. Routing every stdlib `LogRecord` through the same
processor chain as our own logs closes that hole for any current or future
foreign logger. `configure_logging` also installs a `sys.excepthook` that
scrubs an unhandled exception's traceback before it reaches stderr — the one
remaining path that bypasses `logging`/`structlog` entirely.

This module deliberately has no dependency on `tbot.core.*` — it only
duck-types the secret container, so `monitoring` stays a leaf package that
`core`/`execution`/anything else can depend on without a cycle, and so this
redaction applies to *any* secret-shaped value, not only the ones defined by
`tbot.core.config.Secrets` today. `register_secret` is the hook other
packages are expected to call once a raw secret value is known (e.g. right
after `Secrets` is loaded) — this module cannot call it itself without
depending on `tbot.core.config` and creating that cycle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import threading
import traceback
import warnings
from types import TracebackType
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, ProcessorReturnValue

__all__ = [
    "REDACTED",
    "configure_logging",
    "redact_secrets",
    "register_secret",
    "register_secrets_for_logging",
]

REDACTED = "***REDACTED***"

# Matches key names such as: api_key, apiKey, api-key, secret, tabdeal_api_secret,
# telegram_bot_token, signature, password. Case-insensitive, substring match on purpose
# (a field named "tabdeal_api_secret" or "x_signature" must still be caught).
_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|secret|token|signature|password)", re.IGNORECASE)

# Matches `name=value` pairs embedded inside a larger string -- a query string, a signed
# URL, a header dump pasted into a message -- stopping at the next "&", whitespace, quote or
# ">" so only the value is swallowed, not the rest of the string. The trailing ">" exclusion
# (MINOR-1, third fix round) matters for e.g. `<Obj secret=SECRET>` -- without it the ">" was
# treated as part of the value and consumed by the replacement along with it, turning
# `<Obj secret=SECRET>` into `<Obj secret=***REDACTED***` with no closing bracket.
_EMBEDDED_PARAM_RE = re.compile(
    r"(?i)\b(signature|api[_-]?key|token|secret|password)=([^&\s\"'>]+)"
)

# Matches a *bare* secret-shaped token by its recognizable prefix, wherever it sits in a
# string -- a traceback message ("signing failed for key sk-live-...") or a dict key used
# (by mistake) to hold a secret instead of a field name. This is deliberately a narrow,
# well-known set of real-world credential prefixes (Stripe/OpenAI-style `sk-`/`pk-`,
# AWS `AKIA`/`ASIA`, GitHub `ghp_`/`gho_`/`github_pat_`, Slack `xoxb-` etc.) -- defense in
# depth, not the primary mechanism (that's the value registry and key-name rules), so it
# is intentionally conservative to avoid false positives on ordinary trading data. It
# cannot catch a bare alphanumeric secret with no recognizable prefix -- that is what the
# value registry (`register_secret`/`_SECRET_VALUES`) exists for.
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|pk|rk|whsec|ghp|gho|ghu|ghs|ghr|github_pat|xoxa|xoxb|xoxp|AKIA|ASIA)[-_][A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# Module-level registry of raw secret values (MAJOR-3): the only mechanism that redacts a
# secret regardless of key name, prefix, nesting or container type, because it matches the
# secret's literal content rather than guessing its shape. Populated by `register_secret`
# (called once, wherever a raw secret value becomes known -- e.g. right after
# `tbot.core.config.Secrets` is loaded, via `register_secrets_for_logging`) and, as a
# defense-in-depth side effect, automatically whenever `redact_secrets` unwraps a
# `SecretStr`-like container (see `_is_secret_container` call sites below).
#
# MINOR-2 (third fix round): phase 5 adds Telegram and OMS threads, any of which can log
# concurrently with another thread calling `register_secret` (e.g. via auto-registration
# inside `_is_secret_container`). A plain mutable `set[str]` mutated in place is not safe
# under that: the reviewer reproduced `RuntimeError: Set changed size during iteration`
# raised from inside a log call because one thread was iterating the set while another
# added to it. The fix is the standard lock-free-read pattern: `_SECRET_VALUES` is an
# *immutable* tuple, writers hold `_SECRET_VALUES_LOCK` and publish a whole new tuple via a
# single atomic rebind of the module global (safe under the GIL, and readers never see a
# partially-updated collection), and readers (`_scrub_registered`, `_clean_key`) just read
# the current tuple with no lock at all -- there is nothing to corrupt by reading a tuple
# while it is being replaced, only ever a stale-but-internally-consistent snapshot.
_SECRET_VALUES: tuple[str, ...] = ()
_SECRET_VALUES_LOCK = threading.Lock()

# MINOR-1 (third fix round): `register_secret` used to accept any non-empty string, so a
# careless call like `register_secret("1")` would turn every bare "1" anywhere in a log
# line (including inside otherwise-valid JSON, e.g. `"price": "61234.1"`) into redaction
# noise, and `register_secret('"')` could corrupt the rendered line into invalid JSON
# outright. Real secrets (Tabdeal API keys/secrets, Telegram bot tokens) are always
# reasonably long, alphanumeric-plus-a-few-punctuation-characters tokens, so values outside
# that shape are almost certainly a caller bug, not a secret -- reject them instead of
# registering garbage that can corrupt output. ":" is included because Telegram bot tokens
# are shaped like `123456:ABC-DEF...`.
_MIN_SECRET_LENGTH = 8
_SAFE_SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-+/=.:]+$")

_warn_once_lock = threading.Lock()
_warned_invalid_secret = False


def _warn_invalid_secret_once() -> None:
    """Emit the MINOR-1 "ignored a bad value" warning at most once per process.

    A misconfigured or buggy caller could otherwise call `register_secret` with a short or
    oddly-shaped value on every single request (e.g. accidentally passing a recv_window or a
    trade id), which would spam the warning stream without adding any new information after
    the first occurrence.
    """
    global _warned_invalid_secret
    with _warn_once_lock:
        if _warned_invalid_secret:
            return
        _warned_invalid_secret = True
    warnings.warn(
        "register_secret() ignored a value shorter than 8 characters or containing a "
        "character outside [A-Za-z0-9_-+/=.:] -- registering it would risk corrupting "
        "log output (e.g. valid JSON) rather than protecting a real secret. This warning "
        "is shown only once per process.",
        RuntimeWarning,
        stacklevel=3,
    )


def register_secret(value: object) -> None:
    """Register a raw secret value so any future occurrence of it in a log line is redacted.

    This is the fix for the hole no pattern-based rule can close: a Tabdeal API key/secret
    is a bare alphanumeric string with no recognizable prefix, so it can only be caught by
    matching its literal content. Call this once a raw secret value is known -- typically via
    `register_secrets_for_logging` right after loading `tbot.core.config.Secrets` -- and
    every subsequent log line (including a traceback where the secret was carelessly
    interpolated into an exception message) will have that exact value scrubbed, wherever it
    appears: a plain string field, nested inside a dict/list, inside an arbitrary object's
    `repr()`, or inside a `bytes` value.

    Accepts `str` or `bytes` (decoded as UTF-8, replacing undecodable bytes); any other
    type, `None`, or an empty value is ignored rather than raising, since a misuse here
    (e.g. accidentally registering `None`) must never crash the caller's startup path.

    MINOR-1: a value shorter than 8 characters, or containing any character outside
    `[A-Za-z0-9_-+/=.:]`, is also ignored (with a one-time warning) -- registering it could
    corrupt otherwise-valid output (JSON in particular) for every log line that happens to
    contain that short/odd substring, which is a worse outcome than leaving one bad call
    site's "secret" unredacted.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str) or not value:
        return
    if len(value) < _MIN_SECRET_LENGTH or not _SAFE_SECRET_VALUE_RE.match(value):
        _warn_invalid_secret_once()
        return
    global _SECRET_VALUES
    with _SECRET_VALUES_LOCK:
        if value not in _SECRET_VALUES:
            _SECRET_VALUES = (*_SECRET_VALUES, value)


def register_secrets_for_logging(secrets: object) -> None:
    """Register every raw secret value held by a ``Secrets``-like settings object.

    MAJOR M-C (third fix round): `register_secret` is only useful if something actually
    calls it in production. The value-based registry was previously wired up from tests
    only -- no real process ever registered a real secret, so in production `_SECRET_VALUES`
    stayed empty and a bare alphanumeric Tabdeal API key/secret in free text (no recognizable
    prefix, no sensitive key name) was emitted verbatim. This is the production-facing hook:
    call it once, immediately after constructing `tbot.core.config.Secrets()`, in every
    process that might hold credentials.

    Duck-types the same way the rest of this module does (`_is_secret_container`), so this
    leaf module keeps depending on neither `pydantic` nor `tbot.core` and stays free of an
    import cycle. Pydantic v2 model/settings instances keep their field values in
    ``__dict__``, so ``vars(secrets)`` yields every field value (``SecretStr``/``SecretBytes``
    fields and plain ones alike); only the secret-shaped ones are registered, and a plain
    `str`/`bool`/`None` field is silently skipped by `_is_secret_container`'s own
    `get_secret_value` duck-type check. Never raises: a non-model object with no `__dict__`
    (or any other misuse) is a no-op, because a startup-time logging-hygiene call must never
    be the thing that crashes a service's startup.
    """
    with contextlib.suppress(Exception):
        for value in vars(secrets).values():
            _is_secret_container(value)


def _is_secret_container(value: object) -> bool:
    """Duck-types pydantic's SecretStr/SecretBytes without importing pydantic.

    Any object exposing a callable ``get_secret_value`` is treated as a secret,
    whatever key it is stored under. The raw value is also registered (see
    `register_secret`) so the same secret is caught if it later leaks elsewhere as a bare
    string with no container around it.
    """
    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return False
    with contextlib.suppress(Exception):  # a broken secret container must not break logging
        register_secret(get_secret_value())
    return True


def _scrub_registered(s: str) -> str:
    """Replace every occurrence of a registered secret value (see `register_secret`).

    Longest-first so one registered secret that happens to be a substring of another
    (unlikely, but not impossible for short test fixtures) does not leave a partial residue
    behind after the shorter one is replaced first.

    MINOR-3 (third fix round): this runs on the *rendered* line (via `_scrub_rendered_line`)
    as well as on individual string fields, so it must also match a secret's **JSON-escaped**
    form, not just its raw bytes. A secret containing `"` or `\\` survives untouched in a
    JSON string if only the raw value is searched for, because `json.dumps` has already
    turned e.g. `a"b` into `a\\"b` by the time this runs on the rendered line.
    `_SAFE_SECRET_VALUE_RE` means no *currently* registered secret can actually contain
    those characters, but scrubbing the escaped form too is cheap, future-proof against that
    charset ever loosening, and does not affect Tabdeal keys/Telegram tokens either way.
    """
    for secret in sorted(_SECRET_VALUES, key=len, reverse=True):
        if not secret:
            continue
        if secret in s:
            s = s.replace(secret, REDACTED)
        escaped = json.dumps(secret)[1:-1]
        if escaped != secret and escaped in s:
            s = s.replace(escaped, REDACTED)
    return s


def _scrub_string(s: str) -> str:
    """Redact secret-shaped substrings embedded inside a free-text string value.

    Order matters: registered raw values (the primary mechanism, MAJOR-3) are scrubbed
    first, then the `name=value` query-string/header-dump pattern, then bare
    recognizable-prefix tokens with no "=" delimiter at all (e.g. inside a traceback
    message). Applied to every string value, not just ones under a sensitive key name,
    because the leak can be *inside* an innocuously-named field such as ``exception`` or
    ``url``.
    """
    s = _scrub_registered(s)
    s = _EMBEDDED_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", s)
    return _SECRET_TOKEN_RE.sub(REDACTED, s)


def _clean_key(key: Any) -> Any:
    """Return a safe version of a dict key.

    A secret should never appear verbatim as a JSON object key either -- whether it is a
    SecretStr-like container, a registered raw value, or a plain string that itself *is* a
    secret by prefix (the caller used the secret as the key instead of the value, e.g.
    ``log.info("x", **{leaked: "y"})``). When that happens the whole key is replaced; there
    is no partial-redaction equivalent for a key the way there is for a string value.
    """
    if _is_secret_container(key):
        return REDACTED
    if isinstance(key, str) and (
        _SECRET_TOKEN_RE.search(key) or any(secret in key for secret in _SECRET_VALUES)
    ):
        return REDACTED
    return key


def _redact_value(value: Any) -> Any:
    """Redact a value that is not itself gated by a sensitive key name."""
    if _is_secret_container(value):
        return REDACTED
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {_clean_key(k): _redact_keyed(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_redact_value(v) for v in value]
        if hasattr(value, "_fields"):
            # A namedtuple: type(value)(iterable) raises (it needs positional args, not
            # one iterable), so rebuild it field-by-field instead.
            return type(value)(*items)
        return type(value)(items)
    return value


def _redact_keyed(key: Any, value: Any) -> Any:
    """Redact ``value`` if ``key`` itself looks sensitive, else recurse into it."""
    if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
        # Still register the raw value if it came in via a secret container (e.g.
        # `api_secret=SecretStr(...)`) even though the key-name match short-circuits the
        # usual `_redact_value` recursion -- otherwise a SecretStr passed under an obviously
        # sensitive key name would never feed the registry, and the same secret leaking
        # elsewhere as a bare string later would go uncaught.
        _is_secret_container(value)
        return REDACTED
    return _redact_value(value)


def redact_secrets(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact secret-shaped values anywhere in the event dict.

    Must run after ``StackInfoRenderer``/``format_exc_info`` in the processor chain (see
    ``configure_logging``) so the rendered ``exception``/``stack`` text already exists and
    gets scanned like any other string field -- redacting first would mean those fields
    are created later, unscanned, and secrets inside a traceback would leak straight
    through.

    This processor only sees structured Python values still shaped as the caller passed
    them (strings, dicts, containers, secret containers). A value that only becomes text at
    JSON-render time -- an arbitrary object's ``repr()``, a ``bytes`` value rendered via
    structlog's JSON fallback -- is not a string yet when this runs, so it is not caught
    here; the final rendered-line scrub installed by ``configure_logging`` covers those
    cases via the same value registry (MINOR-9).

    Runs before rendering, so a redacted value never reaches the JSON renderer, a file, a
    terminal or (once phase 5 wires it up) Telegram.
    """
    return {_clean_key(k): _redact_keyed(k, v) for k, v in event_dict.items()}


def _scrub_rendered_line(
    _logger: object, _method_name: str, event: EventDict
) -> ProcessorReturnValue:
    """Final structlog processor, run *after* JSONRenderer on the fully-rendered line.

    Closes MINOR-9: a secret that only becomes text at JSON-render time -- embedded in an
    arbitrary object's ``repr()`` (structlog's JSON fallback for anything it cannot
    natively serialize) or inside a ``bytes`` value -- never passes through
    ``redact_secrets`` as a string, because at that point it is still a live Python object.
    By the time this processor runs, the entire line (whatever produced that text) is one
    string, so scrubbing it for every registered secret value (and, as a second line of
    defence, the same embedded-param/prefix patterns ``redact_secrets`` already applies)
    catches it regardless of which container produced it.

    Typed against the generic ``Processor`` signature (``event`` declared as ``EventDict``)
    so this slots into ``ProcessorFormatter.processors`` without a type: ignore; at its
    actual position in the chain -- strictly after ``JSONRenderer`` -- the value received at
    runtime is always the rendered ``str`` (or ``bytes``, with a non-default serializer),
    which is exactly the case the ``isinstance`` check below handles.
    """
    if isinstance(event, str):
        return _scrub_string(event)
    return event


def _excepthook(
    exc_type: type[BaseException], exc_value: BaseException, exc_tb: TracebackType | None
) -> None:
    """``sys.excepthook`` replacement: scrub a registered secret before it reaches stderr.

    MAJOR-4: an unhandled exception bypasses ``logging``/``structlog`` entirely -- Python's
    default excepthook writes the traceback straight to stderr. If the exception message
    carries a raw secret (``raise RuntimeError(f"auth failed for key {secret}")``), nothing
    above ever sees it. This mirrors the default formatting (``traceback.format_exception``)
    but scrubs the result through the same ``_scrub_string`` used everywhere else before
    writing it out.
    """
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    sys.stderr.write(_scrub_string(formatted))


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog (and stdlib logging) for one-JSON-object-per-line output.

    Call this once at process start-up (recorder, bot, any long-lived service).
    Safe to call more than once — e.g. in tests — later calls simply reconfigure.

    Every log record -- whether produced by `structlog.get_logger()` or by a third-party
    library's plain `logging.getLogger(...)` (httpx, python-telegram-bot, ...) -- flows
    through the same processor chain and is redacted and JSON-rendered identically
    (MAJOR-4). This also installs a `sys.excepthook` that scrubs unhandled-exception
    tracebacks, the one remaining path that bypasses `logging` entirely.

    Raises:
        ValueError: if ``level`` is not a known logging level name.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"invalid log level: {level!r}")

    # Processors shared by structlog-originated records and foreign stdlib records alike
    # (used both as structlog's own chain prefix and as the formatter's `foreign_pre_chain`
    # below) -- this is what guarantees one redaction/rendering pipeline for all of them.
    # StackInfoRenderer/format_exc_info must run BEFORE redact_secrets: they are what
    # create the `stack`/`exception` string fields from `stack_info=True`/`exc_info=True`
    # (or an active exception under `log.exception(...)`, or a foreign record's own
    # `exc_info`). redact_secrets must see those fields to scrub them -- running it earlier
    # would redact an event dict that doesn't have them yet, and the traceback text would
    # reach stdout unscanned.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # MINOR-9 (third fix round): a foreign stdlib logger's `extra={...}` fields (e.g.
        # `logging.getLogger(...).info("x", extra={"order_id": "..."})`) were silently
        # dropped -- nothing leaked, but third-party structured context never reached the
        # rendered line. ExtraAdder merges them into the event dict; it must run before
        # redact_secrets so those fields get scanned like any other field, not after.
        structlog.stdlib.ExtraAdder(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_secrets,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
            # Final pass, after rendering: catches a secret that only became text at
            # render time (an arbitrary object's repr(), a bytes value) -- see MINOR-9.
            _scrub_rendered_line,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace whatever handlers are already installed so repeated configure_logging()
    # calls (e.g. in tests) don't accumulate duplicate output lines.
    root.handlers = [handler]
    root.setLevel(numeric_level)

    sys.excepthook = _excepthook
