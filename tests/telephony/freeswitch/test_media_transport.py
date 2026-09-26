"""`mint_media_ticket`/`verify_media_ticket`, `FreeSwitchMediaListener`, and
`WebSocketMediaSocket` (Phase 2.21) -- the real `wss://` media listener over
a real `websockets` client/server pair on `localhost`, not a `Protocol`-level
fake. No `pytest-asyncio` in this repository -- `asyncio.run()` per test,
matching every other async test file here.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider
from voiceagent.telephony.freeswitch.media_transport import (
    FreeSwitchMediaListener,
    TicketVerificationError,
    mint_media_ticket,
    serve_freeswitch_media,
    verify_media_ticket,
)

_SECRET = "test-ticket-secret"  # noqa: S105 -- test-only.  # pragma: allowlist secret


def test_a_freshly_minted_ticket_verifies_to_its_own_call_ref() -> None:
    ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)
    assert verify_media_ticket(ticket, _SECRET) == "call-1"


def test_an_expired_ticket_is_rejected() -> None:
    ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=-1)
    with pytest.raises(TicketVerificationError):
        verify_media_ticket(ticket, _SECRET)


def test_a_ticket_signed_with_a_different_secret_is_rejected() -> None:
    ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)
    with pytest.raises(TicketVerificationError):
        verify_media_ticket(ticket, "a-different-secret")


def test_a_tampered_call_ref_is_rejected_even_with_a_valid_looking_signature() -> None:
    """A signature is bound to one exact `(call_ref, expiry)` pair -- editing
    either half without knowing the secret must not verify against the
    other."""
    ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)
    call_ref, expiry, signature = ticket.rsplit(".", 2)
    tampered = f"call-2.{expiry}.{signature}"
    with pytest.raises(TicketVerificationError):
        verify_media_ticket(tampered, _SECRET)


@pytest.mark.parametrize("malformed", ["", "not-a-ticket", "a.b", "a.notanumber.c"])
def test_a_malformed_ticket_is_rejected(malformed: str) -> None:
    with pytest.raises(TicketVerificationError):
        verify_media_ticket(malformed, _SECRET)


def test_extract_call_ref_rejects_a_path_with_no_ticket_segment() -> None:
    listener = FreeSwitchMediaListener(FreeSwitchMediaProvider(), ticket_secret=_SECRET)
    with pytest.raises(TicketVerificationError):
        listener.extract_call_ref("/not-media/anything")


def test_extract_call_ref_accepts_a_valid_ticket_path() -> None:
    listener = FreeSwitchMediaListener(FreeSwitchMediaProvider(), ticket_secret=_SECRET)
    ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)
    assert listener.extract_call_ref(f"/media/{ticket}") == "call-1"


def test_handle_connection_rejects_an_invalid_ticket_without_registering_a_socket() -> None:
    """Phase 2.21 brief section 9: an unauthorized connection must never
    reach `MediaProvider.attach()` for any call."""

    async def scenario() -> tuple[bool, bool]:
        provider = FreeSwitchMediaProvider()
        listener = FreeSwitchMediaListener(provider, ticket_secret=_SECRET)
        closed = False

        async def _close() -> None:
            nonlocal closed
            closed = True

        async def _send_never_called(_: str) -> None:
            raise AssertionError("must not be called for a rejected connection")

        async def _recv_never_called() -> bytes:
            raise AssertionError("must not be called for a rejected connection")

        async def _wait_closed_never_called() -> None:
            raise AssertionError("must not be called for a rejected connection")

        await listener.handle_connection(
            "/media/not-a-real-ticket",
            send=_send_never_called,
            recv=_recv_never_called,
            close=_close,
            wait_closed=_wait_closed_never_called,
        )
        registered = "call-1" in provider._sockets  # noqa: SLF001 -- test assertion only.
        return closed, registered

    closed, registered = asyncio.run(scenario())
    assert closed is True
    assert registered is False


def test_handle_connection_registers_a_socket_for_a_valid_ticket() -> None:
    async def scenario() -> bool:
        provider = FreeSwitchMediaProvider()
        listener = FreeSwitchMediaListener(provider, ticket_secret=_SECRET)
        ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)
        wait_closed_called = asyncio.Event()

        async def _wait_closed() -> None:
            wait_closed_called.set()

        async def _send_unused(_: str) -> None:
            raise AssertionError("not exercised by this test")

        async def _recv_unused() -> bytes:
            raise AssertionError("not exercised by this test")

        async def _close_unused() -> None:
            raise AssertionError("not exercised by this test")

        await listener.handle_connection(
            f"/media/{ticket}",
            send=_send_unused,
            recv=_recv_unused,
            close=_close_unused,
            wait_closed=_wait_closed,
        )
        return wait_closed_called.is_set() and "call-1" in provider._sockets  # noqa: SLF001

    assert asyncio.run(scenario()) is True


def test_end_to_end_over_a_real_websocket_server() -> None:
    """A real `websockets` client connects to a real `FreeSwitchMediaListener`
    server, presents a valid ticket, and the resulting `MediaProvider.attach()`
    can send/receive real frames -- proving `serve_freeswitch_media()`'s own
    wiring (path extraction, `recv`/`send`/`close`/`wait_closed` plumbing),
    not just `handle_connection()` in isolation."""

    async def scenario() -> tuple[bytes, list[object]]:
        provider = FreeSwitchMediaProvider()
        listener = FreeSwitchMediaListener(provider, ticket_secret=_SECRET)
        server = await serve_freeswitch_media(listener, host="127.0.0.1", port=0)
        host, port = server.sockets[0].getsockname()[:2]
        ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)

        try:
            async with websockets.connect(f"ws://{host}:{port}/media/{ticket}") as client:
                # Give the server handler a moment to register the socket.
                for _ in range(50):
                    if "call-1" in provider._sockets:  # noqa: SLF001
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("server never registered the socket")

                stream = await provider.attach("call-1")
                await client.send(b"\x01\x02\x03\x04")
                received = await asyncio.wait_for(stream.receive().__anext__(), timeout=2.0)

                await stream.send(b"\x05\x06")
                sent_text = await asyncio.wait_for(client.recv(), timeout=2.0)
                await provider.detach("call-1")
                return received, [sent_text]
        finally:
            server.close()
            await server.wait_closed()

    received, sent_texts = asyncio.run(scenario())
    assert received == b"\x01\x02\x03\x04"
    assert len(sent_texts) == 1
    assert isinstance(sent_texts[0], str)
    assert '"streamAudio"' in sent_texts[0]


def test_end_to_end_rejects_an_invalid_ticket_and_closes_the_connection() -> None:
    async def scenario() -> bool:
        provider = FreeSwitchMediaProvider()
        listener = FreeSwitchMediaListener(provider, ticket_secret=_SECRET)
        server = await serve_freeswitch_media(listener, host="127.0.0.1", port=0)
        host, port = server.sockets[0].getsockname()[:2]

        try:
            async with websockets.connect(f"ws://{host}:{port}/media/garbage-ticket") as client:
                with pytest.raises(websockets.ConnectionClosed):
                    await client.recv()
            return "call-1" in provider._sockets  # noqa: SLF001
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) is False


def test_end_to_end_client_disconnect_during_active_media_ends_the_stream_cleanly() -> None:
    """Phase 2.24 brief section 5: "disconnect during active media" -- a
    real client (standing in for FreeSWITCH itself hanging up mid-stream)
    closing its own connection must end `receive_binary()`'s iteration
    cleanly (a graceful stop, not an unhandled exception propagating out of
    `voiceagent.runtime.call_task`'s own audio pump)."""

    async def scenario() -> list[bytes]:
        provider = FreeSwitchMediaProvider()
        listener = FreeSwitchMediaListener(provider, ticket_secret=_SECRET)
        server = await serve_freeswitch_media(listener, host="127.0.0.1", port=0)
        host, port = server.sockets[0].getsockname()[:2]
        ticket = mint_media_ticket("call-1", _SECRET, ttl_seconds=60)

        try:
            client = await websockets.connect(f"ws://{host}:{port}/media/{ticket}")
            for _ in range(50):
                if "call-1" in provider._sockets:  # noqa: SLF001
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("server never registered the socket")

            stream = await provider.attach("call-1")
            await client.send(b"\x01\x02")
            first = await asyncio.wait_for(stream.receive().__anext__(), timeout=2.0)

            await client.close()

            frames = [first]
            async with asyncio.timeout(2.0):
                async for frame in stream.receive():
                    frames.append(frame)
            return frames
        finally:
            server.close()
            await server.wait_closed()

    frames = asyncio.run(scenario())
    assert frames == [b"\x01\x02"]
