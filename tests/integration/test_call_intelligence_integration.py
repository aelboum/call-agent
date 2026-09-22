"""Real-PostgreSQL verification of Phase 2.12 (Advanced AI Post-Call
Intelligence): migration `0010`, Row-Level Security (+FORCE), tenant-safe
FKs, cross-tenant read/rebuild denial, the versioned durable lifecycle
(request/claim/complete/fail, idempotency, rebuild), the completed-
immutability trigger, privacy-gated execution, and the real API routes'
authorization boundary.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0010_call_ai_analyses`) already applied --
excluded from the default `pytest` run (`pytest -m integration`), exactly
mirroring `tests/integration/test_workflow_execution_integration.py` and
`tests/integration/test_knowledge_integration.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version, publish_version
from voiceagent.call_intelligence.errors import (
    CallAiAnalysisExecutionConflictError,
    CallAiAnalysisInProgressError,
    CallAiAnalysisNotFoundError,
    CallNotEligibleForAnalysisError,
)
from voiceagent.call_intelligence.models import CallAiAnalysis
from voiceagent.call_intelligence.schema import CallAiAnalysisResult
from voiceagent.call_intelligence.service import (
    claim_pending_analysis,
    complete_analysis,
    fail_analysis,
    get_latest_analysis,
    list_analysis_versions,
    request_analysis,
)
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.service import create_call_session, transition_call_session
from voiceagent.conversations.service import persist_conversation_turn
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.tenancy import TenantContext

pytestmark = pytest.mark.integration


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


def _config() -> AgentConfig:
    payload = {
        "instructions": "Answer the phone politely.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
    }
    return AgentConfig.model_validate(payload)


def _make_completed_call(context: TenantContext):
    agent = create_agent(context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context, agent.id, config=_config())
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=_phone())
    call = create_call_session(
        context,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    persist_conversation_turn(
        context,
        call.id,
        event_id="e1",
        role="user",
        content="Hi, I have a billing question.",
        tool_payload=None,
    )
    persist_conversation_turn(
        context,
        call.id,
        event_id="e2",
        role="assistant",
        content="Sure, how can I help?",
        tool_payload=None,
    )
    call = transition_call_session(context, call.id, to_status="answered")
    call = transition_call_session(context, call.id, to_status="completed", end_reason="completed")
    return call, version


def _request(context: TenantContext, call_session_id: uuid.UUID, *, force_rebuild: bool = False):
    return request_analysis(
        context,
        call_session_id,
        provider="fake",
        model="fake-model",
        prompt_version="1",
        force_rebuild=force_rebuild,
    )


def _valid_result() -> CallAiAnalysisResult:
    return CallAiAnalysisResult.model_validate(
        {
            "summary": "The customer asked a billing question and it was resolved.",
            "customer_intent": "Billing inquiry.",
            "key_topics": ["billing"],
            "action_items": [],
            "escalation": {"required": False, "reason": None},
            "sentiment": {"overall": "neutral"},
            "confidence": 0.9,
        }
    )


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase212-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase212-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


@pytest.fixture
def solo_tenant() -> TenantContext:
    """A fresh tenant per test -- `claim_pending_analysis()` claims the
    single most-overdue *eligible row anywhere in the tenant* (by design,
    the identical tenant-wide FIFO shape `voiceagent.followups.service
    .claim_due_follow_up()` already has), so any test that asserts *which*
    row a claim returns needs a tenant with no other test's leftover
    pending/failed rows competing for it -- `two_tenants` (module-scoped,
    shared across many tests) is intentionally reused only by the RLS/
    cross-tenant tests above, which do not depend on claim ordering."""
    tenant = create_tenant(f"phase212-solo-{uuid.uuid4().hex[:8]}")
    user = create_user()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())


# --------------------------------------------------------------------------
# Eligibility / lifecycle
# --------------------------------------------------------------------------


def test_request_analysis_rejects_a_non_terminal_call(two_tenants) -> None:
    context_a, _ = two_tenants
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context_a, agent.id, config=_config())
    version = publish_version(context_a, agent.id, draft.id)
    number = register_phone_number(context_a, e164=_phone())
    call = create_call_session(
        context_a,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    with pytest.raises(CallNotEligibleForAnalysisError):
        _request(context_a, call.id)


def test_request_analysis_is_idempotent(two_tenants) -> None:
    context_a, _ = two_tenants
    call, _ = _make_completed_call(context_a)

    first = _request(context_a, call.id)
    second = _request(context_a, call.id)

    assert first.id == second.id
    assert first.version == 1
    assert second.version == 1


def test_full_lifecycle_pending_processing_completed(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)

    row = _request(context_a, call.id)
    assert row.status == "pending"

    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    assert claimed.id == row.id
    assert claimed.status == "processing"

    completed = complete_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, result=_valid_result()
    )
    assert completed.status == "completed"
    assert completed.result is not None
    assert str(completed.result["summary"]).startswith("The customer")

    fetched = get_latest_analysis(context_a, call.id)
    assert fetched.status == "completed"
    assert fetched.version == 1


def test_full_lifecycle_pending_processing_failed(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)

    row = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None and claimed.id == row.id
    assert claimed.execution_id is not None

    failed = fail_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, reason="provider_error"
    )
    assert failed.status == "failed"
    assert failed.failure_reason == "provider_error"
    assert failed.next_attempt_at is not None  # backoff scheduled, not exhausted


def test_retry_after_failure_reclaims_the_same_row(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)

    row = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    failed = fail_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, reason="provider_error"
    )
    assert failed.attempt_count == 1

    # Force the backoff deadline into the past so the retry is due now.
    with tenant_session_scope(context_a.tenant_id) as session:
        db_row = session.get(CallAiAnalysis, row.id)
        assert db_row is not None
        db_row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()

    reclaimed = claim_pending_analysis(context_a)
    assert reclaimed is not None
    assert reclaimed.id == row.id
    assert reclaimed.attempt_count == 2
    assert reclaimed.execution_id == claimed.execution_id  # stable per-row identity


def test_stale_processing_row_is_reclaimed_after_lease_expiry(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)

    row = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None

    # Simulate a crashed worker: force the lease into the past.
    with tenant_session_scope(context_a.tenant_id) as session:
        db_row = session.get(CallAiAnalysis, row.id)
        assert db_row is not None
        db_row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()

    reclaimed = claim_pending_analysis(context_a)
    assert reclaimed is not None
    assert reclaimed.id == row.id
    assert reclaimed.status == "processing"
    assert reclaimed.attempt_count == 2


def test_completing_with_a_stale_execution_id_is_a_conflict(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)
    row = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None

    with pytest.raises(CallAiAnalysisExecutionConflictError):
        complete_analysis(context_a, row.id, execution_id=uuid.uuid4(), result=_valid_result())


def test_completed_analysis_is_never_overwritten(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)
    _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    completed = complete_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, result=_valid_result()
    )

    # A second completion attempt for the same (now non-'processing') row
    # must be refused, never silently overwrite the stored result.
    with pytest.raises(CallAiAnalysisExecutionConflictError):
        complete_analysis(
            context_a, completed.id, execution_id=claimed.execution_id, result=_valid_result()
        )


def test_completed_row_is_immutable_at_the_database_trigger(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)
    row = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    complete_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, result=_valid_result()
    )

    with pytest.raises(Exception, match="immutable"):
        with tenant_session_scope(context_a.tenant_id) as session:
            db_row = session.get(CallAiAnalysis, row.id)
            assert db_row is not None
            db_row.attempt_count = 999
            session.flush()


# --------------------------------------------------------------------------
# Versioning / rebuild
# --------------------------------------------------------------------------


def test_rebuild_creates_a_new_version_without_touching_the_old_one(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)
    v1 = _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    completed_v1 = complete_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, result=_valid_result()
    )

    v2 = request_analysis(
        context_a,
        call.id,
        provider="fake",
        model="fake-model-v2",
        prompt_version="2",
        force_rebuild=True,
    )

    assert v2.version == 2
    assert v2.id != v1.id
    assert v2.status == "pending"

    # v1's completed row is untouched.
    versions = list_analysis_versions(context_a, call.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].id == completed_v1.id
    assert versions[0].status == "completed"
    assert versions[0].result == completed_v1.result


def test_rebuild_while_in_progress_is_refused(two_tenants) -> None:
    context_a, _ = two_tenants
    call, _ = _make_completed_call(context_a)
    _request(context_a, call.id)

    with pytest.raises(CallAiAnalysisInProgressError):
        _request(context_a, call.id, force_rebuild=True)


def test_get_latest_analysis_returns_the_highest_version(solo_tenant) -> None:
    context_a = solo_tenant
    call, _ = _make_completed_call(context_a)
    _request(context_a, call.id)
    claimed = claim_pending_analysis(context_a)
    assert claimed is not None
    assert claimed.execution_id is not None
    complete_analysis(
        context_a, claimed.id, execution_id=claimed.execution_id, result=_valid_result()
    )
    v2 = _request(context_a, call.id, force_rebuild=True)

    latest = get_latest_analysis(context_a, call.id)
    assert latest.id == v2.id
    assert latest.status == "pending"


def test_get_latest_analysis_raises_when_none_requested_yet(two_tenants) -> None:
    context_a, _ = two_tenants
    call, _ = _make_completed_call(context_a)
    with pytest.raises(CallAiAnalysisNotFoundError):
        get_latest_analysis(context_a, call.id)


# --------------------------------------------------------------------------
# Row-Level Security / tenant isolation / cross-tenant FK rejection
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_analysis(two_tenants) -> None:
    context_a, context_b = two_tenants
    call, _ = _make_completed_call(context_a)
    row = _request(context_a, call.id)

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CallAiAnalysis, row.id) is None


def test_cross_tenant_read_raises_not_found(two_tenants) -> None:
    context_a, context_b = two_tenants
    call, _ = _make_completed_call(context_a)
    _request(context_a, call.id)

    with pytest.raises(CallAiAnalysisNotFoundError):
        get_latest_analysis(context_b, call.id)


def test_cross_tenant_rebuild_is_rejected_as_call_not_found(two_tenants) -> None:
    context_a, context_b = two_tenants
    call, _ = _make_completed_call(context_a)

    with pytest.raises(CallSessionNotFoundError):
        _request(context_b, call.id, force_rebuild=True)


def test_analysis_cannot_reference_another_tenants_call(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a, _ = _make_completed_call(context_a)

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallAiAnalysis(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_a.id,  # belongs to tenant A
                    version=1,
                    status="pending",
                    schema_version="1",
                    prompt_version="1",
                    provider="fake",
                    model="fake-model",
                    attempt_count=0,
                    requested_at=datetime.now(UTC),
                    next_attempt_at=datetime.now(UTC),
                )
            )
            session.flush()


def test_row_level_security_is_enabled_and_forced() -> None:
    from sqlalchemy import text

    from voiceagent.db import session_scope

    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'app'::regnamespace AND relname = 'call_ai_analyses'"
            )
        ).all()
    assert rows == [(True, True)]


# --------------------------------------------------------------------------
# End-to-end: analyzer + worker against real persisted data
# --------------------------------------------------------------------------


def test_run_call_ai_analysis_end_to_end_against_real_data(solo_tenant) -> None:
    """The full pipeline: a real transcript, a real privacy authorization
    decision (explicitly allow-listed for this test, matching the
    documented "operator must opt in" default), a fake provider standing in
    for the network, and a real persisted `completed` result."""
    import asyncio

    from voiceagent.call_intelligence.analyzer import run_call_ai_analysis
    from voiceagent.config.settings import AiProviderSettings
    from voiceagent.providers.call_intelligence.fakes import FakeCallIntelligenceProvider
    from voiceagent.runtime.db import DatabaseBoundary

    context = solo_tenant
    call, _ = _make_completed_call(context)
    request_analysis(context, call.id, provider="fake", model="fake-model", prompt_version="1")

    ai_provider_settings = AiProviderSettings(
        eligible_providers=("fake",),
        allowed_data_classifications=("tenant_data",),
        allowed_purposes=("post_call_analysis",),
    )
    provider = FakeCallIntelligenceProvider()
    db = DatabaseBoundary(max_workers=2)
    try:
        result = asyncio.run(
            run_call_ai_analysis(
                db=db,
                context=context,
                provider=provider,
                provider_name="fake",
                ai_provider_settings=ai_provider_settings,
                system_actor_user_id=context.actor_id,
                timeout_seconds=5.0,
            )
        )
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert len(provider.requests) == 1
    # The real transcript reached the provider request, bounded and
    # delimited (brief PROMPT/INPUT CONSTRUCTION, PROMPT-INJECTION).
    assert "billing question" in provider.requests[0].user_content
    assert "BEGIN CALL TRANSCRIPT" in provider.requests[0].user_content

    fetched = get_latest_analysis(context, call.id)
    assert fetched.status == "completed"
    assert fetched.result is not None


def test_run_call_ai_analysis_denies_without_purpose_allow_listed(solo_tenant) -> None:
    """The documented fail-closed default: `AiProviderSettings
    .allowed_purposes` defaults to `("conversation",)`, which does not
    include `"post_call_analysis"` -- the provider must never be invoked."""
    import asyncio

    from voiceagent.call_intelligence.analyzer import run_call_ai_analysis
    from voiceagent.config.settings import AiProviderSettings
    from voiceagent.providers.call_intelligence.fakes import FakeCallIntelligenceProvider
    from voiceagent.runtime.db import DatabaseBoundary

    context = solo_tenant
    call, _ = _make_completed_call(context)
    request_analysis(context, call.id, provider="fake", model="fake-model", prompt_version="1")

    provider = FakeCallIntelligenceProvider()
    db = DatabaseBoundary(max_workers=2)
    try:
        result = asyncio.run(
            run_call_ai_analysis(
                db=db,
                context=context,
                provider=provider,
                provider_name="fake",
                ai_provider_settings=AiProviderSettings(),  # default: no "post_call_analysis"
                system_actor_user_id=context.actor_id,
                timeout_seconds=5.0,
            )
        )
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert result.failure_reason == "privacy_denied"
    assert len(provider.requests) == 0
