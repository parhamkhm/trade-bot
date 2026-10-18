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
# As a bare reference (``clock=time.gmtime``) they are always flagged.
_BANNED_WITHOUT_ARG: dict[str, int] = {
    "time.localtime": 0,
    "time.gmtime": 0,
    "time.ctime": 0,
    "time.strftime": 1,
}
# Constructors that read the clock when handed the string "now" / "today".
_NOW_STRING_CTORS: frozenset[str] = frozenset({"pandas.Timestamp", "pandas.to_datetime", "numpy.datetime64"})
_NOW_STRINGS = frozenset({"now", "today"})


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> fully qualified dotted name, for every import anywhere in the file."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    aliases[item.asname] = item.name
                else:  # ``import a.b`` binds ``a``
                    top = item.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Dotted name of a ``Name`` / ``Attribute`` chain with import aliases resolved, else ``None``."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _reads_clock(name: str, call: ast.Call | None) -> bool:
    """Does ``name`` -- called as ``call``, or merely referenced when ``call`` is None -- read the clock?"""
    if name in _BANNED:
        return True
    limit = _BANNED_WITHOUT_ARG.get(name)
    return limit is not None and (call is None or len(call.args) + len(call.keywords) <= limit)


def _is_now_string_ctor(name: str | None, call: ast.Call) -> bool:
    if name not in _NOW_STRING_CTORS or not call.args:
        return False
    first = call.args[0]
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.strip().lower() in _NOW_STRINGS
    )


def _marked(lines: list[str], node: ast.AST) -> bool:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    span = lines[start - 1 : end]
    return any(MARKER in text and text.split(MARKER, 1)[1].strip() for text in span)


def find_wall_clock_reads(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for each unmarked wall-clock read in ``source``.

    Catches calls (``datetime.now()``), uncalled references (``clock=datetime.now``,
    ``functools.partial(dt.datetime.now, UTC)``), ``from``-imported and aliased spellings, pandas
    ``Timestamp.now/utcnow/today``, and the string forms ``Timestamp("now")`` / ``datetime64("today")``.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    aliases = _import_aliases(tree)
    parent: dict[int, ast.AST] = {}
    for outer in ast.walk(tree):
        for child in ast.iter_child_nodes(outer):
            parent[id(child)] = outer

    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_now_string_ctor(_qualified(node.func, aliases), node) and not _marked(lines, node):
                hits.add((node.lineno, f"{ast.unparse(node.func)}({ast.unparse(node.args[0])})"))
            continue
        if not isinstance(node, ast.Attribute | ast.Name):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue
        enclosing = parent.get(id(node))
        call = enclosing if isinstance(enclosing, ast.Call) and enclosing.func is node else None
        name = _qualified(node, aliases)
        if name is None or not _reads_clock(name, call):
            continue
        anchor: ast.AST = call or node  # a marker anywhere on a multi-line call covers it
        if _marked(lines, anchor):
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
    ],
)
def test_detector_ignores_safe_code(snippet: str) -> None:
    assert find_wall_clock_reads(snippet) == [], snippet


def test_marker_needs_a_reason() -> None:
    assert find_wall_clock_reads("import time\nx = time.time()  # wall-clock-ok:\n")


def test_detector_catches_a_planted_offender_in_a_real_file(tmp_path: Path) -> None:
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "from datetime import datetime\n\n\ndef test_x() -> None:\n    assert datetime.now()\n"
    )
    assert find_wall_clock_reads(planted.read_text(encoding="utf-8")) == [(5, "datetime.now()")]
