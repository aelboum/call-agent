"""Bounded, tenant-and-association-scoped lexical retrieval (Phase 2.11).

`search_items()` is the one and only read path from an AI-issued query to
tenant knowledge text. It is deliberately not PostgreSQL full-text search or
`ILIKE`-at-the-database-layer: `voiceagent.db` (ADR-0007) re-exports no
boolean combinator (`sqlalchemy.or_`/`and_`) an OR-across-tokens `WHERE`
clause would need -- only what SaaS-OS's own `infra.db` sanctions, which
stops at single-predicate ORM comparators. Rather than reaching around that
seam, this module fetches the small, already-bounded candidate set a query
could ever match -- at most `voiceagent.knowledge.config
.MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION` (50) rows, the same ceiling that
already bounds what one `AgentVersion` can approve -- and matches tokens
against `title`/`content` in Python. This is "normalized text matching"
(brief SEARCH DESIGN's first suggested approach), not a compromise: with the
candidate set capped at 50 rows, an in-memory scan is cheaper and exactly as
deterministic as a database-side one, and it needs nothing this seam does
not already provide.

**Every bound here is enforced before a query ever reaches the database**:
`MAX_QUERY_LENGTH` (raw input size), `MAX_QUERY_TOKENS`/`MIN_TOKEN_LENGTH`
(after normalization), `MAX_RESULT_LIMIT` (results returned),
`MAX_SNIPPET_LENGTH` (content per result). None of these is caller-
adjustable upward -- `limit` is clamped, never trusted (brief LLM CONTEXT
BOUNDARY: "never... arbitrary limits beyond the bounded maximum").

**Association + status are both re-checked on every call, never cached**: a
result must be in `agent_version.config["knowledge"]["item_ids"]` *and*
`KnowledgeItem.status == 'active'` *and* its own `KnowledgeSource.status ==
'active'`, all scoped to `context.tenant_id` by `tenant_scope()`/RLS. An item
deactivated after an `AgentVersion` was published stops being retrievable
immediately -- the reference in `config["knowledge"]` stays valid (brief
determinism requirement), but nothing forces a `deactivate_item()` to keep
surfacing content a tenant no longer wants an agent to say aloud.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from voiceagent.db import select
from voiceagent.knowledge.errors import InvalidKnowledgeQueryError
from voiceagent.knowledge.models import KnowledgeItem, KnowledgeSource
from voiceagent.tenancy import TenantContext, tenant_scope

if TYPE_CHECKING:
    from voiceagent.agents.models import AgentVersion

__all__ = [
    "MAX_QUERY_LENGTH",
    "MAX_RESULT_LIMIT",
    "MAX_SNIPPET_LENGTH",
    "KnowledgeSearchResult",
    "search_items",
]

#: Raw, pre-normalization input bound (brief: "maximum query length").
MAX_QUERY_LENGTH = 500
#: Server-side ceiling on results returned, regardless of a caller-requested
#: `limit` (brief: "maximum result count" / never "arbitrary limits beyond
#: the bounded maximum").
MAX_RESULT_LIMIT = 5
#: Per-result content bound, applied after a match is found -- the retrieval
#: result returns only the minimum an engine turn needs, never a whole
#: `KnowledgeItem.content` verbatim if it happens to be long (brief: "bounded
#: returned content size").
MAX_SNIPPET_LENGTH = 500
#: At most this many distinct tokens are matched -- an unbounded token count
#: from a very long query would otherwise make one search do unbounded work
#: even within the already-small candidate set.
MAX_QUERY_TOKENS = 10
_MIN_TOKEN_LENGTH = 2
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """The minimum an engine turn needs to cite one matched item -- never
    the full `KnowledgeItem` row, never `source_id`/`item_id` framed as
    something the model can feed back into a *different* tool call as an
    arbitrary database identifier (brief LLM CONTEXT BOUNDARY)."""

    item_id: uuid.UUID
    source_id: uuid.UUID
    title: str
    snippet: str


def _tokenize(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_PATTERN.findall(query.lower()):
        if len(token) < _MIN_TOKEN_LENGTH or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= MAX_QUERY_TOKENS:
            break
    return tuple(tokens)


def _truncate(content: str) -> str:
    if len(content) <= MAX_SNIPPET_LENGTH:
        return content
    return content[:MAX_SNIPPET_LENGTH].rstrip() + "..."


def _approved_item_ids(agent_version: AgentVersion) -> tuple[uuid.UUID, ...]:
    """Reads `agent_version.config["knowledge"]["item_ids"]` directly, not
    through `voiceagent.knowledge.config.KnowledgeConfig.model_validate()` --
    a published `AgentVersion.config` is already known-valid JSON (it could
    not have been published otherwise), so re-parsing it through pydantic on
    every retrieval call would be pure overhead; the identical shortcut
    `voiceagent.workflows.executor` does not take only because
    `WorkflowDefinition` carries real structure (steps/edges) retrieval logic
    branches on, whereas this is a flat id list."""
    knowledge_config = agent_version.config.get("knowledge")
    if not knowledge_config:
        return ()
    raw_ids = knowledge_config.get("item_ids") or []
    return tuple(uuid.UUID(str(raw_id)) for raw_id in raw_ids)


def search_items(
    context: TenantContext, agent_version: AgentVersion, *, query: str, limit: int
) -> list[KnowledgeSearchResult]:
    """Raises `InvalidKnowledgeQueryError` for an empty/oversized query or one
    with no usable token; otherwise always returns a list (possibly empty --
    "no results" is not an error)."""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise InvalidKnowledgeQueryError()
    tokens = _tokenize(query)
    if not tokens:
        raise InvalidKnowledgeQueryError()

    bounded_limit = max(1, min(int(limit), MAX_RESULT_LIMIT))

    approved_item_ids = _approved_item_ids(agent_version)
    if not approved_item_ids:
        return []

    with tenant_scope(context) as session:
        candidates = (
            session.execute(
                select(KnowledgeItem)
                .where(KnowledgeItem.tenant_id == context.tenant_id)
                .where(KnowledgeItem.id.in_(approved_item_ids))
                .where(KnowledgeItem.status == "active")
            )
            .scalars()
            .all()
        )
        if not candidates:
            return []

        source_ids = {item.source_id for item in candidates}
        active_source_ids = set(
            session.execute(
                select(KnowledgeSource.id)
                .where(KnowledgeSource.tenant_id == context.tenant_id)
                .where(KnowledgeSource.id.in_(source_ids))
                .where(KnowledgeSource.status == "active")
            )
            .scalars()
            .all()
        )
        candidates = [item for item in candidates if item.source_id in active_source_ids]
        for item in candidates:
            session.expunge(item)

    matched = [
        item
        for item in candidates
        if all(token in f"{item.title}\n{item.content}".lower() for token in tokens)
    ]
    # Deterministic order (brief: "deterministic enough to reason about"),
    # never database row order -- (title, id) is stable across re-runs of an
    # identical query against identical data.
    matched.sort(key=lambda item: (item.title, str(item.id)))

    return [
        KnowledgeSearchResult(
            item_id=item.id,
            source_id=item.source_id,
            title=item.title,
            snippet=_truncate(item.content),
        )
        for item in matched[:bounded_limit]
    ]
