"""Tier 2: mocks the HTTP transport boundary for
`voiceagent.providers.llm.gemini`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    ToolCallRequested,
    ToolSpec,
    TurnEnded,
)
from voiceagent.providers.llm import gemini as gemini_module
from voiceagent.providers.llm.gemini import (
    GeminiLlmConfig,
    GeminiLlmProvider,
    create_gemini_llm_provider,
)


def _sse(*chunks: dict[str, object]) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode("utf-8")


def test_create_gemini_llm_provider_reads_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    provider = create_gemini_llm_provider({"model": "gemini-test"})
    assert isinstance(provider, GeminiLlmProvider)


def test_create_gemini_llm_provider_raises_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(EngineException) as exc_info:
        create_gemini_llm_provider({"model": "gemini-test"})
    assert exc_info.value.code is EngineErrorCode.AUTH


def test_streams_text_and_sends_system_instruction_and_auth_header(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = _sse(
            {"candidates": [{"content": {"parts": [{"text": "Hal"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "lo"}]}}]},
        )
        return httpx.Response(200, content=body)

    mock_httpx_client(gemini_module, handler)
    provider = GeminiLlmProvider(
        endpoint="https://example.test/v1beta",
        model="gemini-test",
        api_key="k",
        timeout_seconds=5.0,
    )
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ]

    async def scenario() -> list[object]:
        return [item async for item in provider.stream_turn(messages, [])]

    items = asyncio.run(scenario())
    assert items == ["Hal", "lo", TurnEnded()]

    sent = json.loads(captured[0].content)
    assert sent["systemInstruction"] == {"parts": [{"text": "Be terse."}]}
    assert sent["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert captured[0].headers["x-goog-api-key"] == "k"
    assert "alt=sse" in str(captured[0].url)


def test_function_call_becomes_a_tool_call_requested(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]
                        }
                    }
                ]
            }
        )
        return httpx.Response(200, content=body)

    mock_httpx_client(gemini_module, handler)
    provider = GeminiLlmProvider(
        endpoint="https://example.test/v1beta",
        model="gemini-test",
        api_key="k",
        timeout_seconds=5.0,
    )

    async def scenario() -> list[object]:
        return [
            item
            async for item in provider.stream_turn(
                [], [ToolSpec(name="lookup", description="d", input_schema={})]
            )
        ]

    items = asyncio.run(scenario())
    assert len(items) == 2
    assert isinstance(items[0], ToolCallRequested)
    assert items[0].name == "lookup"
    assert items[0].arguments == {"q": "x"}
    assert items[1] == TurnEnded()


def test_tool_result_message_becomes_a_function_response_part(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    mock_httpx_client(gemini_module, handler)
    provider = GeminiLlmProvider(
        endpoint="https://example.test/v1beta",
        model="gemini-test",
        api_key="k",
        timeout_seconds=5.0,
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "name": "lookup", "arguments": {"q": "x"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": {"found": True}, "name": "lookup"},
    ]

    async def scenario() -> None:
        async for _ in provider.stream_turn(messages, []):
            pass

    asyncio.run(scenario())
    sent_contents = json.loads(captured[0].content)["contents"]
    assert sent_contents[0]["role"] == "model"
    assert sent_contents[0]["parts"] == [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]
    assert sent_contents[1] == {
        "role": "function",
        "parts": [{"functionResponse": {"name": "lookup", "response": {"found": True}}}],
    }


def test_drops_a_malformed_sse_chunk_without_raising(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = b"data: {not json}\n\n" + _sse(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )
        return httpx.Response(200, content=body)

    mock_httpx_client(gemini_module, handler)
    provider = GeminiLlmProvider(
        endpoint="https://example.test/v1beta",
        model="gemini-test",
        api_key="k",
        timeout_seconds=5.0,
    )

    async def scenario() -> list[object]:
        return [item async for item in provider.stream_turn([], [])]

    assert asyncio.run(scenario()) == ["ok", TurnEnded()]


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, EngineErrorCode.AUTH),
        (429, EngineErrorCode.RATE_LIMIT),
        (503, EngineErrorCode.PROVIDER_DOWN),
    ],
)
def test_http_error_statuses_map_to_the_engine_error_taxonomy(
    mock_httpx_client, status: int, expected_code: EngineErrorCode
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"{}")

    mock_httpx_client(gemini_module, handler)
    provider = GeminiLlmProvider(
        endpoint="https://example.test/v1beta",
        model="gemini-test",
        api_key="k",
        timeout_seconds=5.0,
    )

    async def scenario() -> None:
        async for _ in provider.stream_turn([], []):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is expected_code


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 -- pydantic ValidationError.
        GeminiLlmConfig.model_validate({"model": "gemini-test", "bogus": 1})
