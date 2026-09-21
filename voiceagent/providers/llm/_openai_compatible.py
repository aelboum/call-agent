"""Shared machinery for `LlmProvider` adapters that speak the OpenAI-
compatible chat-completions wire shape: SSE-streamed text deltas, a
`tool_calls` array assembled across chunks by index, and a `finish_reason`
that closes the turn. Two Phase 2.3 vendors use this shape verbatim --
Mistral and Groq (both confirmed via `WebFetch` against their own current
API reference during this phase; see `docs/PHASE-2.3-STATUS.md` section 3)
-- so the wire-level parsing lives here once, and each vendor module
(`voiceagent.providers.llm.mistral`, `voiceagent.providers.llm.groq`) is
just its base URL, its secret name and its default model.

Not a vendor SDK: built directly on `httpx`. Nothing here is Mistral- or
Groq-specific; a third OpenAI-compatible vendor (the brief explicitly keeps
this door open) is a five-line new module reusing this class, never a
change to this file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence

import httpx

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    ToolCallRequested,
    ToolSpec,
    TurnEnded,
)

__all__ = ["OpenAiCompatibleLlmProvider"]

_logger = logging.getLogger(__name__)


def _to_wire_message(message: Mapping[str, object]) -> dict[str, object]:
    """Translate one `PipelinedEngineSession` message (its own internal
    shape, `voiceagent.providers.engines.pipelined`) into the OpenAI-
    compatible wire shape. `tool_calls` on an assistant message and
    `tool_call_id` on a tool message are the two keys that shape adds
    (Phase 2.3 deviation -- see `docs/PHASE-2.3-STATUS.md` section 3)."""
    role = message.get("role")
    if role == "tool":
        content = message.get("content")
        return {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id"),
            "content": content if isinstance(content, str) else json.dumps(content),
        }
    if role == "assistant" and message.get("tool_calls"):
        calls = message["tool_calls"]
        assert isinstance(calls, list)  # noqa: S101 -- internal invariant, not user input.
        return {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                }
                for call in calls
            ],
        }
    return {"role": role, "content": message.get("content")}


def _to_wire_tools(tools: Sequence[ToolSpec]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


class OpenAiCompatibleLlmProvider:
    """One `LlmProvider` bound to one base URL, model and API key."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        provider_label: str,
        timeout_seconds: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._label = provider_label
        self._timeout = timeout_seconds

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ) -> AsyncIterator[str | ToolCallRequested | TurnEnded]:
        body: dict[str, object] = {
            "model": self._model,
            "messages": [_to_wire_message(m) for m in messages],
            "stream": True,
        }
        if tools:
            body["tools"] = _to_wire_tools(tools)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        # Assembled across chunks, keyed by the wire's own `index` -- an
        # OpenAI-compatible stream may interleave fragments of more than one
        # tool call's `arguments` string across successive chunks.
        pending_calls: dict[int, dict[str, str]] = {}

        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST", f"{self._base_url}/chat/completions", json=body, headers=headers
                ) as response,
            ):
                if response.status_code != 200:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._status_error(response.status_code, body_text)
                async for line in response.aiter_lines():
                    stripped = line.strip()
                    if not stripped or not stripped.startswith("data:"):
                        continue
                    payload = stripped[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        _logger.warning("%s: dropped a malformed SSE chunk", self._label)
                        continue
                    try:
                        choice = chunk["choices"][0]
                    except (KeyError, IndexError, TypeError):
                        continue
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        yield delta["content"]
                    for tool_call_delta in delta.get("tool_calls") or []:
                        index = tool_call_delta.get("index", 0)
                        entry = pending_calls.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tool_call_delta.get("id"):
                            entry["id"] = tool_call_delta["id"]
                        function = tool_call_delta.get("function") or {}
                        if function.get("name"):
                            entry["name"] = function["name"]
                        if function.get("arguments"):
                            entry["arguments"] += function["arguments"]
        except httpx.TimeoutException as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"{self._label}: request timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"{self._label}: transport failure: {exc}"
            ) from exc

        for index in sorted(pending_calls):
            call = pending_calls[index]
            try:
                arguments = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                _logger.warning(
                    "%s: dropped a tool call with malformed arguments JSON", self._label
                )
                continue
            yield ToolCallRequested(
                call_id=call["id"] or f"call-{index}", name=call["name"], arguments=arguments
            )
        yield TurnEnded()

    def _status_error(self, status: int, body_text: str) -> EngineException:
        if status in (401, 403):
            return EngineException(EngineErrorCode.AUTH, f"{self._label}: auth rejected ({status})")
        if status == 429:
            return EngineException(EngineErrorCode.RATE_LIMIT, f"{self._label}: rate limited")
        if status in (400, 422):
            return EngineException(
                EngineErrorCode.INVALID_REQUEST, f"{self._label}: invalid request ({status})"
            )
        if status >= 500:
            return EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"{self._label}: server error ({status})"
            )
        return EngineException(
            EngineErrorCode.TRANSIENT,
            f"{self._label}: unexpected status {status}: {body_text[:200]}",
        )
