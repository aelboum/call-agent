"""Tier 2: mocks the HTTP transport boundary for
`voiceagent.providers.tts.deepgram_aura` -- deliberately the "no conversion
needed" case (Aura returns 8 kHz linear16 directly), contrasted with
`test_elevenlabs_adapter.py`'s mu-law decode.
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
from voiceagent.providers.tts import deepgram_aura as deepgram_aura_module
from voiceagent.providers.tts.deepgram_aura import (
    DeepgramAuraTtsConfig,
    DeepgramAuraTtsProvider,
    create_deepgram_aura_tts_provider,
)


def test_create_deepgram_aura_tts_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    provider = create_deepgram_aura_tts_provider({})
    assert isinstance(provider, DeepgramAuraTtsProvider)


def test_create_deepgram_aura_tts_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_deepgram_aura_tts_provider({})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_synthesize_requests_native_8khz_linear16_with_no_conversion(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []
    raw_pcm = b"\x01\x02\x03\x04"

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=raw_pcm)

    mock_httpx_client(deepgram_aura_module, handler)
    provider = DeepgramAuraTtsProvider(api_key="k", config=DeepgramAuraTtsConfig())

    async def scenario() -> list[AudioOut]:
        return [
            chunk
            async for chunk in provider.synthesize(
                "hello", VoiceRef(provider="deepgram_aura", voice_id="aura-2-luna-en")
            )
        ]

    chunks = asyncio.run(scenario())
    request = captured[0]
    assert request.url.params["model"] == "aura-2-luna-en"
    assert request.url.params["encoding"] == "linear16"
    assert request.url.params["sample_rate"] == "8000"
    assert request.headers["authorization"] == "Token k"
    # No conversion: bytes pass through exactly as Aura returned them.
    assert b"".join(c.frame for c in chunks) == raw_pcm
    assert all(c.sample_rate == 8000 for c in chunks)


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

    mock_httpx_client(deepgram_aura_module, handler)
    provider = DeepgramAuraTtsProvider(api_key="k", config=DeepgramAuraTtsConfig())

    async def scenario() -> None:
        async for _ in provider.synthesize("hi", None):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is expected_code
