"""Real-PostgreSQL verification of Phase 2.9 (safe scheduled follow-up
execution): claim/execute lifecycle, scheduling, concurrent claims under
real row locking, idempotency, stale-processing recovery, tenant isolation,
bounded retries, and the new API routes/RBAC permission.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0007_follow_up_execution`) already applied
-- excluded from the default `pytest` run (`pytest -m integration`),
mirroring `tests/integration/test_call_outcomes_followups_integration.py`.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from core.identity import add_tenant_membership, create_user
from core.rbac import (
    PrincipalType,
    RoleScope,
    assign_first_role_for_new_tenant,
    can,
    create_role,
    grant_permission,
    register_permission,
)
from core.tenancy import create_tenant

from voiceagent.calendars import service as calendar_service_module
from voiceagent.calendars.service import cancel_event, create_calendar, create_event
from voiceagent.calls.service import create_call_session
from voiceagent.db import tenant_session_scope
from voiceagent.followups.errors import (
    FollowUpActionNotFoundError,
    FollowUpExecutionConflictError,
    FollowUpNotRetryableError,
    InvalidFollowUpTransitionError,
)
from voiceagent.followups.models import FollowUpAction
from voiceagent.followups.permissions import FOLLOW_UP_ACTIONS_RESOURCE
from voiceagent.followups.retry_policy import LEASE_SECONDS, MAX_ATTEMPTS
from voiceagent.followups.service import (
    cancel_follow_up,
    claim_due_follow_up,
    complete_follow_up,
    complete_follow_up_execution,
    create_follow_up,
    execute_due_follow_up,
    fail_follow_up_execution,
    get_follow_up,
    reprocess_follow_up,
)
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.tenancy import TenantContext

pytestmark = pytest.mark.integration


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=1)


def _must_claim(context: TenantContext, *, now: datetime | None = None) -> FollowUpAction:
    """`claim_due_follow_up()` narrowed to non-`None` for a test that already
    knows a due row exists -- a real assertion (not merely a type-checker
    hint), so a regression that makes the claim return `None` here fails
    loudly rather than raising `AttributeError` on the next line."""
    row = claim_due_follow_up(context, now=now)
    assert row is not None
    return row


def _must_execute(context: TenantContext, *, now: datetime | None = None) -> FollowUpAction:
    row = execute_due_follow_up(context, now=now)
    assert row is not None
    return row


def _execution_id_of(row: FollowUpAction) -> uuid.UUID:
    """`FollowUpAction.execution_id` is a nullable column at the schema
    level (it is unset until first claim); every test here calls this only
    on an already-claimed row, where it is always set."""
    assert row.execution_id is not None
    return row.execution_id


def _config(**overrides):
    from voiceagent.agents.config import AgentConfig

    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
    }
    payload.update(overrides)
    return AgentConfig.model_validate(payload)


def _make_call(context: TenantContext):
    from voiceagent.agents.service import create_agent, create_draft_version, publish_version
    from voiceagent.phone_numbers.service import register_phone_number

    agent = create_agent(context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context, agent.id, config=_config())
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=_phone())
    return create_call_session(
        context,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )


def _make_appointment_follow_up(context: TenantContext, *, due_at: datetime, start_hour: int = 9):
    """One `type='appointment'` follow-up, with a real, freshly created
    `CalendarEvent` behind it."""
    call = _make_call(context)
    calendar = create_calendar(context, name=f"Cal {uuid.uuid4().hex[:6]}", timezone="UTC")
    day = due_at.day
    start_at = datetime(due_at.year, due_at.month, day, start_hour, 0, tzinfo=UTC)
    end_at = start_at + timedelta(hours=1)
    return create_follow_up(
        context,
        call.id,
        type="appointment",
        due_at=due_at,
        calendar_id=calendar.id,
        start_at=start_at,
        end_at=end_at,
    )


@pytest.fixture
def two_tenants() -> tuple[TenantContext, TenantContext]:
    """Function-scoped, deliberately unlike the module-scoped `two_tenants`
    fixture other Phase 2.x integration test files use: this domain is a
    work queue where *claim order* is the property under test
    (`claim_due_follow_up()` always returns the single most-overdue
    eligible row across the whole tenant), so a fresh tenant per test is
    what makes "claim returns exactly this follow-up" assertions correct
    regardless of what any other test in this module leaves behind or how
    long the suite takes to run."""
    tenant_a = create_tenant(f"phase29-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase29-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def test_future_due_at_is_not_claimable(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=datetime.now(UTC) + timedelta(days=30))
    assert claim_due_follow_up(context_a) is None


def test_due_follow_up_is_eligible(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id
    assert claimed.status == "processing"
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))


def test_due_at_is_timezone_aware_across_a_non_utc_offset(two_tenants) -> None:
    from zoneinfo import ZoneInfo

    context_a, _ = two_tenants
    # 23:30 in UTC-5 is 04:30 the next day in UTC -- already due right now in
    # UTC terms, even though the naive hour looks late in its own zone.
    due_at = (datetime.now(UTC) - timedelta(minutes=5)).astimezone(ZoneInfo("America/New_York"))
    follow_up = _make_appointment_follow_up(context_a, due_at=due_at)
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))


def test_non_executable_type_is_never_claimed(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(
        context_a,
        call.id,
        type="manual_follow_up",
        due_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert follow_up.status == "pending"
    assert follow_up.next_attempt_at is None
    assert claim_due_follow_up(context_a) is None


def test_no_recurrence_a_completed_follow_up_never_becomes_due_again(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))
    refetched = get_follow_up(context_a, follow_up.id)
    assert refetched.status == "completed"
    assert refetched.next_attempt_at is None


# --------------------------------------------------------------------------
# Lifecycle (DB-touching)
# --------------------------------------------------------------------------


def test_claim_transitions_pending_to_processing(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id
    assert claimed.status == "processing"
    assert claimed.attempt_count == 1
    assert claimed.execution_id is not None
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))


def test_complete_execution_transitions_processing_to_completed(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    completed = complete_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed)
    )
    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert follow_up.id == completed.id


def test_fail_execution_transitions_processing_to_failed(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    failed = fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    assert failed.status == "failed"
    assert failed.failure_reason == "unexpected_error"
    assert failed.next_attempt_at is not None
    assert failed.next_attempt_at > datetime.now(UTC)


def test_cancel_from_pending(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")
    cancelled = cancel_follow_up(context_a, follow_up.id)
    assert cancelled.status == "cancelled"


def test_invalid_transition_is_rejected(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")
    cancelled = cancel_follow_up(context_a, follow_up.id)
    assert cancelled.status == "cancelled"
    # cancelled -> completed has no edge in the lifecycle table
    # (voiceagent.followups.lifecycle) -- complete_follow_up() rejects it
    # directly, unlike complete_follow_up_execution()'s own separate
    # ownership guard (see test_execution_conflict_when_execution_id_no
    # _longer_matches for that one).
    with pytest.raises(InvalidFollowUpTransitionError):
        complete_follow_up(context_a, follow_up.id)


def test_completed_cannot_execute_again(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))
    assert claim_due_follow_up(context_a) is None


def test_cancelled_follow_up_is_never_claimed(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    cancel_follow_up(context_a, follow_up.id)
    assert claim_due_follow_up(context_a) is None


def test_failed_can_be_cancelled_before_exhaustion(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    failed = fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    assert failed.attempt_count < MAX_ATTEMPTS
    cancelled = cancel_follow_up(context_a, failed.id)
    assert cancelled.status == "cancelled"
    assert cancelled.next_attempt_at is None


# --------------------------------------------------------------------------
# Execution / idempotency
# --------------------------------------------------------------------------


def test_execute_due_follow_up_completes_an_appointment_with_a_live_event(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    outcome = _must_execute(context_a)
    assert outcome.id == follow_up.id
    assert outcome.status == "completed"


def test_execute_due_follow_up_fails_when_calendar_event_is_cancelled(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    calendar = create_calendar(context_a, name=f"Cal {uuid.uuid4().hex[:6]}", timezone="UTC")
    start_at = datetime.now(UTC) + timedelta(days=1)
    event = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Consult",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
    )
    cancel_event(context_a, event.id)
    follow_up = create_follow_up(
        context_a,
        call.id,
        type="appointment",
        due_at=_past(),
        calendar_event_id=event.id,
    )
    outcome = _must_execute(context_a)
    assert outcome.id == follow_up.id
    assert outcome.status == "failed"
    assert outcome.failure_reason == "calendar_event_cancelled"


def test_duplicate_execution_attempt_only_executes_once(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    first = _must_execute(context_a)
    assert first.id == follow_up.id
    assert first.status == "completed"
    # A second "duplicate delivery" attempt against the same tenant right
    # now finds nothing due -- the follow-up is already terminal.
    assert execute_due_follow_up(context_a) is None


def test_retry_after_simulated_failure_succeeds_on_next_attempt(two_tenants, monkeypatch) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())

    original_get_event = calendar_service_module.get_event
    call_count = {"n": 0}

    def _flaky_get_event(context, event_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient provider error")
        return original_get_event(context, event_id)

    monkeypatch.setattr(calendar_service_module, "get_event", _flaky_get_event)

    first = _must_execute(context_a)
    assert first.status == "failed"
    assert first.failure_reason == "unexpected_error"
    assert first.attempt_count == 1

    # Force the backoff to have elapsed by claiming "in the future".
    retried = _must_execute(context_a, now=datetime.now(UTC) + timedelta(hours=1))
    assert retried.id == follow_up.id
    assert retried.status == "completed"
    assert retried.attempt_count == 2


def test_retried_execution_never_creates_a_duplicate_calendar_event(
    two_tenants, monkeypatch
) -> None:
    """The one concrete duplicate-side-effect risk this phase could have
    (brief §7's own "duplicate appointment prevention" case): execution
    never calls `calendar_service.create_event()` at all, no matter how
    many times it is retried."""
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())

    def _forbidden_create_event(*args, **kwargs):
        raise AssertionError("execution must never create a calendar event")

    monkeypatch.setattr(calendar_service_module, "create_event", _forbidden_create_event)

    claimed = _must_claim(context_a)
    fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    execute_due_follow_up(context_a, now=datetime.now(UTC) + timedelta(hours=1))
    refetched = get_follow_up(context_a, follow_up.id)
    assert refetched.status == "completed"


def test_execution_conflict_when_execution_id_no_longer_matches(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    with pytest.raises(FollowUpExecutionConflictError):
        complete_follow_up_execution(context_a, claimed.id, execution_id=uuid.uuid4())
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))
    refetched = get_follow_up(context_a, follow_up.id)
    assert refetched.status == "completed"


# --------------------------------------------------------------------------
# Concurrency (real row locking)
# --------------------------------------------------------------------------


def test_two_concurrent_claims_never_claim_the_same_row(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    barrier = Barrier(2)

    def _claim():
        barrier.wait(timeout=5)
        return claim_due_follow_up(context_a)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _claim(), range(2)))

    winners = [row for row in results if row is not None and row.id == follow_up.id]
    assert len(winners) == 1


def test_concurrent_workers_claim_every_row_exactly_once(two_tenants) -> None:
    context_a, _ = two_tenants
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    follow_ups = [_make_appointment_follow_up(context_a, due_at=due_at) for _ in range(5)]
    expected_ids = {f.id for f in follow_ups}
    barrier = Barrier(5)

    def _claim(_index: int):
        barrier.wait(timeout=5)
        return claim_due_follow_up(context_a)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_claim, range(5)))

    claimed_ids = [row.id for row in results if row is not None]
    assert len(claimed_ids) == len(set(claimed_ids)), "no duplicate claims"
    assert set(claimed_ids) <= expected_ids
    assert len(claimed_ids) == 5


# --------------------------------------------------------------------------
# Stale-processing recovery
# --------------------------------------------------------------------------


def test_stale_processing_row_is_safely_reclaimed(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    first_claim = _must_claim(context_a)
    assert first_claim.id == follow_up.id

    # Simulate the lease expiring: claim again "in the future".
    reclaimed = _must_claim(context_a, now=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS + 5))
    assert reclaimed.id == follow_up.id
    assert reclaimed.status == "processing"
    assert reclaimed.attempt_count == 2
    assert reclaimed.execution_id == first_claim.execution_id  # stable identity

    complete_follow_up_execution(context_a, reclaimed.id, execution_id=_execution_id_of(reclaimed))


def test_active_processing_row_is_not_stolen_prematurely(two_tenants) -> None:
    context_a, _ = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id

    # Immediately after claiming, the lease has not expired.
    assert claim_due_follow_up(context_a) is None

    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_cross_tenant_claim_never_sees_another_tenants_due_follow_up(two_tenants) -> None:
    context_a, context_b = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())

    assert claim_due_follow_up(context_b) is None

    # Tenant A can still claim its own row.
    claimed = _must_claim(context_a)
    assert claimed.id == follow_up.id
    complete_follow_up_execution(context_a, claimed.id, execution_id=_execution_id_of(claimed))


def test_rls_hides_execution_metadata_columns_from_another_tenant(two_tenants) -> None:
    context_a, context_b = two_tenants
    follow_up = _make_appointment_follow_up(context_a, due_at=_past())
    claim_due_follow_up(context_a)

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(FollowUpAction, follow_up.id) is None


def test_reprocess_follow_up_cross_tenant_is_not_found(two_tenants) -> None:
    context_a, context_b = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    with pytest.raises(FollowUpActionNotFoundError):
        reprocess_follow_up(context_b, claimed.id)


# --------------------------------------------------------------------------
# Bounded retries / reprocess
# --------------------------------------------------------------------------


def test_bounded_retries_reach_a_final_failed_state(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())

    now = datetime.now(UTC)
    row: FollowUpAction | None = None
    for _ in range(MAX_ATTEMPTS):
        claimed = _must_claim(context_a, now=now)
        row = fail_follow_up_execution(
            context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
        )
        now = (row.next_attempt_at or now) + timedelta(seconds=1)

    assert row is not None
    assert row.status == "failed"
    assert row.attempt_count == MAX_ATTEMPTS
    assert row.next_attempt_at is None  # exhausted -- never claimed again

    # No infinite retry: even far in the future, nothing matches.
    assert claim_due_follow_up(context_a, now=now + timedelta(days=365)) is None


def test_reprocess_follow_up_clears_backoff_immediately(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    failed = fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    assert failed.next_attempt_at is not None
    assert failed.next_attempt_at > datetime.now(UTC)

    reprocessed = reprocess_follow_up(context_a, failed.id)
    assert reprocessed.next_attempt_at is not None
    assert reprocessed.next_attempt_at <= datetime.now(UTC)

    reclaimed = _must_claim(context_a)
    assert reclaimed.id == failed.id


def test_reprocess_follow_up_refuses_once_attempts_are_exhausted(two_tenants) -> None:
    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())

    now = datetime.now(UTC)
    row: FollowUpAction | None = None
    for _ in range(MAX_ATTEMPTS):
        claimed = _must_claim(context_a, now=now)
        row = fail_follow_up_execution(
            context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
        )
        now = (row.next_attempt_at or now) + timedelta(seconds=1)

    assert row is not None
    with pytest.raises(FollowUpNotRetryableError):
        reprocess_follow_up(context_a, row.id)


def test_reprocess_follow_up_refuses_a_non_failed_follow_up(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")
    with pytest.raises(FollowUpNotRetryableError):
        reprocess_follow_up(context_a, follow_up.id)


# --------------------------------------------------------------------------
# API routes (called directly, matching
# tests/integration/test_call_analysis_integration.py's own technique)
# --------------------------------------------------------------------------


def test_list_follow_ups_by_status_route_filters_by_status(two_tenants) -> None:
    from voiceagent.api.v1.follow_ups import list_follow_ups_by_status_route

    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")
    out = list_follow_ups_by_status_route(context=context_a, status_filter="pending")
    assert any(item.id == follow_up.id for item in out)

    completed_out = list_follow_ups_by_status_route(context=context_a, status_filter="completed")
    assert all(item.status == "completed" for item in completed_out)


def test_retry_follow_up_route_reprocesses(two_tenants) -> None:
    from voiceagent.api.v1.follow_ups import retry_follow_up_route

    context_a, _ = two_tenants
    _make_appointment_follow_up(context_a, due_at=_past())
    claimed = _must_claim(context_a)
    fail_follow_up_execution(
        context_a, claimed.id, execution_id=_execution_id_of(claimed), reason="unexpected_error"
    )
    out = retry_follow_up_route(claimed.id, context=context_a)
    assert out.next_attempt_at is not None
    assert out.next_attempt_at <= datetime.now(UTC)


def test_retry_follow_up_route_409s_when_not_retryable(two_tenants) -> None:
    from fastapi import HTTPException

    from voiceagent.api.v1.follow_ups import retry_follow_up_route

    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")
    with pytest.raises(HTTPException) as exc_info:
        retry_follow_up_route(follow_up.id, context=context_a)
    assert exc_info.value.status_code == 409


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase29-rbac-{uuid.uuid4().hex[:8]}")
    admin = create_user()
    membership = add_tenant_membership(tenant.id, admin.id)
    role = create_role(tenant.id, f"bootstrap-admin-{uuid.uuid4().hex[:8]}")
    for resource, action in (*PERMISSIONS, ("service_account_role", "create")):
        permission = register_permission(resource, action)
        grant_permission(tenant.id, role.id, permission.id)
    assign_first_role_for_new_tenant(tenant.id, membership.id, role.id, scope=RoleScope.SELF)
    return tenant.id, admin.id


def test_bootstrap_grants_the_retry_permission(tenant_and_admin) -> None:
    tenant_id, admin_id = tenant_and_admin
    result = bootstrap_tenant_rbac(tenant_id=tenant_id, actor_user_id=admin_id)
    assert (FOLLOW_UP_ACTIONS_RESOURCE, "retry") in result.granted


def test_an_unbootstrapped_actor_cannot_retry(tenant_and_admin) -> None:
    tenant_id, _ = tenant_and_admin
    stranger = create_user()
    assert (
        can(
            actor_id=stranger.id,
            tenant_id=tenant_id,
            action="retry",
            resource=FOLLOW_UP_ACTIONS_RESOURCE,
            actor_type=PrincipalType.USER,
        )
        is False
    )
