"""Tenant-context boundary. See `voiceagent.tenancy.context` for the rules."""

from __future__ import annotations

from voiceagent.tenancy.context import TenantContext, require_tenant, tenant_scope

__all__ = ["TenantContext", "require_tenant", "tenant_scope"]
