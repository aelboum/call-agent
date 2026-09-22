"""`/v1/knowledge` -- the minimal management surface Phase 2.11 needs: create
a source, list sources, create an item, list a source's items, edit a draft
item, activate/deactivate an item.

No delete route, no per-id GET (a client already has the row from
`list_*`/the response of the mutating call that touched it), no debug
retrieval endpoint, and no separate "associate knowledge with an
AgentVersion" endpoint: approving knowledge for an agent is done through the
*existing* `POST /v1/agents/{agent_id}/versions` route
(`voiceagent.api.v1.agents`), whose `AgentVersionCreateRequest.config` is a
`voiceagent.agents.config.AgentConfig` that already carries the optional
`knowledge` field (`voiceagent.knowledge.config.KnowledgeConfig`) -- adding a
second, parallel association endpoint here would only duplicate that one
(brief API: "add only the APIs genuinely required").

Every route is authorized through `voiceagent.tenancy.require_tenant()`, the
same platform authentication -> tenant resolution -> RBAC chain every other
product route already uses (`voiceagent.api.v1.agents`'s own module
docstring, unchanged here).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from voiceagent.api.errors import conflict, not_found
from voiceagent.knowledge.errors import (
    KnowledgeItemNotDraftError,
    KnowledgeItemNotFoundError,
    KnowledgeSourceNotFoundError,
)
from voiceagent.knowledge.models import KnowledgeItem, KnowledgeSource
from voiceagent.knowledge.permissions import RESOURCE_ITEMS, RESOURCE_SOURCES
from voiceagent.knowledge.service import (
    activate_item,
    create_item,
    create_source,
    deactivate_item,
    list_items,
    list_sources,
    update_item,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_sources_read = require_tenant(RESOURCE_SOURCES, "read")
_sources_write = require_tenant(RESOURCE_SOURCES, "create")
_items_read = require_tenant(RESOURCE_ITEMS, "read")
_items_create = require_tenant(RESOURCE_ITEMS, "create")
_items_update = require_tenant(RESOURCE_ITEMS, "update")
_items_activate = require_tenant(RESOURCE_ITEMS, "activate")
_items_deactivate = require_tenant(RESOURCE_ITEMS, "deactivate")


class KnowledgeSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, source: KnowledgeSource) -> KnowledgeSourceOut:
        return cls.model_validate(source)


class KnowledgeItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    title: str
    content: str
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, item: KnowledgeItem) -> KnowledgeItemOut:
        return cls.model_validate(item)


class KnowledgeSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class KnowledgeItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)


class KnowledgeItemUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)


@router.post("/sources", status_code=201)
def create_source_route(
    payload: KnowledgeSourceCreateRequest,
    context: TenantContext = Depends(_sources_write),  # noqa: B008
) -> KnowledgeSourceOut:
    source = create_source(context, name=payload.name, description=payload.description)
    return KnowledgeSourceOut.from_model(source)


@router.get("/sources")
def list_sources_route(
    context: TenantContext = Depends(_sources_read),  # noqa: B008
) -> list[KnowledgeSourceOut]:
    return [KnowledgeSourceOut.from_model(source) for source in list_sources(context)]


@router.post("/sources/{source_id}/items", status_code=201)
def create_item_route(
    source_id: uuid.UUID,
    payload: KnowledgeItemCreateRequest,
    context: TenantContext = Depends(_items_create),  # noqa: B008
) -> KnowledgeItemOut:
    try:
        item = create_item(context, source_id, title=payload.title, content=payload.content)
    except KnowledgeSourceNotFoundError:
        raise not_found("knowledge source") from None
    return KnowledgeItemOut.from_model(item)


@router.get("/sources/{source_id}/items")
def list_items_route(
    source_id: uuid.UUID,
    context: TenantContext = Depends(_items_read),  # noqa: B008
) -> list[KnowledgeItemOut]:
    try:
        items = list_items(context, source_id)
    except KnowledgeSourceNotFoundError:
        raise not_found("knowledge source") from None
    return [KnowledgeItemOut.from_model(item) for item in items]


@router.patch("/items/{item_id}")
def update_item_route(
    item_id: uuid.UUID,
    payload: KnowledgeItemUpdateRequest,
    context: TenantContext = Depends(_items_update),  # noqa: B008
) -> KnowledgeItemOut:
    try:
        item = update_item(context, item_id, title=payload.title, content=payload.content)
    except KnowledgeItemNotFoundError:
        raise not_found("knowledge item") from None
    except KnowledgeItemNotDraftError:
        raise conflict("KnowledgeItem is not a draft.") from None
    return KnowledgeItemOut.from_model(item)


@router.post("/items/{item_id}/activate")
def activate_item_route(
    item_id: uuid.UUID,
    context: TenantContext = Depends(_items_activate),  # noqa: B008
) -> KnowledgeItemOut:
    try:
        item = activate_item(context, item_id)
    except KnowledgeItemNotFoundError:
        raise not_found("knowledge item") from None
    except KnowledgeItemNotDraftError:
        raise conflict("KnowledgeItem is not a draft.") from None
    return KnowledgeItemOut.from_model(item)


@router.post("/items/{item_id}/deactivate")
def deactivate_item_route(
    item_id: uuid.UUID,
    context: TenantContext = Depends(_items_deactivate),  # noqa: B008
) -> KnowledgeItemOut:
    try:
        item = deactivate_item(context, item_id)
    except KnowledgeItemNotFoundError:
        raise not_found("knowledge item") from None
    except KnowledgeItemNotDraftError:
        raise conflict("KnowledgeItem is not active.") from None
    return KnowledgeItemOut.from_model(item)
