"""The provider-neutral contract every post-call intelligence adapter
implements (Phase 2.12).

`CallIntelligenceProvider.analyze()` takes one bounded request (already-
assembled, already-truncated text -- this layer does no further bounding of
its own, see `voiceagent.call_intelligence.prompt`) and returns one bounded
raw-text response. **The raw text is not the canonical result** -- it is
parsed and strictly validated into `voiceagent.call_intelligence.schema
.CallAiAnalysisResult` by the domain layer
(`voiceagent.call_intelligence.analyzer`), never persisted or trusted as-is.
No adapter method here ever returns a vendor SDK object, a vendor-specific
exception, or a raw `httpx.Response`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "CallIntelligenceErrorCode",
    "CallIntelligenceProvider",
    "CallIntelligenceProviderError",
    "CallIntelligenceRequest",
    "CallIntelligenceResponse",
]


class CallIntelligenceErrorCode(StrEnum):
    """A closed, small vocabulary -- mirrors
    `voiceagent.providers.engines.contracts.EngineErrorCode`'s own shape,
    kept as a separate enum rather than reused directly: this package must
    never import `voiceagent.providers.engines` (a live-conversation-engine
    concept this bounded, single-shot abstraction has no business depending
    on)."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    PROVIDER_DOWN = "provider_down"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"


class CallIntelligenceProviderError(Exception):
    """The one exception type every adapter raises -- normalizes a vendor-
    specific HTTP status/transport failure into `CallIntelligenceErrorCode`,
    never lets a raw `httpx` exception or vendor error body reach
    `voiceagent.call_intelligence.analyzer`."""

    def __init__(self, code: CallIntelligenceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CallIntelligenceRequest:
    """Already fully assembled and bounded by
    `voiceagent.call_intelligence.prompt` -- `system_instructions` is the
    fixed, code-owned analyzer policy; `user_content` is the one, already-
    delimited block of structured metadata plus untrusted transcript text
    (see that module's own docstring for the injection-resistance shape).
    Neither field is ever built by this package."""

    system_instructions: str
    user_content: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class CallIntelligenceResponse:
    """`raw_text` is the provider's own unparsed text output -- untrusted,
    unvalidated, never persisted verbatim (see module docstring)."""

    raw_text: str


@runtime_checkable
class CallIntelligenceProvider(Protocol):
    async def analyze(self, request: CallIntelligenceRequest) -> CallIntelligenceResponse: ...
