"""Application service for `KnowledgeSource`/`KnowledgeItem` (Phase 2.11).

Every function takes a verified `voiceagent.tenancy.TenantContext` and opens
its own `tenant_scope()` -- no function accepts a bare `tenant_id` (the same
discipline every other `voiceagent.*.service` module already follows).

**Status transitions are the only way `title`/`content` are ever considered
"published"**: `create_item()` always inserts `status='draft'`;
`update_item()` refuses once the row has left `draft`
(`KnowledgeItemNotDraftError`, checked here so the caller gets a normal
domain error instead of `app.forbid_knowledge_item_content_update`'s raw
trigger exception); `activate_item()`/`deactivate_item()` change only
`status`, never `title`/`content`. See `voiceagent.knowledge` package
docstring for why this is what makes a `KnowledgeConfig.item_ids` reference
deterministic without content-snapshotting.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event

from voiceagent.db import select
from voiceagent.knowledge.errors import (
    KnowledgeItemNotDraftError,
    KnowledgeItemNotFoundError,
    KnowledgeSourceNotFoundError,
)
from voiceagent.knowledge.models import KnowledgeItem, KnowledgeSource
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "activate_item",
    "create_item",
    "create_source",
    "deactivate_item",
    "get_item",
    "get_source",
    "list_items",
    "list_sources",
    "resolve_active_item_ids",
    "update_item",
]


def _get_source_row(session, tenant_id: uuid.UUID, source_id: uuid.UUID) -> KnowledgeSource:
    row = session.get(KnowledgeSource, source_id)
    if row is None or row.tenant_id != tenant_id:
        raise KnowledgeSourceNotFoundError(source_id)
    return row


def _get_item_row(session, tenant_id: uuid.UUID, item_id: uuid.UUID) -> KnowledgeItem:
    row = session.get(KnowledgeItem, item_id)
    if row is None or row.tenant_id != tenant_id:
        raise KnowledgeItemNotFoundError(item_id)
    return row


def create_source(
    context: TenantContext, *, name: str, description: str | None = None
) -> KnowledgeSource:
    with tenant_scope(context) as session:
        source = KnowledgeSource(tenant_id=context.tenant_id, name=name, description=description)
        session.add(source)
        session.flush()
        session.refresh(source)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="knowledge.source_created",
            resource_type="knowledge_source",
            resource_id=str(source.id),
            outcome=AuditOutcome.SUCCESS,
        )

        session.expunge(source)
        return source


def get_source(context: TenantContext, source_id: uuid.UUID) -> KnowledgeSource:
    with tenant_scope(context) as session:
        source = _get_source_row(session, context.tenant_id, source_id)
        session.expunge(source)
        return source


def list_sources(context: TenantContext) -> Sequence[KnowledgeSource]:
    with tenant_scope(context) as session:
        sources = (
            session.execute(
                select(KnowledgeSource).where(KnowledgeSource.tenant_id == context.tenant_id)
            )
            .scalars()
            .all()
        )
        for source in sources:
            session.expunge(source)
        return sources


def create_item(
    context: TenantContext, source_id: uuid.UUID, *, title: str, content: str
) -> KnowledgeItem:
    """Always inserts `status='draft'` -- there is no way to create an
    already-`active` item; every item is reviewed once, in draft, before it
    can ever be retrieved (brief KNOWLEDGE MODEL / TESTING: "creation,
    validation, status transitions")."""
    with tenant_scope(context) as session:
        _get_source_row(session, context.tenant_id, source_id)
        item = KnowledgeItem(
            tenant_id=context.tenant_id,
            source_id=source_id,
            title=title,
            content=content,
            status="draft",
        )
        session.add(item)
        session.flush()
        session.refresh(item)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="knowledge.item_created",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            outcome=AuditOutcome.SUCCESS,
            metadata={"source_id": str(source_id)},
        )

        session.expunge(item)
        return item


def get_item(context: TenantContext, item_id: uuid.UUID) -> KnowledgeItem:
    with tenant_scope(context) as session:
        item = _get_item_row(session, context.tenant_id, item_id)
        session.expunge(item)
        return item


