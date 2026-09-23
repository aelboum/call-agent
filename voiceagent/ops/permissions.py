"""RBAC permission declaration for operator diagnostics (Phase 2.14 brief
section 12: "make it internal/operator-only using the existing
authorization architecture"). One resource, one action -- `read`; there is
no write surface here.

**Deliberately not granted by `voiceagent.rbac_bootstrap.bootstrap_tenant_rbac()`.**
That bootstrap's `PERMISSIONS` tuple grants the call runtime's own *service
account* everything it needs to execute tools on a tenant's behalf
(`voiceagent.rbac_bootstrap`'s own module docstring) -- a machine principal
with no reason to ever call an HTTP diagnostics route. This permission is
for a *human* operator instead, and `core.rbac` has no dedicated
cross-tenant "platform operator" principal type yet (`core.rbac.PrincipalType`
today: `USER`, `SYSTEM`, `SERVICE_ACCOUNT` only) -- see
`voiceagent.api.v1.ops`'s own module docstring for the resulting, documented
scope limitation. An operator grants this permission explicitly, with
`core.rbac.grant_permission()`, to whichever role their own operator-facing
tenant membership holds; it is registered into the global permission
catalog here (`register()`) so that grant is possible, but never
auto-granted the way `voiceagent.rbac_bootstrap.PERMISSIONS` auto-grants the
runtime's own service account.

See `voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.ops"


def register() -> None:
    register_permission(RESOURCE, "read")
