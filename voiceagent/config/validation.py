"""Operational-readiness validation for staging/production (Phase 2.19).

Distinct from `Settings.__post_init__`/`Settings._validate_production`
(`voiceagent.config.settings`) in one deliberate way: those run on every
`Settings` construction -- including every test in this repository -- and
therefore must never touch `infra.secrets` or any other I/O (module
docstring rule 2: "It never reads a secret"). `validate_deployment_readiness`
below is the opposite: it is allowed to ask `infra.secrets.SecretsProvider`
whether a name is *present*, because that is the only way to fail fast on a
missing OIDC client id or vendor API key before the first request/call
depends on it (today, both fail lazily -- an OIDC route returns a bare 503,
a provider adapter raises only when a call is first attempted).

It is therefore never called from `Settings` itself. Each process entrypoint
calls it explicitly, once, right after building `Settings` and before doing
any other work:

* `voiceagent.api.app.build_app()` registers it as a FastAPI startup event
  (runs when the server actually starts, never at import/build time -- the
  same "no I/O at import or build time" invariant `build_app()` already
  documents for everything else).
* `scripts/run_call_runtime.py`, `scripts/run_followup_worker.py`,
  `scripts/run_call_intelligence_worker.py` call it directly in `_run()`.

It reads secret *presence* only (`SecretsProvider.get(name) is not None`),
never a secret's value -- nothing here ever appears in a log line or an
exception message except a variable *name*, exactly like
`ConfigurationError`'s own existing safety contract.

A `deployment_stage` of `"development"` or `"test"` is a deliberate no-op:
Phase 2.19 brief section 10, "distinguish test/development allowances from
staging/production requirements."
"""

from __future__ import annotations

import os

from voiceagent.config.settings import ConfigurationError, Settings

__all__ = ["validate_deployment_readiness"]

#: Provider name -> the `infra.secrets` name its adapter reads at
#: construction time (`voiceagent.providers.llm.groq/gemini/mistral/openai`,
#: `voiceagent.providers.stt.deepgram/assemblyai`,
#: `voiceagent.providers.tts.elevenlabs/deepgram_aura`,
#: `voiceagent.providers.call_intelligence.groq`). `"fake"` deliberately has
#: no entry: it reads no secret and is never checked here.
_PROVIDER_SECRET_NAMES: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "deepgram_aura": "DEEPGRAM_API_KEY",
}


def validate_deployment_readiness(settings: Settings) -> None:
    """Fail fast with a clear `ConfigurationError` when `settings` claims a
    staging/production `deployment_stage` but is not actually ready for it.

    A no-op for `"development"`/`"test"`. Never returns or logs a secret
    value -- only ever names a missing environment/secret variable.
    """
    if settings.deployment_stage not in ("staging", "production"):
        return

    _validate_ai_provider_selection(settings)
    _validate_telephony_consistency(settings)
    _validate_telephony_secrets_present(settings)
    _validate_provider_secrets_present(settings)
    _validate_oidc_configured(settings)


def _validate_ai_provider_selection(settings: Settings) -> None:
    """Section 6/10: staging/production must not be left on the deployment's
    unconfigured default of fake-only, where that would be silent rather
    than a deliberate operator choice."""
    if settings.ai_providers.eligible_providers == ("fake",):
        raise ConfigurationError(
            "VOICEAGENT_AI_ELIGIBLE_PROVIDERS is still the default ('fake') under "
            f"VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r}. Set it to the "
            "real provider(s) this deployment is authorized to use, or set "
            "VOICEAGENT_DEPLOYMENT_STAGE=development if this is intentional."
        )
    if settings.call_intelligence.provider == "fake":
        raise ConfigurationError(
            "VOICEAGENT_CALL_INTELLIGENCE_PROVIDER is still 'fake' under "
            f"VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r}. Set it to a real "
            "provider, or set VOICEAGENT_DEPLOYMENT_STAGE=development if this is "
            "intentional."
        )


def _validate_telephony_consistency(settings: Settings) -> None:
    """Section 7/9: FreeSWITCH is optional (most staging deployments will not
    yet have a live one). But a deployment that *has* pointed this product
    at one (`VOICEAGENT_FREESWITCH_ESL_HOST` set -- Phase 2.21's real ESL/
    media transport, `voiceagent.telephony.freeswitch.esl_transport
    /.media_transport`) must also give it a reachable, non-plaintext public
    media URL -- half-configured telephony is worse than none."""
    freeswitch = settings.freeswitch
    if not freeswitch.is_configured:
        return
    if not freeswitch.media_public_url:
        raise ConfigurationError(
            "VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL is required when "
            "VOICEAGENT_FREESWITCH_ESL_HOST is set "
            f"(VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r})."
        )
    if not freeswitch.media_public_url.startswith(("https://", "wss://")):
        raise ConfigurationError(
            "VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL must be an https:// or wss:// URL "
            f"in staging/production, got: {freeswitch.media_public_url!r}"
        )


