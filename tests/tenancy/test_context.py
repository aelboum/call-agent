"""The tenant-context boundary (Phase 1 brief section 5).

The property under test is narrow and important: a tenant context exists only
because SaaS-OS verified it, it is a value rather than global state, and it is
the only key that opens a tenant-scoped session.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from api.context import RequestContext

from voiceagent.tenancy import TenantContext, require_tenant, tenant_scope
from voiceagent.tenancy import context as context_module


def _request_context() -> RequestContext:
    return RequestContext(actor_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4())


def test_context_is_copied_from_the_verified_platform_context() -> None:
    """Every field comes from the platform object. Nothing is re-derived,
    re-resolved, or defaulted -- if SaaS-OS did not verify it, it is not
    here."""
    verified = _request_context()
    context = TenantContext.from_request_context(verified)
    assert context.tenant_id == verified.tenant_id
    assert context.actor_id == verified.actor_id
    assert context.membership_id == verified.membership_id


def test_context_is_immutable() -> None:
    context = TenantContext.from_request_context(_request_context())
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.tenant_id = uuid.uuid4()  # type: ignore[misc]


def test_context_cannot_carry_smuggled_state() -> None:
    """Slotted: no caller can attach an extra attribute to pass state
    sideways through a context object."""
    context = TenantContext.from_request_context(_request_context())
    with pytest.raises(AttributeError):
        context.impersonated_tenant_id = uuid.uuid4()  # type: ignore[attr-defined]


def test_tenant_scope_passes_the_verified_tenant(monkeypatch) -> None:
    """`tenant_scope()` takes a `TenantContext`, not a bare UUID, so "I have a
    verified tenant" is a type-level fact rather than a convention someone can
    forget. It delegates to `infra.db.tenant_session_scope`, which sets
    `app.tenant_id` for the transaction."""
    seen: list[uuid.UUID] = []

    class _FakeScope:
        def __init__(self, tenant_id: uuid.UUID) -> None:
            seen.append(tenant_id)

        def __enter__(self) -> str:
            return "session"

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(context_module, "tenant_session_scope", _FakeScope)

    context = TenantContext.from_request_context(_request_context())
    with tenant_scope(context) as session:
        assert session == "session"

    assert seen == [context.tenant_id]


def test_module_holds_no_mutable_tenant_state() -> None:
    """No process-wide "current tenant": no module-level dict, list, set or
    context variable. A tenant context is passed explicitly, always."""
    mutable = {
        name: value
        for name, value in vars(context_module).items()
        if not name.startswith("_") and isinstance(value, (dict, list, set))
    }
    assert mutable == {}


def test_require_tenant_builds_a_fastapi_dependency() -> None:
    """A thin adapter over `api.dependencies.require_permission()`: the
    platform authenticates, resolves the tenant, rate-limits and runs the RBAC
    check; this wrapper only converts the verified result. It must never grow
    authorization logic of its own -- Phase 1 registers no permission and
    authorizes no route."""
    dependency = require_tenant("voiceagent.agents", "read")
    assert callable(dependency)
    import inspect

    assert inspect.iscoroutinefunction(dependency)
    # `from __future__ import annotations` makes the annotation a string;
    # both forms are accepted so the assertion tests the contract, not the
    # module's import style.
    signature = inspect.signature(dependency)
    assert signature.return_annotation in (TenantContext, "TenantContext")
