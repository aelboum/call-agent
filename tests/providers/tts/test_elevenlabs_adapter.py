"""Tier 2: mocks the HTTP transport boundary for
`voiceagent.providers.tts.elevenlabs`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from voiceagent.providers.engines.contracts import (
    AudioOut,
    EngineErrorCode,
    EngineException,
    VoiceRef,
)
from voiceagent.providers.tts import elevenlabs as elevenlabs_module
from voiceagent.providers.tts._ulaw import ulaw_to_pcm16
from voiceagent.providers.tts.elevenlabs import (
    ElevenLabsTtsConfig,
    ElevenLabsTtsProvider,
    create_elevenlabs_tts_provider,
)


def test_create_elevenlabs_tts_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    provider = create_elevenlabs_tts_provider({})
    assert isinstance(provider, ElevenLabsTtsProvider)


def test_create_elevenlabs_tts_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_elevenlabs_tts_provider({})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_synthesize_requests_ulaw_8000_and_zero_retention_unconditionally(
    mock_httpx_client,
) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=bytes([0xFF, 0xFF, 0x00]))

    mock_httpx_client(elevenlabs_module, handler)
    provider = ElevenLabsTtsProvider(api_key="k", config=ElevenLabsTtsConfig())

    async def scenario() -> list[AudioOut]:
        return [
            chunk
            async for chunk in provider.synthesize(
                "hello", VoiceRef(provider="elevenlabs", voice_id="voice-1")
            )
        ]

    chunks = asyncio.run(scenario())
    request = captured[0]
    assert request.url.params["output_format"] == "ulaw_8000"
    assert request.url.params["enable_logging"] == "false"
    assert "/text-to-speech/voice-1/stream" in str(request.url)
    assert request.headers["xi-api-key"] == "k"
    # Decoded to PCM16 at the adapter boundary -- 3 mu-law bytes -> 6 PCM bytes.
    assert sum(len(c.frame) for c in chunks) == 6
    assert all(c.sample_rate == 8000 for c in chunks)


def test_synthesize_decodes_mulaw_to_pcm16(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bytes([0x00]))

    mock_httpx_client(elevenlabs_module, handler)
    provider = ElevenLabsTtsProvider(api_key="k", config=ElevenLabsTtsConfig())

    async def scenario() -> list[AudioOut]:
        return [chunk async for chunk in provider.synthesize("hi", None)]

    chunks = asyncio.run(scenario())
    assert chunks[0].frame == ulaw_to_pcm16(bytes([0x00]))


def test_uses_a_default_voice_when_none_is_given(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    mock_httpx_client(elevenlabs_module, handler)
    provider = ElevenLabsTtsProvider(api_key="k", config=ElevenLabsTtsConfig())

    async def scenario() -> None:
        async for _ in provider.synthesize("hi", None):
            pass

    asyncio.run(scenario())
    assert "/text-to-speech/" in str(captured[0].url)


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, EngineErrorCode.AUTH),
        (429, EngineErrorCode.RATE_LIMIT),
        (500, EngineErrorCode.PROVIDER_DOWN),
    ],
)
def test_http_error_statuses_map_to_the_engine_error_taxonomy(
    mock_httpx_client, status: int, expected_code: EngineErrorCode
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"{}")

    mock_httpx_client(elevenlabs_module, handler)
    provider = ElevenLabsTtsProvider(api_key="k", config=ElevenLabsTtsConfig())

    async def scenario() -> None:
        async for _ in provider.synthesize("hi", None):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is expected_code


def test_transport_failure_maps_to_transient(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    mock_httpx_client(elevenlabs_module, handler)
    provider = ElevenLabsTtsProvider(api_key="k", config=ElevenLabsTtsConfig())

    async def scenario() -> None:
        async for _ in provider.synthesize("hi", None):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.TRANSIENT
