"""No test may read the wall clock (D-094).

A test that calls ``datetime.now()`` / ``time.time()`` (directly, through an alias, or by passing
the callable on as a default such as ``clock=datetime.now``) can pass today and fail on another date,
or quietly depend on the machine's clock. Time-dependent code takes an injected clock (``now=`` /
``clock=`` / ``now_ms=``); tests pass a fixed instant.

This is enforcement layer 1 (static), and it is the only layer in force. Layer 2 -- a CI step that
runs the whole suite under libfaketime at two fake dates -- is PROPOSED (D-094) and is NOT yet in
``.github/workflows/ci.yml``.

Allowed without a marker: ``time.monotonic`` / ``time.perf_counter`` (durations, not dates) and
``time.sleep``. A line that genuinely needs the real clock carries ``# wall-clock-ok: <reason>``.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
MARKER = "# wall-clock-ok:"

# Fully-qualified names that read today's date or the epoch clock. The bare ``datetime.now`` /
# ``date.today`` spellings are kept so a test that forgot the import is still caught.
_BANNED: frozenset[str] = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "time.time",
        "time.time_ns",
        "pandas.Timestamp.now",
        "pandas.Timestamp.utcnow",
        "pandas.Timestamp.today",
    }
)
# Banned only when called without the argument that makes them deterministic: ``localtime()`` /
# ``gmtime()`` / ``ctime()`` read the clock, ``localtime(0)`` does not; ``strftime(fmt)`` reads it,
# ``strftime(fmt, tuple)`` does not. Value = highest argument count that still reads the clock.
# As a bare reference (``clock=time.gmtime``) they are always flagged. An explicit literal ``None``
# argument does not count towards this limit: the C stdlib treats ``localtime(None)`` exactly like
# ``localtime()`` ("now"), so it must not look safe merely because an argument was supplied.
_BANNED_WITHOUT_ARG: dict[str, int] = {
    "time.localtime": 0,
    "time.gmtime": 0,
    "time.ctime": 0,
    "time.strftime": 1,
}
# Constructors that read the clock when handed the string "now" / "today", either positionally
# (arg 0) or through specific keyword arguments.
_NOW_STRING_CTORS: frozenset[str] = frozenset(
    {"pandas.Timestamp", "pandas.to_datetime", "numpy.datetime64", "pandas.date_range"}
)
_NOW_STRINGS = frozenset({"now", "today"})
_NOW_STRING_KWARGS: dict[str, frozenset[str]] = {
    "pandas.Timestamp": frozenset({"ts_input"}),
    "pandas.date_range": frozenset({"start", "end"}),
}
# ``time.clock_gettime(time.CLOCK_REALTIME)`` / ``time.clock_gettime_ns(time.CLOCK_REALTIME)`` read
# the real wall clock; the same calls with ``time.CLOCK_MONOTONIC`` (or any other clock id) do not,
# so this one needs its first-argument value inspected rather than just the function name.
_CLOCK_GETTIME_FUNCS: frozenset[str] = frozenset({"time.clock_gettime", "time.clock_gettime_ns"})
_REALTIME_CLOCK_IDS: frozenset[str] = frozenset({"time.CLOCK_REALTIME"})


def _import_aliases(tree: ast.AST) -> dict[str, frozenset[str]]:
    """Local name -> every fully qualified dotted name it has been imported as, anywhere in the
    file (module-level or nested in a function/class).

    This module does no scope analysis, so a name rebound in a narrower scope -- e.g. a
    module-level ``import datetime`` shadowed by a ``from datetime import datetime`` inside one
    function -- used to simply overwrite the single mapping with whichever import ``ast.walk``
    happened to visit last, independent of which binding is actually in effect at a given call
    site. That could make a real ``datetime.datetime.now()`` elsewhere in the file resolve
    through the wrong alias and go unflagged. Instead, every name an import ever binds anywhere
    in the file is recorded, and a call is flagged if ANY of its possible bindings would read the
    clock. False positives are acceptable here; false negatives are not.
    """
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    aliases.setdefault(item.asname, set()).add(item.name)
                else:  # ``import a.b`` binds ``a``
                    top = item.name.split(".")[0]
                    aliases.setdefault(top, set()).add(top)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for item in node.names:
                local = item.asname or item.name
                aliases.setdefault(local, set()).add(f"{node.module}.{item.name}")
    return {name: frozenset(qualified) for name, qualified in aliases.items()}


def _qualified(node: ast.AST, aliases: dict[str, frozenset[str]]) -> frozenset[str] | None:
    """Every candidate dotted name a ``Name`` / ``Attribute`` chain could resolve to, with every
    recorded import binding for each name expanded (see ``_import_aliases``), else ``None``."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, frozenset({node.id}))
    if isinstance(node, ast.Attribute):
        bases = _qualified(node.value, aliases)
        return None if bases is None else frozenset(f"{base}.{node.attr}" for base in bases)
    return None


