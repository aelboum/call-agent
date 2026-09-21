"""Deterministic RBAC bootstrap for the product's own permissions (Phase 2.2
brief section 25).

Resolves the Phase 2.1 "known limitation" recorded in
`docs/PHASE-2.1-STATUS.md` section 10: `voiceagent.agents`/`calls`/
`phone_numbers` each declare RBAC permissions (their own `register()`), but
nothing registered them into a role, granted that role anything, or assigned
it to a principal -- so a route exercised through the real HTTP/RBAC chain
denied every caller.

**Never called at import time, application-build time, or process startup**
(`tests/architecture/test_import_side_effects.py` covers this module too):
every function here writes to the database. It is a one-time, explicit,
operator-invoked bootstrap step -- see `scripts/bootstrap_rbac.py` -- never a
side effect of importing or running `voiceagent`, exactly the same rule
`voiceagent.agents.permissions.register()` already establishes and this
module inherits without weakening.

**Idempotent and safe to re-run.** `register_permission()` is idempotent by
SaaS-OS's own design (returns the existing row rather than raising); role,
grant and service-account-role-assignment creation here each catch the
matching SaaS-OS `Duplicate*Error` and treat "already exists" as success,
never as a fresh write or a fresh audit entry.

**Auditable.** Every SaaS-OS primitive this module calls (`create_role`,
`grant_permission`, `assign_service_account_role`) already writes its own
`core.audit_log` entry on the write it performs; no second audit mechanism is
added here, and no audit entry is written for a call that turned out to be a
no-op (matching `core.rbac`'s own idempotent-write discipline).

**No excessive grant.** `PERMISSIONS` below is exactly the set every
`voiceagent.*.permissions` module declares today -- this module does not
invent a superuser role, and granting it is not a substitute for reviewing
what a real production role should hold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.rbac import (
    DuplicatePermissionGrantError,
    DuplicateRoleNameError,
    DuplicateServiceAccountRoleAssignmentError,
    RoleScope,
    assign_service_account_role,
    create_role,
    grant_permission,
    list_roles,
    register_permission,
)

from voiceagent.agents import permissions as agents_permissions
from voiceagent.calls import permissions as calls_permissions
from voiceagent.phone_numbers import permissions as phone_numbers_permissions
from voiceagent.tools import permissions as tools_permissions
from voiceagent.tools.registry import TOOL_REGISTRY

__all__ = [
    "DEFAULT_ROLE_NAME",
    "PERMISSIONS",
    "BootstrapResult",
    "bootstrap_tenant_rbac",
    "register_permissions",
]

#: Every (resource, action) pair this product declares, across every
#: domain module's own `register()` -- one explicit table here, rather than a
#: dynamic discovery mechanism, so the set a bootstrap run grants is always
#: exactly and visibly this list (brief section 25: "document exactly which
#: permissions exist"). Kept in sync by hand with each `voiceagent.*.permissions`
#: module; a mismatch would under- or over-grant, which is why both are small
#: and read together.
#:
#: The tools portion (Phase 2.4) is the one deliberate exception: it is
#: computed from `TOOL_REGISTRY.known_tool_ids()` rather than hand-listed,
#: because `voiceagent.tools.permissions.register()` already derives its own
#: permission set the same way (see that module) -- hand-listing tool IDs a
#: *second* time here would be the exact "mismatch would under- or
#: over-grant" risk this docstring warns about, applied to the one case
#: where a second static list genuinely could drift from the first.
PERMISSIONS: tuple[tuple[str, str], ...] = (
    (agents_permissions.RESOURCE, "read"),
    (agents_permissions.RESOURCE, "write"),
    (calls_permissions.RESOURCE, "read"),
    (phone_numbers_permissions.RESOURCE, "read"),
    (phone_numbers_permissions.RESOURCE, "write"),
    # Phase 2.4: one permission per built-in Tool Gateway tool
    # (`voiceagent.tools.permissions`) -- computed from `TOOL_REGISTRY` at
    # import time, not hand-listed, so this tuple can never omit a
    # registered tool's permission or list one that does not exist.
    *((tools_permissions.RESOURCE, tool_id) for tool_id in TOOL_REGISTRY.known_tool_ids()),
)

#: The role bootstrap creates/reuses -- "which role receives them" (brief
#: section 25). A deployment that wants a different shape passes its own
#: `role_name` to `bootstrap_tenant_rbac()`; this is only the default.
DEFAULT_ROLE_NAME = "voiceagent-runtime"


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    tenant_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    granted: tuple[tuple[str, str], ...]
    service_account_id: uuid.UUID | None


def register_permissions() -> None:
    """Idempotently declare every `voiceagent.*` permission into SaaS-OS's
    global permission catalog. Global, not tenant-scoped: `register_permission()`
    has no tenant dimension -- a permission is a `(resource, action)` pair,
    shared across every tenant; only its *grant* to a role (`grant_permission()`,
    below) is tenant-scoped."""
    agents_permissions.register()
    calls_permissions.register()
    phone_numbers_permissions.register()
    tools_permissions.register()


def _get_or_create_role(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    try:
        return create_role(tenant_id, name).id
    except DuplicateRoleNameError:
        for role in list_roles(tenant_id):
            if role.name == name:
                return role.id
        raise


def bootstrap_tenant_rbac(
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    role_name: str = DEFAULT_ROLE_NAME,
    service_account_id: uuid.UUID | None = None,
    scope: RoleScope = RoleScope.SELF,
) -> BootstrapResult:
    """Create (or reuse) `role_name` in `tenant_id`, grant it every
    permission in `PERMISSIONS`, and -- if `service_account_id` is given --
    assign the role to that service account (the call runtime's own acting
    principal, Phase 0 report section 14.4).

    `actor_user_id` must already hold, through ordinary membership-role
    authorization, at least `scope`-level authority over every permission in
    `PERMISSIONS` at `tenant_id`, plus the dedicated
    `(resource="service_account_role", action="create")` capability if
    `service_account_id` is given (`assign_service_account_role()`'s own
    anti-amplification gate) -- exactly the pattern
    `examples/reference-consumer/reference_consumer/scenarios.py`'s
    `provision_service_account_with_key()` already uses: this is how a real
    product provisions its first tenant admin, not something this module
    invents. `scripts/bootstrap_rbac.py` documents the operator flow this
    implies for a brand-new tenant.

    Safe to call repeatedly: every step tolerates "already exists" as a
    success, not an error.
    """
    register_permissions()
    role_id = _get_or_create_role(tenant_id, role_name)

    granted: list[tuple[str, str]] = []
    for resource, action in PERMISSIONS:
        permission = register_permission(resource, action)
        try:
            grant_permission(tenant_id, role_id, permission.id)
        except DuplicatePermissionGrantError:
            pass
        granted.append((resource, action))

    if service_account_id is not None:
        try:
            assign_service_account_role(
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                service_account_id=service_account_id,
                role_id=role_id,
                scope=scope,
            )
        except DuplicateServiceAccountRoleAssignmentError:
            pass

    return BootstrapResult(
        tenant_id=tenant_id,
        role_id=role_id,
        role_name=role_name,
        granted=tuple(granted),
        service_account_id=service_account_id,
    )
