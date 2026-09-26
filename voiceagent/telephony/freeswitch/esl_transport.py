"""The real TCP transport for `EslConnection` (Phase 2.21).

`voiceagent.telephony.freeswitch.esl`'s own module docstring named exactly
this as out of scope for every phase before this one: "establishing an
actual TCP connection to `mod_event_socket` (host, port, password,
reconnection with backoff) is a real implementation's own concern." This
module is that concern, and nothing else -- it implements `EslConnection`
(`send`, `events`) for real, over a real socket, and is the only file in
this product that understands ESL's own wire framing.

**Wire protocol** (`mod_event_socket`, inbound mode, plain-text events --
FreeSWITCH's own documented format, not vendored from any third-party
client library): each frame is a block of `Name: Value` header lines
terminated by a blank line; a `Content-Length` header means that many more
raw bytes immediately follow as the frame's body. `Content-Type`
distinguishes what a frame *is*:

* `auth/request` -- FreeSWITCH's greeting, sent once, no body. The one
  packet `_authenticate()` waits for before it ever sends a byte.
* `command/reply` -- the reply to a plain command (`auth`, `event plain
  ...`, `uuid_answer ...`, `bgapi ...`); the reply text lives in a
  `Reply-Text` header, not a body.
* `api/response` -- the reply to a synchronous `api` command; its result
  lives in the body (this product never sends a plain `api` command today,
  only `bgapi`, but `send()` handles either shape uniformly).
* `text/event-plain` -- an event. Its OWN fields are a second, nested
  `Name: Value` block inside the frame's *body* (parsed by `_parse_kv_block`),
  never in the outer frame's headers.
* `text/disconnect-notice` -- FreeSWITCH is closing the connection.

Commands are sent one at a time (`_send_lock`): ESL gives no correlation id
for an ordinary `command/reply`, so replies are matched to commands purely
by the order they arrive on the one shared connection -- exactly why a
second `send()` must never overlap a first.

Subscribes to `event plain ALL` rather than an enumerated list: this module
has no event-name knowledge of its own (`voiceagent.telephony.freeswitch
.provider._normalize_event()` already safely drops anything it does not
recognize) -- enumerating a subscription list here would just be a second
copy of that module's own event-name knowledge, guaranteed to drift.

**Reconnection** (`ManagedEslConnection`, brief section 5: "reconnect
behavior must NOT accidentally create duplicate call ownership"): bounded
exponential backoff, and deliberately does no resynchronization of its
own. `voiceagent.runtime.reconciliation`'s own module docstring already
establishes the governing invariant this connects to: a `CallSession` whose
owning runtime's heartbeat expired is marked `interrupted`, never
reassigned -- there is no "resume a channel across a reconnect" concept
anywhere in this product for `ManagedEslConnection` to plug into, so it does
not invent one. A command in flight during a disconnect fails immediately
with `TransportError` (never hangs indefinitely); a fresh connection after
reconnect starts with no memory of any previous channel, exactly as if it
were the first connection ever made -- no channel-UUID map is carried
across a reconnect, so there is nothing for a reconnect to duplicate
ownership *of*. Full channel/session reconciliation on reconnect (the
"parked-channel reaper" `docs/PHASE-0-ARCHITECTURE.md` §10.6 describes as
"design-in, not retrofit") is a real, separate, larger feature and is
explicitly deferred -- see `docs/PHASE-2.21-FREESWITCH-TELEPHONY
-INTEGRATION.md`'s "Known limitations."

**Credentials**: the ESL password is never read from `Settings` or logged --
`connect_esl()`'s `password_provider` callable is how a caller supplies it
(production wiring reads it from `infra.secrets`, exactly like every AI
vendor API key already does).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping

from voiceagent.telephony.contracts import TransportError
from voiceagent.telephony.freeswitch.esl import EslEvent

__all__ = ["EslAuthenticationError", "EslTcpConnection", "ManagedEslConnection", "connect_esl"]

_logger = logging.getLogger(__name__)

#: Bounded: an unconsumed backlog of raw frames (events awaiting a consumer,
#: or -- vanishingly rarely -- control replies) cannot grow without limit.
#: Generous relative to real ESL traffic (lifecycle events, not audio) --
#: overflow drops the newest item and logs, mirroring `voiceagent.runtime
#: .conversation_persistence`'s own established bounded-queue discipline
#: (drop rather than block the reader, never grow past the bound).
_QUEUE_MAXSIZE = 256


class EslAuthenticationError(TransportError):
    """ESL rejected the configured password. Never carries the password
    itself -- only ever raised with a fixed, non-secret message."""


class _RawFrame:
    __slots__ = ("headers", "body")

    def __init__(self, headers: dict[str, str], body: str | None) -> None:
        self.headers = headers
        self.body = body


async def _read_frame(reader: asyncio.StreamReader) -> _RawFrame | None:
    """One ESL frame, or `None` on a clean EOF (the peer closed the
    connection without a `text/disconnect-notice`)."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if text == "":
            break
        name, sep, value = text.partition(":")
        if sep:
            headers[name.strip()] = value.strip()
    body: str | None = None
    raw_length = headers.get("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise TransportError(
                f"malformed ESL frame: non-integer Content-Length {raw_length!r}"
            ) from exc
        body = (await reader.readexactly(length)).decode("utf-8", errors="replace")
    return _RawFrame(headers=headers, body=body)


