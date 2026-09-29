"""`FreeSwitchMediaProvider` over `FakeMediaSocket` (Phase 2.2 brief section
10): `mod_audio_stream`'s documented wire envelope, attach/detach, format
negotiation, health -- with no running FreeSWITCH instance.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from voiceagent.telephony.contracts import (
    AudioFormat,
    MediaProvider,
    TransportError,
    UnsupportedFormatError,
)
from voiceagent.telephony.freeswitch.fakes import FakeMediaSocket
from voiceagent.telephony.freeswitch.media import (
    _LATENCY_MARGIN_SECONDS,
    _MIN_CHUNK_SECONDS,
    FreeSwitchMediaProvider,
)


def test_provider_satisfies_the_media_provider_contract() -> None:
    assert isinstance(FreeSwitchMediaProvider(), MediaProvider)


def test_attach_requires_a_registered_socket() -> None:
    provider = FreeSwitchMediaProvider()
    with pytest.raises(TransportError):
        asyncio.run(provider.attach("call-1"))


def test_attach_rejects_an_unsupported_format() -> None:
    provider = FreeSwitchMediaProvider()
    provider.register_socket("call-1", FakeMediaSocket())
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(provider.attach("call-1", AudioFormat(sample_rate=44100)))


def test_double_attach_is_rejected() -> None:
    provider = FreeSwitchMediaProvider()
    provider.register_socket("call-1", FakeMediaSocket())

    async def scenario() -> None:
        await provider.attach("call-1")
        with pytest.raises(TransportError):
            await provider.attach("call-1")

    asyncio.run(scenario())


def test_send_wraps_the_documented_streamaudio_envelope() -> None:
    """`close()` flushes: Phase 2.26's own coalescing buffer
    (`_MIN_CHUNK_SECONDS`) does not wire out a frame this small (4 bytes,
    far under the threshold) until either enough audio has accumulated or
    the stream closes -- see `test_send_buffers_small_frames_and_flushes_
    on_close` for that behavior on its own; this test only needs `close()`
    to observe the envelope shape at all."""
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x01\x02\x03\x04")
        await stream.close()

    asyncio.run(scenario())

    assert len(socket.sent_text) == 1
    envelope = json.loads(socket.sent_text[0])
    assert envelope["type"] == "streamAudio"
    assert envelope["data"]["audioDataType"] == "raw"
    assert envelope["data"]["sampleRate"] == 8000
    assert base64.b64decode(envelope["data"]["audioData"]) == b"\x01\x02\x03\x04"


def test_receive_yields_raw_binary_frames_unwrapped() -> None:
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)
    socket.push_binary(b"raw-l16-frame")
    socket.end_inbound()

    async def scenario() -> list[bytes]:
        stream = await provider.attach("call-1")
        return [frame async for frame in stream.receive()]

    assert asyncio.run(scenario()) == [b"raw-l16-frame"]


def test_detach_closes_the_socket_and_updates_health() -> None:
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        await provider.attach("call-1")
        await provider.detach("call-1")

    asyncio.run(scenario())
    assert socket.closed
    health = provider.health("call-1")
    assert health.attached is False


def test_detach_unknown_call_raises() -> None:
    provider = FreeSwitchMediaProvider()
    with pytest.raises(TransportError):
        asyncio.run(provider.detach("nonexistent"))


def test_no_cross_call_frame_leakage() -> None:
    """Phase 2.24 brief section 5: "no cross-call frame leakage"/"no
    cross-tenant frame leakage" -- this layer has no concept of tenant at
    all (by design: it only ever sees `call_ref` strings), so the only
    thing that could leak frames between tenants is frames leaking between
    *calls* at all. Two independently registered sockets for two different
    `call_ref`s must never cross-deliver: each attached stream yields only
    its own socket's own pushed frames."""
    provider = FreeSwitchMediaProvider()
    socket_a = FakeMediaSocket()
    socket_b = FakeMediaSocket()
    provider.register_socket("call-a", socket_a)
    provider.register_socket("call-b", socket_b)
    socket_a.push_binary(b"frame-for-a")
    socket_a.end_inbound()
    socket_b.push_binary(b"frame-for-b")
    socket_b.end_inbound()

    async def scenario() -> tuple[list[bytes], list[bytes]]:
        stream_a = await provider.attach("call-a")
        stream_b = await provider.attach("call-b")
        frames_a = [frame async for frame in stream_a.receive()]
        frames_b = [frame async for frame in stream_b.receive()]
        return frames_a, frames_b

    frames_a, frames_b = asyncio.run(scenario())
    assert frames_a == [b"frame-for-a"]
    assert frames_b == [b"frame-for-b"]


