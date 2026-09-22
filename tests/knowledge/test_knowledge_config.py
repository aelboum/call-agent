"""`voiceagent.knowledge.config.KnowledgeConfig` (Phase 2.11) -- pure,
DB-free pydantic validation, hermetically testable exactly like
`voiceagent.workflows.config.WorkflowDefinition`."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from voiceagent.knowledge.config import MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION, KnowledgeConfig


def test_empty_item_ids_is_the_default() -> None:
    config = KnowledgeConfig.model_validate({})
    assert config.item_ids == []


def test_item_ids_round_trips() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    config = KnowledgeConfig.model_validate({"item_ids": [str(i) for i in ids]})
    assert config.item_ids == ids


def test_duplicate_item_ids_are_rejected() -> None:
    dup = uuid.uuid4()
    with pytest.raises(ValidationError):
        KnowledgeConfig.model_validate({"item_ids": [str(dup), str(dup)]})


def test_more_than_the_bound_is_rejected() -> None:
    ids = [str(uuid.uuid4()) for _ in range(MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION + 1)]
    with pytest.raises(ValidationError):
        KnowledgeConfig.model_validate({"item_ids": ids})


def test_exactly_the_bound_is_accepted() -> None:
    ids = [str(uuid.uuid4()) for _ in range(MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION)]
    config = KnowledgeConfig.model_validate({"item_ids": ids})
    assert len(config.item_ids) == MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        KnowledgeConfig.model_validate({"item_ids": [], "query": "arbitrary"})


def test_config_is_frozen() -> None:
    config = KnowledgeConfig.model_validate({})
    with pytest.raises(ValidationError):
        config.item_ids = [uuid.uuid4()]  # type: ignore[misc]
