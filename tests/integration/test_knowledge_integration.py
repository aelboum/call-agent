"""Real-PostgreSQL verification of Phase 2.11 (Agent Knowledge & Context):
migration `0009`, Row-Level Security (+FORCE) on both new tables, tenant
isolation, cross-tenant `KnowledgeSource`/`KnowledgeItem` reference rejection,
cross-tenant `AgentVersion` knowledge-association denial, the
`KnowledgeItem` content-immutability trigger, bounded retrieval (tenant
scope, association scope, active-status exclusion), and the
`knowledge.search` Tool Gateway tool executing end-to-end under real
tenant/RBAC authorization.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0009_knowledge_tables`) already applied --
excluded from the default `pytest` run (`pytest -m integration`), exactly
mirroring `tests/integration/test_workflow_execution_integration.py`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from core.identity import add_tenant_membership, create_service_account, create_user
from core.rbac import (
    RoleScope,
    assign_first_role_for_new_tenant,
    create_role,
    grant_permission,
    register_permission,
)
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.errors import InvalidAgentConfigError
from voiceagent.agents.service import create_agent, create_draft_version, publish_version
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.knowledge.errors import KnowledgeItemNotDraftError
from voiceagent.knowledge.models import KnowledgeItem, KnowledgeSource
from voiceagent.knowledge.retrieval import search_items
from voiceagent.knowledge.service import (
    activate_item,
    create_item,
    create_source,
    deactivate_item,
    resolve_active_item_ids,
    update_item,
)
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway
from voiceagent.tools.registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration

SERVICE_ACCOUNT_NAME = "voiceagent-runtime"


def _config(*, knowledge_item_ids: list[uuid.UUID] | None = None) -> AgentConfig:
    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
        "tools": [{"key": "knowledge.search", "config": {}}],
    }
    if knowledge_item_ids is not None:
        payload["knowledge"] = {"item_ids": [str(i) for i in knowledge_item_ids]}
    return AgentConfig.model_validate(payload)


def _active_item(context: TenantContext, *, title: str = "Hours", content: str = "9-5 Mon-Fri"):
    source = create_source(context, name=f"Source {uuid.uuid4().hex[:8]}")
    item = create_item(context, source.id, title=title, content=content)
    return activate_item(context, item.id)


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase211-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase211-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


# --------------------------------------------------------------------------
# Lifecycle / content immutability
# --------------------------------------------------------------------------


def test_source_and_item_lifecycle(two_tenants) -> None:
    context_a, _ = two_tenants
    source = create_source(context_a, name=f"Source {uuid.uuid4().hex[:8]}", description="d")
    item = create_item(context_a, source.id, title="Pricing", content="Basic plan is $10/mo.")
    assert item.status == "draft"

    updated = update_item(context_a, item.id, content="Basic plan is $12/mo.")
    assert updated.content == "Basic plan is $12/mo."

    activated = activate_item(context_a, item.id)
    assert activated.status == "active"

    deactivated = deactivate_item(context_a, item.id)
    assert deactivated.status == "archived"


def test_editing_an_active_item_is_rejected_at_the_service_layer(two_tenants) -> None:
    context_a, _ = two_tenants
    item = _active_item(context_a)
    with pytest.raises(KnowledgeItemNotDraftError):
        update_item(context_a, item.id, content="rewritten")


def test_editing_an_active_item_is_rejected_at_the_database_trigger(two_tenants) -> None:
    """Bypasses `voiceagent.knowledge.service.update_item()`'s own guard --
    proves the database trigger itself is the backstop, not merely
    application-layer discipline (mirrors `test_domain_rls_integration.py
    ::test_direct_update_of_a_published_version_is_rejected`)."""
    context_a, _ = two_tenants
    item = _active_item(context_a)
    with pytest.raises(Exception, match="immutable"):
        with tenant_session_scope(context_a.tenant_id) as session:
            row = session.get(KnowledgeItem, item.id)
            assert row is not None
            row.content = "hacked"
            session.flush()


def test_activate_then_archive_is_the_one_permitted_status_path(two_tenants) -> None:
    context_a, _ = two_tenants
    item = _active_item(context_a)
    archived = deactivate_item(context_a, item.id)
    assert archived.status == "archived"
    assert archived.content == item.content


# --------------------------------------------------------------------------
# Row-Level Security / tenant isolation / cross-tenant FK rejection
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_knowledge_source(two_tenants) -> None:
    context_a, context_b = two_tenants
    source = create_source(context_a, name=f"Isolated {uuid.uuid4().hex[:8]}")
    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(KnowledgeSource, source.id) is None


def test_tenant_cannot_read_another_tenants_knowledge_item(two_tenants) -> None:
    context_a, context_b = two_tenants
    item = _active_item(context_a)
    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(KnowledgeItem, item.id) is None


def test_knowledge_item_cannot_reference_another_tenants_source(two_tenants) -> None:
    context_a, context_b = two_tenants
    source_a = create_source(context_a, name=f"Foreign Source {uuid.uuid4().hex[:8]}")
    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                KnowledgeItem(
                    tenant_id=context_b.tenant_id,
                    source_id=source_a.id,  # belongs to tenant A
                    title="x",
                    content="y",
                    status="draft",
                )
            )
            session.flush()


def test_create_item_rejects_a_foreign_source(two_tenants) -> None:
    context_a, context_b = two_tenants
    source_a = create_source(context_a, name=f"Foreign Source 2 {uuid.uuid4().hex[:8]}")
    from voiceagent.knowledge.errors import KnowledgeSourceNotFoundError

    with pytest.raises(KnowledgeSourceNotFoundError):
        create_item(context_b, source_a.id, title="x", content="y")


# --------------------------------------------------------------------------
# Cross-tenant AgentVersion knowledge-association denial
# --------------------------------------------------------------------------


def test_draft_version_rejects_an_unknown_knowledge_item(two_tenants) -> None:
    context_a, _ = two_tenants
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    with pytest.raises(InvalidAgentConfigError):
        create_draft_version(context_a, agent.id, config=_config(knowledge_item_ids=[uuid.uuid4()]))


def test_draft_version_rejects_another_tenants_knowledge_item(two_tenants) -> None:
    context_a, context_b = two_tenants
    foreign_item = _active_item(context_b)
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    with pytest.raises(InvalidAgentConfigError):
        create_draft_version(
            context_a, agent.id, config=_config(knowledge_item_ids=[foreign_item.id])
        )


def test_draft_version_rejects_a_draft_status_knowledge_item(two_tenants) -> None:
    """Only `status='active'` items are approvable -- a `draft` item's id is
    not yet a valid reference (brief: "unpublished/invalid... rejection")."""
    context_a, _ = two_tenants
    source = create_source(context_a, name=f"Source {uuid.uuid4().hex[:8]}")
    draft_item = create_item(context_a, source.id, title="Draft", content="not yet approved")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    with pytest.raises(InvalidAgentConfigError):
        create_draft_version(
            context_a, agent.id, config=_config(knowledge_item_ids=[draft_item.id])
        )


def test_draft_version_accepts_a_valid_active_item(two_tenants) -> None:
    context_a, _ = two_tenants
    item = _active_item(context_a)
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        context_a, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    assert version.config["knowledge"]["item_ids"] == [str(item.id)]


def test_resolve_active_item_ids_excludes_foreign_and_inactive(two_tenants) -> None:
    context_a, context_b = two_tenants
    active = _active_item(context_a)
    source = create_source(context_a, name=f"Source {uuid.uuid4().hex[:8]}")
    draft = create_item(context_a, source.id, title="Draft2", content="x")
    foreign = _active_item(context_b)

    resolved = resolve_active_item_ids(context_a, [active.id, draft.id, foreign.id])
    assert resolved == {active.id}


# --------------------------------------------------------------------------
# Bounded retrieval: tenant scope, association scope, active-status exclusion
# --------------------------------------------------------------------------


def test_search_finds_an_approved_active_item(two_tenants) -> None:
    context_a, _ = two_tenants
    item = _active_item(context_a, title="Hours", content="We are open 9 to 5, Monday to Friday.")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        context_a, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(context_a, agent.id, version.id)

    results = search_items(context_a, published, query="open hours", limit=5)
    assert [r.item_id for r in results] == [item.id]


def test_search_excludes_an_item_not_in_the_approved_association(two_tenants) -> None:
    context_a, _ = two_tenants
    approved = _active_item(context_a, title="Approved", content="approved content about pricing")
    unapproved = _active_item(context_a, title="Unapproved", content="unapproved pricing content")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        context_a, agent.id, config=_config(knowledge_item_ids=[approved.id])
    )
    published = publish_version(context_a, agent.id, version.id)

    results = search_items(context_a, published, query="pricing", limit=5)
    assert [r.item_id for r in results] == [approved.id]
    assert unapproved.id not in [r.item_id for r in results]


def test_search_excludes_a_deactivated_item(two_tenants) -> None:
    context_a, _ = two_tenants
    item = _active_item(context_a, title="Policy", content="our refund policy is 30 days")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        context_a, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(context_a, agent.id, version.id)

    deactivate_item(context_a, item.id)

    results = search_items(context_a, published, query="refund policy", limit=5)
    assert results == []


def test_search_never_returns_another_tenants_data_even_if_config_names_it(two_tenants) -> None:
    """A hand-crafted `AgentVersion.config["knowledge"]["item_ids"]`
    referencing tenant B's item (this can never happen through
    `create_draft_version()`'s own validation -- proven directly above; this
    proves the *retrieval* layer's own independent tenant filter, defense in
    depth) must still return nothing for tenant A's own search."""
    context_a, context_b = two_tenants
    foreign_item = _active_item(context_b, title="Foreign", content="foreign tenant secret pricing")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(context_a, agent.id, config=_config())
    published = publish_version(context_a, agent.id, version.id)
    object.__setattr__(
        published,
        "config",
        {**published.config, "knowledge": {"item_ids": [str(foreign_item.id)]}},
    )

    results = search_items(context_a, published, query="pricing", limit=5)
    assert results == []


