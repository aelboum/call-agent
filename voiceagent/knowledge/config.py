"""`KnowledgeConfig` -- the exact, closed shape `AgentVersion.config["knowledge"]`
validates against (Phase 2.11).

Mirrors `voiceagent.workflows.config.WorkflowDefinition` in one important
way and differs in another. The similarity: it lives inside
`voiceagent.agents.config.AgentConfig`, inheriting that field's
immutability-once-published for free (ADR-0004) -- there is no separate
knowledge-association version/lifecycle table. The difference: a workflow
step graph is fully self-contained (every step, transition and predicate is
literal, inline data), whereas `KnowledgeConfig` is a bounded *reference
list* (`item_ids`) pointing at `voiceagent.knowledge.models.KnowledgeItem`
rows that live outside this document entirely.

That reference-not-copy design is the "smallest architecture that preserves
deterministic published-agent behavior" this phase's brief asks for
(choice A: immutable/versioned knowledge items, over choice B: snapshotting
content into the version). It works only because a `KnowledgeItem`'s own
`title`/`content` become immutable the moment it leaves `status='draft'`
(see `voiceagent.knowledge` package docstring) -- an id referenced here can
never come to mean different text later; the alternative (choice B) would
duplicate potentially many kilobytes of tenant text into every
`AgentVersion.config`, for no additional determinism this product does not
already get more cheaply.

**Whether an `item_id` here actually names an existing, tenant-owned,
`status='active'` `KnowledgeItem` is not checked here** -- the same "parsing
is pure, existence needs the database" split `voiceagent.workflows.config`
already establishes for `tool_id` (see that module's docstring). It is
checked by `voiceagent.knowledge.validation.validate_item_references()`,
called from `voiceagent.agents.service.create_draft_version()` before a
draft version is ever persisted. This module has no dependency on
`voiceagent.db`/`voiceagent.knowledge.service` for the identical reason
`voiceagent.workflows.config` has none on `voiceagent.tools`:
`voiceagent.agents.config` is imported by `voiceagent.providers.engines
.factory`, which must never transitively reach the database
(import-linter's "The ConversationEngine never imports conversation
persistence" contract, which forbids `voiceagent.db` outright).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION", "KnowledgeConfig"]

#: A published `AgentVersion` may approve at most this many knowledge items
#: (brief BOUNDS: "bounded"). Also the practical upper bound on how much work
#: `voiceagent.knowledge.retrieval.search_items()` ever does per query: it
#: fetches at most this many candidate rows before filtering in memory (see
#: that module's own docstring for why that is safe and deliberate).
MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION = 50


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeConfig(_Strict):
    """The closed knowledge-association shape. `item_ids` is an ordered,
    de-duplicated, bounded list of `KnowledgeItem.id` values this
    `AgentVersion` approves for retrieval -- never a query, a filter
    expression, or a source id (a source is not itself retrievable; only its
    individual, individually-approved items are)."""

    item_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION
    )

    @field_validator("item_ids")
    @classmethod
    def _no_duplicates(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("knowledge.item_ids must not contain duplicates")
        return value
