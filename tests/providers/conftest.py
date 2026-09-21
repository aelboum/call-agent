"""Shared Tier 2 (provider adapter) test mocking fixtures: mock the
transport boundary of each adapter's real client (`httpx`/`websockets`),
never the adapter's own logic -- so a Tier 2 test exercises the adapter's
actual request construction and response parsing with no network and no
credentials (Phase 2.3 brief section 11).

Exposed as pytest fixtures rather than plain importable helpers because
nothing under `tests/` is a Python package (no `__init__.py` anywhere,
matching the rest of this repository's test layout) -- fixture injection is
how a nested test module reaches this file, not a relative import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from types import ModuleType

import httpx
import pytest

__all__ = ["FakeWebSocketConnection"]


def _install_mock_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module.httpx, "AsyncClient", fake_async_client)


@pytest.fixture
def mock_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ModuleType, Callable[[httpx.Request], httpx.Response]], None]:
    """`mock_httpx_client(module, handler)` replaces `<module>.httpx
    .AsyncClient` with a factory injecting `httpx.MockTransport(handler)`,
    keeping every other constructor argument (`timeout=...`) exactly as the
    adapter passes it."""

    def _install(module: ModuleType, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        _install_mock_httpx_client(monkeypatch, module, handler)

    return _install


class FakeWebSocketConnection:
    """A minimal double for a `websockets.ClientConnection`: an async
    context manager, an async iterator over a scripted message sequence,
    and a `send()` that records what an adapter sent instead of touching a
    socket."""

    def __init__(self, messages: Sequence[str | bytes]) -> None:
        self.sent: list[str | bytes] = []
        self._messages = list(messages)

    async def __aenter__(self) -> FakeWebSocketConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    def __aiter__(self) -> FakeWebSocketConnection:
        return self

    async def __anext__(self) -> str | bytes:
        if not self._messages:
            raise StopAsyncIteration
        # Yield control once so a concurrent audio-sending task gets a
        # chance to run between messages, exactly like a real socket would
        # interleave with the sender.
        await asyncio.sleep(0)
        return self._messages.pop(0)


@pytest.fixture
def fake_websocket_connection_class() -> type[FakeWebSocketConnection]:
    """Returns the `FakeWebSocketConnection` class itself (not an
    instance) -- a test constructs its own with a scripted message
    sequence: `connection = fake_websocket_connection_class([...])`."""
    return FakeWebSocketConnection


@pytest.fixture
def mock_websockets_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ModuleType, Callable[..., Awaitable[object] | object]], None]:
    """`mock_websockets_connect(module, connect)` replaces `<module>
    .websockets.connect` with `connect`, which may return a
    `FakeWebSocketConnection` directly or raise (to simulate a connect-time
    failure such as `websockets.exceptions.InvalidStatus`)."""

    def _install(module: ModuleType, connect: Callable[..., object]) -> None:
        monkeypatch.setattr(module.websockets, "connect", connect)

    return _install
