"""Product configuration (Phase 1 brief section 7).

Two properties carry real weight here and the rest is ordinary parsing:
production configuration fails closed, and no provider settings object has
anywhere to put a credential.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from core.config import Settings as PlatformSettings

from voiceagent.config import (
    AiProviderSettings,
    ConfigurationError,
    FreeSwitchSettings,
    ObjectStorageSettings,
    RuntimeSettings,
    Settings,
    settings_from_env,
)


def _platform(**overrides) -> PlatformSettings:
    return PlatformSettings(**{"environment": "test", **overrides})


def test_defaults_are_safe(settings: Settings) -> None:
    """An unconfigured deployment gets no CORS, no object storage, no
    FreeSWITCH and the fake engine -- nothing that reaches outward."""
    assert settings.cors_allowed_origins == ()
    assert settings.object_storage.is_configured is False
    assert settings.freeswitch.is_configured is False
    assert settings.ai_providers.default_engine == "fake"


def test_platform_settings_are_composed_not_duplicated(settings: Settings) -> None:
    """Environment, debug and the `/v1` prefix have exactly one owner
    (`core.config`), so the two layers cannot disagree."""
    assert settings.environment == settings.platform.environment
    assert settings.debug == settings.platform.debug
    assert settings.api_prefix == settings.platform.api_v1_prefix


def test_display_name_comes_from_configuration(monkeypatch) -> None:
    """The commercial name is deliberately undecided (ADR-0005): it is
    configuration, never a literal in the code."""
    monkeypatch.setenv("VOICEAGENT_APP_DISPLAY_NAME", "Acme Reception")
    assert settings_from_env(_platform()).app_display_name == "Acme Reception"


def test_empty_display_name_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_APP_DISPLAY_NAME", "   ")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_origins_parse_as_a_list(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_CORS_ALLOWED_ORIGINS", "https://a.test, https://b.test ,")
    assert settings_from_env(_platform()).cors_allowed_origins == (
        "https://a.test",
        "https://b.test",
    )


def test_invalid_port_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_ESL_PORT", "not-a-port")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_production_rejects_debug() -> None:
    with pytest.raises(ConfigurationError):
        Settings(platform=_platform(environment="production", debug=True))


def test_production_requires_https_origins() -> None:
    with pytest.raises(ConfigurationError):
        Settings(
            platform=_platform(environment="production"),
            cors_allowed_origins=("http://insecure.test",),
        )


def test_production_rejects_wildcard_origin() -> None:
    with pytest.raises(ConfigurationError):
        Settings(platform=_platform(environment="production"), cors_allowed_origins=("*",))


def test_valid_production_configuration_is_accepted() -> None:
    config = Settings(
        platform=_platform(environment="production"),
        cors_allowed_origins=("https://app.test",),
    )
    assert config.environment == "production"


@pytest.mark.parametrize(
    "settings_class", [ObjectStorageSettings, FreeSwitchSettings, AiProviderSettings]
)
def test_provider_settings_hold_no_credentials(settings_class) -> None:
    """Phase 0 report section 2.4 G-3: provider credentials are not
    deployment-global. A tenant's credentials become product rows encrypted
    with `core.crypto` (tenant_id as AAD) in a later phase; a deployment-level
    secret is read through `infra.secrets`. An optional key field here is how
    a global credential quietly becomes the default, so there is none -- not
    even optional."""
    forbidden = {"key", "secret", "token", "password", "credential", "api_key"}
    names = {field.name for field in dataclasses.fields(settings_class)}
    assert not any(any(word in name for word in forbidden) for name in names), names


def test_settings_are_immutable(settings: Settings) -> None:
    """Configuration is a value, not mutable global state."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.app_display_name = "changed"  # type: ignore[misc]


def test_runtime_defaults_are_conservative(settings: Settings) -> None:
    """ADR-0008 point 15: real values are a benchmarking task, not
    invented here -- but an unconfigured deployment must still get sane,
    non-zero tunables and no system actor (fail-closed on privacy
    authorization, `voiceagent.runtime.privacy`)."""
    assert settings.runtime.max_concurrent_calls > 0
    assert settings.runtime.to_thread_pool_size > 0
    assert settings.runtime.heartbeat_ttl_seconds > settings.runtime.heartbeat_interval_seconds
    assert settings.runtime.system_actor_user_id is None
    assert settings.runtime.system_service_account_name == "voiceagent-runtime"


