"""Google Gemini chat completions (ADR-0009-successor first LLM reference
combination). Implemented directly against the Generative Language REST API
(`generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent`)
via `httpx` -- no `google-generativeai`/`google-genai` SDK dependency.

Gemini's wire shape differs from the OpenAI-compatible one Mistral and Groq
share (`voiceagent.providers.llm._openai_compatible`) in three ways this
adapter translates at its own boundary, never upward: no `system` role
inside `contents` (a separate `systemInstruction` field instead), `"model"`
instead of `"assistant"` as the assistant's role name, and tool results
returned as a `functionResponse` part rather than a `tool`-role message.

**Verification note** (Phase 2.3 brief section 2 /
`docs/PHASE-2.3-STATUS.md` section 3): `WebFetch` against
`ai.google.dev/gemini-api/docs/text-generation` during this phase returned
content inconsistent with Gemini's actual known API shape (an endpoint
`/v1beta/interactions` and a model name `gemini-3.8-flash` that do not match
the real, long-documented `:generateContent`/`:streamGenerateContent`
surface) -- treated as unreliable and not used. The request/response shape
implemented below follows the Generative Language API's real, stable
documented shape from training-era knowledge instead, flagged here rather
than silently presented as freshly verified; it should be re-checked against
current documentation before any production use, and `model` is a fully
operator-configurable field for exactly this reason (no version is
hardcoded).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence

import httpx
from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    ToolCallRequested,
    ToolSpec,
    TurnEnded,
)

__all__ = ["GeminiLlmConfig", "GeminiLlmProvider", "create_gemini_llm_provider"]

_logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
_SECRET_NAME = "GEMINI_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class GeminiLlmConfig(BaseModel):
    """The typed shape of `voiceagent.providers.llm.registry`'s merged
    `{"model": ..., **LlmComponentConfig.config}` mapping for
    `provider: "gemini"` -- see `voiceagent.providers.llm.mistral
    .MistralLlmConfig`'s docstring for why `model` is folded in here."""

    model_config = ConfigDict(extra="forbid")

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0


def _to_gemini_contents(
    messages: Sequence[Mapping[str, object]],
) -> tuple[str | None, list[dict[str, object]]]:
    """Splits our internal message list into Gemini's `systemInstruction`
    (any `system`-role messages, concatenated) and `contents` (everything
    else, translated role-by-role)."""
    system_parts: list[str] = []
    contents: list[dict[str, object]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": message.get("content", "")}]})
        elif role == "assistant":
            parts: list[dict[str, object]] = []
            if message.get("content"):
                parts.append({"text": message["content"]})
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    assert isinstance(call, dict)  # noqa: S101 -- internal invariant, not user input.
                    parts.append(
                        {"functionCall": {"name": call["name"], "args": call["arguments"]}}
                    )
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            content = message.get("content")
            contents.append(
                {
                    "role": "function",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.get("name", "tool"),
                                "response": content
                                if isinstance(content, dict)
                                else {"result": content},
                            }
                        }
                    ],
                }
            )
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _to_gemini_tools(tools: Sequence[ToolSpec]) -> list[dict[str, object]]:
    if not tools:
        return []
    return [
        {
            "functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.input_schema}
                for t in tools
            ]
        }
    ]


class GeminiLlmProvider:
    def __init__(self, *, endpoint: str, model: str, api_key: str, timeout_seconds: float) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ) -> AsyncIterator[str | ToolCallRequested | TurnEnded]:
        system_instruction, contents = _to_gemini_contents(messages)
        body: dict[str, object] = {"contents": contents}
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        wire_tools = _to_gemini_tools(tools)
        if wire_tools:
            body["tools"] = wire_tools

        url = f"{self._endpoint}/models/{self._model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": self._api_key}
        pending_calls: list[ToolCallRequested] = []
        call_index = 0

        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream("POST", url, json=body, headers=headers) as response,
            ):
                if response.status_code != 200:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._status_error(response.status_code, body_text)
                async for line in response.aiter_lines():
                    stripped = line.strip()
                    if not stripped or not stripped.startswith("data:"):
                        continue
                    payload = stripped[len("data:") :].strip()
                    if not payload:
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        _logger.warning("gemini: dropped a malformed SSE chunk")
                        continue
                    try:
                        candidate = chunk["candidates"][0]
                        parts = candidate.get("content", {}).get("parts", [])
                    except (KeyError, IndexError, TypeError):
                        continue
                    for part in parts:
                        if "text" in part:
                            yield part["text"]
                        elif "functionCall" in part:
                            call = part["functionCall"]
                            call_index += 1
                            pending_calls.append(
                                ToolCallRequested(
                                    call_id=f"gemini-call-{call_index}",
                                    name=call.get("name", ""),
                                    arguments=call.get("args", {}),
                                )
                            )
        except httpx.TimeoutException as exc:
            raise EngineException(EngineErrorCode.TRANSIENT, "gemini: request timed out") from exc
        except httpx.TransportError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"gemini: transport failure: {exc}"
            ) from exc

        for call in pending_calls:
            yield call
        yield TurnEnded()

    def _status_error(self, status: int, body_text: str) -> EngineException:
        if status in (401, 403):
            return EngineException(EngineErrorCode.AUTH, f"gemini: auth rejected ({status})")
        if status == 429:
            return EngineException(EngineErrorCode.RATE_LIMIT, "gemini: rate limited")
        if status in (400, 422):
            return EngineException(
                EngineErrorCode.INVALID_REQUEST, f"gemini: invalid request ({status})"
            )
        if status >= 500:
            return EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"gemini: server error ({status})"
            )
        return EngineException(
            EngineErrorCode.TRANSIENT, f"gemini: unexpected status {status}: {body_text[:200]}"
        )


def create_gemini_llm_provider(config: Mapping[str, object]) -> GeminiLlmProvider:
    parsed = GeminiLlmConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"gemini: {_SECRET_NAME} is not configured"
        ) from exc
    return GeminiLlmProvider(
        endpoint=parsed.endpoint,
        model=parsed.model,
        api_key=api_key,
        timeout_seconds=parsed.timeout_seconds,
    )
