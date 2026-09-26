"""`EslTcpConnection`/`ManagedEslConnection` (Phase 2.21) against a real
`asyncio` TCP server speaking ESL's own wire framing -- not a `Protocol`-level
fake. This is deliberately a stronger test than `FakeEslConnection` gives
`FreeSwitchTelephonyProvider`: it exercises the actual byte-level framing
(`Content-Length`, header blocks, `text/event-plain` bodies), not just the
already-normalized `EslConnection` shape.

No `pytest-asyncio` in this repository (`asyncio.run()` per test, matching
every other async test file here) -- each test defines and runs exactly one
`scenario()` coroutine that owns both the fake server and the connection
under test for its own lifetime, since both are bound to whichever event
loop `asyncio.run()` creates for that one call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from voiceagent.telephony.contracts import TransportError
from voiceagent.telephony.freeswitch.esl_transport import (
    EslAuthenticationError,
    EslTcpConnection,
    ManagedEslConnection,
)


class _FakeFreeSwitchServer:
    """A minimal, honest ESL peer: sends the real `auth/request` greeting,
    authenticates against a real password, replies to `event plain ALL`,
    and lets a test script per-command replies or push events on demand."""

    def __init__(self, *, password: str = "secret") -> None:  # noqa: S107 -- test fixture, not a real credential.
        self.password = password
        self.received_commands: list[str] = []
        self.reply_script: dict[str, str] = {}
        self.silence_on: set[str] = set()
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        await self._send_frame({"Content-Type": "auth/request"})
        while True:
            command = await self._read_command(reader)
            if command is None:
                return
            self.received_commands.append(command)
            if command in self.silence_on:
                continue  # deliberately never reply -- simulates a wedge.
            if command.startswith("auth "):
                password = command[len("auth ") :]
                ok = password == self.password
                await self._send_frame(
                    {
                        "Content-Type": "command/reply",
                        "Reply-Text": "+OK accepted" if ok else "-ERR invalid",
                    }
                )
            else:
                reply = self.reply_script.get(command, "+OK")
                await self._send_frame({"Content-Type": "command/reply", "Reply-Text": reply})

    async def _read_command(self, reader: asyncio.StreamReader) -> str | None:
        line = await reader.readline()
        if not line:
            return None
        await reader.readline()  # the blank line terminating the command.
        return line.decode().rstrip("\r\n")

    async def _send_frame(self, headers: dict[str, str], body: str | None = None) -> None:
        assert self._writer is not None  # noqa: S101 -- test helper invariant.
        lines = [f"{k}: {v}" for k, v in headers.items()]
        if body is not None:
            lines.append(f"Content-Length: {len(body.encode())}")
        self._writer.write(("\n".join(lines) + "\n\n").encode())
        if body is not None:
            self._writer.write(body.encode())
        await self._writer.drain()

    async def send_raw(self, data: bytes) -> None:
        assert self._writer is not None  # noqa: S101
        self._writer.write(data)
        await self._writer.drain()

    async def push_event(self, fields: dict[str, str]) -> None:
        body = "".join(f"{k}: {v}\n" for k, v in fields.items())
        await self._send_frame({"Content-Type": "text/event-plain"}, body)

    async def send_disconnect_notice(self) -> None:
        await self._send_frame({"Content-Type": "text/disconnect-notice"})

    async def disconnect_abruptly(self) -> None:
        assert self._writer is not None  # noqa: S101
        self._writer.close()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def test_connect_authenticates_and_subscribes() -> None:
    async def scenario() -> list[str]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                return list(server.received_commands)
            finally:
                await connection.close()
        finally:
            await server.close()

    assert asyncio.run(scenario()) == ["auth secret", "event plain ALL"]


def test_wrong_password_raises_authentication_error() -> None:
    async def scenario() -> None:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            with pytest.raises(EslAuthenticationError):
                await EslTcpConnection.connect(host, port, "wrong-password")
        finally:
            await server.close()

    asyncio.run(scenario())


def test_connect_to_a_closed_port_raises_transport_error() -> None:
    async def scenario() -> None:
        with pytest.raises(TransportError):
            await EslTcpConnection.connect("127.0.0.1", 1, "secret", connect_timeout_seconds=1.0)

    asyncio.run(scenario())


def test_command_success_returns_reply_text() -> None:
    async def scenario() -> str:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        server.reply_script["uuid_answer call-1"] = "+OK"
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                return await connection.send("uuid_answer call-1")
            finally:
                await connection.close()
        finally:
            await server.close()

    assert asyncio.run(scenario()) == "+OK"


def test_command_failure_returns_the_err_reply_text() -> None:
    async def scenario() -> str:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        server.reply_script["uuid_answer call-1"] = "-ERR no such channel"
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                return await connection.send("uuid_answer call-1")
            finally:
                await connection.close()
        finally:
            await server.close()

    assert asyncio.run(scenario()) == "-ERR no such channel"


def test_a_wedged_command_times_out_and_closes_the_connection() -> None:
    """No internal timeout exists in `EslTcpConnection.send()` itself
    (`FreeSwitchTelephonyProvider._command()` is what bounds it, exactly as
    it already bounds `FakeEslConnection`) -- this proves a caller-applied
    `asyncio.wait_for` cancels cleanly, and that doing so poisons this one
    connection (by design, see `send()`'s own docstring) rather than
    silently letting the abandoned reply corrupt a later command."""

    async def scenario() -> None:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        server.silence_on.add("uuid_answer call-1")
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(connection.send("uuid_answer call-1"), timeout=0.2)
                with pytest.raises(TransportError):
                    await connection.send("uuid_answer call-2")
            finally:
                await connection.close()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_events_are_delivered_as_flat_string_maps() -> None:
    async def scenario() -> dict[str, str]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                await server.push_event(
                    {
                        "Event-Name": "CHANNEL_ANSWER",
                        "Unique-ID": "call-1",
                        "Call-Direction": "outbound",
                    }
                )
                event = await asyncio.wait_for(connection.events().__anext__(), timeout=2.0)
                return dict(event)
            finally:
                await connection.close()
        finally:
            await server.close()

    event = asyncio.run(scenario())
    assert event["Event-Name"] == "CHANNEL_ANSWER"
    assert event["Unique-ID"] == "call-1"


def test_event_field_values_are_url_decoded() -> None:
    """Phase 2.23 regression: a real FreeSWITCH server percent-encodes
    `event plain` field values (`Caller-Destination-Number: %2B15551234567`
    for `+15551234567`) -- found by validating against a real instance,
    where every E.164 phone number (every one starts with `+`) arrived
    corrupted, silently breaking `voiceagent.calls.routing
    .resolve_inbound_route()`'s exact-match lookup for every real call.
    `FakeEslConnection`/`FakeTelephonyProvider` never encode anything, so no
    prior test caught this; `_FakeFreeSwitchServer` here is a real local TCP
    server exercising the real frame-parsing code path end to end."""

    async def scenario() -> dict[str, str]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                await server.push_event(
                    {
                        "Event-Name": "CHANNEL_PARK",
                        "Unique-ID": "call-1",
                        "Caller-Caller-ID-Number": "%2B15550100",
                        "Caller-Destination-Number": "%2B15551234567",
                    }
                )
                event = await asyncio.wait_for(connection.events().__anext__(), timeout=2.0)
                return dict(event)
            finally:
                await connection.close()
        finally:
            await server.close()

    event = asyncio.run(scenario())
    assert event["Caller-Caller-ID-Number"] == "+15550100"
    assert event["Caller-Destination-Number"] == "+15551234567"


def test_events_and_command_replies_do_not_cross_streams() -> None:
    """A command reply must never be mistaken for an event, and vice versa,
    even when they're interleaved on the wire."""

    async def scenario() -> tuple[str, str]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                await server.push_event({"Event-Name": "CHANNEL_PARK", "Unique-ID": "call-x"})
                reply = await connection.send("uuid_answer call-1")
                event = await asyncio.wait_for(connection.events().__anext__(), timeout=2.0)
                return reply, event["Event-Name"]
            finally:
                await connection.close()
        finally:
            await server.close()

    reply, event_name = asyncio.run(scenario())
    assert reply == "+OK"
    assert event_name == "CHANNEL_PARK"


def test_a_malformed_content_length_ends_the_connection_safely() -> None:
    async def scenario() -> list[object]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                await server.send_raw(
                    b"Content-Type: text/event-plain\nContent-Length: not-a-number\n\n"
                )
                events = [
                    event async for event in _bounded(connection.events(), timeout_seconds=2.0)
                ]
                with pytest.raises(TransportError):
                    await connection.send("uuid_answer call-1")
                return events
            finally:
                await connection.close()
        finally:
            await server.close()

    assert asyncio.run(scenario()) == []


def test_disconnect_notice_ends_the_event_stream() -> None:
    async def scenario() -> list[object]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                await server.send_disconnect_notice()
                events = [
                    event async for event in _bounded(connection.events(), timeout_seconds=2.0)
                ]
                with pytest.raises(TransportError):
                    await connection.send("uuid_answer call-1")
                return events
            finally:
                await connection.close()
        finally:
            await server.close()

    assert asyncio.run(scenario()) == []


async def _bounded(
    aiter: AsyncIterator[object], *, timeout_seconds: float
) -> AsyncIterator[object]:
    """Wraps an async iterator so a test never hangs if the module under
    test regresses into never ending its own stream."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("async iterator did not end within the bound")
        try:
            yield await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return


def test_cancellation_during_send_propagates_and_is_not_swallowed() -> None:
    async def scenario() -> None:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        server.silence_on.add("uuid_answer call-1")
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                task = asyncio.create_task(connection.send("uuid_answer call-1"))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                await connection.close()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_concurrent_sends_are_serialized_not_interleaved() -> None:
    """No two commands may be in flight at once -- ESL gives no correlation
    id, so overlapping sends would risk reply misattribution."""

    async def scenario() -> tuple[list[str], list[str]]:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        try:
            connection = await EslTcpConnection.connect(host, port, "secret")
            try:
                results = await asyncio.gather(
                    connection.send("uuid_answer call-1"),
                    connection.send("uuid_answer call-2"),
                    connection.send("uuid_answer call-3"),
                )
                return list(results), server.received_commands[2:]
            finally:
                await connection.close()
        finally:
            await server.close()

    results, commands = asyncio.run(scenario())
    assert results == ["+OK", "+OK", "+OK"]
    assert sorted(commands) == ["uuid_answer call-1", "uuid_answer call-2", "uuid_answer call-3"]


def test_managed_connection_reconnects_after_a_disconnect() -> None:
    async def scenario() -> bool:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        managed = ManagedEslConnection(
            host=host,
            port=port,
            password_provider=lambda: "secret",
            reconnect_initial_backoff_seconds=0.05,
            reconnect_max_backoff_seconds=0.1,
        )
        try:
            await managed.start()
            assert await managed.send("uuid_answer call-1") == "+OK"

            await server.disconnect_abruptly()
            for _ in range(100):
                try:
                    if await managed.send("uuid_answer call-2") == "+OK":
                        return True
                except TransportError:
                    pass
                await asyncio.sleep(0.05)
            return False
        finally:
            await managed.close()
            await server.close()

    assert asyncio.run(scenario()) is True


def test_managed_connection_send_fails_fast_while_disconnected() -> None:
    async def scenario() -> None:
        server = _FakeFreeSwitchServer()
        host, port = await server.start()
        managed = ManagedEslConnection(
            host=host,
            port=port,
            password_provider=lambda: "secret",
            reconnect_initial_backoff_seconds=10.0,
            reconnect_max_backoff_seconds=10.0,
        )
        try:
            await managed.start()
            await server.disconnect_abruptly()
            # Stop listening entirely: the supervisor's very first reconnect
            # attempt (immediate, no backoff before a *first* try) must
            # itself fail, so the 10s backoff genuinely governs the next one
            # -- otherwise this test would pass by accident if reconnection
            # happened to succeed instantly.
            await server.close()
            await asyncio.sleep(0.2)
            with pytest.raises(TransportError):
                await asyncio.wait_for(managed.send("uuid_answer call-1"), timeout=1.0)
        finally:
            await managed.close()

    asyncio.run(scenario())
