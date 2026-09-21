"""Real-PostgreSQL verification of `voiceagent.rbac_bootstrap` (Phase 2.2
brief section 25): permission registration, role/grant idempotency, service
account assignment, and that a bootstrapped principal can actually pass
`core.rbac.can()` where an un-bootstrapped one cannot.

Excluded from the default `pytest` run (`pytest -m integration`).
"""

from __future__ import annotations

import uuid

import pytest
from core.identity import add_tenant_membership, create_service_account, create_user
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

from voiceagent.agents.permissions import RESOURCE as AGENTS_RESOURCE
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant_and_admin():
    """An operator user holding ordinary authority over every permission in
    `PERMISSIONS`, plus the dedicated `service_account_role:create`
    capability -- exactly the pre-condition
    `bootstrap_tenant_rbac()`'s own docstring documents, provisioned the same
    way `examples/reference-consumer/reference_consumer/scenarios.py`'s
    `provision_service_account_with_key()` provisions a tenant's first
    admin."""
    tenant = create_tenant(f"phase22-rbac-{uuid.uuid4().hex[:8]}")
    admin = create_user()
    membership = add_tenant_membership(tenant.id, admin.id)
    role = create_role(tenant.id, f"bootstrap-admin-{uuid.uuid4().hex[:8]}")
    for resource, action in (*PERMISSIONS, ("service_account_role", "create")):
        permission = register_permission(resource, action)
        grant_permission(tenant.id, role.id, permission.id)
    assign_first_role_for_new_tenant(tenant.id, membership.id, role.id, scope=RoleScope.SELF)
    return tenant.id, admin.id


def test_bootstrap_registers_and_grants_every_declared_permission(tenant_and_admin) -> None:
    tenant_id, admin_id = tenant_and_admin
    result = bootstrap_tenant_rbac(tenant_id=tenant_id, actor_user_id=admin_id)
    assert result.granted == PERMISSIONS
    assert result.tenant_id == tenant_id


def test_bootstrap_is_idempotent(tenant_and_admin) -> None:
    tenant_id, admin_id = tenant_and_admin
    first = bootstrap_tenant_rbac(tenant_id=tenant_id, actor_user_id=admin_id)
    second = bootstrap_tenant_rbac(tenant_id=tenant_id, actor_user_id=admin_id)
    assert first.role_id == second.role_id
    assert second.granted == PERMISSIONS


def test_bootstrap_assigns_the_service_account_and_it_can_act(tenant_and_admin) -> None:
    tenant_id, admin_id = tenant_and_admin
    service_account = create_service_account(tenant_id, f"runtime-{uuid.uuid4().hex[:8]}")

    result = bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=service_account.id
    )
    assert result.service_account_id == service_account.id

    assert can(
        actor_id=service_account.id,
        tenant_id=tenant_id,
        action="read",
        resource=AGENTS_RESOURCE,
        actor_type=PrincipalType.SERVICE_ACCOUNT,
        actor_tenant_id=tenant_id,
    )


def test_an_unbootstrapped_service_account_cannot_act(tenant_and_admin) -> None:
    tenant_id, _ = tenant_and_admin
    service_account = create_service_account(
        tenant_id, f"never-bootstrapped-{uuid.uuid4().hex[:8]}"
    )

    register_permission(AGENTS_RESOURCE, "read")  # declared, but never granted to any role

    assert not can(
        actor_id=service_account.id,
        tenant_id=tenant_id,
        action="read",
        resource=AGENTS_RESOURCE,
        actor_type=PrincipalType.SERVICE_ACCOUNT,
        actor_tenant_id=tenant_id,
    )


def test_bootstrap_re_run_with_a_second_service_account_does_not_duplicate_grants(
    tenant_and_admin,
) -> None:
    tenant_id, admin_id = tenant_and_admin
    service_account_a = create_service_account(tenant_id, f"svc-a-{uuid.uuid4().hex[:8]}")
    service_account_b = create_service_account(tenant_id, f"svc-b-{uuid.uuid4().hex[:8]}")

    bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=service_account_a.id
    )
    result = bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=service_account_b.id
    )
    assert result.granted == PERMISSIONS
