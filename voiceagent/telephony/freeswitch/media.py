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

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from voiceagent.metrics import (
    record_media_playback_scheduling_delay,
    record_media_session_duration,
    record_media_session_ended,
    record_media_session_started,
    record_provider_operation,
)
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

#: Phase 2.26: the minimum amount of audio `_FreeSwitchMediaStream.send()`
#: accumulates before actually wiring a `streamAudio` envelope out
#: (`_flush()`). `AudioOut` frames (`voiceagent.runtime.call_task
#: .pump_engine_events()`) arrive at whatever granularity the TTS
#: provider's own HTTP stream happens to deliver them
#: (`voiceagent.providers.tts.deepgram_aura`: raw `response.aiter_bytes()`
#: chunks, empirically as small as ~20-50ms each) -- sending one
#: `uuid_broadcast` per tiny chunk (`_handle_play_event()`) multiplies the
#: real per-broadcast ESL round trip (`_LATENCY_MARGIN_SECONDS` exists
#: because of it) across dozens of chunks for one reply, which
#: `_LATENCY_MARGIN_SECONDS` alone could not fully absorb -- found
#: empirically against a real SIP call (docs/PHASE-2.26-REAL-SIP-TTS-
#: PLAYBACK.md). Coalescing into fewer, larger chunks before pacing/sending
#: cuts the number of broadcasts (and so the total accumulated round-trip
#: overhead) by roughly this factor over the provider's own raw chunk size,
#: with no change to the bytes actually played, only to how many separate
#: `uuid_broadcast` calls deliver them.
_MIN_CHUNK_SECONDS = 0.2

#: Phase 2.26: `_FreeSwitchMediaStream._pace()`'s own per-frame safety
#: margin, added on top of each frame's real playback duration. Absorbs the
#: real, observed, non-zero round trip a paced send's *next* frame's own
#: `uuid_broadcast` still has to clear before this one's predecessor
#: finishes playing -- FreeSWITCH delivering the `mod_audio_stream::play`
#: CUSTOM event over ESL, this process parsing it, and issuing the
#: broadcast command back over the same ESL connection
#: (`voiceagent.telephony.freeswitch.provider._handle_play_event()`) all
#: take real, measurable time no pure audio-duration calculation accounts
#: for. Found empirically: pacing on audio duration alone (no margin)
#: measurably improved a real SIP call's return-audio intelligibility over
#: no pacing at all, but did not make it fully clean --
#: docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md documents the exact before/after
#: transcripts this margin was tuned against.
_LATENCY_MARGIN_SECONDS = 0.03


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

    def __init__(
        self,
        socket: MediaSocket,
        fmt: AudioFormat,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._socket = socket
        self._format = fmt
        self._sent = 0
        self._received = 0
        self._closed = False
        self._clock = clock
        self._sleep = sleep
        self._playback_started_at: float | None = None
        self._audio_seconds_sent = 0.0
        self._buffer = bytearray()

    @property
    def format(self) -> AudioFormat:
        return self._format

    @property
    def frames_sent(self) -> int:
        return self._sent

    @property
    def frames_received(self) -> int:
        return self._received

    async def _pace(self, frame: bytes) -> None:
        """Phase 2.26: never send a frame before its predecessor's own real
        playback duration has elapsed.

        `mod_audio_stream`'s own pinned build never queues multiple
        playback requests itself -- every `streamAudio` envelope this
        stream sends becomes its own immediate, independent
        `uuid_broadcast` (`voiceagent.telephony.freeswitch.provider
        ._handle_play_event()`), which interrupts whatever that channel leg
        is already playing rather than queuing behind it. The engine loop
        above this transport (`voiceagent.runtime.call_task
        .pump_engine_events()`) sends every `AudioOut` frame the instant
        the engine produces it, with no pacing of its own -- correctly so,
        it is transport-agnostic (brief section 18's boundary) and must not
        know FreeSWITCH's own playback semantics. For one real streaming
        TTS reply broken into many small frames, unpaced sending is a burst
        of dozens of overlapping broadcasts within milliseconds, each
        cutting off the last -- found empirically: a real SIP call's own
        captured return audio was substantial in volume but unintelligible
        until this fix (docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md). Pacing
        entirely within this FreeSWITCH-specific transport keeps that
        engine loop unchanged; `clock`/`sleep` are injected purely so a
        hermetic test can assert the pacing math without a real sleep."""
        bytes_per_sample = 2  # pcm_s16le -- this stream's only encoding.
        duration_seconds = (
            len(frame) / bytes_per_sample / self._format.channels / self._format.sample_rate
        )
        now = self._clock()
        if self._playback_started_at is None:
            self._playback_started_at = now
        else:
            scheduled_at = self._playback_started_at + self._audio_seconds_sent
            delay_seconds = scheduled_at - now
            if delay_seconds > 0:
                await self._sleep(delay_seconds)
            record_media_playback_scheduling_delay(max(delay_seconds, 0.0))
        self._audio_seconds_sent += duration_seconds + _LATENCY_MARGIN_SECONDS

    async def _flush(self, chunk: bytes) -> None:
        await self._pace(chunk)
        envelope = {
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": self._format.sample_rate,
                "audioData": base64.b64encode(chunk).decode("ascii"),
            },
        }
        await self._socket.send_text(json.dumps(envelope))
        self._sent += 1

    async def send(self, frame: bytes) -> None:
        """Buffers `frame` and wires out an accumulated chunk (`_flush()`)
        only once at least `_MIN_CHUNK_SECONDS` of audio is buffered --
        see `_MIN_CHUNK_SECONDS`'s own docstring for why coalescing, not
        just pacing, is necessary. `send()` returning does not mean `frame`
        has reached the wire yet; `close()` flushes whatever remains
        buffered, so no caller-provided audio is ever silently dropped."""
        if self._closed:
            raise TransportError("stream is closed")
        self._buffer.extend(frame)
        bytes_per_sample = 2  # pcm_s16le -- this stream's only encoding.
        threshold_bytes = int(
            _MIN_CHUNK_SECONDS * self._format.sample_rate * self._format.channels * bytes_per_sample
        )
        if len(self._buffer) >= threshold_bytes:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            await self._flush(chunk)

    async def receive(self) -> AsyncIterator[bytes]:
        async for frame in self._socket.receive_binary():
            self._received += 1
            yield frame

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._buffer:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            # Best-effort: the remote end (mod_audio_stream) tearing down
            # its own WebSocket as part of a real caller hangup, *before*
            # this call's own detach() ever runs, is an ordinary, expected
            # race on the live call path, not a bug -- found immediately
            # once this close-time flush existed, on a real SIP call
            # (docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md).
            # `WebSocketMediaSocket.send_text()` propagates a closed
            # connection unchanged (`voiceagent.telephony.freeswitch
            # .media_transport`'s own docstring: only `receive_binary()`
            # converts it to a clean end), so this is the one place that
            # must not let it turn a normal hangup's own cleanup into an
            # unhandled exception. Losing this last, sub-`_MIN_CHUNK_SECONDS`
            # tail of already-decided audio to a connection that is already
            # gone is an acceptable, bounded loss -- there is no live
            # channel left for it to reach anyway.
            with contextlib.suppress(Exception):
                await self._flush(chunk)
        await self._socket.close()