# --------------------------------------------------------------------------
# knowledge.search through the real Tool Gateway, real RBAC
# --------------------------------------------------------------------------


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase211-tools-{uuid.uuid4().hex[:8]}")
    admin = create_user()
    membership = add_tenant_membership(tenant.id, admin.id)
    role = create_role(tenant.id, f"bootstrap-admin-{uuid.uuid4().hex[:8]}")
    for resource, action in (*PERMISSIONS, ("service_account_role", "create")):
        permission = register_permission(resource, action)
        grant_permission(tenant.id, role.id, permission.id)
    assign_first_role_for_new_tenant(tenant.id, membership.id, role.id, scope=RoleScope.SELF)
    return tenant.id, admin.id


@pytest.fixture
def tenant_context(tenant_and_admin) -> TenantContext:
    tenant_id, admin_id = tenant_and_admin
    return TenantContext(tenant_id=tenant_id, actor_id=admin_id, membership_id=uuid.uuid4())


@pytest.fixture
def bootstrapped_service_account(tenant_and_admin) -> str:
    tenant_id, admin_id = tenant_and_admin
    account = create_service_account(tenant_id, SERVICE_ACCOUNT_NAME)
    bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=account.id
    )
    return SERVICE_ACCOUNT_NAME


def _run_execute(gateway, /, **kwargs):
    return asyncio.run(gateway.execute(**kwargs))


