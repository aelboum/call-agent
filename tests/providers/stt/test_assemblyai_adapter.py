"""Tier 2: mocks `websockets.connect` for
`voiceagent.providers.stt.assemblyai` -- the second STT adapter, whose wire
shape (base64 JSON audio frames, a `message_type` discriminator) is
deliberately different from Deepgram's (raw binary frames, an `is_final`
boolean) to prove the registry/factory design is genuinely provider-neutral.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from collections.abc import Sequence

import pytest
from websockets.exceptions import InvalidStatus

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    FinalTranscript,
    PartialTranscript,
)
from voiceagent.providers.stt import assemblyai as assemblyai_module
from voiceagent.providers.stt.assemblyai import (
    AssemblyAiSttConfig,
    AssemblyAiSttProvider,
    create_assemblyai_stt_provider,
)


def _without_event_ids(events: Sequence[object]) -> list[object]:
    """`FinalTranscript.event_id` (Phase 2.5) is a random per-instance
    idempotency key -- irrelevant to this file's own "does the adapter map
    the wire shape correctly" assertions, so it is normalized out before
    equality comparison."""
    return [
        dataclasses.replace(event, event_id="") if isinstance(event, FinalTranscript) else event
        for event in events
    ]


class _FakeStatusResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


async def _one_frame_audio():
    yield b"\xaa\xbb"


async def _empty_audio():
    return
    yield  # pragma: no cover


def test_create_assemblyai_stt_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai-test-key")
    provider = create_assemblyai_stt_provider({"language": "nl"})
    assert isinstance(provider, AssemblyAiSttProvider)


def test_create_assemblyai_stt_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_assemblyai_stt_provider({})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_stream_sends_base64_json_audio_and_yields_partial_and_final_transcripts(
    mock_websockets_connect, fake_websocket_connection_class
) -> None:
    partial = json.dumps({"message_type": "PartialTranscript", "text": "hall"})
    final = json.dumps({"message_type": "FinalTranscript", "text": "hallo", "confidence": 0.9})
    connection = fake_websocket_connection_class([partial, final])
    mock_websockets_connect(assemblyai_module, lambda *a, **kw: connection)
    provider = AssemblyAiSttProvider(api_key="k", config=AssemblyAiSttConfig())

    async def scenario() -> list[PartialTranscript | FinalTranscript]:
        return [event async for event in provider.stream(_one_frame_audio())]

    events = asyncio.run(scenario())
    assert _without_event_ids(events) == _without_event_ids(
        [PartialTranscript(text="hall"), FinalTranscript(text="hallo", confidence=0.9)]
    )

    sent_audio = json.loads(connection.sent[0])
    assert base64.b64decode(sent_audio["audio_data"]) == b"\xaa\xbb"
    assert json.loads(connection.sent[-1]) == {"terminate_session": True}


def test_stream_drops_events_with_no_recognized_message_type(
    mock_websockets_connect, fake_websocket_connection_class
) -> None:
    connection = fake_websocket_connection_class(
        [json.dumps({"message_type": "SessionBegins"}), "{ broken json"]
    )
    mock_websockets_connect(assemblyai_module, lambda *a, **kw: connection)
    provider = AssemblyAiSttProvider(api_key="k", config=AssemblyAiSttConfig())

    async def scenario() -> list[object]:
        return [event async for event in provider.stream(_empty_audio())]

    assert asyncio.run(scenario()) == []


def test_auth_failure_maps_to_auth_error_code(mock_websockets_connect) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise InvalidStatus(_FakeStatusResponse(403))  # type: ignore[arg-type]

    mock_websockets_connect(assemblyai_module, fail_connect)
    provider = AssemblyAiSttProvider(api_key="bad", config=AssemblyAiSttConfig())

    async def scenario() -> None:
        async for _ in provider.stream(_empty_audio()):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_transport_failure_maps_to_transient_error_code(mock_websockets_connect) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise OSError("connection reset")

    mock_websockets_connect(assemblyai_module, fail_connect)
    provider = AssemblyAiSttProvider(api_key="k", config=AssemblyAiSttConfig())

    async def scenario() -> None:
        async for _ in provider.stream(_empty_audio()):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.TRANSIENT