def list_items(context: TenantContext, source_id: uuid.UUID) -> Sequence[KnowledgeItem]:
    with tenant_scope(context) as session:
        _get_source_row(session, context.tenant_id, source_id)  # 404 if foreign/missing
        items = (
            session.execute(
                select(KnowledgeItem)
                .where(KnowledgeItem.tenant_id == context.tenant_id)
                .where(KnowledgeItem.source_id == source_id)
            )
            .scalars()
            .all()
        )
        for item in items:
            session.expunge(item)
        return items


def update_item(
    context: TenantContext,
    item_id: uuid.UUID,
    *,
    title: str | None = None,
    content: str | None = None,
) -> KnowledgeItem:
    """`title`/`content` may only change while `status='draft'` -- see module
    docstring. Raises `KnowledgeItemNotDraftError` otherwise, never lets the
    UPDATE reach the database only to have `app.forbid_knowledge_item_content_update`
    reject it."""
    with tenant_scope(context) as session:
        item = _get_item_row(session, context.tenant_id, item_id)
        if item.status != "draft" and (title is not None or content is not None):
            raise KnowledgeItemNotDraftError(item_id, item.status)
        if title is not None:
            item.title = title
        if content is not None:
            item.content = content
        session.flush()
        session.refresh(item)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="knowledge.item_updated",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            outcome=AuditOutcome.SUCCESS,
        )

        session.expunge(item)
        return item


def activate_item(context: TenantContext, item_id: uuid.UUID) -> KnowledgeItem:
    """`draft -> active`: from this call forward, `title`/`content` are
    frozen (`app.forbid_knowledge_item_content_update`) and the item becomes
    eligible for `voiceagent.knowledge.retrieval.search_items()`."""
    with tenant_scope(context) as session:
        item = _get_item_row(session, context.tenant_id, item_id)
        if item.status != "draft":
            raise KnowledgeItemNotDraftError(item_id, item.status)
        item.status = "active"
        session.flush()
        session.refresh(item)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="knowledge.item_activated",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            outcome=AuditOutcome.SUCCESS,
        )

        session.expunge(item)
        return item


def deactivate_item(context: TenantContext, item_id: uuid.UUID) -> KnowledgeItem:
    """`active -> archived`. Never removes the row (an already-published
    `AgentVersion.config["knowledge"]["item_ids"]` may still reference it --
    the reference itself stays valid; only future retrieval excludes it, see
    `voiceagent.knowledge.retrieval`)."""
    with tenant_scope(context) as session:
        item = _get_item_row(session, context.tenant_id, item_id)
        if item.status != "active":
            raise KnowledgeItemNotDraftError(item_id, item.status)
        item.status = "archived"
        session.flush()
        session.refresh(item)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="knowledge.item_deactivated",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            outcome=AuditOutcome.SUCCESS,
        )

        session.expunge(item)
        return item


def resolve_active_item_ids(
    context: TenantContext, item_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of `item_ids` that exist, belong to `context.tenant_id`,
    and are `status='active'` right now -- the "available set"
    `voiceagent.knowledge.validation.validate_item_references()` compares
    against. A point-in-time snapshot, not a transactional guarantee: the
    identical, already-accepted race
    `voiceagent.workflows.validation.validate_tool_references()` has against
    `TOOL_REGISTRY` (an item deactivated between this call and the
    surrounding `create_draft_version()` write is a narrow, accepted gap, not
    a security boundary -- retrieval re-checks `status='active'` on every
    call regardless, so nothing here is a lasting authorization decision)."""
    if not item_ids:
        return set()
    with tenant_scope(context) as session:
        rows = (
            session.execute(
                select(KnowledgeItem.id)
                .where(KnowledgeItem.tenant_id == context.tenant_id)
                .where(KnowledgeItem.id.in_(item_ids))
                .where(KnowledgeItem.status == "active")
            )
            .scalars()
            .all()
        )
        return set(rows)
