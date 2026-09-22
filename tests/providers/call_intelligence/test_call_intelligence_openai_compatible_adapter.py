"""`voiceagent.providers.call_intelligence.openai_compatible
.OpenAiCompatibleCallIntelligenceProvider` -- mocks the HTTP transport
boundary (`httpx.MockTransport`), exactly like
`tests/providers/llm/test_openai_compatible_adapter.py`'s own Tier 2
discipline: testing the adapter's actual request construction and response
parsing with no network and no credentials.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from voiceagent.providers.call_intelligence import openai_compatible as oac_module
from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProviderError,
    CallIntelligenceRequest,
)
from voiceagent.providers.call_intelligence.openai_compatible import (
    OpenAiCompatibleCallIntelligenceProvider,
)


def _provider(mock_httpx_client, handler) -> OpenAiCompatibleCallIntelligenceProvider:
    mock_httpx_client(oac_module, handler)
    return OpenAiCompatibleCallIntelligenceProvider(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="k",
        provider_label="testvendor",
        timeout_seconds=5.0,
    )


def _request() -> CallIntelligenceRequest:
    return CallIntelligenceRequest(
        system_instructions="system prompt", user_content="user content", max_output_tokens=256
    )


def test_sends_a_non_streaming_request_with_the_right_shape(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    provider = _provider(mock_httpx_client, handler)
    response = asyncio.run(provider.analyze(_request()))

    assert response.raw_text == '{"ok": true}'
    sent_body = json.loads(captured[0].content)
    assert sent_body["model"] == "test-model"
    assert sent_body["stream"] is False
    assert sent_body["max_tokens"] == 256
    assert sent_body["response_format"] == {"type": "json_object"}
    assert sent_body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user content"},
    ]
    assert captured[0].headers["authorization"] == "Bearer k"


@pytest.mark.parametrize(
    "status, expected_code",
    [
        (401, CallIntelligenceErrorCode.AUTH),
        (403, CallIntelligenceErrorCode.AUTH),
        (429, CallIntelligenceErrorCode.RATE_LIMIT),
        (400, CallIntelligenceErrorCode.INVALID_REQUEST),
        (422, CallIntelligenceErrorCode.INVALID_REQUEST),
        (500, CallIntelligenceErrorCode.PROVIDER_DOWN),
        (503, CallIntelligenceErrorCode.PROVIDER_DOWN),
        (418, CallIntelligenceErrorCode.TRANSIENT),
    ],
)
def test_http_status_codes_are_normalized(mock_httpx_client, status, expected_code) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="error body")

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is expected_code


def test_timeout_is_normalized(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is CallIntelligenceErrorCode.TIMEOUT


def test_transport_failure_is_normalized(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is CallIntelligenceErrorCode.TRANSIENT


def test_missing_choices_shape_is_malformed_response(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is CallIntelligenceErrorCode.MALFORMED_RESPONSE


def test_non_string_content_is_malformed_response(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": 12345}}]})

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is CallIntelligenceErrorCode.MALFORMED_RESPONSE


def test_invalid_json_body_is_malformed_response(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )

    provider = _provider(mock_httpx_client, handler)
    with pytest.raises(CallIntelligenceProviderError) as excinfo:
        asyncio.run(provider.analyze(_request()))
    assert excinfo.value.code is CallIntelligenceErrorCode.MALFORMED_RESPONSE
