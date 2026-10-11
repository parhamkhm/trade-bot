"""SPEC §7.2 J / D-040: no Phase 3 strategy or indicator may read bar volume while D-040 is open.

Static check over every module in ``src/tbot/strategies`` and ``src/tbot/indicators``: any attribute,
name, keyword or string literal mentioning ``volume`` fails the test. A future strategy that needs volume
must wait until D-040 is closed and this test is changed by Parham's decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("src/tbot/strategies", "src/tbot/indicators")


def _modules() -> list[Path]:
    return sorted(p for pkg in SCANNED_PACKAGES for p in (REPO_ROOT / pkg).rglob("*.py"))


def _volume_uses(source: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and "volume" in node.attr.lower():
            hits.append(f"line {node.lineno}: attribute .{node.attr}")
        elif isinstance(node, ast.Name) and "volume" in node.id.lower():
            hits.append(f"line {node.lineno}: name {node.id}")
        elif isinstance(node, ast.keyword) and node.arg and "volume" in node.arg.lower():
            hits.append(f"line {node.value.lineno}: keyword {node.arg}=")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "volume" in node.value.lower()
            and not _is_docstring_like(node)
        ):
            hits.append(f"line {node.lineno}: string {node.value!r}")
    return hits


def _is_docstring_like(node: ast.Constant) -> bool:
    # Prose mentioning volume (docstrings, comments in strings) is not a data access.
    value = node.value
    return isinstance(value, str) and (" " in value.strip() or "\n" in value)


def test_scanned_packages_exist() -> None:
    for pkg in SCANNED_PACKAGES:
        assert (REPO_ROOT / pkg).is_dir(), pkg


@pytest.mark.parametrize("module", _modules(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_strategy_or_indicator_reads_bar_volume(module: Path) -> None:
    hits = _volume_uses(module.read_text(encoding="utf-8"))
    assert not hits, (
        f"{module.relative_to(REPO_ROOT)} reads bar volume ({'; '.join(hits)}); "
        "blocked while D-040 is open (SPEC §7.2 J)"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(w):\n    return w.bars[-1].volume\n",
        "def f(df):\n    return df['volume'].mean()\n",
        "def f(bar):\n    v = getattr(bar, 'volume')\n    return v\n",
        "quote_volume = 1\n",
        "def f(x):\n    return g(volume=x)\n",
    ],
)
def test_detector_catches_volume_access(snippet: str) -> None:
    assert _volume_uses(snippet)


def test_detector_ignores_prose() -> None:
    assert not _volume_uses('"""This strategy never uses volume."""\nx = 1\n')
