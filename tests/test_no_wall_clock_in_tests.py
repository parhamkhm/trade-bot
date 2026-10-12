"""No test may read the wall clock (D-094).

A test that calls ``datetime.now()`` / ``time.time()`` (directly or through an alias) can pass
today and fail on another date, or quietly depend on the machine's clock. Time-dependent code takes
an injected clock (``now=`` / ``clock=`` / ``now_ms=``); tests pass a fixed instant.

This is enforcement layer 1 (static). Layer 2 is the CI step that runs the whole suite under
libfaketime at two fake dates (``.github/workflows/ci.yml``).

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

# (module, attribute) pairs that read today's date or the epoch clock.
_BANNED_ATTRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("date", "today"),
        ("time", "time"),
        ("time", "time_ns"),
        ("time", "localtime"),
        ("time", "gmtime"),
        ("time", "ctime"),
        ("time", "strftime"),  # strftime(fmt) with no tuple reads the clock
    }
)
# ``from time import time`` style: bare names that are banned when imported from these modules.
_BANNED_FROM_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {("time", "time"), ("time", "time_ns"), ("time", "localtime"), ("time", "gmtime"), ("time", "ctime")}
)


def find_wall_clock_reads(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for each unmarked wall-clock read in ``source``."""
    tree = ast.parse(source)
    lines = source.splitlines()

    # alias -> real dotted name, for ``import datetime as dt`` / ``from datetime import datetime as D``
    module_alias: dict[str, str] = {}
    name_alias: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                module_alias[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                name_alias[item.asname or item.name] = (node.module, item.name)

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        found: str | None = None
        if isinstance(func, ast.Attribute):
            owner = func.value
            # time.time() / dt.datetime.now() / datetime.now() / datetime.datetime.now()
            if isinstance(owner, ast.Name):
                base = owner.id
                if base in module_alias:
                    mod, cls = module_alias[base], None
                elif base in name_alias:
                    mod, cls = name_alias[base]
                else:
                    mod, cls = base, None
                key = (cls or mod, func.attr)
                if key in _BANNED_ATTRS:
                    found = f"{ast.unparse(func)}()"
            elif isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name):
                # dt.datetime.now()
                mod = module_alias.get(owner.value.id, owner.value.id)
                if mod == "datetime" and (owner.attr, func.attr) in _BANNED_ATTRS:
                    found = f"{ast.unparse(func)}()"
        elif isinstance(func, ast.Name) and func.id in name_alias:
            origin = name_alias[func.id]
            if origin in _BANNED_FROM_IMPORTS:
                found = f"{origin[0]}.{origin[1]}() (imported as {func.id})"
        if found is None:
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        span = lines[node.lineno - 1 : end]
        if any(MARKER in text and text.split(MARKER, 1)[1].strip() for text in span):
            continue
        hits.append((node.lineno, found))
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
