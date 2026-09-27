"""Phase 2.26: `FreeSwitchTelephonyProvider.events()` reacting to
`mod_audio_stream::play` by issuing `uuid_broadcast` -- the missing half of
real outbound TTS playback (docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md).

`mod_audio_stream`'s own pinned build decodes a `streamAudio` payload,
writes it to a temp file, and fires this CUSTOM event naming that file, but
never plays it into the channel itself (this vendor's own commercial-vs-free
split, see the doc). This module -- not `_normalize_event()`, which never
sees this event, and never yields a `CallEvent` for it -- is the one place
with a live ESL connection to react and finish the job.
"""

from __future__ import annotations

import asyncio

from voiceagent.telephony.contracts import CallEvent
from voiceagent.telephony.freeswitch.fakes import FakeEslConnection
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider


def _play_event(call_ref: str, body: str) -> dict[str, str]:
    return {
        "Event-Name": "CUSTOM",
        "Event-Subclass": "mod_audio_stream::play",
        "Unique-ID": call_ref,
        "__body__": body,
    }


def _drain(provider: FreeSwitchTelephonyProvider) -> list[CallEvent]:
    async def scenario() -> list[CallEvent]:
        return [event async for event in provider.events()]

    return asyncio.run(scenario())


def test_a_play_event_issues_the_documented_broadcast_command() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(
        _play_event(
            "call-1",
            '{"audioDataType":"raw","sampleRate":8000,"file":"/tmp/call-1_0.tmp.r8"}',
        )
    )
    esl.close_event_stream()

    events = _drain(provider)

    assert events == []  # the play event is consumed internally, never yielded.
    assert esl.commands == ["api uuid_broadcast call-1 /tmp/call-1_0.tmp.r8 aleg"]


def test_a_play_event_with_no_file_field_issues_no_broadcast() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(_play_event("call-1", '{"audioDataType":"raw","sampleRate":8000}'))
    esl.close_event_stream()

    _drain(provider)

    assert esl.commands == []


def test_a_play_event_with_malformed_json_body_issues_no_broadcast_and_does_not_raise() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(_play_event("call-1", "not valid json {"))
    esl.close_event_stream()

    _drain(provider)  # must not raise.

    assert esl.commands == []


def test_a_play_event_with_no_call_ref_issues_no_broadcast() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(
        {
            "Event-Name": "CUSTOM",
            "Event-Subclass": "mod_audio_stream::play",
            "__body__": '{"file":"/tmp/x.tmp.r8"}',
        }
    )
    esl.close_event_stream()

    _drain(provider)

    assert esl.commands == []


def test_a_play_event_with_an_unsafe_file_path_issues_no_broadcast() -> None:
    """Defense in depth (this module's own `_E164_PATTERN`/`_DTMF_PATTERN`
    precedent): `mod_audio_stream` never actually generates a path shaped
    like this, but a value that could break `uuid_broadcast`'s own
    space-delimited ESL argument parsing must never be interpolated into
    the command string unchecked."""
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(_play_event("call-1", '{"file":"/tmp/evil path; api uuid_kill other-call"}'))
    esl.close_event_stream()

    _drain(provider)

    assert esl.commands == []


def test_a_failed_broadcast_is_swallowed_and_later_events_still_flow() -> None:
    """A caller who already hung up (or any other real ESL failure) while
    its own play event is in flight must never take down the whole event
    stream every other call on this connection depends on."""
    esl = FakeEslConnection()
    esl.responses["api uuid_broadcast call-1 /tmp/call-1_0.tmp.r8 aleg"] = "-ERR no such channel"
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(
        _play_event(
            "call-1",
            '{"audioDataType":"raw","sampleRate":8000,"file":"/tmp/call-1_0.tmp.r8"}',
        )
    )
    esl.push_event({"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Unique-ID": "call-2"})
    esl.close_event_stream()

    events = _drain(provider)  # must not raise despite the -ERR.

    assert len(events) == 1
    assert events[0].call_ref == "call-2"


def test_cross_call_play_events_broadcast_to_the_correct_call_ref_each() -> None:
    """Two calls' own play events, interleaved, must never have their
    `call_ref`/`file` pairing mixed up (cross-call isolation)."""
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(_play_event("call-1", '{"file":"/tmp/call-1_0.tmp.r8"}'))
    esl.push_event(_play_event("call-2", '{"file":"/tmp/call-2_0.tmp.r8"}'))
    esl.close_event_stream()

    _drain(provider)

    assert esl.commands == [
        "api uuid_broadcast call-1 /tmp/call-1_0.tmp.r8 aleg",
        "api uuid_broadcast call-2 /tmp/call-2_0.tmp.r8 aleg",
    ]


def test_other_mod_audio_stream_subclasses_are_dropped_like_any_unmapped_event() -> None:
    """Only `::play` is handled -- `::connect`/`::disconnect`/`::error`/
    `::json` fall through to `_normalize_event()`'s existing, unchanged
    drop-anything-unmapped behavior (`Event-Name` is `CUSTOM`, never a key
    in `_EVENT_TYPE_MAP`)."""
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    esl.push_event(
        {
            "Event-Name": "CUSTOM",
            "Event-Subclass": "mod_audio_stream::connect",
            "Unique-ID": "call-1",
        }
    )
    esl.close_event_stream()

    events = _drain(provider)

    assert events == []
    assert esl.commands == []
