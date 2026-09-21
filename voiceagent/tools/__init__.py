"""The product-owned Tool Gateway (ADR-0003; Phase 2.4).

```text
ToolRequest (voiceagent.providers.engines.contracts.ToolCallRequested)
    |
    v
ToolGateway
    |
    +-- resolve static ToolDefinition        (voiceagent.tools.registry)
    +-- verify AgentVersion allowlist        (voiceagent.tools.allowlist)
    +-- validate input                       (ToolDefinition.input_model)
    +-- verify authorization                 (core.rbac.can, via DatabaseBoundary)
    +-- enforce idempotency                  (bounded, per-call, in-memory)
    +-- execute application capability       (ToolDefinition.handler -> TelephonyProvider)
    +-- validate output                      (ToolDefinition.output_model)
    +-- audit outcome                        (core.audit_log.record, via DatabaseBoundary)
    |
    v
ToolResult (voiceagent.providers.engines.contracts.ToolResult)
```

**No second `ToolRequest`/`ToolResult` type exists here.** The Phase 1/2.2
`ToolCallRequested`/`ToolResult`/`ToolSpec` contract in
`voiceagent.providers.engines.contracts` already is the provider-neutral
boundary the engine and every `LlmProvider` adapter speak; this package is
the *consumer* of that contract, not a second one competing with it.

**Provider-neutral.** Nothing in this package imports Gemini, Mistral, Groq,
Deepgram, ElevenLabs, or any `voiceagent.providers.{stt,llm,tts}.*` vendor
module -- a `ToolCallRequested` arrives with a `call_id`/`name`/`arguments`
triple, and the gateway never learns which `LlmProvider` produced it.

**Composed from SaaS-OS primitives, not a parallel platform** (ADR-0003 point
2): `core.rbac.can()` for authorization, `core.audit_log.record()` for audit,
`voiceagent.telephony.contracts.TelephonyProvider` for the one application
capability this phase implements (call control). See
`voiceagent.tools.gateway`'s module docstring for the one deliberate,
documented deviation from ADR-0003's `core.idempotency.run_idempotent()` --
idempotency here is a bounded, in-memory, per-call mechanism instead, because
`run_idempotent()`'s synchronous, DB-session-scoped `business_fn` shape cannot
host an async `TelephonyProvider` call.

**No dynamic code execution, no generic "run arbitrary tool" escape hatch.**
`voiceagent.tools.registry.TOOL_REGISTRY` holds exactly the four
statically-defined, code-owned tools `voiceagent.tools.handlers` registers at
import time (`call.hangup`, `call.transfer`, `call.hold`, `call.resume`) --
nothing a tenant configures, uploads, or supplies becomes a handler.
"""

from __future__ import annotations

__all__: list[str] = []
