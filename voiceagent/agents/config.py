"""`AgentVersion.config` -- the validated boundary of the one JSON field this
platform stores (Phase 2.0 report §9.3; Phase 2.1 brief §9: "If JSON
configuration exists in the approved contract, validate its boundary and do
not treat it as an untyped dumping ground").

`AgentConfig` is the exact documented shape, `extra="forbid"` at every level
this report gives a real shape for -- an unrecognized top-level or nested key
is a validation error, not a silently-accepted passenger. The few fields the
Phase 2.0 report itself leaves as an opaque bag (`voice.settings`,
`engine.*.config`, a tool's own `config`) stay `dict[str, object]`: that is
what the approved contract specifies for them, not an escape hatch this
module invents.

Two things this module deliberately does NOT do, both by design, both
recorded in the Phase 2.0 report:

* **No secret-shaped-key scanning.** The report's §10.3 explicitly assigns
  that defense-in-depth check to "Phase 2.2+, not built now" -- adding it
  here would be scope beyond what was approved.
* **No provider-credential field of any kind**, anywhere in this schema --
  the same rule `voiceagent.config.settings` already applies to deployment
  configuration (Phase 1) applies here to agent configuration: a provider is
  *selected* by name, never authenticated to, from this document.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from voiceagent.workflows.config import WorkflowDefinition

# NOTE: this module deliberately does NOT import voiceagent.tools.handlers
# for TOOL_REGISTRY's registration side effect, even though
# WorkflowDefinition's own tool-id validator needs that registry populated
# to be meaningful -- voiceagent.providers.engines.factory imports this
# module (for EngineSelection) and must never transitively reach
# voiceagent.tools/voiceagent.conversations/voiceagent.db (import-linter's
# "The ConversationEngine never imports the Tool Gateway" contract). The
# side-effect import instead lives in voiceagent.api.v1.agents (the one
# place a workflow-bearing AgentConfig is actually parsed from an untrusted
# request body) and at the top of every hermetic test that constructs a
# WorkflowDefinition tool step directly.

__all__ = ["AgentConfig", "canonical_config_dict", "compute_config_hash"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VoiceRef(_Strict):
    provider: str
    voice_id: str
    settings: dict[str, object] = Field(default_factory=dict)


class EngineComponentConfig(_Strict):
    provider: str
    config: dict[str, object] = Field(default_factory=dict)


class LlmComponentConfig(_Strict):
    provider: str
    model: str
    config: dict[str, object] = Field(default_factory=dict)


class EngineSelection(_Strict):
    kind: Literal["pipelined", "realtime"]
    stt: EngineComponentConfig | None = None
    llm: LlmComponentConfig | None = None
    tts: EngineComponentConfig | None = None
    realtime: EngineComponentConfig | None = None


class ToolBinding(_Strict):
    key: str
    config: dict[str, object] = Field(default_factory=dict)


class TransferRule(_Strict):
    condition: dict[str, object] = Field(default_factory=dict)
    destination_e164: str
    fallback: dict[str, object] = Field(default_factory=dict)


class BusinessHours(_Strict):
    timezone: str
    windows: list[dict[str, object]] = Field(default_factory=list)


class CallLimits(_Strict):
    max_duration_seconds: int | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None


class RecordingSettings(_Strict):
    enabled: bool = False
    announce: bool = False


class PrivacySettings(_Strict):
    data_classification: str
    purpose: str


class AgentConfig(_Strict):
    """The exact shape from Phase 2.0 report §9.3. `workflow` is `None` for
    the prompt-only "no-workflow happy path" (Phase 0 report §3.5) -- when
    present, it is the closed, bounded
    `voiceagent.workflows.config.WorkflowDefinition` shape Phase 2.10 adds
    (a finite, acyclic, five-step-type graph; see that module's own
    docstring), never an arbitrary or opaque document."""

    instructions: str
    greeting: str | None = None
    language: str
    voice: VoiceRef
    engine: EngineSelection
    tools: list[ToolBinding] = Field(default_factory=list)
    workflow: WorkflowDefinition | None = None
    transfer_rules: list[TransferRule] = Field(default_factory=list)
    business_hours: BusinessHours
    call_limits: CallLimits = Field(default_factory=CallLimits)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    privacy: PrivacySettings


def canonical_config_dict(config: AgentConfig) -> dict[str, object]:
    """A plain `dict`, JSON-round-trip-stable, for storage in the `JSON`
    column and for hashing. `mode="json"` ensures every nested value is
    already JSON-primitive (no stray Python types) before it reaches
    `voiceagent.db`."""
    return config.model_dump(mode="json")


def compute_config_hash(config: Mapping[str, object]) -> str:
    """SHA-256 over a canonical (sorted-key, separator-normalized) JSON
    serialization (ADR-0004 §7.1: "two publishes of identical configuration
    ... share a hash"). Deterministic regardless of the dict's original key
    order."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
