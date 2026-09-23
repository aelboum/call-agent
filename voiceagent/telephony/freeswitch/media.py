"""`FreeSwitchMediaProvider` -- media transport over `mod_audio_stream`
(ADR-0002 point 5, amendment point 1; Phase 2.0 report §12.1's documented
wire-protocol mapping, now given a minimal concrete adapter).

`mod_audio_stream`'s wire protocol is asymmetric (Phase 0 report §10.4,
carried forward unverified against a live server in this phase -- see
`voiceagent.telephony.freeswitch.provider`'s own docstring for the same
caveat): FreeSWITCH -> product is raw binary L16 PCM with no envelope;
product -> FreeSWITCH is a JSON text frame
(`{"type": "streamAudio", "data": {"audioDataType": "raw", "sampleRate":
<rate>, "audioData": "<base64 L16>"}}`). This module is the only place that
envelope is constructed or parsed -- the product-owned `MediaStream` contract
(`voiceagent.telephony.contracts`) deals only in opaque PCM `bytes`.

`MediaSocket` is injected, exactly like `EslConnection` (ADR-0002 amendment
point 2's dependency-injection requirement): this module never opens its own
WebSocket. Establishing the actual `wss://` listener that accepts FreeSWITCH's
connection (Phase 2.0 report §5.1: "FS->>RT: WebSocket connect directly to
assigned runtime") is deployment/transport wiring outside this phase's scope
(brief section 29) -- `register_socket()` is the seam a real listener calls
into once a socket exists for a call leg.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voiceagent.metrics import record_provider_operation
from voiceagent.telephony.contracts import (
    AudioFormat,
    CallRef,
    StreamHealth,
    TransportError,
    UnsupportedFormatError,
)

__all__ = ["FreeSwitchMediaProvider", "MediaSocket"]

#: Mirrors `voiceagent.telephony.fakes.FakeMediaProvider.FORMATS`: the
#: realistic PSTN leg plus one wideband format.
_SUPPORTED_FORMATS = (
    AudioFormat(encoding="pcm_s16le", sample_rate=8000, channels=1),
    AudioFormat(encoding="pcm_s16le", sample_rate=16000, channels=1),
)


@runtime_checkable
class MediaSocket(Protocol):
    """One call leg's raw duplex byte/text socket, as `mod_audio_stream`
    actually speaks it -- binary frames inbound, JSON text frames outbound.
    Injected per attached call leg."""

    async def send_text(self, text: str) -> None: ...

    def receive_binary(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class _FreeSwitchMediaStream:
    """Adapts one `MediaSocket` to the product's `MediaStream` contract,
    translating the wire envelope exactly once per frame in each
    direction."""

    def __init__(self, socket: MediaSocket, fmt: AudioFormat) -> None:
        self._socket = socket
        self._format = fmt
        self._sent = 0
        self._received = 0
        self._closed = False

    @property
    def format(self) -> AudioFormat:
        return self._format

    @property
    def frames_sent(self) -> int:
        return self._sent

    @property
    def frames_received(self) -> int:
        return self._received

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise TransportError("stream is closed")
        envelope = {
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": self._format.sample_rate,
                "audioData": base64.b64encode(frame).decode("ascii"),
            },
        }
        await self._socket.send_text(json.dumps(envelope))
        self._sent += 1

    async def receive(self) -> AsyncIterator[bytes]:
        async for frame in self._socket.receive_binary():
            self._received += 1
            yield frame

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._socket.close()


class FreeSwitchMediaProvider:
    """`MediaProvider` over injected `MediaSocket`s, one per attached call
    leg. Issuing the ESL command that starts the stream
    (`uuid_audio_stream <uuid> start <wss-url> ...`) is
    `FreeSwitchTelephonyProvider`'s/the orchestrator's job, not this class's;
    this class owns the transport only once a socket exists for a call leg
    (`register_socket()`)."""

    def __init__(self) -> None:
        self._streams: dict[CallRef, _FreeSwitchMediaStream] = {}
        self._sockets: dict[CallRef, MediaSocket] = {}

    def register_socket(self, call_ref: CallRef, socket: MediaSocket) -> None:
        """Called once a call leg's media WebSocket has actually connected
        (Phase 2.0 report §5.1)."""
        self._sockets[call_ref] = socket

    def supported_formats(self) -> tuple[AudioFormat, ...]:
        return _SUPPORTED_FORMATS

    async def attach(
        self, call_ref: CallRef, fmt: AudioFormat | None = None
    ) -> _FreeSwitchMediaStream:
        started = time.monotonic()
        chosen = fmt if fmt is not None else _SUPPORTED_FORMATS[0]
        if chosen not in _SUPPORTED_FORMATS:
            record_provider_operation("media", "attach", "failure", time.monotonic() - started)
            raise UnsupportedFormatError(f"unsupported format: {chosen}")
        if call_ref in self._streams:
            record_provider_operation("media", "attach", "failure", time.monotonic() - started)
            raise TransportError(f"stream already attached: {call_ref}")
        socket = self._sockets.get(call_ref)
        if socket is None:
            record_provider_operation("media", "attach", "failure", time.monotonic() - started)
            raise TransportError(f"no media socket registered for {call_ref}")
        stream = _FreeSwitchMediaStream(socket, chosen)
        self._streams[call_ref] = stream
        record_provider_operation("media", "attach", "success", time.monotonic() - started)
        return stream

    async def detach(self, call_ref: CallRef) -> None:
        started = time.monotonic()
        stream = self._streams.pop(call_ref, None)
        if stream is None:
            record_provider_operation("media", "detach", "failure", time.monotonic() - started)
            raise TransportError(f"no attached stream: {call_ref}")
        await stream.close()
        self._sockets.pop(call_ref, None)
        record_provider_operation("media", "detach", "success", time.monotonic() - started)

    def health(self, call_ref: CallRef) -> StreamHealth:
        stream = self._streams.get(call_ref)
        if stream is None:
            return StreamHealth(attached=False, frames_sent=0, frames_received=0)
        return StreamHealth(
            attached=True,
            frames_sent=stream.frames_sent,
            frames_received=stream.frames_received,
        )
