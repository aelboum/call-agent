"""Domain errors for `voiceagent.knowledge` (Phase 2.11)."""

from __future__ import annotations

import uuid

__all__ = [
    "InvalidKnowledgeQueryError",
    "KnowledgeError",
    "KnowledgeItemNotDraftError",
    "KnowledgeItemNotFoundError",
    "KnowledgeSourceNotFoundError",
    "UnknownKnowledgeItemError",
]


class KnowledgeError(Exception):
    """Base class for every knowledge domain error."""


class KnowledgeSourceNotFoundError(KnowledgeError):
    def __init__(self, source_id: object) -> None:
        super().__init__(f"KnowledgeSource not found: {source_id}")


class KnowledgeItemNotFoundError(KnowledgeError):
    def __init__(self, item_id: object) -> None:
        super().__init__(f"KnowledgeItem not found: {item_id}")


class KnowledgeItemNotDraftError(KnowledgeError):
    """A caller tried to edit `title`/`content` on an item that has already
    left `status='draft'` -- refused here, at the application layer, before
    the identical rejection `app.forbid_knowledge_item_content_update`
    (migration `0009`) would raise anyway, so the caller gets a normal
    domain error instead of a raw database trigger exception."""

    def __init__(self, item_id: object, status: str) -> None:
        super().__init__(f"KnowledgeItem {item_id} is {status!r}; content is immutable")


class UnknownKnowledgeItemError(KnowledgeError):
    """A `voiceagent.knowledge.config.KnowledgeConfig.item_ids` entry does not
    name an existing, tenant-owned, `status='active'` `KnowledgeItem` at the
    moment a draft `AgentVersion` is created
    (`voiceagent.knowledge.validation.validate_item_references()`)."""

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(f"unknown or unavailable knowledge item id: {item_id}")
        self.item_id = item_id


class InvalidKnowledgeQueryError(KnowledgeError):
    """`voiceagent.knowledge.retrieval.search_items()`'s own bounded-input
    contract was violated -- an empty query, one over
    `voiceagent.knowledge.retrieval.MAX_QUERY_LENGTH`, or one with no usable
    token after normalization. Never raised for "no results found", which is
    an empty list, not an error."""
