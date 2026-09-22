"""`voiceagent.knowledge.validation.validate_item_references()` (Phase 2.11)
-- pure, DB-free, exactly like `voiceagent.workflows.validation
.validate_tool_references()`."""

from __future__ import annotations

import uuid

import pytest

from voiceagent.knowledge.errors import UnknownKnowledgeItemError
from voiceagent.knowledge.validation import validate_item_references


def test_every_id_available_passes() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    validate_item_references([a, b], {a, b})  # does not raise


def test_empty_item_ids_needs_no_available_set() -> None:
    validate_item_references([], set())  # does not raise


def test_an_id_missing_from_the_available_set_is_rejected() -> None:
    a, missing = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(UnknownKnowledgeItemError) as excinfo:
        validate_item_references([a, missing], {a})
    assert excinfo.value.item_id == missing


def test_reports_the_first_missing_id_in_order() -> None:
    first_missing, second_missing, available = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with pytest.raises(UnknownKnowledgeItemError) as excinfo:
        validate_item_references([first_missing, second_missing], {available})
    assert excinfo.value.item_id == first_missing
