"""`/v1/agents` -- the exact endpoint set from Phase 2.0 report §23.9.

No endpoint beyond this list exists: create agent, read one/list, update
mutable fields, create a draft version, publish, archive. No delete route (no
lifecycle-based deletion is specified for `Agent` in Phase 2.0; see Phase 2.1
brief §16).

Every route is authorized through `voiceagent.tenancy.require_tenant()`
(Phase 1, unchanged) -- the platform's own authentication -> tenant
resolution -> rate limiting -> RBAC chain runs ahead of every handler here,
exactly as it does for SaaS-OS's own routes. No handler accepts or trusts a
`tenant_id` from the request; every read/write is scoped to
`context.tenant_id`, which is who SaaS-OS says the caller is, never who a
request body claims to be.

Responses are hand-built Pydantic schemas, never the raw ORM model (Phase 2.1
brief §17): `AgentOut`/`AgentVersionOut` list exactly the fields a client may
see, so a future column added to `Agent`/`AgentVersion` is not silently
serialized until someone adds it here deliberately.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.errors import (
    AgentNotFoundError,
    AgentVersionNotDraftError,
    AgentVersionNotFoundError,
    AgentVersionNotPublishedError,
    InvalidAgentConfigError,
)
from voiceagent.agents.models import Agent, AgentVersion
from voiceagent.agents.permissions import RESOURCE
from voiceagent.agents.service import (
    archive_version,
    create_agent,
    create_draft_version,
    get_agent,
    list_agents,
    publish_version,
    update_agent,
)
from voiceagent.api.errors import conflict, not_found
from voiceagent.tenancy import TenantContext, require_tenant

# Side-effect import: populates voiceagent.tools.registry.TOOL_REGISTRY
# (voiceagent.tools.handlers's own module-level TOOL_REGISTRY.register()
# calls) before an AgentVersionCreateRequest's `config.workflow` (a
# voiceagent.workflows.config.WorkflowDefinition) is ever parsed from a
# request body here -- its own tool-id validator needs the registry
# populated to be meaningful. See voiceagent.agents.config's own module
# docstring for why this import lives here rather than there (this route
# module, unlike voiceagent.agents.config, is never imported by
# voiceagent.providers.engines.factory). Mirrors voiceagent.tools
# .permissions's identical side-effect import.
from voiceagent.tools import handlers as _tool_handlers  # noqa: F401

router = APIRouter(prefix="/agents", tags=["agents"])

_read = require_tenant(RESOURCE, "read")
_write = require_tenant(RESOURCE, "write")


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: str
    draft_version_id: uuid.UUID | None
    published_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, agent: Agent) -> AgentOut:
        return cls.model_validate(agent)


class AgentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    status: str
    config_hash: str
    published_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, version: AgentVersion) -> AgentVersionOut:
        return cls.model_validate(version)


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class AgentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: str | None = None


class AgentVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: AgentConfig


@router.post("", status_code=201)
def create_agent_route(
    payload: AgentCreateRequest,
    context: TenantContext = Depends(_write),  # noqa: B008
) -> AgentOut:
    agent = create_agent(context, name=payload.name, description=payload.description)
    return AgentOut.from_model(agent)


@router.get("/{agent_id}")
def get_agent_route(
    agent_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> AgentOut:
    try:
        agent = get_agent(context, agent_id)
    except AgentNotFoundError:
        raise not_found("agent") from None
    return AgentOut.from_model(agent)


@router.get("")
def list_agents_route(
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[AgentOut]:
    return [AgentOut.from_model(agent) for agent in list_agents(context)]


@router.patch("/{agent_id}")
def update_agent_route(
    agent_id: uuid.UUID,
    payload: AgentUpdateRequest,
    context: TenantContext = Depends(_write),  # noqa: B008 -- FastAPI's own dependency idiom
) -> AgentOut:
    try:
        agent = update_agent(
            context,
            agent_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
        )
    except AgentNotFoundError:
        raise not_found("agent") from None
    return AgentOut.from_model(agent)


@router.post("/{agent_id}/versions", status_code=201)
def create_version_route(
    agent_id: uuid.UUID,
    payload: AgentVersionCreateRequest,
    context: TenantContext = Depends(_write),  # noqa: B008 -- FastAPI's own dependency idiom
) -> AgentVersionOut:
    try:
        version = create_draft_version(context, agent_id, config=payload.config)
    except AgentNotFoundError:
        raise not_found("agent") from None
    except InvalidAgentConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    return AgentVersionOut.from_model(version)


@router.post("/{agent_id}/versions/{version_id}/publish")
def publish_version_route(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    context: TenantContext = Depends(_write),  # noqa: B008 -- FastAPI's own dependency idiom
) -> AgentVersionOut:
    try:
        version = publish_version(context, agent_id, version_id)
    except (AgentNotFoundError, AgentVersionNotFoundError):
        raise not_found("agent version") from None
    except AgentVersionNotDraftError:
        raise conflict("AgentVersion is not a draft.") from None
    return AgentVersionOut.from_model(version)


@router.post("/{agent_id}/versions/{version_id}/archive")
def archive_version_route(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    context: TenantContext = Depends(_write),  # noqa: B008 -- FastAPI's own dependency idiom
) -> AgentVersionOut:
    try:
        version = archive_version(context, agent_id, version_id)
    except (AgentNotFoundError, AgentVersionNotFoundError):
        raise not_found("agent version") from None
    except AgentVersionNotPublishedError:
        raise conflict("AgentVersion is not published.") from None
    return AgentVersionOut.from_model(version)