def _parse_kv_block(text: str) -> dict[str, str]:
    """An event's own fields, one `Name: Value` pair per line -- the exact
    same flat shape `EslEvent` already is, just arriving one level deeper
    (inside a `text/event-plain` frame's body) than a frame's own headers."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep:
            fields[name.strip()] = value.strip()
    return fields


class EslTcpConnection:
    """One real TCP connection to `mod_event_socket`. Implements
    `EslConnection` (`send`, `events`); use `connect()` to construct one --
    the plain constructor takes an already-open stream pair and assumes
    nothing about how it was obtained (a real socket in production, a bound
    in-memory pipe in a test)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._control_queue: asyncio.Queue[Mapping[str, str] | Exception] = asyncio.Queue()
        self._event_queue: asyncio.Queue[EslEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._reader_task: asyncio.Task[None] = asyncio.ensure_future(self._read_loop())

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        password: str,
        *,
        connect_timeout_seconds: float = 10.0,
    ) -> EslTcpConnection:
        """Bounded end to end: the TCP handshake, the auth exchange, and the
        event subscription must all complete within `connect_timeout_seconds`
        combined, or this raises `TransportError` and leaves nothing open."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout_seconds
            )
        except TimeoutError as exc:
            raise TransportError(f"ESL connection to {host}:{port} timed out") from exc
        except OSError as exc:
            raise TransportError(f"ESL connection to {host}:{port} failed: {exc}") from exc
        connection = cls(reader, writer)
        try:
            await asyncio.wait_for(connection._handshake(password), timeout=connect_timeout_seconds)
        except BaseException:
            await connection.close()
            raise
        return connection

    async def _handshake(self, password: str) -> None:
        greeting = await self._next_control()
        if greeting.get("Content-Type") != "auth/request":
            raise TransportError("unexpected ESL greeting (not auth/request)")
        await self._write_command(f"auth {password}")
        reply = await self._next_control()
        if not reply.get("Reply-Text", "").startswith("+OK"):
            raise EslAuthenticationError("ESL authentication rejected")
        await self.send("event plain ALL")

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    frame = await _read_frame(self._reader)
                except (asyncio.IncompleteReadError, ConnectionError, OSError, TransportError):
                    break
                if frame is None:
                    break
                content_type = frame.headers.get("Content-Type", "")
                if content_type == "text/event-plain":
                    self._push_event(_parse_kv_block(frame.body or ""))
                elif content_type == "text/disconnect-notice":
                    break
                else:
                    payload: dict[str, str] = dict(frame.headers)
                    if frame.body is not None:
                        payload["__body__"] = frame.body
                    self._control_queue.put_nowait(payload)
        finally:
            self._closed = True
            self._push_event(None)
            with contextlib.suppress(asyncio.QueueFull):
                self._control_queue.put_nowait(TransportError("ESL connection closed"))

    def _push_event(self, event: EslEvent | None) -> None:
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            _logger.warning("esl.event_queue_full: dropping one event")

    async def _next_control(self) -> Mapping[str, str]:
        item = await self._control_queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def _write_command(self, command: str) -> None:
        self._writer.write(f"{command}\n\n".encode())
        await self._writer.drain()

    async def send(self, command: str) -> str:
        if self._closed:
            raise TransportError("ESL connection is closed")
        async with self._send_lock:
            try:
                await self._write_command(command)
                reply = await self._next_control()
            except BaseException:
                # Cancellation-safety (brief section 5/12): ESL gives no
                # correlation id for an ordinary command reply -- ordering
                # on the wire is the only thing that pairs one with its
                # command, which is exactly why commands are serialized
                # (`_send_lock`) in the first place. Abandoning a wait here
                # (a timeout in the caller, e.g. `FreeSwitchTelephonyProvider
                # ._command()`'s own `asyncio.wait_for`, or a direct
                # cancellation) means this connection can no longer prove
                # which future reply belongs to which future command -- a
                # later, unrelated `send()` could otherwise silently receive
                # *this* command's own stale, late-arriving reply. There is
                # no safe way to resume using a connection in that state, so
                # it is torn down here instead: `ManagedEslConnection`
                # reconnects (a fresh connection has no such ambiguity); a
                # bare `EslTcpConnection` surfaces the failure to its own
                # caller via `self._closed`.
                self._closed = True
                self._reader_task.cancel()
                raise
        reply_text = reply.get("Reply-Text")
        if reply_text is not None:
            return reply_text
        return reply.get("__body__", "")

    async def events(self) -> AsyncIterator[EslEvent]:
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def wait_closed(self) -> None:
        """Resolves once the read loop has ended, for any reason (a clean
        disconnect notice, a transport-level error, or `close()`)."""
        with contextlib.suppress(Exception):
            await asyncio.shield(self._reader_task)

    async def close(self) -> None:
        if self._closed and self._reader_task.done():
            return
        self._closed = True
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def connect_esl(
    *, host: str, port: int, password: str, connect_timeout_seconds: float = 10.0
) -> EslTcpConnection:
    """A thin, named entrypoint for the common case (one connection, no
    reconnect) -- `ManagedEslConnection` is the one that reconnects."""
    return await EslTcpConnection.connect(
        host, port, password, connect_timeout_seconds=connect_timeout_seconds
    )


class ManagedEslConnection:
    """`EslConnection` over an `EslTcpConnection` that reconnects itself,
    with bounded exponential backoff, on disconnect. See this module's own
    docstring for exactly what reconnecting does and does not do (no
    resynchronization, no cross-reconnect channel memory).

    `password_provider` is called fresh on every connection attempt
    (including every reconnect) -- never cached here -- so a real deployment
    reads it from `infra.secrets` each time, exactly like every other secret
    read in this product; this class never stores the password itself.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password_provider: Callable[[], str],
        connect_timeout_seconds: float = 10.0,
        reconnect_initial_backoff_seconds: float = 1.0,
        reconnect_max_backoff_seconds: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._password_provider = password_provider
        self._connect_timeout_seconds = connect_timeout_seconds
        self._initial_backoff = reconnect_initial_backoff_seconds
        self._max_backoff = reconnect_max_backoff_seconds
        self._connection: EslTcpConnection | None = None
        self._closed = False
        self._supervisor_task: asyncio.Task[None] | None = None
        self._forward_task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[EslEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    async def start(self) -> None:
        """Establishes the first connection -- bounded, raises on failure --
        then starts the background reconnect supervisor. Call once."""
        await self._connect_once()
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def _connect_once(self) -> None:
        connection = await EslTcpConnection.connect(
            self._host,
            self._port,
            self._password_provider(),
            connect_timeout_seconds=self._connect_timeout_seconds,
        )
        self._connection = connection
        if self._forward_task is not None:
            self._forward_task.cancel()
        self._forward_task = asyncio.create_task(self._forward_events(connection))

    async def _forward_events(self, connection: EslTcpConnection) -> None:
        async for event in connection.events():
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                _logger.warning("esl.managed_event_queue_full: dropping one event")

    async def _supervise(self) -> None:
        backoff = self._initial_backoff
        while not self._closed:
            connection = self._connection
            if connection is None:
                return
            await connection.wait_closed()
            if self._closed:
                return
            _logger.warning("esl.disconnected: attempting reconnect")
            while not self._closed:
                try:
                    await self._connect_once()
                    backoff = self._initial_backoff
                    break
                except TransportError:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)

    async def send(self, command: str) -> str:
        connection = self._connection
        if connection is None or connection._closed:  # noqa: SLF001 -- same module.
            raise TransportError("ESL connection is not currently established")
        return await connection.send(command)

    async def events(self) -> AsyncIterator[EslEvent]:
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
        if self._forward_task is not None:
            self._forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._forward_task
        if self._connection is not None:
            await self._connection.close()
        self._event_queue.put_nowait(None)
