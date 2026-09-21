"""`ConversationTurn` schema shape (SQLAlchemy metadata introspection, no
database needed) -- mirrors `tests/calls/test_call_session_models.py`."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table

from voiceagent.conversations.models import ROLES, ConversationTurn


def test_conversation_turns_table_shape() -> None:
    table = cast(Table, ConversationTurn.__table__)
    assert table.schema == "app"
    assert table.name == "conversation_turns"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "call_session_id",
        "event_id",
        "sequence",
        "role",
        "content",
        "tool_payload",
        "created_at",
        "updated_at",
    }
    for required in ("tenant_id", "call_session_id", "event_id", "sequence", "role"):
        assert not table.columns[required].nullable, required
    for optional in ("content", "tool_payload"):
        assert table.columns[optional].nullable, optional


def test_no_conversations_table_exists() -> None:
    """`voiceagent/conversations/models.py`'s own module docstring: one call
    is always exactly one conversation, so `call_session_id` is this
    table's own conversation identity -- no separate `conversations`
    wrapper table."""
    from voiceagent.db import Base

    assert "app.conversations" not in Base.metadata.tables


def test_role_check_constraint_lists_exactly_the_five_roles() -> None:
    table = cast(Table, ConversationTurn.__table__)
    (role_check_constraint,) = [
        c
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_conversation_turns_role"
    ]
    role_check = role_check_constraint.sqltext.text
    for role in ROLES:
        assert f"'{role}'" in role_check


def test_roles_are_exactly_the_five_provider_neutral_shapes() -> None:
    assert ROLES == {"system", "user", "assistant", "tool_call", "tool_result"}


def test_composite_fk_into_call_sessions_is_tenant_aware() -> None:
    table = cast(Table, ConversationTurn.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert len(composite) == 1
    (fk,) = composite
    local = {col.name for col in fk.columns}
    assert local == {"call_session_id", "tenant_id"}
    assert next(iter(fk.elements)).column.table.name == "call_sessions"


def test_call_session_fk_cascades_on_delete() -> None:
    table = cast(Table, ConversationTurn.__table__)
    (fk,) = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert fk.ondelete == "CASCADE"


def test_idempotency_and_ordering_unique_constraints_exist() -> None:
    from sqlalchemy import UniqueConstraint

    table = cast(Table, ConversationTurn.__table__)
    unique_column_sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert frozenset({"call_session_id", "event_id", "role"}) in unique_column_sets
    assert frozenset({"call_session_id", "sequence"}) in unique_column_sets


def test_payload_shape_check_constraint_exists() -> None:
    table = cast(Table, ConversationTurn.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("tool_payload" in text for text in checks)
