"""`app.conversation_turns` (Phase 2.5).

One row per durable conversation-turn: a finalized system/user/assistant
message, or a tool call/result pair, in provider-neutral shape -- never a
vendor SDK type, never raw audio, never a partial (still-revisable) STT
result.

**No separate `conversations` table.** Phase 2.0's report (§11.2) forecast
one alongside `conversation_turns`, but this product has no concept of a
conversation independent of the call that carries it -- one `CallSession` is
always exactly one conversation, with no multi-session continuation or
merge. A wrapper table whose only content would be `(id, tenant_id,
call_session_id)` duplicates `CallSession` for no independent data, which
Phase 2.5's own brief section 9 rules out ("do not introduce tables for
future features that are not part of this phase"). `call_session_id` is
therefore this table's own conversation identity.

**Ordering** (Phase 2.5 brief section 5): `sequence` is a plain per-call
`Integer`, assigned by `voiceagent.conversations.service
.persist_conversation_turn()` under a `SELECT ... FOR UPDATE` lock on the
owning `CallSession` row -- the same locking primitive
`voiceagent.calls.service.claim_runtime_ownership()` already uses for its own
read-then-write invariant. Never a timestamp: two turns persisted within the
same clock tick must still sort deterministically.

**Idempotency**: `UniqueConstraint(call_session_id, event_id, role)` --
`role`, not just `event_id`, because a tool call's own "tool_call" and
"tool_result" turns share one `event_id`
(`ToolCallRequested.call_id`/`ToolResult.call_id`, Phase 1/2.2 -- see
`voiceagent.conversations.service.persist_conversation_turn()`'s own
docstring for why that reuse is intentional): without `role` in the
constraint, persisting the "tool_result" turn would be indistinguishable
from a duplicate delivery of the "tool_call" turn and would be silently
dropped as a no-op -- caught by this table's own integration tests against
a real database, not merely reasoned about. `event_id` is the stable,
provider-neutral id every persistable `EngineEvent` now carries
(`voiceagent.providers.engines.contracts`) -- never a provider SDK id, and
never regenerated per persistence retry (generated once, at event
construction).
"""

from __future__ import annotations

import uuid

from voiceagent.db import (
    JSON,
    Base,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Mapped,
    String,
    Text,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["ROLES", "ConversationTurn"]

#: The five provider-neutral turn shapes Phase 2.5's brief section 2B
#: requires, and exactly those -- no vendor-specific role, no generic
#: "event" shape.
ROLES = frozenset({"system", "user", "assistant", "tool_call", "tool_result"})

_PAYLOAD_SHAPE_CHECK = (
    "(role IN ('system', 'user', 'assistant') AND content IS NOT NULL "
    "AND tool_payload IS NULL) OR "
    "(role IN ('tool_call', 'tool_result') AND tool_payload IS NOT NULL)"
)


class ConversationTurn(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One durable conversation turn (Phase 2.5 report, "Conversation
    model"). Insert-only: nothing in this module updates an existing row --
    `TimestampMixin.updated_at` is present only because it is a shared
    mixin, never written to a second value in practice."""

    __tablename__ = "conversation_turns"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: The stable, provider-neutral idempotency key a persistable
    #: `EngineEvent` carries (its own `event_id`, or a `ToolCallRequested`/
    #: `ToolResult.call_id`) -- never a provider SDK id.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Per-call monotonic order -- see module docstring, "Ordering".
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Structured tool-call/tool-result payload (`{"name", "arguments"}` or
    #: `{"value", "error_code"}`) -- never a raw provider SDK object.
    #: `JSON(none_as_null=True)`, not bare `JSON`: SQLAlchemy's own default
    #: for a JSON-typed column is `none_as_null=False`, which binds a Python
    #: `None` as the *JSON* literal `null` rather than SQL `NULL` --
    #: verified against a real PostgreSQL 16 instance, where the bare-`JSON`
    #: version of this column made `ck_conversation_turns_payload_shape`'s
    #: own `tool_payload IS NULL` clause false for every system/user/
    #: assistant turn, rejecting every one of them at insert time.
    tool_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_conversation_turns_call_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "call_session_id", "event_id", "role", name="uq_conversation_turns_call_event_role"
        ),
        UniqueConstraint("call_session_id", "sequence", name="uq_conversation_turns_call_sequence"),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool_call', 'tool_result')",
            name="ck_conversation_turns_role",
        ),
        CheckConstraint(_PAYLOAD_SHAPE_CHECK, name="ck_conversation_turns_payload_shape"),
        Index("ix_conversation_turns_tenant_id", "tenant_id"),
        Index("ix_conversation_turns_call_session_sequence", "call_session_id", "sequence"),
    )
