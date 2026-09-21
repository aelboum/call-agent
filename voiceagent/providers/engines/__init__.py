"""Conversation engines (ADR-0006).

`contracts` is product-owned and framework-free; `fakes` is the deterministic
engine that keeps a non-vendor path in CI at all times. Real engines arrive in
Phase 2: a `PipelinedEngine` (which may use Pipecat internally, and only from
`voiceagent.providers.engines.pipecat`) and a product-owned `RealtimeEngine`.
"""

from __future__ import annotations

from voiceagent.providers.engines.contracts import (
    ConversationEngine,
    EngineErrorCode,
    EngineEvent,
    EngineException,
    EngineSession,
    EngineSessionConfig,
    ToolResult,
    ToolSpec,
    VoiceRef,
)

__all__ = [
    "ConversationEngine",
    "EngineErrorCode",
    "EngineEvent",
    "EngineException",
    "EngineSession",
    "EngineSessionConfig",
    "ToolResult",
    "ToolSpec",
    "VoiceRef",
]
