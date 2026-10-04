"""Repo-wide pytest fixtures and the real-network guard.

MINOR-17 (first fix round): no test anywhere in this suite may reach a real network socket.
Every HTTP-touching test already mocks at the transport level with ``respx``, which never opens a
real ``socket.socket`` in the first place -- so this guard is a pure safety net. If a future test
(or a regression in one that exists today) ever falls through to a real connection attempt, it
must fail loudly and immediately, not hang on a real DNS lookup or -- far worse -- actually reach
the Tabdeal exchange (CLAUDE.md section 3.6: dry-run by default, the real order path must be
unreachable outside LIVE_TRADING).

MINOR-3 (second fix round): the original guard had two gaps, both demonstrated by the reviewer:

(a) It only patched ``socket.socket``. Plain DNS resolution via ``socket.getaddrinfo`` is a
    separate C-level entry point that does not go through the ``socket.socket`` class at all, so
    ``socket.getaddrinfo("example.com", 443)`` still returned a real answer -- a real outbound
    network call (and a real information leak: the hostname), independent of whether any
    subsequent connect would have been blocked.
(b) The guard was installed by a function-scoped ``autouse`` fixture, which is only active while a
    test is *running*. It is not active while test modules are being imported during collection,
    and it is not active for session- or module-scoped fixtures (which run before any
    function-scoped fixture). Any of those could reach the network unchecked.

Fix: install the guard once, directly, from ``pytest_configure`` -- which pytest runs before
collection starts -- so it is live for the entire process: during collection, for session/module
-scoped fixtures, and for every test. Three entry points are patched: ``socket.socket``,
``socket.getaddrinfo`` and ``socket.create_connection`` (the stdlib convenience function several
HTTP libraries call directly). The function-scoped ``autouse`` fixture below is kept, but now only
as a per-test re-assertion that the process-wide guard is still in place -- defence in depth
against some other plugin or fixture monkeypatching ``socket`` back -- not as the primary
mechanism.

The guard is installed with a self-check: immediately after patching, ``pytest_configure`` calls
all three guarded functions and requires every one of them to raise. If any of them does not
raise, the whole test run refuses to start rather than silently running with a broken guard.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

__all__: list[str] = ["BlockedNetworkAccessError"]


class BlockedNetworkAccessError(RuntimeError):
    """Raised in place of opening a real socket, resolving DNS, or connecting during tests."""


def _make_blocker(name: str) -> Any:
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise BlockedNetworkAccessError(
            f"tests/conftest.py blocks socket.{name} -- no test may reach a real network. "
            "Mock the HTTP layer with respx (see tests/execution/test_tabdeal_client.py) instead "
            "of relaxing this guard."
        )

    return _raise


_blocked_socket = _make_blocker("socket")
_blocked_getaddrinfo = _make_blocker("getaddrinfo")
_blocked_create_connection = _make_blocker("create_connection")


def _install_network_guard() -> None:
    socket.socket = _blocked_socket  # type: ignore[misc]  # assigning over the socket.socket type
    socket.getaddrinfo = _blocked_getaddrinfo
    socket.create_connection = _blocked_create_connection


def _guard_is_active() -> bool:
    return (
        socket.socket is _blocked_socket
        and socket.getaddrinfo is _blocked_getaddrinfo
        and socket.create_connection is _blocked_create_connection
    )


def pytest_configure(config: pytest.Config) -> None:
    """Install the real-network guard before collection starts.

    This runs once, before any test module is imported -- closing the gap where a module-level
    statement, or a session/module-scoped fixture, could reach the network before the
    function-scoped fixture below ever ran.
    """
    del config  # unused -- the hook signature requires it
    _install_network_guard()

    probes: tuple[Any, ...] = (
        lambda: socket.socket(),
        lambda: socket.getaddrinfo("example.com", 443),
        lambda: socket.create_connection(("example.com", 443)),
    )
    for probe in probes:
        try:
            probe()
        except BlockedNetworkAccessError:
            continue
        raise RuntimeError(
            "tests/conftest.py: real-network guard self-check failed -- a guarded socket "
            "function did not raise BlockedNetworkAccessError. Refusing to run the test suite "
            "with a broken network guard."
        )


@pytest.fixture(autouse=True)
def _block_real_sockets() -> Iterator[None]:
    """Per-test re-assertion that the process-wide guard installed by ``pytest_configure`` is
    still in place. The guard itself is process-wide and is never torn down between tests; this
    fixture exists so a broken guard still fails loudly at the start of the test that exposed it,
    rather than only at the next full test-suite run."""
    if not _guard_is_active():
        _install_network_guard()
    yield