def test_runtime_settings_parse_from_env(monkeypatch) -> None:
    system_actor = uuid.uuid4()
    monkeypatch.setenv("VOICEAGENT_RUNTIME_MAX_CONCURRENT_CALLS", "25")
    monkeypatch.setenv("VOICEAGENT_RUNTIME_TO_THREAD_POOL_SIZE", "4")
    monkeypatch.setenv("VOICEAGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("VOICEAGENT_RUNTIME_HEARTBEAT_TTL_SECONDS", "7.5")
    monkeypatch.setenv("VOICEAGENT_RUNTIME_RECONCILIATION_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("VOICEAGENT_RUNTIME_SYSTEM_ACTOR_USER_ID", str(system_actor))
    monkeypatch.setenv("VOICEAGENT_RUNTIME_SYSTEM_SERVICE_ACCOUNT_NAME", "custom-runtime-name")

    runtime = settings_from_env(_platform()).runtime
    assert runtime == RuntimeSettings(
        max_concurrent_calls=25,
        to_thread_pool_size=4,
        heartbeat_interval_seconds=2.5,
        heartbeat_ttl_seconds=7.5,
        reconciliation_interval_seconds=60.0,
        system_actor_user_id=system_actor,
        system_service_account_name="custom-runtime-name",
    )


def test_invalid_system_actor_user_id_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_RUNTIME_SYSTEM_ACTOR_USER_ID", "not-a-uuid")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_invalid_runtime_integer_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_RUNTIME_MAX_CONCURRENT_CALLS", "not-an-int")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_deployment_stage_defaults_to_development(settings: Settings) -> None:
    """Unconfigured (test environment): no staging/production posture is
    silently assumed."""
    assert settings.deployment_stage == "development"


def test_deployment_stage_defaults_from_production_environment(monkeypatch) -> None:
    """Phase 2.18's own Compose stack sets `ENVIRONMENT=production` and knows
    nothing of `VOICEAGENT_DEPLOYMENT_STAGE` -- it must keep getting the
    hardened validation path, not silently fall back to 'development'."""
    monkeypatch.delenv("VOICEAGENT_DEPLOYMENT_STAGE", raising=False)
    resolved = settings_from_env(_platform(environment="production"))
    assert resolved.deployment_stage == "production"


def test_deployment_stage_reads_explicit_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_DEPLOYMENT_STAGE", "staging")
    resolved = settings_from_env(_platform(environment="production"))
    assert resolved.deployment_stage == "staging"


def test_invalid_deployment_stage_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Settings(platform=_platform(environment="production"), deployment_stage="prod")


def test_staging_deployment_stage_requires_production_environment() -> None:
    """SaaS-OS's `ENVIRONMENT` has no 'staging' value (ADR-0001: SaaS-OS is
    never modified) -- staging must run underneath `ENVIRONMENT=production`."""
    with pytest.raises(ConfigurationError):
        Settings(platform=_platform(environment="test"), deployment_stage="staging")


def test_staging_deployment_stage_is_accepted_under_production_environment() -> None:
    config = Settings(platform=_platform(environment="production"), deployment_stage="staging")
    assert config.deployment_stage == "staging"


def test_freeswitch_command_timeout_parses_from_env(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_COMMAND_TIMEOUT_SECONDS", "2.5")
    assert settings_from_env(_platform()).freeswitch.command_timeout_seconds == 2.5


def test_freeswitch_command_timeout_has_a_conservative_default(settings: Settings) -> None:
    assert settings.freeswitch.command_timeout_seconds == 10.0


def test_invalid_freeswitch_command_timeout_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_COMMAND_TIMEOUT_SECONDS", "not-a-number")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_call_intelligence_endpoint_defaults_to_none(settings: Settings) -> None:
    assert settings.call_intelligence.endpoint is None


def test_freeswitch_media_listen_defaults(settings: Settings) -> None:
    assert settings.freeswitch.media_listen_host == "0.0.0.0"  # noqa: S104
    assert settings.freeswitch.media_listen_port == 8100


def test_freeswitch_media_listen_parses_from_env(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_MEDIA_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_MEDIA_LISTEN_PORT", "9100")
    freeswitch = settings_from_env(_platform()).freeswitch
    assert freeswitch.media_listen_host == "127.0.0.1"
    assert freeswitch.media_listen_port == 9100


def test_invalid_freeswitch_media_listen_port_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_FREESWITCH_MEDIA_LISTEN_PORT", "not-a-port")
    with pytest.raises(ConfigurationError):
        settings_from_env(_platform())


def test_call_intelligence_endpoint_parses_from_env(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_CALL_INTELLIGENCE_ENDPOINT", "https://proxy.internal/v1")
    assert settings_from_env(_platform()).call_intelligence.endpoint == "https://proxy.internal/v1"


def test_ai_provider_policy_lists_parse_from_env(monkeypatch) -> None:
    monkeypatch.setenv("VOICEAGENT_AI_ELIGIBLE_PROVIDERS", "fake, deepgram-shaped")
    monkeypatch.setenv("VOICEAGENT_AI_ALLOWED_DATA_CLASSIFICATIONS", "tenant_data, pii")
    monkeypatch.setenv("VOICEAGENT_AI_ALLOWED_PURPOSES", "conversation")

    ai_providers = settings_from_env(_platform()).ai_providers
    assert ai_providers.eligible_providers == ("fake", "deepgram-shaped")
    assert ai_providers.allowed_data_classifications == ("tenant_data", "pii")
    assert ai_providers.allowed_purposes == ("conversation",)