def _validate_telephony_secrets_present(settings: Settings) -> None:
    """Phase 2.21: the two real secrets a live FreeSWITCH integration needs
    -- `FREESWITCH_ESL_PASSWORD` (`voiceagent.telephony.freeswitch
    .esl_transport.ManagedEslConnection`'s `password_provider`) and
    `FREESWITCH_MEDIA_TICKET_SECRET` (`voiceagent.telephony.freeswitch
    .media_transport.mint_media_ticket()`/`verify_media_ticket()`) -- are
    never `Settings` fields (Phase 2.19's own established rule: a
    deployment-level secret is read through `infra.secrets`, never carried
    on a settings dataclass) and so cannot be checked by
    `Settings.__post_init__`. Checked here, by presence only, for the
    identical reason every AI-provider secret already is."""
    if not settings.freeswitch.is_configured:
        return
    from infra.secrets import get_secrets_provider

    provider = get_secrets_provider()
    for secret_name in ("FREESWITCH_ESL_PASSWORD", "FREESWITCH_MEDIA_TICKET_SECRET"):
        if not provider.get(secret_name):
            raise ConfigurationError(
                f"{secret_name} is required when VOICEAGENT_FREESWITCH_ESL_HOST is set "
                f"(VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r}) but is not "
                "configured in this deployment's secret store."
            )


def _validate_provider_secrets_present(settings: Settings) -> None:
    """Section 6/10: "missing provider credentials when a real provider is
    selected" -- checked for every provider named in the deployment-wide
    eligibility policy (`voiceagent.runtime.privacy`) plus the
    call-intelligence provider, since both select a real adapter by name.
    Presence only: the value itself is never read into this process's own
    memory beyond the provider's own `get()` call, and never logged."""
    from infra.secrets import get_secrets_provider

    provider = get_secrets_provider()
    selected = set(settings.ai_providers.eligible_providers) | {settings.call_intelligence.provider}
    for name in sorted(selected):
        secret_name = _PROVIDER_SECRET_NAMES.get(name)
        if secret_name is None:
            continue
        if not provider.get(secret_name):
            raise ConfigurationError(
                f"{secret_name} is required (selected provider: {name!r}) but is not "
                "configured in this deployment's secret store."
            )


def _validate_oidc_configured(settings: Settings) -> None:
    """Section 5: "the application must fail clearly when staging is
    configured to require OIDC but required configuration is missing."
    SaaS-OS's own OIDC routes validate this lazily, per-request
    (`api/auth/routes.py` catches `OIDCConfigurationError`/`LookupError` and
    returns a bare 503) -- this check exists to surface the identical gap at
    startup instead, without duplicating the platform's own parsing.

    `OIDC_REDIRECT_URI` is plain, non-secret environment configuration
    (`api/auth/config.py:AuthHttpConfig`); `ZITADEL_ISSUER_URL`/
    `ZITADEL_CLIENT_ID` are read through `infra.secrets`
    (`core/identity/provider.py`) even though they are not credentials in
    the traditional sense, because that is where SaaS-OS itself put them --
    this module follows that choice rather than re-deciding it.
    """
    from infra.secrets import get_secrets_provider

    redirect_uri = os.environ.get("OIDC_REDIRECT_URI")
    if not redirect_uri:
        raise ConfigurationError(
            "OIDC_REDIRECT_URI is required "
            f"(VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r}) but is not set."
        )
    if not redirect_uri.startswith("https://"):
        raise ConfigurationError(
            f"OIDC_REDIRECT_URI must be an https:// URL in staging/production, got: "
            f"{redirect_uri!r}"
        )

    provider = get_secrets_provider()
    for secret_name in ("ZITADEL_ISSUER_URL", "ZITADEL_CLIENT_ID"):
        if not provider.get(secret_name):
            raise ConfigurationError(
                f"{secret_name} is required "
                f"(VOICEAGENT_DEPLOYMENT_STAGE={settings.deployment_stage!r}) but is not "
                "configured in this deployment's secret store."
            )
