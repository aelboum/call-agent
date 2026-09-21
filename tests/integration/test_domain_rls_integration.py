"""Real-PostgreSQL verification of the security properties Phase 2.1's brief
§23 asks to be tested explicitly, not merely documented: tenant isolation,
composite-FK cross-tenant rejection, RLS enable/force/policy behavior
(including *missing* tenant context), AgentVersion immutability at the
database boundary, phone-number duplicate handling, CallSession lifecycle,
and historical version stability.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations already applied (see `tests/integration/README.md`) --
excluded from the default `pytest` run (`pytest -m integration` to run it
explicitly), exactly mirroring the pinned SaaS-OS's own
`tests/infra/test_db_integration.py` convention.
"""

from __future__ import annotations

import uuid

import pytest
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.errors import AgentVersionNotDraftError
from voiceagent.agents.models import Agent, AgentVersion
from voiceagent.agents.service import (
    archive_version,
    create_agent,
    create_draft_version,
    get_agent_version,
    publish_version,
    update_agent,
)
from voiceagent.calls.errors import InvalidCallSessionTransitionError
from voiceagent.calls.models import CallSession
from voiceagent.calls.service import create_call_session, transition_call_session
from voiceagent.db import IntegrityError, session_scope, tenant_session_scope
from voiceagent.phone_numbers.errors import PhoneNumberUnavailableError
from voiceagent.phone_numbers.models import PhoneNumber
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.tenancy import TenantContext

pytestmark = pytest.mark.integration


def _config(**overrides) -> AgentConfig:
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


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    """`actor_id` must be a real `core.users` row -- `core.audit_log.record()`
    (called by `publish_version()`/`archive_version()`) has a foreign key
    into `core.users`, exactly as it would for a real authenticated caller.
    `membership_id` has no such constraint and stays a bare UUID."""
    tenant_a = create_tenant(f"phase21-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase21-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


@pytest.fixture
def agent_and_published_version(two_tenants):
    """One tenant, one agent, one published version -- the fixture shape
    most of these tests build on."""
    context_a, _ = two_tenants
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context_a, agent.id, config=_config())
    published = publish_version(context_a, agent.id, draft.id)
    return context_a, agent, published


# --------------------------------------------------------------------------
# Tenant isolation (Phase 2.1 brief S23 "Tenant isolation")
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_agent(two_tenants) -> None:
    context_a, context_b = two_tenants
    agent = create_agent(context_a, name=f"Isolated Agent {uuid.uuid4().hex[:8]}")

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(Agent, agent.id) is None


def test_tenant_cannot_read_another_tenants_agent_version(
    agent_and_published_version, two_tenants
) -> None:
    _, _, version = agent_and_published_version
    _, context_b = two_tenants

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(AgentVersion, version.id) is None


def test_tenant_cannot_read_another_tenants_phone_number(two_tenants) -> None:
    context_a, context_b = two_tenants
    number = register_phone_number(context_a, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(PhoneNumber, number.id) is None


def test_tenant_cannot_read_another_tenants_call_session(
    agent_and_published_version, two_tenants
) -> None:
    context_a, agent, version = agent_and_published_version
    _, context_b = two_tenants
    number = register_phone_number(context_a, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context_a,
        direction="inbound",
        from_e164="+15550100",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CallSession, call.id) is None


def test_no_tenant_context_reads_nothing(agent_and_published_version) -> None:
    """Missing tenant context (Phase 2.1 brief S23: "wrong/missing tenant
    context cannot read tenant data") -- a plain `session_scope()` that never
    sets `app.tenant_id` must see zero rows, not every tenant's rows. The
    policy's `NULLIF(current_setting(...), '')::uuid` turns an unset setting
    into SQL NULL, and `tenant_id = NULL` is never true -- deny by default."""
    _, agent, _ = agent_and_published_version
    with session_scope() as session:
        assert session.get(Agent, agent.id) is None


def test_row_level_security_is_enabled_and_forced_for_every_table() -> None:
    with session_scope() as session:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relnamespace = 'app'::regnamespace AND relkind = 'r'"
            )
        ).all()
    by_name = {row[0]: (row[1], row[2]) for row in rows}
    assert by_name == {
        "agents": (True, True),
        "agent_versions": (True, True),
        "phone_numbers": (True, True),
        "call_sessions": (True, True),
        "conversation_turns": (True, True),
    }


# --------------------------------------------------------------------------
# Composite FK isolation (Phase 2.1 brief S8/S23)
# --------------------------------------------------------------------------


def test_agent_version_cannot_reference_another_tenants_agent(two_tenants) -> None:
    """Attempt to reference a parent id belonging to tenant A using tenant
    B's tenant_id -- must fail at the database constraint layer, not merely
    be hidden by RLS."""
    context_a, context_b = two_tenants
    agent_a = create_agent(context_a, name=f"Foreign Parent {uuid.uuid4().hex[:8]}")

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                AgentVersion(
                    tenant_id=context_b.tenant_id,
                    agent_id=agent_a.id,  # belongs to tenant A
                    version_number=1,
                    status="draft",
                    config={},
                    config_hash="0" * 64,
                )
            )
            session.flush()


def test_phone_number_cannot_reference_another_tenants_agent(two_tenants) -> None:
    context_a, context_b = two_tenants
    agent_a = create_agent(context_a, name=f"Foreign Parent 2 {uuid.uuid4().hex[:8]}")

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                PhoneNumber(
                    tenant_id=context_b.tenant_id,
                    e164=f"+1555{uuid.uuid4().int % 10**7:07d}",
                    agent_id=agent_a.id,  # belongs to tenant A
                )
            )
            session.flush()