def test_sending_on_one_call_never_reaches_another_calls_socket() -> None:
    """The outbound half of the same isolation property: `stream.send()`
    for one call must only ever write to that call's own registered
    socket."""
    provider = FreeSwitchMediaProvider()
    socket_a = FakeMediaSocket()
    socket_b = FakeMediaSocket()
    provider.register_socket("call-a", socket_a)
    provider.register_socket("call-b", socket_b)

    async def scenario() -> None:
        stream_a = await provider.attach("call-a")
        await stream_a.send(b"only-for-a")
        await stream_a.close()  # flush Phase 2.26's own coalescing buffer.

    asyncio.run(scenario())
    assert len(socket_a.sent_text) == 1
    assert socket_b.sent_text == []


def test_truly_concurrent_sends_on_two_calls_never_share_pacing_or_buffer_state() -> None:
    """Phase 2.27: proves per-call isolation under *actual* concurrent
    scheduling (`asyncio.gather`, both streams' own `send()` coroutines
    genuinely interleaved on the same event loop), not merely two
    sequential calls that happen never to touch each other. Each
    `_FreeSwitchMediaStream` owns its own `_buffer`/`_playback_started_at`/
    `_audio_seconds_sent` -- if pacing or coalescing state were ever
    accidentally shared (a module-level variable, a class-level mutable
    default), interleaving these two calls' own sends would corrupt each
    other's chunk boundaries or scheduling; this test would then fail on
    either call's own envelope content, not just on cross-socket delivery
    (already covered by `test_sending_on_one_call_never_reaches_another
    _calls_socket`)."""
    provider = FreeSwitchMediaProvider()
    socket_a = FakeMediaSocket()
    socket_b = FakeMediaSocket()
    provider.register_socket("call-a", socket_a)
    provider.register_socket("call-b", socket_b)

    async def _drive(call_ref: str, marker: bytes) -> None:
        stream = await provider.attach(call_ref)
        for _ in range(5):
            await stream.send(marker * 1600)  # each send is below the coalescing threshold.
            await asyncio.sleep(0)  # yield, so the other call's own sends can interleave.
        await stream.close()

    async def scenario() -> None:
        await asyncio.gather(_drive("call-a", b"\xaa"), _drive("call-b", b"\xbb"))

    asyncio.run(scenario())

    def _concatenated(socket: FakeMediaSocket) -> bytes:
        out = bytearray()
        for text in socket.sent_text:
            out.extend(base64.b64decode(json.loads(text)["data"]["audioData"]))
        return bytes(out)

    audio_a = _concatenated(socket_a)
    audio_b = _concatenated(socket_b)
    assert audio_a == b"\xaa" * 8000
    assert audio_b == b"\xbb" * 8000
    assert b"\xbb" not in audio_a
    assert b"\xaa" not in audio_b


def test_closing_one_call_during_concurrent_playback_never_disturbs_another() -> None:
    """Phase 2.27 brief section 9: "media disconnect during playback ...
    other simultaneous calls continue unaffected" -- exercised hermetically
    here (real-FreeSWITCH validation is section 9's own real-call
    counterpart, docs/PHASE-2.27-CONCURRENT-MEDIA-VALIDATION.md). Call A
    closing mid-stream (a real caller hangup's own effect) must never
    raise, and must never affect call B's own independent stream."""
    provider = FreeSwitchMediaProvider()
    socket_a = FakeMediaSocket()
    socket_b = FakeMediaSocket()
    provider.register_socket("call-a", socket_a)
    provider.register_socket("call-b", socket_b)

    disturbed: dict[str, bool | None] = {"b_closed_after_a_detach": None}

    async def scenario() -> None:
        stream_a = await provider.attach("call-a")
        stream_b = await provider.attach("call-b")
        await stream_a.send(b"\xaa" * 8000)
        await stream_b.send(b"\xbb" * 2000)  # 2000 < 3200 threshold -- stays buffered.
        await provider.detach("call-a")  # simulates call A's own real hangup.
        disturbed["b_closed_after_a_detach"] = socket_b.closed
        await stream_b.send(b"\xbb" * 6000)  # call B carries on unaffected.
        await stream_b.close()

    asyncio.run(scenario())

    assert socket_a.closed
    assert disturbed["b_closed_after_a_detach"] is False  # never touched by call A's own detach.
    assert len(socket_a.sent_text) == 1
    assert len(socket_b.sent_text) == 1
    envelope_b = json.loads(socket_b.sent_text[0])
    assert base64.b64decode(envelope_b["data"]["audioData"]) == b"\xbb" * 8000
    health_a = provider.health("call-a")
    assert health_a.attached is False


