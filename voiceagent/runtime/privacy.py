"""The privacy authorization point (Phase 0 report §16.5; Phase 2.0 report
§14): **no call audio reaches an AI engine before `authorize_data_access()`
succeeds for that `CallSession`, and it is evaluated exactly once per call,
never per frame.**

Reuses `control_plane.data_authorization.authorize_data_access()` unchanged
-- no parallel policy system is invented (ADR-0009 point 7).

Two things this module does NOT resolve, both recorded here rather than
worked around, because working around either would mean either modifying
SaaS-OS (forbidden, ADR-0001) or inventing a table Phase 2.2's brief
prohibits:

1. **No per-tenant `TenantAIDataPolicy` store exists yet** (Phase 2.0 report
   §11.2 defers `provider_credentials`/tenant policy tables entirely, and
   this phase adds no domain table). `AiDataPolicySource` is the seam a real
   per-tenant policy table plugs into later without changing anything above
   it; `StaticAiDataPolicySource` is the only implementation Phase 2.2 ships,
   backed by `voiceagent.config.settings.AiProviderSettings`'s own
   deployment-wide default. This proves the *ordering* invariant, not real
   per-tenant policy enforcement -- see `docs/PHASE-2.2-STATUS.md`.
2. **`authorize_data_access()`'s `actor_user_id` parameter carries a real
   foreign key to `core.users.id`** (verified at the pinned SHA), while the
   call runtime has no human user in a call's context -- it acts as a
   `core.identity.ServiceAccount` (Phase 0 report §14.4). This module
   requires a configured `voiceagent.config.settings.RuntimeSettings
   .system_actor_user_id` (a real, operator-provisioned `core.users` row) and
   fails closed -- raising, never silently degrading to `None` or a made-up
   UUID that would only fail later, inside SaaS-OS's own foreign key, with a
   far less legible error.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from control_plane.data_authorization import (
    DataAuthorizationDecision,
    DataAuthorizationOutcome,
    DataAuthorizationRequest,
    ProviderEligibilityPolicy,
    TenantAIDataPolicy,
    authorize_data_access,
)

from voiceagent.config.settings import AiProviderSettings
from voiceagent.runtime.errors import DataAuthorizationDeniedError

__all__ = [
    "AiDataPolicySource",
    "PrivacyConfigurationError",
    "StaticAiDataPolicySource",
    "authorize_call_data_access",
]


class PrivacyConfigurationError(Exception):
    """`RuntimeSettings.system_actor_user_id` is not configured. Raised
    instead of guessing a UUID, which would only fail later inside SaaS-OS's
    own `core.audit_log.actor_user_id -> core.users.id` foreign key with a
    far less legible error at write time."""


@runtime_checkable
class AiDataPolicySource(Protocol):
    """Resolves the tenant-facing half of a `DataAuthorizationRequest`'s
    inputs: what this tenant is allowed to send where, for what purpose. The
    seam a real per-tenant policy table (Phase 2.2+/2.3) plugs into without
    changing `authorize_call_data_access()`."""

    def tenant_policy(self, tenant_id: uuid.UUID) -> TenantAIDataPolicy | None: ...

    def provider_policy(self) -> ProviderEligibilityPolicy: ...


class StaticAiDataPolicySource:
    """A deployment-wide default policy, identical for every tenant --
    Phase 2.2's only `AiDataPolicySource` (see module docstring point 1).
    Built from `voiceagent.config.settings.AiProviderSettings` so it changes
    only through ordinary deployment configuration, never a code change."""

    def __init__(self, settings: AiProviderSettings) -> None:
        self._settings = settings

    def tenant_policy(self, tenant_id: uuid.UUID) -> TenantAIDataPolicy:
        return TenantAIDataPolicy(
            tenant_id=tenant_id,
            allowed_data_classifications=frozenset(self._settings.allowed_data_classifications),
            allowed_purposes=frozenset(self._settings.allowed_purposes),
            allowed_providers=frozenset(self._settings.eligible_providers),
        )

    def provider_policy(self) -> ProviderEligibilityPolicy:
        return ProviderEligibilityPolicy(
            eligible_providers=frozenset(self._settings.eligible_providers)
        )


def authorize_call_data_access(
    *,
    tenant_id: uuid.UUID,
    call_session_id: uuid.UUID,
    data_classification: str,
    purpose: str,
    provider: str,
    policy_source: AiDataPolicySource,
    system_actor_user_id: uuid.UUID | None,
) -> DataAuthorizationDecision:
    """Evaluate and audit exactly one data-authorization decision for
    `call_session_id`. Called once, by the call task, after it loads the
    `CallSession`/`AgentVersion` snapshot and before `ConversationEngine.start()`
    (Phase 2.0 report §14.1's exact position) -- never per audio frame.

    Raises `PrivacyConfigurationError` if no system actor is configured (see
    module docstring point 2). Raises `DataAuthorizationDeniedError` if the
    decision is a denial -- the caller (`voiceagent.runtime.call_task`) never
    has to remember to check `.outcome` itself; the engine simply cannot be
    reached from code that runs after this call returns normally.
    """
    if system_actor_user_id is None:
        raise PrivacyConfigurationError(
            "VOICEAGENT_RUNTIME_SYSTEM_ACTOR_USER_ID is not configured; "
            "authorize_data_access() requires a real core.users.id to attribute "
            "its audit entry to (see voiceagent.runtime.privacy's module docstring)"
        )

    request = DataAuthorizationRequest(
        tenant_id=tenant_id,
        data_classification=data_classification,
        purpose=purpose,
        provider=provider,
        resource_type="call_session",
        resource_id=str(call_session_id),
    )
    decision = authorize_data_access(
        request,
        tenant_policy=policy_source.tenant_policy(tenant_id),
        provider_policy=policy_source.provider_policy(),
        actor_user_id=system_actor_user_id,
        correlation_id=str(call_session_id),
    )
    if decision.outcome is not DataAuthorizationOutcome.ALLOW:
        reason = decision.reason.value if decision.reason is not None else None
        raise DataAuthorizationDeniedError(call_session_id, reason)
    return decision
