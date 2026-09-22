"""A non-streaming OpenAI-compatible chat-completions client for post-call
intelligence (Phase 2.12).

Deliberately not `voiceagent.providers.llm._openai_compatible
.OpenAiCompatibleLlmProvider`: that class speaks SSE streaming with
assembled tool calls, built for a live conversational turn. This adapter
issues one `stream: false` request and reads one JSON response body --
the correct shape for a single bounded "analyze this transcript" call. Both
share the same wire vendor family (Groq, and any future OpenAI-compatible
vendor) but nothing else; duplicating this much simpler client is cheaper
and clearer than parameterizing the streaming one to also support a
non-streaming mode it will never otherwise need.

No vendor SDK -- direct `httpx` only, matching
`voiceagent.providers.llm._openai_compatible`'s own discipline.
"""

from __future__ import annotations

import json

import httpx

from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProviderError,
    CallIntelligenceRequest,
    CallIntelligenceResponse,
)

__all__ = ["OpenAiCompatibleCallIntelligenceProvider"]


class OpenAiCompatibleCallIntelligenceProvider:
    """One provider bound to one base URL, model and API key."""

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

    async def analyze(self, request: CallIntelligenceRequest) -> CallIntelligenceResponse:
        body: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system_instructions},
                {"role": "user", "content": request.user_content},
            ],
            "stream": False,
            "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise CallIntelligenceProviderError(
                CallIntelligenceErrorCode.TIMEOUT, f"{self._label}: request timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise CallIntelligenceProviderError(
                CallIntelligenceErrorCode.TRANSIENT, f"{self._label}: transport failure: {exc}"
            ) from exc

        if response.status_code != 200:
            raise self._status_error(response.status_code, response.text)

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise CallIntelligenceProviderError(
                CallIntelligenceErrorCode.MALFORMED_RESPONSE,
                f"{self._label}: response did not match the expected chat-completions shape",
            ) from exc
        if not isinstance(content, str):
            raise CallIntelligenceProviderError(
                CallIntelligenceErrorCode.MALFORMED_RESPONSE,
                f"{self._label}: message content was not a string",
            )
        return CallIntelligenceResponse(raw_text=content)

    def _status_error(self, status: int, body_text: str) -> CallIntelligenceProviderError:
        if status in (401, 403):
            return CallIntelligenceProviderError(
                CallIntelligenceErrorCode.AUTH, f"{self._label}: auth rejected ({status})"
            )
        if status == 429:
            return CallIntelligenceProviderError(
                CallIntelligenceErrorCode.RATE_LIMIT, f"{self._label}: rate limited"
            )
        if status in (400, 422):
            return CallIntelligenceProviderError(
                CallIntelligenceErrorCode.INVALID_REQUEST,
                f"{self._label}: invalid request ({status})",
            )
        if status >= 500:
            return CallIntelligenceProviderError(
                CallIntelligenceErrorCode.PROVIDER_DOWN, f"{self._label}: server error ({status})"
            )
        return CallIntelligenceProviderError(
            CallIntelligenceErrorCode.TRANSIENT,
            f"{self._label}: unexpected status {status}: {body_text[:200]}",
        )
