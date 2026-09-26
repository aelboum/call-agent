"""`FreeSwitchTelephonyProvider` over `FakeEslConnection` (Phase 2.2 brief
section 9): command translation and lifecycle-event normalization, with no
running FreeSWITCH instance.
"""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony.contracts import (
    CallDirection,
    CallEvent,
    CallEventType,
    HangupCause,
    OriginateRequest,
    TransportError,
)
from voiceagent.telephony.freeswitch.esl import EslConnection
from voiceagent.telephony.freeswitch.fakes import FakeEslConnection
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider


def test_provider_satisfies_the_telephony_provider_contract() -> None:
    from voiceagent.telephony.contracts import TelephonyProvider

    provider = FreeSwitchTelephonyProvider(FakeEslConnection())
    assert isinstance(provider, TelephonyProvider)


def test_fake_esl_connection_satisfies_its_own_protocol() -> None:
    assert isinstance(FakeEslConnection(), EslConnection)


def test_answer_issues_the_expected_esl_command() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.answer("call-1"))
    assert esl.commands == ["uuid_answer call-1"]


def test_hangup_denormalizes_the_cause() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.hangup("call-1", HangupCause.BUSY))
    assert esl.commands == ["uuid_kill call-1 USER_BUSY"]


def test_hold_and_unhold() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.hold("call-1"))
    asyncio.run(provider.unhold("call-1"))
    assert esl.commands == ["uuid_hold call-1", "uuid_hold off call-1"]


def test_send_dtmf_and_recording_commands() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.send_dtmf("call-1", "123#"))
    asyncio.run(provider.start_recording("call-1"))
    asyncio.run(provider.stop_recording("call-1"))
    assert esl.commands == [
        "uuid_send_dtmf call-1 123#",
        "uuid_record call-1 start /dev/null",
        "uuid_record call-1 stop /dev/null",
    ]


def test_bridge() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.bridge("call-1", "call-2"))
    assert esl.commands == ["uuid_bridge call-1 call-2"]


def test_an_error_response_raises_transport_error() -> None:
    esl = FakeEslConnection()
    esl.responses["uuid_answer call-1"] = "-ERR no such channel"
    provider = FreeSwitchTelephonyProvider(esl)
    with pytest.raises(TransportError):
        asyncio.run(provider.answer("call-1"))


class _SlowEslConnection(FakeEslConnection):
    """A `FakeEslConnection` whose `send()` never returns -- simulates a
    wedged control connection (Phase 2.13 hardening, brief §10: "every
    external provider operation on the live call path must have an explicit
    timeout")."""

    async def send(self, command: str) -> str:
        self.commands.append(command)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


def test_a_wedged_command_times_out_as_a_transport_error() -> None:
    esl = _SlowEslConnection()
    provider = FreeSwitchTelephonyProvider(esl, command_timeout_seconds=0.05)
    with pytest.raises(TransportError):
        asyncio.run(provider.answer("call-1"))
    assert esl.commands == ["uuid_answer call-1"]


def test_command_timeout_defaults_do_not_affect_a_normal_response() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl, command_timeout_seconds=0.05)
    asyncio.run(provider.answer("call-1"))
    assert esl.commands == ["uuid_answer call-1"]


def test_events_are_normalized_and_unmapped_events_are_dropped() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)

    esl.push_event(
        {
            "Event-Name": "CHANNEL_PARK",
            "Unique-ID": "call-1",
            "Caller-Caller-ID-Number": "+15550100",
            "Caller-Destination-Number": "+15550199",
        }
    )
    esl.push_event({"Event-Name": "SOME_UNMAPPED_INTERNAL_EVENT", "Unique-ID": "call-1"})
    esl.push_event(
        {
            "Event-Name": "CHANNEL_HANGUP_COMPLETE",
            "Unique-ID": "call-1",
            "Hangup-Cause": "USER_BUSY",
        }
    )
    esl.close_event_stream()

    async def scenario() -> list[CallEvent]:
        return [event async for event in provider.events()]

    events = asyncio.run(scenario())
    assert len(events) == 2
    assert events[0].type is CallEventType.OFFERED
    assert events[0].direction is CallDirection.INBOUND
    assert events[0].from_number == "+15550100"
    assert events[0].to_number == "+15550199"
    assert events[1].type is CallEventType.HUNGUP
    assert events[1].hangup_cause is HangupCause.BUSY


def test_originate_mints_its_own_call_ref_and_never_parses_the_esl_reply() -> None:
    """Phase 2.21: the product mints `origination_uuid` itself and stamps it
    into the dial string -- `bgapi`'s own immediate reply (a Job-UUID, not a
    channel UUID) is only ever consulted for its `-ERR`/non-`-ERR` prefix by
    `_command()`, never parsed for a correlation id."""
    esl = FakeEslConnection()
    esl.responses[
        "bgapi originate {origination_uuid=call-abc,origination_caller_id_number=+15557654321}"  # noqa: E501
        "sofia/gateway/default/+15551234567"
    ] = "+OK Job-UUID: some-unrelated-job-id"
    provider = FreeSwitchTelephonyProvider(esl, uuid_factory=lambda: "call-abc")

    call_ref = asyncio.run(
        provider.originate(OriginateRequest(to_number="+15551234567", from_number="+15557654321"))
    )

    assert call_ref == "call-abc"


def test_originate_raises_on_a_rejected_dial_attempt() -> None:
    esl = FakeEslConnection()
    esl.responses[
        "bgapi originate {origination_uuid=call-abc,origination_caller_id_number=+15557654321}"  # noqa: E501
        "sofia/gateway/default/+15551234567"
    ] = "-ERR NORMAL_TEMPORARY_FAILURE"
    provider = FreeSwitchTelephonyProvider(esl, uuid_factory=lambda: "call-abc")

    with pytest.raises(TransportError):
        asyncio.run(
            provider.originate(
                OriginateRequest(to_number="+15551234567", from_number="+15557654321")
            )
        )


def test_transfer_mints_a_new_call_ref_for_the_second_leg() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl, uuid_factory=lambda: "leg-2")

    new_call_ref = asyncio.run(provider.transfer("call-1", "+15551234567"))

    assert new_call_ref == "leg-2"
    assert esl.commands == [
        "bgapi originate {origination_uuid=leg-2}sofia/gateway/default/+15551234567"
    ]


def test_start_media_stream_issues_the_expected_esl_command() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.start_media_stream("call-1", "wss://runtime.example.test/media/ticket-1"))
    assert esl.commands == [
        "uuid_audio_stream call-1 start wss://runtime.example.test/media/ticket-1 mono 8k"
    ]


def test_stop_media_stream_issues_the_expected_esl_command() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.stop_media_stream("call-1"))
    assert esl.commands == ["uuid_audio_stream call-1 stop"]
