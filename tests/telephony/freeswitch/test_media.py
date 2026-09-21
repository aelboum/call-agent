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
from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider


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
    provider = FreeSwitchMediaProvider()
    socket = FakeMediaSocket()
    provider.register_socket("call-1", socket)

    async def scenario() -> None:
        stream = await provider.attach("call-1")
        await stream.send(b"\x01\x02\x03\x04")

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


def test_health_before_attach_reports_unattached() -> None:
    provider = FreeSwitchMediaProvider()
    health = provider.health("never-attached")
    assert health == provider.health("never-attached")
    assert health.attached is False
    assert health.frames_sent == 0
    assert health.frames_received == 0
