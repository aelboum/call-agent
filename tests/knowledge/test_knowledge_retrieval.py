"""`voiceagent.knowledge.retrieval` (Phase 2.11): the pure token-normalization/
truncation helpers, and the bounded-input paths of `search_items()` that
reject before ever opening `tenant_scope()` -- hermetically testable exactly
like `voiceagent.knowledge.validation`. The tenant/association-scoped
database path itself is proven against a real PostgreSQL instance in
`tests/integration/test_knowledge_integration.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest

from voiceagent.agents.models import AgentVersion
from voiceagent.knowledge.errors import InvalidKnowledgeQueryError
from voiceagent.knowledge.retrieval import (
    MAX_QUERY_LENGTH,
    MAX_QUERY_TOKENS,
    MAX_SNIPPET_LENGTH,
    _tokenize,
    _truncate,
    search_items,
)
from voiceagent.tenancy import TenantContext


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _agent_version(item_ids: list[uuid.UUID] | None = None) -> AgentVersion:
    # search_items() only ever reads `.config` off this object -- a
    # SimpleNamespace duck-types an AgentVersion perfectly for this test
    # without a real database row; cast() documents that, rather than
    # working around it (the same idiom voiceagent.tools.handlers already
    # uses for a ToolHandler's own uniformly-typed parameter).
    config: dict[str, object] = {}
    if item_ids is not None:
        config["knowledge"] = {"item_ids": [str(i) for i in item_ids]}
    return cast(AgentVersion, SimpleNamespace(config=config))


# -- _tokenize ----------------------------------------------------------------


def test_tokenize_lowercases_and_dedupes() -> None:
    assert _tokenize("Hours Hours HOURS") == ("hours",)


def test_tokenize_drops_single_character_tokens() -> None:
    """No stopword list -- only `_MIN_TOKEN_LENGTH` filters anything (brief:
    the smallest architecture that still bounds the work done, not a
    natural-language feature)."""
    assert _tokenize("a hours") == ("hours",)


def test_tokenize_strips_punctuation() -> None:
    assert _tokenize("what are your hours?!") == ("what", "are", "your", "hours")


def test_tokenize_caps_token_count() -> None:
    query = " ".join(f"word{i}" for i in range(MAX_QUERY_TOKENS + 10))
    assert len(_tokenize(query)) == MAX_QUERY_TOKENS


def test_tokenize_of_only_single_character_tokens_is_empty() -> None:
    assert _tokenize("a I") == ()


# -- _truncate ------------------------------------------------------------


def test_truncate_leaves_short_content_untouched() -> None:
    assert _truncate("short") == "short"


def test_truncate_bounds_long_content() -> None:
    content = "x" * (MAX_SNIPPET_LENGTH + 100)
    truncated = _truncate(content)
    assert len(truncated) <= MAX_SNIPPET_LENGTH + 3
    assert truncated.endswith("...")


# -- search_items(): bounded-input rejection, no database access needed -------


def test_empty_query_is_rejected_without_touching_the_database() -> None:
    with pytest.raises(InvalidKnowledgeQueryError):
        search_items(_context(), _agent_version(), query="   ", limit=5)


def test_oversized_query_is_rejected() -> None:
    with pytest.raises(InvalidKnowledgeQueryError):
        search_items(_context(), _agent_version(), query="x" * (MAX_QUERY_LENGTH + 1), limit=5)


def test_query_with_no_usable_token_is_rejected() -> None:
    with pytest.raises(InvalidKnowledgeQueryError):
        search_items(_context(), _agent_version(), query="a I", limit=5)


def test_no_approved_items_returns_empty_without_touching_the_database() -> None:
    """`agent_version.config` has no `"knowledge"` key -- `search_items()`
    must return `[]` before ever opening `tenant_scope()` (there is nothing
    to search for), never raise."""
    assert search_items(_context(), _agent_version(), query="hours", limit=5) == []


def test_empty_item_ids_list_returns_empty_without_touching_the_database() -> None:
    assert search_items(_context(), _agent_version([]), query="hours", limit=5) == []
