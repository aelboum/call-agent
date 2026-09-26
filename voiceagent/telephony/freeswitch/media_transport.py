"""The real `wss://` media listener for `mod_audio_stream` (Phase 2.21).

`voiceagent.telephony.freeswitch.media`'s own module docstring named exactly
this as out of scope for every phase before this one: "establishing the
actual `wss://` listener that accepts FreeSWITCH's connection ... is
deployment/transport wiring outside this phase's scope -- `register_socket()`
is the seam a real listener calls into." This module is that listener, and
nothing else: it accepts a real WebSocket connection, verifies which call it
belongs to, and hands a `MediaSocket`-conforming wrapper to `FreeSwitchMediaProvider
.register_socket()`. Everything past that point (the wire envelope,
`attach()`/`detach()`, format negotiation) is unchanged, existing code.

**Trust boundary** (`docs/PHASE-0-ARCHITECTURE.md` §14.3, quoted in
`voiceagent.telephony.freeswitch.media`'s own docstring): "the `<wss-url>`
carries a short-lived signed session ticket, not a tenant id." A connecting
socket proves *which call* it is for by presenting a ticket this product
itself minted and signed (`mint_media_ticket()`) when it commanded
FreeSWITCH to open this stream (`FreeSwitchTelephonyProvider
.start_media_stream()`) -- never by a channel variable, a query parameter
FreeSWITCH merely echoes back, or any other value an external party could
forge. A ticket is single-purpose (bound to one `call_ref`), short-lived
(`ttl_seconds`), and verified with `hmac.compare_digest` (constant-time,
never a `==` string comparison) before a socket is ever registered -- an
invalid or expired ticket gets the connection closed immediately, with
`register_socket()` never called, so a rejected connection can never reach
`MediaProvider.attach()` for any call.

The signing secret is a real secret (`FREESWITCH_MEDIA_TICKET_SECRET`),
read through `infra.secrets` exactly like every other credential in this
product -- never stored on `Settings`, never logged, never part of the
ticket itself (only its HMAC output is).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.exceptions import ConnectionClosed

from voiceagent.telephony.contracts import CallRef, TransportError
from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider

__all__ = [
    "FreeSwitchMediaListener",
    "TicketVerificationError",
    "WebSocketMediaSocket",
    "mint_media_ticket",
    "serve_freeswitch_media",
    "verify_media_ticket",
]

_logger = logging.getLogger(__name__)


class TicketVerificationError(TransportError):
    """A media ticket was missing, malformed, expired, or failed signature
    verification. Never carries the raw ticket or the signing secret."""


def mint_media_ticket(call_ref: CallRef, secret: str, *, ttl_seconds: float = 60.0) -> str:
    """A short-lived, single-call-scoped, HMAC-signed ticket -- the *only*
    thing a real `wss://` connection can use to claim it is for `call_ref`.
    Format is `<call_ref>.<expiry_epoch>.<hex_signature>`; `call_ref` is a
    product-minted UUID (`FreeSwitchTelephonyProvider`'s own `uuid_factory`)
    and therefore never contains a `.`, so a single `rsplit(".", 2)` in
    `verify_media_ticket()` is unambiguous."""
    expiry = int(time.time() + ttl_seconds)
    signature = _sign(call_ref, expiry, secret)
    return f"{call_ref}.{expiry}.{signature}"


def verify_media_ticket(ticket: str, secret: str) -> CallRef:
    """Returns the ticket's `call_ref` if -- and only if -- the signature is
    valid for the exact `(call_ref, expiry)` pair encoded in the ticket
    *and* `expiry` has not yet passed. Raises `TicketVerificationError`
    otherwise; the message never includes the ticket, the secret, or the
    computed signature."""
    parts = ticket.rsplit(".", 2)
    if len(parts) != 3:
        raise TicketVerificationError("malformed media ticket")
    call_ref, expiry_raw, signature = parts
    try:
        expiry = int(expiry_raw)
    except ValueError as exc:
        raise TicketVerificationError("malformed media ticket") from exc
    expected = _sign(call_ref, expiry, secret)
    if not hmac.compare_digest(expected, signature):
        raise TicketVerificationError("media ticket signature is invalid")
    if time.time() > expiry:
        raise TicketVerificationError("media ticket has expired")
    return call_ref


def _sign(call_ref: CallRef, expiry: int, secret: str) -> str:
    message = f"{call_ref}.{expiry}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class WebSocketMediaSocket:
    """Adapts a real WebSocket connection (server-side) to
    `voiceagent.telephony.freeswitch.media.MediaSocket` -- binary frames in
    (`mod_audio_stream`'s own raw-L16-PCM inbound shape), JSON text frames
    out, exactly the asymmetric envelope `media.py` already expects and
    builds. This class knows nothing about ESL, tickets, or call
    correlation -- purely a duck-typed adapter over whatever WebSocket
    connection object it is given (production: `websockets`; a test: any
    object with the same three calls)."""

    def __init__(
        self,
        *,
        send: Callable[[str], Awaitable[None]],
        recv: Callable[[], Awaitable[bytes | str]],
        close: Callable[[], Awaitable[None]],
    ) -> None:
        self._send = send
        self._recv = recv
        self._close = close
        self._closed = False

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def receive_binary(self) -> AsyncIterator[bytes]:
        while True:
            try:
                message = await self._recv()
            except _ConnectionClosed:
                return
            if isinstance(message, bytes):
                yield message
            # A text message from FreeSWITCH on the media socket would be
            # protocol-non-conforming (Phase 2.21 brief section 11:
            # "malformed media frame") -- dropped, not raised, so one
            # unexpected frame cannot abort an otherwise-good stream,
            # mirroring `_normalize_event()`'s identical "drop, don't raise"
            # posture for an unmapped ESL event.

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close()


class _ConnectionClosed(Exception):
    """Raised internally by the `recv` callable passed to
    `WebSocketMediaSocket` to signal a closed connection -- never a `websockets`
    type directly, so this module (and its tests) do not depend on which
    WebSocket library is in use beneath `FreeSwitchMediaListener`."""


class FreeSwitchMediaListener:
    """Owns the real inbound `wss://` server FreeSWITCH's media stream
    connects to. One instance per `call-runtime` process, wrapping one
    `FreeSwitchMediaProvider` -- registers a verified connection's socket
    with it (`register_socket()`) and otherwise does nothing but keep that
    connection's own handler task alive for as long as the socket exists
    (`voiceagent.telephony.freeswitch.media._FreeSwitchMediaStream` owns the
    actual send/receive/close calls once registered).
    """

    def __init__(self, media_provider: FreeSwitchMediaProvider, *, ticket_secret: str) -> None:
        self._media_provider = media_provider
        self._ticket_secret = ticket_secret

    def extract_call_ref(self, path: str) -> CallRef:
        """`path` is `/media/<ticket>` (the exact suffix
        `FreeSwitchTelephonyProvider.start_media_stream()`'s own `media_url`
        must be minted with, e.g. `wss://runtime.example/media/<ticket>`).
        Raises `TicketVerificationError` for anything else -- a malformed
        path is treated identically to an invalid ticket, since both mean
        "this connection cannot prove which call it is for"."""
        prefix = "/media/"
        if not path.startswith(prefix):
            raise TicketVerificationError("media path is missing the ticket segment")
        ticket = path[len(prefix) :]
        return verify_media_ticket(ticket, self._ticket_secret)

    async def handle_connection(
        self,
        path: str,
        *,
        send: Callable[[str], Awaitable[None]],
        recv: Callable[[], Awaitable[bytes | str]],
        close: Callable[[], Awaitable[None]],
        wait_closed: Callable[[], Awaitable[None]],
    ) -> None:
        """The transport-agnostic connection handler: verify the ticket
        first, *before* ever registering a socket (Phase 2.21 brief section
        9: "a FreeSWITCH event or media connection must not bypass
        authorization") -- an unverifiable connection is closed immediately
        and never reaches `MediaProvider.attach()` for any call. `websockets
        .serve()`'s own per-connection handler (a thin wrapper supplying the
        real `send`/`recv`/`close`/`wait_closed`) is the only production
        caller; a test can call this directly with fakes."""
        try:
            call_ref = self.extract_call_ref(path)
        except TicketVerificationError:
            _logger.warning("media.ticket_rejected")
            await close()
            return
        socket = WebSocketMediaSocket(send=send, recv=recv, close=close)
        self._media_provider.register_socket(call_ref, socket)
        # Registration only hands the socket over; `attach()` (called by
        # `voiceagent.runtime.call_task.run_call_task()`, off the audio
        # path entirely) is what actually starts using it. This handler's
        # only remaining job is to keep the connection's own task alive for
        # as long as the underlying transport does -- `receive_binary()`
        # reads directly from `recv`, independent of this coroutine.
        await wait_closed()


async def serve_freeswitch_media(
    listener: FreeSwitchMediaListener, *, host: str, port: int
) -> Server:
    """Binds `listener` to a real `websockets` server. The only function in
    this module that imports `websockets` directly -- `FreeSwitchMediaListener
    .handle_connection()` itself is transport-agnostic (a test calls it with
    plain callables, no real socket needed)."""

    async def _recv(connection: ServerConnection) -> bytes | str:
        try:
            return await connection.recv()
        except ConnectionClosed as exc:
            raise _ConnectionClosed from exc

    async def _handler(connection: ServerConnection) -> None:
        await listener.handle_connection(
            connection.request.path if connection.request is not None else "",
            send=connection.send,
            recv=lambda: _recv(connection),
            close=connection.close,
            wait_closed=connection.wait_closed,
        )

    return await websockets.serve(_handler, host, port)
