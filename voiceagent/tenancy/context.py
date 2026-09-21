"""The product's tenant-context boundary (Phase 1).

SaaS-OS owns tenancy, identity and authorization; this module owns exactly
one thing: turning the platform's *verified* `api.context.RequestContext`
into the product's own `TenantContext`, and making that the only key that
opens a tenant-scoped database session.

What this module deliberately does NOT do:

* No second tenant hierarchy. `core.tenancy` owns tenants, their ancestry,
  their lifecycle states and their purge.
* No second RBAC system. `core.rbac.can()` decides; `api.dependencies`
  composes authentication -> tenant resolution -> rate limiting -> RBAC ahead
  of any handler. `require_tenant()` below adds nothing to that chain, it only
  adapts its result.
* No domain-level authorization. Phase 1 registers no permission and mounts
  no authorized route. The `(resource, action)` pair is the caller's.
* No global mutable tenant state. There is no module-level "current tenant",
  no context variable, no thread local. A `TenantContext` is a value, passed
  explicitly; a test asserts this module holds no mutable module-level state.

**The trust boundary.** A `TenantContext` may only ever be constructed from a
`RequestContext` the platform's ingress chain has already verified (Phase 2
adds one more source: the call-session context the Call Orchestrator builds
server-side). It is never built from a URL path segment, a request body, a
webhook payload, a caller ID, or model output. `tenant_scope()` accepts a
`TenantContext` rather than a bare `uuid.UUID` so that "I have a verified
tenant" is a type-level fact rather than a convention someone can forget.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from api.context import RequestContext
from api.dependencies import require_permission
from fastapi import Depends

from voiceagent.db import Session, tenant_session_scope

__all__ = ["TenantContext", "require_tenant", "tenant_scope"]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """A verified tenant context, derived from the platform's ingress chain.

    Frozen and slotted: it cannot be mutated after construction, and a caller
    cannot attach an extra attribute to smuggle state through it.
    """

    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    membership_id: uuid.UUID

    @classmethod
    def from_request_context(cls, context: RequestContext) -> TenantContext:
        """Adapt a verified platform `RequestContext`.

        Every field is copied from the platform object; nothing is re-derived,
        re-resolved, or defaulted. If SaaS-OS did not verify it, it is not
        here.
        """
        return cls(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            membership_id=context.membership_id,
        )


@contextmanager
def tenant_scope(context: TenantContext) -> Iterator[Session]:
    """A database session scoped to `context`'s tenant.

    Delegates to `infra.db.tenant_session_scope()`, which sets
    `app.tenant_id` for the transaction as a bound parameter; Row-Level
    Security (`ENABLE` + `FORCE`) then scopes every read and write underneath.
    This product neither weakens that policy nor offers any path to set
    `app.tenant_id` itself -- application code cannot execute arbitrary SQL
    (ADR-0007).

    Synchronous by necessity: `infra.db` exposes no async engine or session
    (Phase 0 report section 2.4 G-1). The Phase 2 call runtime calls this
    through `asyncio.to_thread`, never on the event loop.
    """
    with tenant_session_scope(context.tenant_id) as session:
        yield session


def require_tenant(resource: str, action: str):
    """A FastAPI dependency yielding a `TenantContext` for an authorized call.

    A thin adapter over `api.dependencies.require_permission()`: the platform
    performs authentication, tenant resolution, rate limiting and the RBAC
    check, and this wrapper converts the verified result into the product's
    own value type. It adds no authorization logic of its own, and must never
    be given one -- a product route that needs a different rule registers a
    different permission with `core.rbac.register_permission()`.

    Unused in Phase 1: no product route is authorized yet.
    """
    platform_dependency = require_permission(resource, action)

    async def _dependency(
        context: RequestContext = Depends(platform_dependency),  # noqa: B008 -- FastAPI's idiom
    ) -> TenantContext:
        return TenantContext.from_request_context(context)

    return _dependency
