"""Tier 2 (Phase 2.3 brief section 11): mocks `websockets.connect`, never
the network. Proves request construction, response-event mapping, error
mapping, malformed-event handling and cancellation for
`voiceagent.providers.stt.deepgram`.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.exceptions import InvalidStatus

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    FinalTranscript,
    PartialTranscript,
)
from voiceagent.providers.stt import deepgram as deepgram_module
from voiceagent.providers.stt.deepgram import (
    DeepgramSttConfig,
    DeepgramSttProvider,
    create_deepgram_stt_provider,
)


class _FakeStatusResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


async def _one_frame_audio():
    yield b"\x00\x01\x02\x03"


async def _empty_audio():
    return
    yield  # pragma: no cover -- makes this an async generator with no items.


def test_create_deepgram_stt_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")
    provider = create_deepgram_stt_provider({"model": "nova-3", "language": "nl"})
    assert isinstance(provider, DeepgramSttProvider)


def test_create_deepgram_stt_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_deepgram_stt_provider({})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 -- pydantic ValidationError.
        DeepgramSttConfig.model_validate({"not_a_real_field": 1})


def test_stream_sends_raw_audio_and_yields_partial_and_final_transcripts(
    mock_websockets_connect, fake_websocket_connection_class
) -> None:
    results_partial = json.dumps(
        {
            "type": "Results",
            "is_final": False,
            "channel": {"alternatives": [{"transcript": "hall", "confidence": 0.5}]},
        }
    )
    results_final = json.dumps(
        {
            "type": "Results",
            "is_final": True,
            "channel": {"alternatives": [{"transcript": "hallo", "confidence": 0.97}]},
        }
    )
    connection = fake_websocket_connection_class([results_partial, results_final])
    mock_websockets_connect(deepgram_module, lambda *a, **kw: connection)

    provider = DeepgramSttProvider(
        api_key="k", config=DeepgramSttConfig(model="nova-3", language="nl")
    )

    async def scenario() -> list[PartialTranscript | FinalTranscript]:
        return [event async for event in provider.stream(_one_frame_audio())]

    events = asyncio.run(scenario())
    assert events == [
        PartialTranscript(text="hall"),
        FinalTranscript(text="hallo", confidence=0.97),
    ]
    assert connection.sent[0] == b"\x00\x01\x02\x03"  # raw binary, not JSON-wrapped.
    assert json.loads(connection.sent[-1]) == {"type": "CloseStream"}


def test_stream_drops_malformed_events_without_raising(
    mock_websockets_connect, fake_websocket_connection_class
) -> None:
    good = json.dumps(
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "ok"}]}}
    )
    connection = fake_websocket_connection_class(["not json at all", good])
    mock_websockets_connect(deepgram_module, lambda *a, **kw: connection)
    provider = DeepgramSttProvider(api_key="k", config=DeepgramSttConfig())

    async def scenario() -> list[object]:
        return [event async for event in provider.stream(_empty_audio())]

    events = asyncio.run(scenario())
    assert events == [FinalTranscript(text="ok")]


def test_auth_failure_maps_to_auth_error_code(mock_websockets_connect) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise InvalidStatus(_FakeStatusResponse(401))  # type: ignore[arg-type]

    mock_websockets_connect(deepgram_module, fail_connect)
    provider = DeepgramSttProvider(api_key="bad", config=DeepgramSttConfig())

    async def scenario() -> None:
        async for _ in provider.stream(_empty_audio()):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_rate_limit_maps_to_rate_limit_error_code(mock_websockets_connect) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise InvalidStatus(_FakeStatusResponse(429))  # type: ignore[arg-type]

    mock_websockets_connect(deepgram_module, fail_connect)
    provider = DeepgramSttProvider(api_key="k", config=DeepgramSttConfig())

    async def scenario() -> None:
        async for _ in provider.stream(_empty_audio()):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.RATE_LIMIT


def test_connection_timeout_maps_to_transient_error_code(mock_websockets_connect) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise TimeoutError("connect timed out")

    mock_websockets_connect(deepgram_module, fail_connect)
    provider = DeepgramSttProvider(api_key="k", config=DeepgramSttConfig())

    async def scenario() -> None:
        async for _ in provider.stream(_empty_audio()):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.TRANSIENT


def test_stream_can_be_cancelled_mid_iteration(
    mock_websockets_connect, fake_websocket_connection_class
) -> None:
    """A slow/never-ending stream must not block cancellation -- the
    consumer's task can be cancelled while iterating `stream()`."""

    class _NeverEndingConnection(fake_websocket_connection_class):
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)
            raise StopAsyncIteration  # pragma: no cover -- unreachable in the test.

    connection = _NeverEndingConnection([])
    mock_websockets_connect(deepgram_module, lambda *a, **kw: connection)
    provider = DeepgramSttProvider(api_key="k", config=DeepgramSttConfig())

    async def scenario() -> None:
        async def consume() -> None:
            async for _ in provider.stream(_empty_audio()):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