def test_knowledge_search_tool_executes_end_to_end(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    item = _active_item(
        tenant_context, title="Hours", content="We are open 9 to 5, Monday through Friday."
    )
    agent = create_agent(tenant_context, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        tenant_context, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(tenant_context, agent.id, version.id)

    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=uuid.uuid4(),
            agent_version=published,
            call_ref=call_ref,
            telephony=telephony,
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(
                call_id="c1", name="knowledge.search", arguments={"query": "open hours"}
            ),
        )
    finally:
        db.close()

    assert result.error_code is None
    results = result.value["results"]
    assert len(results) == 1
    assert results[0]["item_id"] == str(item.id)


def test_knowledge_search_not_in_allowlist_is_denied(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    item = _active_item(tenant_context)
    agent = create_agent(tenant_context, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        tenant_context, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(tenant_context, agent.id, version.id)
    object.__setattr__(published, "config", {**published.config, "tools": []})

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=uuid.uuid4(),
            agent_version=published,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(
                call_id="c1", name="knowledge.search", arguments={"query": "hours"}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "tool_not_allowed"


def test_knowledge_search_unauthorized_tenant_fails_closed(tenant_context) -> None:
    """No RBAC bootstrap has been run for this tenant/service account here
    -- must fail closed as `unauthorized`, never silently execute (the same
    property the Phase 2.7/2.10 tool-integration suites already prove for
    their own tools)."""
    from voiceagent.runtime.db import DatabaseBoundary

    item = _active_item(tenant_context)
    agent = create_agent(tenant_context, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        tenant_context, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(tenant_context, agent.id, version.id)

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=uuid.uuid4(),
            agent_version=published,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=SERVICE_ACCOUNT_NAME,
            request=ToolCallRequested(
                call_id="c1", name="knowledge.search", arguments={"query": "hours"}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "unauthorized"


def test_knowledge_search_malformed_query_is_rejected(
    tenant_context, bootstrapped_service_account
) -> None:
    """An empty `query` fails `KnowledgeSearchInput`'s own `min_length=1` at
    the Tool Gateway's input-validation step, before the handler (and
    therefore the database) is ever reached."""
    from voiceagent.runtime.db import DatabaseBoundary

    item = _active_item(tenant_context)
    agent = create_agent(tenant_context, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        tenant_context, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(tenant_context, agent.id, version.id)

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=uuid.uuid4(),
            agent_version=published,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(
                call_id="c1", name="knowledge.search", arguments={"query": ""}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "invalid_arguments"


# --------------------------------------------------------------------------
# ContextAssembler, against real persisted state
# --------------------------------------------------------------------------


def test_assemble_call_context_combines_call_state_and_knowledge(two_tenants) -> None:
    from voiceagent.calls.service import create_call_session
    from voiceagent.knowledge.context import assemble_call_context
    from voiceagent.phone_numbers.service import register_phone_number

    context_a, _ = two_tenants
    item = _active_item(context_a, title="Hours", content="Open 9 to 5 daily.")
    agent = create_agent(context_a, name=f"Agent {uuid.uuid4().hex[:8]}")
    version = create_draft_version(
        context_a, agent.id, config=_config(knowledge_item_ids=[item.id])
    )
    published = publish_version(context_a, agent.id, version.id)
    number = register_phone_number(context_a, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context_a,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=published.id,
    )

    result = assemble_call_context(context_a, call.id, published, knowledge_query="open hours")
    assert result.call_state.status == "initiated"
    assert result.contact is None
    assert [r.item_id for r in result.knowledge] == [item.id]
