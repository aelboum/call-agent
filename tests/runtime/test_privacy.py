"""`voiceagent.runtime.privacy` (Phase 0 report §16.5; Phase 2.0 report §14):
the hermetic slice -- `StaticAiDataPolicySource`'s shape and the fail-closed
"no system actor configured" path. The full allow/deny decision (which
writes a real `core.audit_log` entry via `authorize_data_access()`) is
exercised in `tests/integration/test_runtime_integration.py`."""

from __future__ import annotations

import uuid

import pytest

from voiceagent.config.settings import AiProviderSettings
from voiceagent.runtime.privacy import (
    AiDataPolicySource,
    PrivacyConfigurationError,
    StaticAiDataPolicySource,
    authorize_call_data_access,
)


def test_static_policy_source_satisfies_the_protocol() -> None:
    source = StaticAiDataPolicySource(AiProviderSettings())
    assert isinstance(source, AiDataPolicySource)


def test_static_policy_source_reflects_settings() -> None:
    settings = AiProviderSettings(
        eligible_providers=("fake",),
        allowed_data_classifications=("tenant_data",),
        allowed_purposes=("conversation",),
    )
    source = StaticAiDataPolicySource(settings)
    tenant_id = uuid.uuid4()

    tenant_policy = source.tenant_policy(tenant_id)
    assert tenant_policy.tenant_id == tenant_id
    assert tenant_policy.allowed_data_classifications == frozenset({"tenant_data"})
    assert tenant_policy.allowed_purposes == frozenset({"conversation"})
    assert tenant_policy.allowed_providers == frozenset({"fake"})

    assert source.provider_policy().eligible_providers == frozenset({"fake"})


def test_authorize_call_data_access_fails_closed_with_no_system_actor() -> None:
    """Refuses to guess a `core.users.id` rather than fail later inside
    SaaS-OS's own foreign key with a far less legible error."""
    source = StaticAiDataPolicySource(AiProviderSettings())
    with pytest.raises(PrivacyConfigurationError):
        authorize_call_data_access(
            tenant_id=uuid.uuid4(),
            call_session_id=uuid.uuid4(),
            data_classification="tenant_data",
            purpose="conversation",
            provider="fake",
            policy_source=source,
            system_actor_user_id=None,
        )
