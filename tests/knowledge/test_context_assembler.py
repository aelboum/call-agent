"""`voiceagent.knowledge.context.assemble_call_context()` (Phase 2.11).

Every field this function reads comes from an already-tenant-scoped
application service, each proven against a real database elsewhere
(`voiceagent.calls.service`/`voiceagent.contacts.service`/
`voiceagent.followups.service`/`voiceagent.knowledge.retrieval`'s own test
suites). This module hermetically tests only the assembler's own combination
/bounding logic, with those four calls monkeypatched -- exactly the technique
`tests/tools/test_gateway.py`'s own module docstring describes for testing a
function that, in production, always crosses a real database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest

import voiceagent.knowledge.context as context_module
from voiceagent.agents.models import AgentVersion
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.followups.errors import CallOutcomeNotFoundError
from voiceagent.knowledge.context import (
    MAX_AGENT_INSTRUCTIONS_LENGTH,
    assemble_call_context,
)
from voiceagent.knowledge.retrieval import KnowledgeSearchResult
from voiceagent.tenancy import TenantContext


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _agent_version(instructions: str = "Answer the phone.") -> AgentVersion:
    # assemble_call_context() only ever reads `.config` off this object --
    # see the identical cast() in tests/knowledge/test_knowledge_retrieval.py.
    return cast(AgentVersion, SimpleNamespace(config={"instructions": instructions}))


def _patch_happy_path(monkeypatch: pytest.MonkeyPatch, *, contact_id: uuid.UUID | None) -> None:
    call = SimpleNamespace(status="in_progress", contact_id=contact_id)
    monkeypatch.setattr(context_module.calls_service, "get_call_session", lambda ctx, cid: call)

    def _no_outcome(ctx, cid):
        raise CallOutcomeNotFoundError(cid)

    monkeypatch.setattr(context_module.followups_service, "get_call_outcome", _no_outcome)

    def _no_contact(ctx, cid):
        raise ContactNotFoundError(cid)

    monkeypatch.setattr(context_module.contacts_service, "get_contact", _no_contact)
    monkeypatch.setattr(context_module, "search_items", lambda *a, **k: [])


def test_no_contact_no_outcome_no_knowledge_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_happy_path(monkeypatch, contact_id=None)
    result = assemble_call_context(_context(), uuid.uuid4(), _agent_version())
    assert result.contact is None
    assert result.call_state.status == "in_progress"
    assert result.call_state.outcome is None
    assert result.knowledge == ()


def test_contact_is_included_when_associated_and_found(monkeypatch: pytest.MonkeyPatch) -> None:
    contact_id = uuid.uuid4()
    call = SimpleNamespace(status="in_progress", contact_id=contact_id)
    monkeypatch.setattr(context_module.calls_service, "get_call_session", lambda ctx, cid: call)
    monkeypatch.setattr(
        context_module.followups_service,
        "get_call_outcome",
        lambda ctx, cid: (_ for _ in ()).throw(CallOutcomeNotFoundError(cid)),
    )
    contact = SimpleNamespace(id=contact_id, name="Jane", phone_e164="+15551234567")
    monkeypatch.setattr(context_module.contacts_service, "get_contact", lambda ctx, cid: contact)
    monkeypatch.setattr(context_module, "search_items", lambda *a, **k: [])

    result = assemble_call_context(_context(), uuid.uuid4(), _agent_version())
    assert result.contact is not None
    assert result.contact.contact_id == contact_id
    assert result.contact.name == "Jane"
    assert result.contact.phone_e164 == "+15551234567"


def test_outcome_is_included_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = SimpleNamespace(outcome="resolved")
    monkeypatch.setattr(
        context_module.calls_service,
        "get_call_session",
        lambda ctx, cid: SimpleNamespace(status="ended", contact_id=None),
    )
    monkeypatch.setattr(
        context_module.followups_service, "get_call_outcome", lambda ctx, cid: outcome
    )
    monkeypatch.setattr(context_module, "search_items", lambda *a, **k: [])

    result = assemble_call_context(_context(), uuid.uuid4(), _agent_version())
    assert result.call_state.outcome == "resolved"


def test_knowledge_query_is_forwarded_and_results_are_carried_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_happy_path(monkeypatch, contact_id=None)
    expected = [
        KnowledgeSearchResult(
            item_id=uuid.uuid4(), source_id=uuid.uuid4(), title="Hours", snippet="9-5"
        )
    ]
    captured: dict[str, object] = {}

    def _fake_search(ctx, agent_version, *, query, limit):
        captured["query"] = query
        captured["limit"] = limit
        return expected

    monkeypatch.setattr(context_module, "search_items", _fake_search)

    result = assemble_call_context(
        _context(), uuid.uuid4(), _agent_version(), knowledge_query="what are your hours"
    )
    assert result.knowledge == tuple(expected)
    assert captured["query"] == "what are your hours"


def test_no_knowledge_query_skips_retrieval_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail_if_called(*a, **k):
        nonlocal called
        called = True
        return []

    _patch_happy_path(monkeypatch, contact_id=None)
    monkeypatch.setattr(context_module, "search_items", _fail_if_called)

    result = assemble_call_context(_context(), uuid.uuid4(), _agent_version())
    assert called is False
    assert result.knowledge == ()


def test_agent_instructions_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_happy_path(monkeypatch, contact_id=None)
    long_instructions = "x" * (MAX_AGENT_INSTRUCTIONS_LENGTH + 500)
    result = assemble_call_context(_context(), uuid.uuid4(), _agent_version(long_instructions))
    assert len(result.agent_instructions) == MAX_AGENT_INSTRUCTIONS_LENGTH
