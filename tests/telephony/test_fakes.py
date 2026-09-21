"""The telephony and media fakes (Phase 1 brief sections 8 and 12).

These tests prove the contracts are implementable and that the fakes behave
like an honest small telephony system -- an unknown call raises, a closed
stream refuses sends, health reflects what happened. A fake that accepted
anything would make every Phase 2 test that depends on it worthless.

`asyncio.run()` is used rather than an async test plugin: the foundation adds
no test dependency it does not need.
"""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony import (
    AudioFormat,
    CallEventType,
    HangupCause,
    MediaProvider,
    OriginateRequest,
    TelephonyProvider,
    TransportError,
    UnsupportedFormatError,
)
from voiceagent.telephony.fakes import (
    FakeMediaProvider,
    FakeTelephonyProvider,
    UnknownCallError,
)


def test_fakes_satisfy_the_contracts() -> None:
    """Structural conformance, checked at runtime so a contract change that
    the fakes no longer satisfy fails here rather than in Phase 2."""
    assert isinstance(FakeTelephonyProvider(), TelephonyProvider)
    assert isinstance(FakeMediaProvider(), MediaProvider)


def test_call_control_commands_are_recorded() -> None:
    provider = FakeTelephonyProvider()

    async def scenario() -> None:
        call = await provider.originate(
            OriginateRequest(to_number="+15550100", from_number="+15550199")
        )
        await provider.answer(call)
        await provider.start_recording(call)
        await provider.hold(call)
        await provider.unhold(call)
        await provider.send_dtmf(call, "12")
        await provider.stop_recording(call)
        await provider.hangup(call, HangupCause.NORMAL)

    asyncio.run(scenario())

    assert [command[0] for command in provider.commands] == [
        "originate",
        "answer",
        "start_recording",
        "hold",
        "unhold",
        "send_dtmf",
        "stop_recording",
        "hangup",
    ]
    assert provider.live_calls == set()
    assert provider.recording == set()


def test_commands_against_an_unknown_call_are_refused() -> None:
    provider = FakeTelephonyProvider()
    with pytest.raises(UnknownCallError):
        asyncio.run(provider.answer("no-such-call"))


def test_inbound_offer_is_the_resolution_point() -> None:
    """The product resolves the called number to a tenant and agent version
    on `OFFERED`, server-side. The numbers on the event are inputs to that
    resolution and carry no authority: caller ID is spoofable."""
    provider = FakeTelephonyProvider()

    async def scenario() -> list[object]:
        provider.offer_inbound(from_number="+15550100", to_number="+15550111")
        provider.close_event_stream()
        return [event async for event in provider.events()]

    events = asyncio.run(scenario())
    assert len(events) == 1
    event = events[0]
    assert event.type is CallEventType.OFFERED  # type: ignore[attr-defined]
    assert event.to_number == "+15550111"  # type: ignore[attr-defined]


def test_transfer_creates_a_second_leg() -> None:
    provider = FakeTelephonyProvider()

    async def scenario() -> tuple[str, str]:
        call = provider.offer_inbound(from_number="+15550100", to_number="+15550111")
        destination = await provider.transfer(call, "+15550122")
        await provider.bridge(call, destination)
        return call, destination

    call, destination = asyncio.run(scenario())
    assert call != destination
    assert ("bridge", call, destination) in provider.commands


def test_media_attach_send_receive_and_detach() -> None:
    media = FakeMediaProvider()

    async def scenario() -> tuple[list[bytes], list[bytes]]:
        stream = await media.attach("call-1")
        stream.push(b"inbound-frame")
        stream.end_inbound()
        received = [frame async for frame in stream.receive()]
        await stream.send(b"outbound-frame")
        return received, stream.sent_frames

    received, sent = asyncio.run(scenario())
    assert received == [b"inbound-frame"]
    assert sent == [b"outbound-frame"]

    health = media.health("call-1")
    assert health.attached is True
    assert health.frames_sent == 1
    assert health.frames_received == 1


def test_format_negotiation_rejects_an_unsupported_format() -> None:
    media = FakeMediaProvider()
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(media.attach("call-1", AudioFormat(encoding="opus", sample_rate=48000)))


def test_double_attach_is_refused() -> None:
    media = FakeMediaProvider()

    async def scenario() -> None:
        await media.attach("call-1")
        await media.attach("call-1")

    with pytest.raises(TransportError):
        asyncio.run(scenario())


def test_detached_stream_is_closed_and_refuses_sends() -> None:
    """A media stream can die while the call lives -- the reason control and
    media are separate contracts (ADR-0002 amendment)."""
    media = FakeMediaProvider()

    async def scenario() -> None:
        stream = await media.attach("call-1")
        await media.detach("call-1")
        assert stream.closed is True
        await stream.send(b"too-late")

    with pytest.raises(TransportError):
        asyncio.run(scenario())

    assert media.health("call-1").attached is False


def test_detaching_an_unknown_call_raises() -> None:
    media = FakeMediaProvider()
    with pytest.raises(UnknownCallError):
        asyncio.run(media.detach("no-such-call"))
