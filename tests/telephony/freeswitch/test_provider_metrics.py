"""`FreeSwitchTelephonyProvider`'s Phase 2.14 metrics recording:
`_command()` records exactly one `voiceagent.metrics.record_provider_operation()`
call per invocation, tagged with a bounded operation name (never the raw ESL
command string, which carries a `call_ref`)."""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony.contracts import TransportError
from voiceagent.telephony.freeswitch.fakes import FakeEslConnection
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider


@pytest.fixture
def recorded_operations(monkeypatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.provider.record_provider_operation",
        lambda provider_family, operation, outcome, duration_seconds: calls.append(
            (provider_family, operation, outcome)
        ),
    )
    return calls


def test_successful_command_records_success_with_bounded_operation_name(
    recorded_operations,
) -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.answer("call-1"))
    assert recorded_operations == [("telephony", "answer", "success")]


def test_esl_error_response_records_failure(monkeypatch) -> None:
    esl = FakeEslConnection()
    esl.responses["api uuid_answer call-1"] = "-ERR no such channel"
    provider = FreeSwitchTelephonyProvider(esl)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.provider.record_provider_operation",
        lambda provider_family, operation, outcome, duration_seconds: calls.append(
            (operation, outcome)
        ),
    )
    with pytest.raises(TransportError):
        asyncio.run(provider.answer("call-1"))

    assert calls == [("answer", "failure")]


def test_command_timeout_records_timeout_outcome(monkeypatch) -> None:
    esl = FakeEslConnection()

    async def _hang(command: str) -> str:
        await asyncio.sleep(1.0)
        return "+OK"

    esl.send = _hang  # type: ignore[method-assign]
    provider = FreeSwitchTelephonyProvider(esl, command_timeout_seconds=0.01)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.provider.record_provider_operation",
        lambda provider_family, operation, outcome, duration_seconds: calls.append(
            (operation, outcome)
        ),
    )
    with pytest.raises(TransportError):
        asyncio.run(provider.hold("call-1"))
    assert calls == [("hold", "timeout")]


def test_every_public_operation_uses_its_own_bounded_operation_name(recorded_operations) -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)

    asyncio.run(provider.answer("call-1"))
    asyncio.run(provider.hangup("call-1"))
    asyncio.run(provider.bridge("call-1", "call-2"))
    asyncio.run(provider.hold("call-1"))
    asyncio.run(provider.unhold("call-1"))
    asyncio.run(provider.send_dtmf("call-1", "1"))
    asyncio.run(provider.start_recording("call-1"))
    asyncio.run(provider.stop_recording("call-1"))

    operations = [operation for _, operation, _ in recorded_operations]
    assert operations == [
        "answer",
        "hangup",
        "bridge",
        "hold",
        "unhold",
        "send_dtmf",
        "start_recording",
        "stop_recording",
    ]
    # No `call_ref` value ("call-1", "call-2") ever leaked into an operation
    # name -- every recorded operation is a fixed ESL verb.
    assert "call-1" not in operations
    assert "call-2" not in operations