def test_call_session_cannot_reference_another_tenants_phone_number(
    agent_and_published_version, two_tenants
) -> None:
    context_a, agent, version = agent_and_published_version
    _, context_b = two_tenants
    number_a = register_phone_number(context_a, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallSession(
                    tenant_id=context_b.tenant_id,
                    direction="inbound",
                    from_e164="+15550100",
                    to_e164=number_a.e164,
                    phone_number_id=number_a.id,  # belongs to tenant A
                    agent_id=agent.id,
                    agent_version_id=version.id,
                )
            )
            session.flush()


# --------------------------------------------------------------------------
# AgentVersion immutability (Phase 2.1 brief S10/S23)
# --------------------------------------------------------------------------


def test_direct_update_of_a_published_version_is_rejected(agent_and_published_version) -> None:
    context, _, version = agent_and_published_version
    with pytest.raises(Exception, match="immutable"):
        with tenant_session_scope(context.tenant_id) as session:
            row = session.get(AgentVersion, version.id)
            assert row is not None
            row.config = {"instructions": "hacked"}
            session.flush()


def test_direct_update_of_config_hash_on_a_published_version_is_rejected(
    agent_and_published_version,
) -> None:
    context, _, version = agent_and_published_version
    with pytest.raises(Exception, match="immutable"):
        with tenant_session_scope(context.tenant_id) as session:
            row = session.get(AgentVersion, version.id)
            assert row is not None
            row.config_hash = "1" * 64
            session.flush()


def test_archive_transition_is_the_one_permitted_update(agent_and_published_version) -> None:
    context, agent, version = agent_and_published_version
    original_config_hash = version.config_hash

    archived = archive_version(context, agent.id, version.id)

    assert archived.status == "archived"
    assert archived.config_hash == original_config_hash  # untouched by the transition

    fetched = get_agent_version(context, version.id)
    assert fetched.status == "archived"
    assert fetched.config == version.config


def test_publishing_a_non_draft_version_is_rejected(agent_and_published_version) -> None:
    context, agent, version = agent_and_published_version
    with pytest.raises(AgentVersionNotDraftError):
        publish_version(context, agent.id, version.id)  # already published


# --------------------------------------------------------------------------
# Historical version stability (Phase 2.1 brief S15/S23)
# --------------------------------------------------------------------------


def test_changing_agent_configuration_does_not_alter_an_existing_version(
    agent_and_published_version,
) -> None:
    context, agent, version = agent_and_published_version
    before = get_agent_version(context, version.id)

    update_agent(context, agent.id, name="A Completely Renamed Agent")

    after = get_agent_version(context, version.id)
    assert after.config == before.config
    assert after.config_hash == before.config_hash
    assert after.status == before.status


# --------------------------------------------------------------------------
# Phone-number information disclosure (Phase 2.1 brief S12/S23)
# --------------------------------------------------------------------------


def test_duplicate_e164_within_same_tenant_is_generic(two_tenants) -> None:
    context_a, _ = two_tenants
    e164 = f"+1555{uuid.uuid4().int % 10**7:07d}"
    register_phone_number(context_a, e164=e164)

    with pytest.raises(PhoneNumberUnavailableError):
        register_phone_number(context_a, e164=e164)


def test_duplicate_e164_across_tenants_is_the_identical_generic_error(two_tenants) -> None:
    """The security-critical case: tenant B attempting a number tenant A
    already owns must raise the exact same error class -- with no field, no
    message variation -- as the same-tenant case above."""
    context_a, context_b = two_tenants
    e164 = f"+1555{uuid.uuid4().int % 10**7:07d}"
    register_phone_number(context_a, e164=e164)

    with pytest.raises(PhoneNumberUnavailableError) as exc_info:
        register_phone_number(context_b, e164=e164)

    # No tenant id, owner, or existence detail anywhere in the message.
    message = str(exc_info.value)
    assert str(context_a.tenant_id) not in message
    assert message == "Phone number unavailable."


# --------------------------------------------------------------------------
# CallSession lifecycle (Phase 2.1 brief S14/S23)
# --------------------------------------------------------------------------


def test_call_session_full_lifecycle(agent_and_published_version) -> None:
    context, agent, version = agent_and_published_version
    number = register_phone_number(context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context,
        direction="inbound",
        from_e164="+15550100",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    assert call.status == "initiated"
    assert call.started_at is not None

    call = transition_call_session(context, call.id, to_status="ringing")
    assert call.status == "ringing"

    call = transition_call_session(context, call.id, to_status="answered")
    assert call.answered_at is not None

    call = transition_call_session(context, call.id, to_status="completed", end_reason="completed")
    assert call.status == "completed"
    assert call.ended_at is not None
    assert call.duration_ms is not None
    assert call.end_reason == "completed"


def test_call_session_duplicate_terminal_event_is_a_no_op(agent_and_published_version) -> None:
    context, agent, version = agent_and_published_version
    number = register_phone_number(context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context,
        direction="outbound",
        from_e164=number.e164,
        to_e164="+15550199",
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    call = transition_call_session(context, call.id, to_status="answered")
    call = transition_call_session(context, call.id, to_status="completed")
    first_ended_at = call.ended_at

    # Redelivery of the same terminal event: no-op, not an error.
    call = transition_call_session(context, call.id, to_status="completed")
    assert call.ended_at == first_ended_at


def test_call_session_cannot_reopen_a_terminal_state(agent_and_published_version) -> None:
    context, agent, version = agent_and_published_version
    number = register_phone_number(context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context,
        direction="outbound",
        from_e164=number.e164,
        to_e164="+15550199",
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    call = transition_call_session(context, call.id, to_status="failed")

    with pytest.raises(InvalidCallSessionTransitionError):
        transition_call_session(context, call.id, to_status="in_progress")
