"""Typed product configuration (Phase 1).

Three rules shape this module:

1. **It never duplicates SaaS-OS configuration.** Environment, debug, log
   level, host/port and the `/v1` prefix are already owned by
   `core.config.Settings`; the database URL by `infra.db.config`; Redis/jobs
   by `infra.jobs.config`; telemetry by `infra.observability`. `Settings`
   below *composes* the platform's settings object rather than re-parsing the
   same variables, so the two can never disagree.
2. **It never reads a secret.** Every field here is non-secret configuration.
   A real secret is read through `infra.secrets`' `SecretsProvider`, never
   from this module, and never appears in an exception message.
3. **Provider credentials are not deployment-global.** The Phase 0
   architecture (section 2.4 G-3) established that a tenant's own provider
   credentials become product-owned rows encrypted with `core.crypto` using
   `tenant_id` as associated data. The placeholders below therefore carry
   *selection* and *endpoint* configuration only -- never an API key field,
   not even an optional one, because an optional key field is how a
   deployment-global credential quietly becomes the default.

Product variables are namespaced `VOICEAGENT_*`; platform variables keep
their own names (`ENVIRONMENT`, `DEBUG`, `DATABASE_URL`, ...).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from core.config import Settings as PlatformSettings
from core.config import get_settings as get_platform_settings

__all__ = [
    "AiProviderSettings",
    "ConfigurationError",
    "FreeSwitchSettings",
    "ObjectStorageSettings",
    "Settings",
    "get_settings",
]


class ConfigurationError(ValueError):
    """Raised when a product environment variable holds an invalid value.

    Carries the variable *name* and the offending value. Safe today because
    no field on `Settings` is a secret; if that ever changes, the offending
    value must not be included (docs/SECURITY discipline inherited from
    `core.config`).
    """


@dataclass(frozen=True, slots=True)
class ObjectStorageSettings:
    """Where call artifacts will live (Phase 0 report section 2.4 G-2: SaaS-OS
    provides no product object storage; `boto3` exists there only for
    platform backup tooling).

    Phase 1 carries configuration only -- no client, no bucket creation, no
    credentials. Credentials are resolved by the storage adapter from the
    deployment's own secret source when that adapter is built (Phase 2+).
    """

    provider: str | None = None
    bucket: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    prefix: str = "recordings"

    @property
    def is_configured(self) -> bool:
        return bool(self.provider and self.bucket)


@dataclass(frozen=True, slots=True)
class FreeSwitchSettings:
    """Telephony core endpoint configuration (ADR-0002).

    Phase 1 carries configuration only: nothing connects to FreeSWITCH, and
    no ESL client exists. There is deliberately no ESL password field -- that
    is a secret, read through `infra.secrets` by the adapter in Phase 2.
    """

    esl_host: str | None = None
    esl_port: int = 8021
    media_public_url: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.esl_host)


@dataclass(frozen=True, slots=True)
class AiProviderSettings:
    """AI provider *selection* configuration (ADR-0006).

    Deliberately holds no credential field of any kind. `default_engine`
    names which `ConversationEngine` implementation a tenant gets when it has
    expressed no preference; it is a selector, not a vendor coupling, and the
    only value the Phase 1 foundation understands is `"fake"`.
    """

    default_engine: str = "fake"


@dataclass(frozen=True, slots=True)
class Settings:
    """The product's configuration root.

    `platform` is SaaS-OS's own settings object, held rather than copied.
    """

    platform: PlatformSettings
    app_display_name: str = "AI Call Agent"
    cors_allowed_origins: tuple[str, ...] = ()
    telemetry_service_name: str = "voiceagent"
    object_storage: ObjectStorageSettings = field(default_factory=ObjectStorageSettings)
    freeswitch: FreeSwitchSettings = field(default_factory=FreeSwitchSettings)
    ai_providers: AiProviderSettings = field(default_factory=AiProviderSettings)

    @property
    def environment(self) -> str:
        return self.platform.environment

    @property
    def debug(self) -> bool:
        return self.platform.debug

    @property
    def api_prefix(self) -> str:
        return self.platform.api_v1_prefix

    def __post_init__(self) -> None:
        if not self.app_display_name.strip():
            raise ConfigurationError("VOICEAGENT_APP_DISPLAY_NAME must not be empty")
        if "*" in self.cors_allowed_origins and self.environment == "production":
            raise ConfigurationError(
                "VOICEAGENT_CORS_ALLOWED_ORIGINS must not contain '*' in production"
            )
        if self.environment == "production":
            self._validate_production()

    def _validate_production(self) -> None:
        """Fail closed on configuration that is merely inconvenient in
        development but unsafe in production. Called from `__post_init__`, so
        a misconfigured production process cannot start."""
        if self.platform.debug:
            raise ConfigurationError("DEBUG must be false when ENVIRONMENT=production")
        for origin in self.cors_allowed_origins:
            if not origin.startswith("https://"):
                raise ConfigurationError(
                    "VOICEAGENT_CORS_ALLOWED_ORIGINS entries must be https:// URLs in production, "
                    f"got: {origin!r}"
                )


def _parse_origins(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


def _parse_port(name: str, raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} must be between 1 and 65535, got: {port}")
    return port


def settings_from_env(platform: PlatformSettings | None = None) -> Settings:
    """Build `Settings` from the environment. Pure apart from `os.environ`:
    no database, no network, no secret store. Tests call it directly with
    `monkeypatch.setenv(...)` instead of clearing a cache."""
    esl_port_raw = os.environ.get("VOICEAGENT_FREESWITCH_ESL_PORT")

    return Settings(
        platform=platform if platform is not None else get_platform_settings(),
        app_display_name=os.environ.get("VOICEAGENT_APP_DISPLAY_NAME", "AI Call Agent"),
        cors_allowed_origins=_parse_origins(os.environ.get("VOICEAGENT_CORS_ALLOWED_ORIGINS")),
        telemetry_service_name=os.environ.get("VOICEAGENT_TELEMETRY_SERVICE_NAME", "voiceagent"),
        object_storage=ObjectStorageSettings(
            provider=os.environ.get("VOICEAGENT_OBJECT_STORAGE_PROVIDER"),
            bucket=os.environ.get("VOICEAGENT_OBJECT_STORAGE_BUCKET"),
            endpoint_url=os.environ.get("VOICEAGENT_OBJECT_STORAGE_ENDPOINT_URL"),
            region=os.environ.get("VOICEAGENT_OBJECT_STORAGE_REGION"),
            prefix=os.environ.get("VOICEAGENT_OBJECT_STORAGE_PREFIX", "recordings"),
        ),
        freeswitch=FreeSwitchSettings(
            esl_host=os.environ.get("VOICEAGENT_FREESWITCH_ESL_HOST"),
            esl_port=(
                _parse_port("VOICEAGENT_FREESWITCH_ESL_PORT", esl_port_raw)
                if esl_port_raw is not None
                else 8021
            ),
            media_public_url=os.environ.get("VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL"),
        ),
        ai_providers=AiProviderSettings(
            default_engine=os.environ.get("VOICEAGENT_DEFAULT_ENGINE", "fake"),
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings, read once from the environment.

    Injected into the application by `voiceagent.api.build_app()`; a test that
    needs different values passes its own `Settings` to `build_app()` rather
    than mutating this cache.
    """
    return settings_from_env()
