"""`FreeSwitchMediaProvider`'s Phase 2.14 metrics recording: `attach()`/
`detach()` each record one `voiceagent.metrics.record_provider_operation()`
call, `provider_family="media"`."""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony.contracts import AudioFormat, TransportError, UnsupportedFormatError
from voiceagent.telephony.freeswitch.fakes import FakeMediaSocket
from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider


@pytest.fixture
def recorded_operations(monkeypatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "voiceagent.telephony.freeswitch.media.record_provider_operation",
        lambda provider_family, operation, outcome, duration_seconds: calls.append(
            (provider_family, operation, outcome)
        ),
    )
    return calls


def test_successful_attach_records_success(recorded_operations) -> None:
    provider = FreeSwitchMediaProvider()
    provider.register_socket("call-1", FakeMediaSocket())
    asyncio.run(provider.attach("call-1"))
    assert recorded_operations == [("media", "attach", "success")]


def test_attach_with_no_registered_socket_records_failure(recorded_operations) -> None:
    provider = FreeSwitchMediaProvider()
    with pytest.raises(TransportError):
        asyncio.run(provider.attach("call-1"))
    assert recorded_operations == [("media", "attach", "failure")]


def test_attach_with_unsupported_format_records_failure(recorded_operations) -> None:
    provider = FreeSwitchMediaProvider()
    provider.register_socket("call-1", FakeMediaSocket())
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(provider.attach("call-1", AudioFormat(sample_rate=44100)))
    assert recorded_operations == [("media", "attach", "failure")]


def test_successful_detach_records_success(recorded_operations) -> None:
    provider = FreeSwitchMediaProvider()
    provider.register_socket("call-1", FakeMediaSocket())
    asyncio.run(provider.attach("call-1"))
    asyncio.run(provider.detach("call-1"))
    assert recorded_operations == [
        ("media", "attach", "success"),
        ("media", "detach", "success"),
    ]


def test_detach_with_no_attached_stream_records_failure(recorded_operations) -> None:
    provider = FreeSwitchMediaProvider()
    with pytest.raises(TransportError):
        asyncio.run(provider.detach("call-1"))
    assert recorded_operations == [("media", "detach", "failure")]
