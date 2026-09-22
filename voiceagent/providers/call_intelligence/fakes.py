"""`FakeCallIntelligenceProvider` -- the one provider every hermetic test and
the default deployment configuration use, exactly mirroring
`voiceagent.providers.engines.component_fakes.FakeLlmProvider`'s role for
the live engine registry.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceProviderError,
    CallIntelligenceRequest,
    CallIntelligenceResponse,
)

__all__ = ["FakeCallIntelligenceProvider", "create_fake_call_intelligence_provider"]

_DEFAULT_RESULT: dict[str, object] = {
    "summary": "The customer called with a question and the call concluded normally.",
    "customer_intent": "General inquiry.",
    "key_topics": [],
    "action_items": [],
    "escalation": {"required": False, "reason": None},
    "sentiment": None,
    "confidence": 0.5,
}


class FakeCallIntelligenceProvider:
    """Configurable double: returns `response_text` (defaulting to a valid,
    minimal `CallAiAnalysisResult` JSON document) verbatim, or raises
    `error` if one is given, or hangs past `delay_seconds` to exercise a
    caller's own timeout (`asyncio.wait_for`) -- this class enforces no
    timeout of its own, matching every real adapter's own contract."""

    def __init__(
        self,
        *,
        response_text: str | None = None,
        error: CallIntelligenceProviderError | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._response_text = response_text or json.dumps(_DEFAULT_RESULT)
        self._error = error
        self._delay_seconds = delay_seconds
        self.requests: list[CallIntelligenceRequest] = []

    async def analyze(self, request: CallIntelligenceRequest) -> CallIntelligenceResponse:
        self.requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        return CallIntelligenceResponse(raw_text=self._response_text)


def create_fake_call_intelligence_provider(
    config: Mapping[str, object],
) -> FakeCallIntelligenceProvider:
    return FakeCallIntelligenceProvider()
