"""`assemble_call_context()` -- the controlled `ContextAssembler` (Phase
2.11).

Combines only approved, already-persisted sources into one bounded,
deterministic, inspectable structure: the governing `AgentVersion`'s own
`instructions`, the call's own lifecycle state, its outcome (if any), its
associated contact (if any), and a bounded knowledge search (if a query is
given). It never queries a table directly -- every field comes from an
existing application service (`voiceagent.calls.service`,
`voiceagent.contacts.service`, `voiceagent.followups.service`,
`voiceagent.knowledge.retrieval`), each already tenant-scoped and RLS-backed
on its own.

**Not a conversation dump.** There is no conversation-turn field here at all
(brief: "Do not dump the entire conversation into every LLM request"); a
future phase that needs a bounded prior-turn summary must add its own
explicit, size-bounded field here, never widen this one to "recent turns".
**Not memory.** Nothing this function returns is written back anywhere --
`CallContext` is read, assembled fresh, and discarded; there is no
autonomous memory creation, no cross-call persistence (brief NON-GOALS).

**Privacy boundary (brief PRIVACY).** `voiceagent.runtime.privacy
.authorize_call_data_access()` already ran once for this call, before its
`ConversationEngine` ever started (Phase 2.0 report §14.1's ordering
invariant) -- if this function is reachable at all, that gate has already
passed for this `call_session_id`. This mirrors the exact precedent
`voiceagent.tools.handlers._lookup_contact_by_phone`/`_check_availability`
already set in Phase 2.6: neither re-checks `authorize_data_access()` either,
because both are reachable only from a call already past that one-per-call
gate. This module adds no second privacy system and does not bypass the
first -- it relies on the same ordering guarantee every other call-scoped
application-service caller already relies on.

**Prompt-injection boundary (brief).** `CallContext.knowledge` and
`.contact` are returned as separate, typed, plain-data fields -- never
concatenated into `agent_instructions` or into any single string this module
produces. A caller that assembles a final prompt is responsible for
presenting knowledge/contact content to the model as clearly-labeled
reference *data* (System instructions > Agent configuration > Tool/security
policy > Retrieved knowledge DATA > Conversation/user content, per the
brief's own ordering) -- this module does not itself build that prompt, so
it cannot itself weaken that ordering, but it also performs no sanitization
of `KnowledgeItem.content`/contact fields: they are untrusted tenant data,
never interpreted or executed here, only passed through as opaque text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from voiceagent.calls import service as calls_service
from voiceagent.contacts import service as contacts_service
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.followups import service as followups_service
from voiceagent.followups.errors import CallOutcomeNotFoundError
from voiceagent.knowledge.errors import InvalidKnowledgeQueryError
from voiceagent.knowledge.retrieval import MAX_RESULT_LIMIT, KnowledgeSearchResult, search_items
from voiceagent.tenancy import TenantContext

if TYPE_CHECKING:
    from voiceagent.agents.models import AgentVersion

__all__ = [
    "MAX_AGENT_INSTRUCTIONS_LENGTH",
    "CallContext",
    "CallStateContext",
    "ContactContext",
    "assemble_call_context",
]

#: `AgentConfig.instructions` carries no length bound of its own today
#: (`voiceagent.agents.config`); this is a defensive ceiling on what this
#: function ever hands onward as "the agent's instructions", independent of
#: whatever bound (or lack of one) that field gets in the future.
MAX_AGENT_INSTRUCTIONS_LENGTH = 4_000


@dataclass(frozen=True, slots=True)
class ContactContext:
    """Only the fields a call-control tool already exposes today
    (`voiceagent.tools.handlers.ContactRef`) -- never a raw `Contact` row,
    never a field this product has not already decided is safe to hand an
    engine (e.g. no free-form notes field exists on `Contact` to leak in the
    first place)."""

    contact_id: uuid.UUID
    name: str
    phone_e164: str


@dataclass(frozen=True, slots=True)
class CallStateContext:
    status: str
    outcome: str | None


@dataclass(frozen=True, slots=True)
class CallContext:
    call_session_id: uuid.UUID
    agent_instructions: str
    contact: ContactContext | None
    call_state: CallStateContext
    knowledge: tuple[KnowledgeSearchResult, ...]


def assemble_call_context(
    context: TenantContext,
    call_session_id: uuid.UUID,
    agent_version: AgentVersion,
    *,
    knowledge_query: str | None = None,
    knowledge_limit: int = MAX_RESULT_LIMIT,
) -> CallContext:
    """Raises `voiceagent.calls.errors.CallSessionNotFoundError` if
    `call_session_id` is foreign/missing -- the one error this function does
    not swallow, since every other field is optional-by-construction
    (no contact, no outcome yet, no knowledge query) but the call itself is
    not. `knowledge_query=None` (the default) skips retrieval entirely --
    `.knowledge` is then simply empty, not an error."""
    call = calls_service.get_call_session(context, call_session_id)

    contact: ContactContext | None = None
    if call.contact_id is not None:
        try:
            contact_row = contacts_service.get_contact(context, call.contact_id)
        except ContactNotFoundError:
            contact = None
        else:
            contact = ContactContext(
                contact_id=contact_row.id,
                name=contact_row.name,
                phone_e164=contact_row.phone_e164,
            )

    outcome_value: str | None = None
    try:
        outcome_row = followups_service.get_call_outcome(context, call_session_id)
    except CallOutcomeNotFoundError:
        outcome_value = None
    else:
        outcome_value = outcome_row.outcome

    knowledge_results: tuple[KnowledgeSearchResult, ...] = ()
    if knowledge_query is not None:
        try:
            knowledge_results = tuple(
                search_items(context, agent_version, query=knowledge_query, limit=knowledge_limit)
            )
        except InvalidKnowledgeQueryError:
            knowledge_results = ()

    instructions = str(agent_version.config.get("instructions", ""))[:MAX_AGENT_INSTRUCTIONS_LENGTH]

    return CallContext(
        call_session_id=call_session_id,
        agent_instructions=instructions,
        contact=contact,
        call_state=CallStateContext(status=call.status, outcome=outcome_value),
        knowledge=knowledge_results,
    )
