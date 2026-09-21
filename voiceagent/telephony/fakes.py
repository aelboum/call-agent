"""In-memory telephony and media fakes.

They exist so the architecture can be tested before any transport exists, and
so Phase 2's call-orchestration tests never need FreeSWITCH. They are shipped
(not test-only) because the Phase 2 runtime tests will import them too.

These are fakes, not mocks: they hold real state and behave like a small,
honest telephony system -- an unknown `CallRef` raises, a closed stream
refuses sends, health reflects what actually happened. A test that passes
against these should fail against a broken adapter for the same reason.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator

from voiceagent.telephony.contracts import (
    AudioFormat,
    CallDirection,
    CallEvent,
    CallEventType,
    CallRef,
    HangupCause,
    OriginateRequest,
    StreamHealth,
    TelephonyError,
    TransportError,
    UnsupportedFormatError,
)

__all__ = ["FakeMediaProvider", "FakeMediaStream", "FakeTelephonyProvider", "UnknownCallError"]


class UnknownCallError(TelephonyError):
    """The referenced call leg is not known to this fake."""


class FakeTelephonyProvider:
    """A `TelephonyProvider` that records commands and replays scripted events."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.live_calls: set[CallRef] = set()
        self.recording: set[CallRef] = set()
        self.held: set[CallRef] = set()
        self._events: asyncio.Queue[CallEvent | None] = asyncio.Queue()
        self._ids = itertools.count(1)

    # -- test-side helpers -------------------------------------------------

    def emit(self, event: CallEvent) -> None:
        """Queue a lifecycle event for `events()` to yield."""
        self._events.put_nowait(event)

    def close_event_stream(self) -> None:
        """End `events()` -- the fake's stand-in for a control-plane shutdown."""
        self._events.put_nowait(None)

    def offer_inbound(self, *, from_number: str, to_number: str) -> CallRef:
        """Simulate an inbound call arriving and being offered."""
        call_ref: CallRef = f"fake-call-{next(self._ids)}"
        self.live_calls.add(call_ref)
        self.emit(
            CallEvent(
                type=CallEventType.OFFERED,
                call_ref=call_ref,
                direction=CallDirection.INBOUND,
                from_number=from_number,
                to_number=to_number,
            )
        )
        return call_ref

    def _require_live(self, call_ref: CallRef) -> None:
        if call_ref not in self.live_calls:
            raise UnknownCallError(call_ref)

    # -- TelephonyProvider -------------------------------------------------

    async def originate(self, request: OriginateRequest) -> CallRef:
        call_ref: CallRef = f"fake-call-{next(self._ids)}"
        self.commands.append(("originate", request.to_number, request.from_number))
        self.live_calls.add(call_ref)
        return call_ref

    async def answer(self, call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self.commands.append(("answer", call_ref))

    async def hangup(self, call_ref: CallRef, cause: HangupCause = HangupCause.NORMAL) -> None:
        self._require_live(call_ref)
        self.commands.append(("hangup", call_ref, cause.value))
        self.live_calls.discard(call_ref)
        self.recording.discard(call_ref)
        self.held.discard(call_ref)

    async def bridge(self, call_ref: CallRef, other_call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self._require_live(other_call_ref)
        self.commands.append(("bridge", call_ref, other_call_ref))

    async def transfer(self, call_ref: CallRef, destination: str) -> CallRef:
        self._require_live(call_ref)
        destination_ref: CallRef = f"fake-call-{next(self._ids)}"
        self.commands.append(("transfer", call_ref, destination))
        self.live_calls.add(destination_ref)
        return destination_ref

    async def hold(self, call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self.commands.append(("hold", call_ref))
        self.held.add(call_ref)

    async def unhold(self, call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self.commands.append(("unhold", call_ref))
        self.held.discard(call_ref)

    async def send_dtmf(self, call_ref: CallRef, digits: str) -> None:
        self._require_live(call_ref)
        self.commands.append(("send_dtmf", call_ref, digits))

    async def start_recording(self, call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self.commands.append(("start_recording", call_ref))
        self.recording.add(call_ref)

    async def stop_recording(self, call_ref: CallRef) -> None:
        self._require_live(call_ref)
        self.commands.append(("stop_recording", call_ref))
        self.recording.discard(call_ref)

    async def events(self) -> AsyncIterator[CallEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event


class FakeMediaStream:
    """An in-memory `MediaStream`. Frames sent by the product land in
    `sent_frames`; frames pushed with `push()` are yielded by `receive()`."""

    def __init__(self, fmt: AudioFormat) -> None:
        self._format = fmt
        self.sent_frames: list[bytes] = []
        self.closed = False
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._received = 0

    @property
    def format(self) -> AudioFormat:
        return self._format

    @property
    def frames_received(self) -> int:
        return self._received

    def push(self, frame: bytes) -> None:
        """Simulate inbound audio from the far end."""
        self._inbound.put_nowait(frame)

    def end_inbound(self) -> None:
        self._inbound.put_nowait(None)

    async def send(self, frame: bytes) -> None:
        if self.closed:
            raise TransportError("stream is closed")
        self.sent_frames.append(frame)

    async def receive(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._inbound.get()
            if frame is None:
                return
            self._received += 1
            yield frame

    async def close(self) -> None:
        self.closed = True
        self.end_inbound()


class FakeMediaProvider:
    """A `MediaProvider` over `FakeMediaStream`."""

    #: Mirrors the realistic PSTN leg plus one wideband format, so format
    #: negotiation is actually exercised rather than always trivially true.
    FORMATS = (
        AudioFormat(encoding="pcm_s16le", sample_rate=8000, channels=1),
        AudioFormat(encoding="pcm_s16le", sample_rate=16000, channels=1),
    )

    def __init__(self) -> None:
        self.streams: dict[CallRef, FakeMediaStream] = {}

    def supported_formats(self) -> tuple[AudioFormat, ...]:
        return self.FORMATS

    async def attach(self, call_ref: CallRef, fmt: AudioFormat | None = None) -> FakeMediaStream:
        chosen = fmt if fmt is not None else self.FORMATS[0]
        if chosen not in self.FORMATS:
            raise UnsupportedFormatError(f"unsupported format: {chosen}")
        if call_ref in self.streams:
            raise TransportError(f"stream already attached: {call_ref}")
        stream = FakeMediaStream(chosen)
        self.streams[call_ref] = stream
        return stream

    async def detach(self, call_ref: CallRef) -> None:
        stream = self.streams.pop(call_ref, None)
        if stream is None:
            raise UnknownCallError(call_ref)
        await stream.close()

    def health(self, call_ref: CallRef) -> StreamHealth:
        stream = self.streams.get(call_ref)
        if stream is None:
            return StreamHealth(attached=False, frames_sent=0, frames_received=0)
        return StreamHealth(
            attached=not stream.closed,
            frames_sent=len(stream.sent_frames),
            frames_received=stream.frames_received,
        )