def _effective_arg_count(call: ast.Call) -> int:
    """Argument count for the ``_BANNED_WITHOUT_ARG`` check, except an explicit literal ``None``
    positional argument never counts: ``time.localtime(None)`` reads the real clock exactly like
    ``time.localtime()``."""
    args = [a for a in call.args if not (isinstance(a, ast.Constant) and a.value is None)]
    return len(args) + len(call.keywords)


def _reads_clock(names: frozenset[str], call: ast.Call | None) -> bool:
    """Does any candidate name in ``names`` -- called as ``call``, or merely referenced when
    ``call`` is ``None`` -- read the clock?"""
    if names & _BANNED:
        return True
    count = _effective_arg_count(call) if call is not None else None
    for name in names:
        limit = _BANNED_WITHOUT_ARG.get(name)
        if limit is not None and (count is None or count <= limit):
            return True
    return False


def _is_now_string(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().lower() in _NOW_STRINGS
    )


def _is_now_string_ctor(names: frozenset[str] | None, call: ast.Call) -> bool:
    if not names or not (names & _NOW_STRING_CTORS):
        return False
    if call.args and _is_now_string(call.args[0]):
        return True
    allowed_kwargs: set[str] = set()
    for name in names & _NOW_STRING_CTORS:
        allowed_kwargs |= _NOW_STRING_KWARGS.get(name, frozenset())
    return any(kw.arg in allowed_kwargs and _is_now_string(kw.value) for kw in call.keywords if kw.arg)


def _is_realtime_clock_gettime(
    names: frozenset[str] | None, call: ast.Call, aliases: dict[str, frozenset[str]]
) -> bool:
    if not names or not (names & _CLOCK_GETTIME_FUNCS) or not call.args:
        return False
    arg_names = _qualified(call.args[0], aliases)
    return bool(arg_names and arg_names & _REALTIME_CLOCK_IDS)


def _comment_tokens(source: str) -> dict[int, str]:
    """Map line number -> comment text, for real ``COMMENT`` tokens only (via ``tokenize``) -- a
    ``# wall-clock-ok:`` that merely appears inside a string literal (e.g. in a docstring showing
    an example) must never suppress a real finding, and scanning raw source text for the marker
    substring cannot tell the two apart."""
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return comments


def _marked(comments: dict[int, str], node: ast.AST) -> bool:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    for lineno in range(start, end + 1):
        text = comments.get(lineno)
        if text and MARKER in text and text.split(MARKER, 1)[1].strip():
            return True
    return False


