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
    from voiceagent.agents.models import AgentVersion
    from voiceagent.runtime.db import DatabaseBoundary
    from voiceagent.telephony.contracts import CallRef, TelephonyProvider
    from voiceagent.tenancy import TenantContext
    from voiceagent.tools.gateway import ToolGateway

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

    `tenant_context`/`db` (Phase 2.6) are the one addition since Phase 2.4:
    a Contact/Calendar tool handler needs to call a synchronous application
    service (`voiceagent.contacts.service`/`voiceagent.calendars.service`),
    which requires the full `TenantContext` `tenant_scope()` takes, not just
    a bare `tenant_id`, and must cross `DatabaseBoundary.run()` rather than
    ever touching `voiceagent.db`/`voiceagent.tenancy.tenant_scope` directly
    from inside `voiceagent.tools` (`tests/architecture
    /test_tool_gateway_isolation.py
    ::test_no_tool_module_imports_voiceagent_db_directly`). Both are the
    exact `db`/`context` values `ToolGateway.execute()` was already handed
    by `voiceagent.runtime.call_task` -- passed through, not re-derived.
    Optional, defaulting to `None`, so the four Phase 2.4 call-control
    handlers (which need neither) are unaffected.

    `agent_version`/`tool_gateway`/`system_service_account_name` (Phase
    2.10) exist for exactly one handler: `voiceagent.tools.handlers
    ._workflow_advance` (`workflow.advance`). A workflow `tool` step must
    invoke another Tool Gateway tool with the identical enforcement --
    allowlist, RBAC, idempotency, audit -- an ordinary model-issued call
    gets (brief STEP TYPES: "For tool steps... enforce its existing
    permission checks"). Rather than re-deriving any of that, the handler
    is simply handed the same `ToolGateway` instance already executing it
    (`voiceagent.tools.gateway.ToolGateway.execute()` passes `self`) and
    calls `.execute()` on it again, recursively, for the one nested tool
    call a workflow step names -- never for `workflow.advance` itself
    (`voiceagent.workflows.config.WorkflowToolStep` rejects that at parse
    time), so this recursion is bounded to depth one. `agent_version` is
    the same object `ToolGateway.execute()` was already handed by
    `voiceagent.runtime.call_task` -- passed through so a workflow handler
    never needs its own database round trip just to re-fetch it.
    """

    tenant_id: uuid.UUID
    call_session_id: uuid.UUID
    agent_version_id: uuid.UUID
    tool_call_id: str
    correlation_id: str
    call_ref: CallRef
    telephony: TelephonyProvider
    tenant_context: TenantContext | None = None
    db: DatabaseBoundary | None = None
    agent_version: AgentVersion | None = None
    tool_gateway: ToolGateway | None = None
    system_service_account_name: str | None = None


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
