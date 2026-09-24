"""`voiceagent.config.validation.validate_deployment_readiness` (Phase 2.19).

Every case here builds a `Settings` value with `deployment_stage="staging"`
(the identical checks apply to `"production"`; the module makes no
distinction between the two -- see its own docstring) and either sets or
clears real process-environment variables via `monkeypatch`, because
`infra.secrets.get_secrets_provider()` for `ENVIRONMENT` values `test` and
`production` reads real `os.environ`, never a file. No test here ever reads
a secret value back out of a `ConfigurationError` -- only asserts that one
was raised.
"""

from __future__ import annotations

import pytest
from core.config import Settings as PlatformSettings

from voiceagent.config import ConfigurationError, Settings, validate_deployment_readiness
from voiceagent.config.settings import (
    AiProviderSettings,
    CallIntelligenceSettings,
    FreeSwitchSettings,
)


def _platform(**overrides) -> PlatformSettings:
    return PlatformSettings(**{"environment": "production", **overrides})


def _staging(**overrides) -> Settings:
    return Settings(platform=_platform(), deployment_stage="staging", **overrides)


#: Every env var a fully-configured, real-provider deployment needs present
#: for `validate_deployment_readiness` to accept it -- individual tests
#: `monkeypatch.delenv` exactly one to prove it is actually checked.
_REQUIRED_SECRETS = {
    "GROQ_API_KEY": "test-value",  # pragma: allowlist secret -- fake value, test-only
    "ZITADEL_ISSUER_URL": "https://idp.example.test",
    "ZITADEL_CLIENT_ID": "voiceagent-staging",
}


def _set_ready_environment(monkeypatch) -> None:
    for name, value in _REQUIRED_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://staging.example.test/auth/callback")


def _ready_settings() -> Settings:
    return _staging(
        ai_providers=AiProviderSettings(eligible_providers=("groq",)),
        call_intelligence=CallIntelligenceSettings(provider="groq", model="llama-3.3-70b"),
    )


def test_development_and_test_stages_are_a_no_op() -> None:
    """No secret is even looked up outside staging/production -- an
    unconfigured local/test deployment must never fail here."""
    validate_deployment_readiness(Settings(platform=PlatformSettings(environment="test")))
    validate_deployment_readiness(
        Settings(
            platform=PlatformSettings(environment="production"), deployment_stage="development"
        )
    )


def test_fully_configured_staging_deployment_passes(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    validate_deployment_readiness(_ready_settings())


def test_default_fake_ai_provider_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="VOICEAGENT_AI_ELIGIBLE_PROVIDERS"):
        validate_deployment_readiness(_staging())


def test_fake_call_intelligence_provider_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="VOICEAGENT_CALL_INTELLIGENCE_PROVIDER"):
        validate_deployment_readiness(
            _staging(ai_providers=AiProviderSettings(eligible_providers=("groq",)))
        )


def test_missing_ai_provider_secret_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="GROQ_API_KEY"):
        validate_deployment_readiness(_ready_settings())


def test_missing_oidc_redirect_uri_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
    with pytest.raises(ConfigurationError, match="OIDC_REDIRECT_URI"):
        validate_deployment_readiness(_ready_settings())


def test_insecure_oidc_redirect_uri_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    monkeypatch.setenv("OIDC_REDIRECT_URI", "http://staging.example.test/auth/callback")
    with pytest.raises(ConfigurationError, match="https://"):
        validate_deployment_readiness(_ready_settings())


def test_missing_zitadel_issuer_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    monkeypatch.delenv("ZITADEL_ISSUER_URL", raising=False)
    with pytest.raises(ConfigurationError, match="ZITADEL_ISSUER_URL"):
        validate_deployment_readiness(_ready_settings())


def test_missing_zitadel_client_id_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    monkeypatch.delenv("ZITADEL_CLIENT_ID", raising=False)
    with pytest.raises(ConfigurationError, match="ZITADEL_CLIENT_ID"):
        validate_deployment_readiness(_ready_settings())


def test_freeswitch_without_media_public_url_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    with pytest.raises(ConfigurationError, match="VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL"):
        validate_deployment_readiness(
            _ready_settings_with_freeswitch(FreeSwitchSettings(esl_host="fs.internal"))
        )


def test_freeswitch_with_insecure_media_public_url_is_rejected(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    with pytest.raises(ConfigurationError, match="https://|wss://"):
        validate_deployment_readiness(
            _ready_settings_with_freeswitch(
                FreeSwitchSettings(
                    esl_host="fs.internal", media_public_url="http://fs.example.test"
                )
            )
        )


def test_freeswitch_with_secure_media_public_url_passes(monkeypatch) -> None:
    _set_ready_environment(monkeypatch)
    validate_deployment_readiness(
        _ready_settings_with_freeswitch(
            FreeSwitchSettings(esl_host="fs.internal", media_public_url="wss://fs.example.test")
        )
    )


def _ready_settings_with_freeswitch(freeswitch: FreeSwitchSettings) -> Settings:
    return _staging(
        ai_providers=AiProviderSettings(eligible_providers=("groq",)),
        call_intelligence=CallIntelligenceSettings(provider="groq", model="llama-3.3-70b"),
        freeswitch=freeswitch,
    )


def test_unconfigured_freeswitch_is_not_required(monkeypatch) -> None:
    """FreeSWITCH stays optional (Phase 2.19 brief section 7 caveat: there is
    still no real ESL transport in this repository) -- a staging deployment
    that never set `VOICEAGENT_FREESWITCH_ESL_HOST` is not forced to."""
    _set_ready_environment(monkeypatch)
    validate_deployment_readiness(_ready_settings())
