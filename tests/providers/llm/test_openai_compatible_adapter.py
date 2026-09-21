"""Tier 2: mocks the HTTP transport boundary (`httpx.MockTransport`) for
`voiceagent.providers.llm._openai_compatible.OpenAiCompatibleLlmProvider` --
the shared wire mechanics Mistral and Groq both use. Testing it once here
covers both vendors' actual request/response handling; the per-vendor test
files only need to prove their own endpoint/secret wiring.
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
from voiceagent.providers.llm import _openai_compatible as oac_module
from voiceagent.providers.llm._openai_compatible import OpenAiCompatibleLlmProvider


def _sse(*payloads: dict[str, object] | str) -> bytes:
    lines = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode("utf-8")


def _provider(mock_httpx_client, handler) -> OpenAiCompatibleLlmProvider:
    mock_httpx_client(oac_module, handler)
    return OpenAiCompatibleLlmProvider(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="k",
        provider_label="testvendor",
        timeout_seconds=5.0,
    )


def test_streams_text_deltas_and_ends_with_turn_ended(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = _sse(
            {"choices": [{"delta": {"content": "Hal"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            "[DONE]",
        )
        return httpx.Response(200, content=body)

    provider = _provider(mock_httpx_client, handler)

    async def scenario() -> list[object]:
        return [
            item async for item in provider.stream_turn([{"role": "user", "content": "hi"}], [])
        ]

    items = asyncio.run(scenario())
    assert items == ["Hal", "lo", TurnEnded()]
    sent_body = json.loads(captured[0].content)
    assert sent_body["model"] == "test-model"
    assert sent_body["stream"] is True
    assert sent_body["messages"] == [{"role": "user", "content": "hi"}]
    assert captured[0].headers["authorization"] == "Bearer k"


def test_assembles_a_tool_call_across_chunks_by_index(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"name": "lookup", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}}
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]}}
                ]
            },
            "[DONE]",
        )
        return httpx.Response(200, content=body)

    provider = _provider(mock_httpx_client, handler)
    tools = [ToolSpec(name="lookup", description="Look something up", input_schema={})]

    async def scenario() -> list[object]:
        return [item async for item in provider.stream_turn([], tools)]

    items = asyncio.run(scenario())
    assert items == [
        ToolCallRequested(call_id="call-1", name="lookup", arguments={"q": "x"}),
        TurnEnded(),
    ]


def test_drops_a_malformed_sse_chunk_without_raising(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b"data: {not json}\n\n"
            b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body)

    provider = _provider(mock_httpx_client, handler)

    async def scenario() -> list[object]:
        return [item async for item in provider.stream_turn([], [])]

    assert asyncio.run(scenario()) == ["ok", TurnEnded()]


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, EngineErrorCode.AUTH),
        (403, EngineErrorCode.AUTH),
        (429, EngineErrorCode.RATE_LIMIT),
        (400, EngineErrorCode.INVALID_REQUEST),
        (500, EngineErrorCode.PROVIDER_DOWN),
    ],
)
def test_http_error_statuses_map_to_the_engine_error_taxonomy(
    mock_httpx_client, status: int, expected_code: EngineErrorCode
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b'{"error": "boom"}')

    provider = _provider(mock_httpx_client, handler)

    async def scenario() -> None:
        async for _ in provider.stream_turn([], []):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is expected_code


def test_transport_failure_maps_to_transient(mock_httpx_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider(mock_httpx_client, handler)

    async def scenario() -> None:
        async for _ in provider.stream_turn([], []):
            pass

    with pytest.raises(EngineException) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.code is EngineErrorCode.TRANSIENT


def test_tool_message_is_translated_to_the_wire_shape(mock_httpx_client) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_sse("[DONE]"))

    provider = _provider(mock_httpx_client, handler)
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "book a table"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "name": "lookup", "arguments": {"q": "x"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": {"found": True}, "error_code": None},
    ]

    async def scenario() -> None:
        async for _ in provider.stream_turn(messages, []):
            pass

    asyncio.run(scenario())
    sent = json.loads(captured[0].content)["messages"]
    assert sent[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }
        ],
    }
    assert sent[3] == {"role": "tool", "tool_call_id": "call-1", "content": '{"found": true}'}