def find_wall_clock_reads(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for each unmarked wall-clock read in ``source``.

    Catches calls (``datetime.now()``), uncalled references (``clock=datetime.now``,
    ``functools.partial(dt.datetime.now, UTC)``), ``from``-imported and aliased spellings, pandas
    ``Timestamp.now/utcnow/today``, the string forms ``Timestamp("now")`` / ``datetime64("today")``
    (positionally or via ``ts_input=``/``start=``/``end=``), and ``time.clock_gettime(_ns)`` called
    with ``time.CLOCK_REALTIME``.
    """
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    comments = _comment_tokens(source)
    parent: dict[int, ast.AST] = {}
    for outer in ast.walk(tree):
        for child in ast.iter_child_nodes(outer):
            parent[id(child)] = outer

    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_names = _qualified(node.func, aliases)
            flagged = _is_now_string_ctor(func_names, node) or _is_realtime_clock_gettime(
                func_names, node, aliases
            )
            if flagged and not _marked(comments, node):
                hits.add((node.lineno, ast.unparse(node)))
            continue
        if not isinstance(node, ast.Attribute | ast.Name):
            continue
        if not isinstance(node.ctx, ast.Load):  # e.g. ``time.time = lambda: 0.0`` is not a read
            continue
        enclosing = parent.get(id(node))
        call = enclosing if isinstance(enclosing, ast.Call) and enclosing.func is node else None
        names = _qualified(node, aliases)
        if names is None or not _reads_clock(names, call):
            continue
        anchor: ast.AST = call or node  # a marker anywhere on a multi-line call covers it
        if _marked(comments, anchor):
            continue
        text = ast.unparse(node)
        hits.add((node.lineno, f"{text}()" if call is not None else f"{text} (uncalled reference)"))
    return sorted(hits)


def _test_files() -> Iterator[Path]:
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def test_no_test_reads_the_wall_clock() -> None:
    offenders = []
    for path in _test_files():
        for lineno, what in find_wall_clock_reads(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(TESTS_DIR.parent).as_posix()}:{lineno}: {what}")
    assert not offenders, (
        "tests must not read the wall clock; inject a fixed clock (now= / clock= / now_ms=) or, if the "
        f"real clock is the thing under test, add '{MARKER} <reason>' on the line:\n  "
        + "\n  ".join(offenders)
    )


def test_test_files_discovers_the_real_test_tree() -> None:
    """Sanity check on ``_test_files()`` itself, separate from any audit result: a glob that
    silently stopped covering a subdirectory (e.g. a typo'd pattern, or ``tests/deploy/`` dropping
    out) would make every rule above vacuously true for exactly the files it stopped seeing.
    Pin down that the walk still reaches this file and a file nested under a subdirectory, and
    that the total count is comfortably above what a badly scoped pattern could produce."""
    discovered = {path.relative_to(TESTS_DIR) for path in _test_files()}
    assert len(discovered) > 40
    assert Path(__file__).resolve().relative_to(TESTS_DIR) in discovered
    assert Path("deploy") / "test_backup_lbank_safety.py" in discovered


# -- negative control: the detector itself must detect --------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\nx = time.time()\n",
        "import time as t\nx = t.time_ns()\n",
        "from time import time\nx = time()\n",
        "from time import time as clock\nx = clock()\n",
        "import datetime\nx = datetime.datetime.now()\n",
        "import datetime as dt\nx = dt.datetime.utcnow()\n",
        "from datetime import datetime\nx = datetime.now()\n",
        "from datetime import datetime as D\nx = D.now()\n",
        "from datetime import date\nx = date.today()\n",
        "import datetime\nx = datetime.date.today()\n",
        "import datetime\nx = datetime.datetime.today()\n",
        "from datetime import datetime, UTC\nx = datetime.now(UTC)\n",
        "import time\nx = time.gmtime()\n",
        "import time\nx = time.localtime()\n",
        "import time\nx = time.ctime()\n",
        "import time\nx = time.strftime('%Y')\n",
        "from time import strftime\nx = strftime('%Y')\n",
        # uncalled references: a default clock handed on is still a clock read
        "from datetime import datetime\nf(clock=datetime.now)\n",
        "import datetime as dt\nf(clock=dt.datetime.utcnow)\n",
        "import time\nf(now_ms=time.time_ns)\n",
        "from time import time_ns\nf(now_ms=time_ns)\n",
        "import functools\nimport datetime as dt\nclock = functools.partial(dt.datetime.now, dt.UTC)\n",
        "from datetime import date\nf(today=date.today)\n",
        "import time\nf(clock=time.gmtime)\n",
        # pandas and numpy
        "import pandas as pd\nx = pd.Timestamp.now()\n",
        "import pandas as pd\nx = pd.Timestamp.utcnow()\n",
        "import pandas as pd\nx = pd.Timestamp.today()\n",
        "from pandas import Timestamp\nx = Timestamp.now(tz='UTC')\n",
        "import pandas as pd\nf(clock=pd.Timestamp.now)\n",
        "import pandas as pd\nx = pd.Timestamp('now')\n",
        "import pandas as pd\nx = pd.Timestamp('today')\n",
        "import pandas as pd\nx = pd.to_datetime('now')\n",
        "import numpy as np\nx = np.datetime64('now')\n",
        "import numpy as np\nx = np.datetime64('today')\n",
        "from numpy import datetime64\nx = datetime64('now', 'ms')\n",
        # a name rebound in a narrower scope must not shield the module-level usage (item 3a)
        "import datetime\n\n\ndef helper() -> None:\n"
        "    from datetime import datetime\n    return datetime\n\n\nx = datetime.datetime.now()\n",
        # explicit None argument still reads the clock (item 3b)
        "import time\nx = time.localtime(None)\n",
        "import time\nx = time.gmtime(None)\n",
        "import time\nx = time.ctime(None)\n",
        # time.clock_gettime(_ns) with CLOCK_REALTIME (item 3b)
        "import time\nx = time.clock_gettime(time.CLOCK_REALTIME)\n",
        "import time\nx = time.clock_gettime_ns(time.CLOCK_REALTIME)\n",
        "from time import CLOCK_REALTIME, clock_gettime\nx = clock_gettime(CLOCK_REALTIME)\n",
        # keyword forms of the "now"/"today" string constructors (item 3b)
        "import pandas as pd\nx = pd.Timestamp(ts_input='now')\n",
        "import pandas as pd\nx = pd.date_range(start='now', periods=3)\n",
        "import pandas as pd\nx = pd.date_range(end='today', periods=3)\n",
        "import pandas as pd\nx = pd.date_range('now', periods=3)\n",
        # a marker that only appears inside a string literal, on the SAME line as the call
        # (item 3c), is not a real comment and must not suppress this real finding
        "import time\nx = time.time(); s = '# wall-clock-ok: not a real comment'\n",
    ],
)
def test_detector_flags_wall_clock_reads(snippet: str) -> None:
    assert find_wall_clock_reads(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\nx = time.monotonic()\ntime.sleep(1)\ny = time.perf_counter()\n",
        "from datetime import datetime, UTC\nx = datetime(2031, 2, 28, tzinfo=UTC)\n",
        "from datetime import datetime, UTC\nx = datetime.fromtimestamp(0, UTC)\n",
        "from datetime import datetime\nx = datetime.fromisoformat('2031-02-28')\n",
        "class C:\n    def now(self): ...\nx = C().now()\n",
        "import time\nx = time.time()  # wall-clock-ok: the real clock is the thing under test\n",
        "import time\nx = time.localtime(0)\ny = time.gmtime(86400)\nz = time.strftime('%Y', x)\n",
        "import pandas as pd\nx = pd.Timestamp('2031-02-28')\ny = pd.Timestamp(0, unit='s')\n",
        "import numpy as np\nx = np.datetime64('2031-02-28')\n",
        "import time\nf(clock=time.monotonic)\n",
        "import time\nx = time.process_time()\n",
        "import datetime as dt\nclock = lambda: dt.datetime(2031, 2, 28, tzinfo=dt.UTC)\n",
        "from datetime import datetime\nf(clock=datetime.now)  # wall-clock-ok: the default under test\n",
        # safe controls requested for the new rules (item 3f)
        "import time\nx = time.ctime(0)\n",
        "import pandas as pd\nx = pd.to_datetime('2031-02-28')\n",
        # explicit non-None argument is still safe (item 3b)
        "import time\nx = time.localtime(0)\ny = time.gmtime(86400)\nz = time.ctime(0)\n",
        # CLOCK_MONOTONIC (or any non-realtime id) must not be flagged (item 3b)
        "import time\nx = time.clock_gettime(time.CLOCK_MONOTONIC)\n",
        "import time\nx = time.clock_gettime_ns(time.CLOCK_MONOTONIC)\n",
        # a plain variable passed as the clock id resolves to nothing known -> not flagged
        "import time\nclock_id = 1\nx = time.clock_gettime(clock_id)\n",
        # keyword forms with an ordinary fixed value are safe (item 3f)
        "import pandas as pd\nx = pd.Timestamp(ts_input='2031-02-28')\n",
        "import pandas as pd\nx = pd.date_range(start='2031-01-01', end='2031-02-01')\n",
        # assignment / deletion targets are not reads (item 3d)
        "import time\ntime.time = lambda: 0.0\n",
        "import datetime\ndatetime.datetime.now = lambda: 0\n",
        "import time\ndel time.time\n",
    ],
)
def test_detector_ignores_safe_code(snippet: str) -> None:
    assert find_wall_clock_reads(snippet) == [], snippet


def test_marker_needs_a_reason() -> None:
    assert find_wall_clock_reads("import time\nx = time.time()  # wall-clock-ok:\n")


def test_marker_as_a_real_comment_suppresses_the_finding() -> None:
    """Companion to the string-literal negative control above: the SAME marker text, as an
    actual ``#`` comment, does suppress the finding -- proving ``_comment_tokens`` is keyed by
    real ``tokenize.COMMENT`` tokens and not merely "the marker text appears somewhere nearby"."""
    assert find_wall_clock_reads("import time\nx = time.time()  # wall-clock-ok: reason here\n") == []


def test_detector_catches_a_planted_offender_in_a_real_file(tmp_path: Path) -> None:
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "from datetime import datetime\n\n\ndef test_x() -> None:\n    assert datetime.now()\n"
    )
    assert find_wall_clock_reads(planted.read_text(encoding="utf-8")) == [(5, "datetime.now()")]