def test_health_before_attach_reports_unattached() -> None:
    provider = FreeSwitchMediaProvider()
    health = provider.health("never-attached")
    assert health == provider.health("never-attached")
    assert health.attached is False
    assert health.frames_sent == 0
    assert health.frames_received == 0


class _FakeClock:
    """A deterministic, manually-advanced clock/sleep pair for asserting
    `_FreeSwitchMediaStream`'s own pacing math with no real `asyncio.sleep`
    (Phase 2.26)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_send_paces_the_second_frame_to_the_first_frames_real_duration() -> None:
    """Phase 2.26 regression: `mod_audio_stream`'s own pinned build turns
    every `streamAudio` envelope into its own immediate `uuid_broadcast`
    (`voiceagent.telephony.freeswitch.provider._handle_play_event()`),
    which interrupts whatever the channel is already playing -- found
    empirically against a real SIP call (docs/PHASE-2.26-REAL-SIP-TTS-
    PLAYBACK.md): dozens of unpaced frames arrived at mod_audio_stream
    within milliseconds of each other, each broadcast cutting off the last,
    producing loud but unintelligible return audio. 8000 Hz, 16-bit, 1
    channel: one second of audio is 16000 bytes, so an 8000-byte frame is
    exactly 0.5 real seconds, plus this stream's own fixed per-frame latency
    margin (`_LATENCY_MARGIN_SECONDS`, absorbing the real ESL round trip
    `_handle_play_event()`'s own broadcast needs) -- the second `send()`
    call must not be allowed to proceed until that much time has passed
    since the first."""
    clock = _FakeClock()
    provider = FreeSwitchMediaProvider(clock=clock.clock, sleep=clock.sleep)
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * 8000)  # 0.5s of 8kHz mono 16-bit PCM.
        await stream.send(b"\x00" * 8000)

    asyncio.run(scenario())

    assert clock.sleeps == [0.5 + _LATENCY_MARGIN_SECONDS]


def test_send_never_sleeps_when_the_caller_is_already_behind_real_time() -> None:
    """If frames arrive slower than real-time playback (e.g. a slow AI
    pipeline), pacing must never add an *extra* artificial delay on top --
    only ever prevent sending *ahead* of real time, never behind it."""
    clock = _FakeClock()
    provider = FreeSwitchMediaProvider(clock=clock.clock, sleep=clock.sleep)
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * 8000)  # 0.5s of audio.
        clock.now += 5.0  # far more real time than the frame's own duration.
        await stream.send(b"\x00" * 8000)

    asyncio.run(scenario())

    assert clock.sleeps == []
    assert len(socket.sent_text) == 2


def test_pace_records_the_scheduling_delay_when_a_wait_was_needed(monkeypatch) -> None:
    """Phase 2.27: `voiceagent.metrics.record_media_playback_scheduling_delay()`
    -- the "playback scheduling delay" signal this phase's own concurrency
    investigation needed and did not have from logs alone
    (docs/PHASE-2.27-CONCURRENT-MEDIA-VALIDATION.md)."""
    delays: list[float] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.media.record_media_playback_scheduling_delay",
        delays.append,
    )
    clock = _FakeClock()
    provider = FreeSwitchMediaProvider(clock=clock.clock, sleep=clock.sleep)
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * 8000)  # 0.5s of audio -- no predecessor, no delay recorded.
        await stream.send(b"\x00" * 8000)  # must wait for the first frame's own duration + margin.

    asyncio.run(scenario())

    assert delays == [0.5 + _LATENCY_MARGIN_SECONDS]


def test_pace_records_zero_delay_when_already_behind_real_time(monkeypatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.media.record_media_playback_scheduling_delay",
        delays.append,
    )
    clock = _FakeClock()
    provider = FreeSwitchMediaProvider(clock=clock.clock, sleep=clock.sleep)
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * 8000)
        clock.now += 5.0
        await stream.send(b"\x00" * 8000)

    asyncio.run(scenario())

    assert delays == [0.0]


def test_send_buffers_small_frames_until_close_flushes_the_remainder() -> None:
    """Phase 2.26 regression: real TTS output arrives as many small chunks
    (`voiceagent.providers.tts.deepgram_aura`'s own raw HTTP stream
    chunks) -- each individually far under `_MIN_CHUNK_SECONDS`. Sending
    one `uuid_broadcast` per tiny chunk multiplied real ESL round-trip
    overhead across dozens of chunks for one reply and made a real SIP
    call's return audio unintelligible (docs/PHASE-2.26-REAL-SIP-TTS-
    PLAYBACK.md) -- fixed by buffering until enough audio has accumulated.
    No caller-provided byte may ever be silently dropped: whatever remains
    buffered below the threshold when the stream closes must still reach
    the wire, intact and in order."""
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x01" * 100)
        await stream.send(b"\x02" * 100)
        assert socket.sent_text == []  # still well under _MIN_CHUNK_SECONDS.
        await stream.close()

    asyncio.run(scenario())

    assert len(socket.sent_text) == 1
    envelope = json.loads(socket.sent_text[0])
    assert base64.b64decode(envelope["data"]["audioData"]) == b"\x01" * 100 + b"\x02" * 100


def test_send_flushes_automatically_once_the_threshold_is_crossed() -> None:
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)
    threshold_bytes = int(_MIN_CHUNK_SECONDS * 8000 * 1 * 2)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * (threshold_bytes - 1))
        assert socket.sent_text == []
        await stream.send(b"\x01")  # crosses the threshold by exactly one byte.
        assert len(socket.sent_text) == 1
        await stream.send(b"\x02" * 10)  # starts a fresh buffer, not yet flushed.
        assert len(socket.sent_text) == 1
        await stream.close()

    asyncio.run(scenario())

    assert len(socket.sent_text) == 2
    first = json.loads(socket.sent_text[0])
    second = json.loads(socket.sent_text[1])
    assert base64.b64decode(first["data"]["audioData"]) == b"\x00" * (threshold_bytes - 1) + b"\x01"
    assert base64.b64decode(second["data"]["audioData"]) == b"\x02" * 10


class _AlreadyClosedMediaSocket(FakeMediaSocket):
    """A `MediaSocket` whose remote end already tore down the connection --
    `send_text()` raises, the same as a real `WebSocketMediaSocket` does
    against an already-closed real WebSocket (which raises `TransportError`,
    Phase 2.29 fix). A plain `ConnectionError` here is deliberate: this test
    exercises `close()`'s own `contextlib.suppress(Exception)`, which must
    swallow *any* exception from a dying flush, not specifically
    `TransportError` -- using a different exception type proves the suppress
    is not narrower than that."""

    async def send_text(self, text: str) -> None:
        raise ConnectionError("connection already closed")


def test_close_swallows_a_flush_failure_from_an_already_closed_socket() -> None:
    """Phase 2.26 regression: found on a real SIP call -- the remote end
    (`mod_audio_stream`) tearing down its own WebSocket as part of a real
    caller hangup, before this call's own `detach()`/`close()` ever runs,
    is an ordinary race on the live call path, not a bug. Before this fix,
    `close()`'s own new best-effort flush of a buffered tail turned that
    ordinary race into an unhandled exception straight out of
    `voiceagent.runtime.call_task.run_call_task()`'s own `media.detach()`
    call -- this must never happen: there is no live channel left for that
    last, already-decided tail of audio to reach anyway."""
    provider = FreeSwitchMediaProvider()
    socket = _AlreadyClosedMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x01" * 100)  # buffered, under the threshold.
        await stream.close()  # must not raise.

    asyncio.run(scenario())  # would raise ConnectionError before this fix.

    assert socket.closed


def test_send_does_not_pace_the_very_first_frame() -> None:
    clock = _FakeClock()
    provider = FreeSwitchMediaProvider(clock=clock.clock, sleep=clock.sleep)
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x00" * 8000)

    asyncio.run(scenario())

    assert clock.sleeps == []
    assert len(socket.sent_text) == 1
