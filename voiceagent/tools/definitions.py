"""`ToolDefinition` -- a typed, immutable, code-owned tool declaration
(ADR-0003 point 5; Phase 2.4 brief section 3).

A tool's input and output shape is a Pydantic model, not a hand-written JSON
Schema dict: validation stays strongly typed internally (the brief's own
requirement), and `ToolDefinition.input_schema`/`output_schema` derive the
JSON-Schema view from that same model (`model_json_schema()`) for the one
place a schema, not a type, is genuinely needed -- advertising the tool to an
LLM as a `voiceagent.providers.engines.contracts.ToolSpec`. One source of
truth, two representations; never a schema hand-maintained separately from
the model that actually validates against it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from voiceagent.telephony.contracts import CallRef, TelephonyProvider

__all__ = [
    "StrictToolModel",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolHandler",
    "ToolRisk",
]


class StrictToolModel(BaseModel):
    """The base every tool's input/output model extends.

    `extra="forbid"`: an argument the model did not declare (including an
    attempted `tenant_id`, `call_session_id`, or anything else that looks
    like an identity/scope field) is a validation error, never a silently
    accepted passenger -- the same discipline
    `voiceagent.agents.config._Strict` already applies to `AgentVersion
    .config`, applied here to the one other place untrusted structured input
    (model-supplied tool arguments) crosses into this product.
    """

    model_config = ConfigDict(extra="forbid")


class ToolRisk(StrEnum):
    """A tool's execution/side-effect classification (ADR-0003 point 5's
    "side-effect class", narrowed to what Phase 2.4's four call-control tools
    need -- not a general risk-scoring system)."""

    #: Reversible, no durable effect outside this call (e.g. hold/resume).
    LOW = "low"
    #: Ends or redirects the call itself -- irreversible from the caller's
    #: side once acted on (hangup, transfer).
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """What a handler receives, alongside its validated typed input.

    Deliberately narrow: a handler sees only what it needs to call a
    `TelephonyProvider` method for *this* call -- never a `TenantContext`,
    never a database session, never another call's data. `tenant_id` is
    included for logging/audit correlation *inside* a handler only; a
    handler must never use it to look anything up itself (there is nothing
    in this package for it to look up with -- no DB access exists below the
    gateway).

    Every field here is server-resolved before the gateway ever calls a
    handler (Phase 2.4 brief section 7: "never infer tenant identity from
    user-controlled tool arguments") -- none of it comes from
    `ToolCallRequested.arguments`.
    """

    tenant_id: uuid.UUID
    call_session_id: uuid.UUID
    agent_version_id: uuid.UUID
    tool_call_id: str
    correlation_id: str
    call_ref: CallRef
    telephony: TelephonyProvider


type ToolHandler = Callable[
    [ToolExecutionContext, StrictToolModel], Awaitable[Mapping[str, object]]
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A statically registered, code-owned application capability (Phase 2.4
    brief section 3). Never constructed from tenant configuration -- every
    instance in this product is built once, by `voiceagent.tools.handlers`,
    at import time.
    """

    tool_id: str
    name: str
    description: str
    input_model: type[StrictToolModel]
    output_model: type[StrictToolModel]
    #: The `core.rbac` action checked against `voiceagent.tools.gateway
    #: .RESOURCE` -- one permission per tool (ADR-0003 point 6: holding the
    #: permission is one of *two* independent gates, the other being the
    #: AgentVersion allowlist).
    permission_action: str
    risk: ToolRisk
    #: Whether a duplicate delivery of the *same* `ToolCallRequested.call_id`
    #: is safe to answer from a cached result rather than re-run (Phase 2.4
    #: brief section 8). All four Phase 2.4 tools are idempotent by this
    #: definition -- a second `call.hangup` for a call already ending is a
    #: safe no-op to report identically, not a reason to call
    #: `TelephonyProvider.hangup()` twice.
    idempotent: bool
    timeout_seconds: float
    handler: ToolHandler

    @property
    def input_schema(self) -> Mapping[str, object]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> Mapping[str, object]:
        return self.output_model.model_json_schema()
