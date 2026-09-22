"""Existence/ownership validation for a parsed `KnowledgeConfig` (Phase
2.11).

Deliberately separate from, and DB-free unlike,
`voiceagent.knowledge.service` -- see `voiceagent.knowledge.config`'s module
docstring for why `KnowledgeConfig` itself must stay free of any dependency
on `voiceagent.db`. `validate_item_references()` is pure: it takes the
already-fetched set of ids that are actually valid right now (tenant-owned,
`status='active'`) and compares, never querying anything itself.

`voiceagent.agents.service.create_draft_version()` is the one caller in
production: it fetches that available set with
`voiceagent.knowledge.service.resolve_active_item_ids()` (its own
`tenant_scope()`) and passes it here, exactly the two-step shape
`voiceagent.workflows.validation.validate_tool_references()` already
establishes for `tool_id` (there, the "available" set is
`TOOL_REGISTRY`'s in-memory keys instead of a database query, but the split
of responsibility -- pure comparison here, existence-source at the call site
-- is the same)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence

from voiceagent.knowledge.errors import UnknownKnowledgeItemError

__all__ = ["validate_item_references"]


def validate_item_references(
    item_ids: Sequence[uuid.UUID], available_item_ids: Collection[uuid.UUID]
) -> None:
    """Raises `UnknownKnowledgeItemError` for the first `item_ids` entry, in
    order, not present in `available_item_ids` -- called once, at draft-
    `AgentVersion`-creation time, never at every retrieval: a published
    `AgentVersion` is immutable (ADR-0004), so an `item_id` valid at publish
    time stays a valid *reference* for the version's entire lifetime (whether
    it is still returned by a search is a separate, retrieval-time concern --
    see `voiceagent.knowledge.retrieval`)."""
    available = set(available_item_ids)
    for item_id in item_ids:
        if item_id not in available:
            raise UnknownKnowledgeItemError(item_id)