class FreeSwitchMediaProvider:
    """`MediaProvider` over injected `MediaSocket`s, one per attached call
    leg. Issuing the ESL command that starts the stream
    (`uuid_audio_stream <uuid> start <wss-url> ...`) is
    `FreeSwitchTelephonyProvider`'s/the orchestrator's job, not this class's;
    this class owns the transport only once a socket exists for a call leg
    (`register_socket()`)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._streams: dict[CallRef, _FreeSwitchMediaStream] = {}
        self._sockets: dict[CallRef, MediaSocket] = {}
        self._attached_at: dict[CallRef, float] = {}
        self._clock = clock
        self._sleep = sleep

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
        stream = _FreeSwitchMediaStream(socket, chosen, clock=self._clock, sleep=self._sleep)
        self._streams[call_ref] = stream
        self._attached_at[call_ref] = time.monotonic()
        record_provider_operation("media", "attach", "success", time.monotonic() - started)
        record_media_session_started()
        return stream

    async def detach(self, call_ref: CallRef) -> None:
        started = time.monotonic()
        stream = self._streams.pop(call_ref, None)
        if stream is None:
            record_provider_operation("media", "detach", "failure", time.monotonic() - started)
            raise TransportError(f"no attached stream: {call_ref}")
        await stream.close()
        self._sockets.pop(call_ref, None)
        attached_at = self._attached_at.pop(call_ref, None)
        if attached_at is not None:
            record_media_session_duration(time.monotonic() - attached_at)
        record_provider_operation("media", "detach", "success", time.monotonic() - started)
        record_media_session_ended()

    def health(self, call_ref: CallRef) -> StreamHealth:
        stream = self._streams.get(call_ref)
        if stream is None:
            return StreamHealth(attached=False, frames_sent=0, frames_received=0)
        return StreamHealth(
            attached=True,
            frames_sent=stream.frames_sent,
            frames_received=stream.frames_received,
        )
