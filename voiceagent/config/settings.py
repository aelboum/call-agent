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
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

from core.config import Settings as PlatformSettings
from core.config import get_settings as get_platform_settings

__all__ = [
    "AiProviderSettings",
    "ConfigurationError",
    "FreeSwitchSettings",
    "ObjectStorageSettings",
    "RuntimeSettings",
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

    `eligible_providers`/`allowed_data_classifications`/`allowed_purposes` are
    a **deployment-wide default** `control_plane.data_authorization` policy
    (Phase 2.2 brief section 20; `voiceagent.runtime.privacy`). No per-tenant
    AI data policy table exists yet (Phase 2.0 report section 11.2 defers
    `provider_credentials`/tenant policy storage entirely) -- this default is
    what lets Phase 2.2 prove the *ordering* invariant ("no audio reaches an
    engine before authorization succeeds") against fakes; it is not real
    per-tenant policy enforcement and is documented as a known limitation in
    `docs/PHASE-2.2-STATUS.md`.
    """

    default_engine: str = "fake"
    eligible_providers: tuple[str, ...] = ("fake",)
    allowed_data_classifications: tuple[str, ...] = ("tenant_data",)
    allowed_purposes: tuple[str, ...] = ("conversation",)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """`call-runtime` process tunables (ADR-0008 point 15: "each needs a
    benchmark against real load before a default is chosen"). Every default
    below is a conservative placeholder for Phase 2.2's own tests and local
    development, not a benchmarked production value -- OQ-1 (Phase 2.0
    report section 20) remains open.
    """

    #: Least-loaded assignment refuses a runtime at or above this many
    #: concurrent calls (ADR-0008 points 5, 6, 14).
    max_concurrent_calls: int = 50
    #: Size of the bounded thread pool every database/SaaS-OS call from the
    #: call-runtime process crosses (ADR-0008 point 2; section 16 of this
    #: phase's brief). Never the asyncio default executor, so the boundary
    #: is an explicit, sized resource rather than an implicit shared one.
    to_thread_pool_size: int = 8
    #: How often a runtime process refreshes its own Redis heartbeat key.
    heartbeat_interval_seconds: float = 5.0
    #: The heartbeat key's TTL. Must exceed `heartbeat_interval_seconds` by a
    #: comfortable margin, or ordinary scheduling jitter reads as a crash.
    heartbeat_ttl_seconds: float = 15.0
    #: How often the reconciliation loop scans for stale ownership.
    reconciliation_interval_seconds: float = 30.0
    #: The `core.users.id` the runtime attributes `authorize_data_access()`
    #: calls to (`voiceagent.runtime.privacy`). That SaaS-OS function's own
    #: `actor_user_id` parameter carries a real foreign key to `core.users`
    #: (verified at the pinned SHA), while the call runtime's own acting
    #: principal is a `core.identity.ServiceAccount` with no human user in a
    #: call's context (Phase 0 report section 14.4) -- a genuine mismatch
    #: this phase documents rather than works around by modifying SaaS-OS
    #: (forbidden, ADR-0001). An operator provisions this user once (e.g.
    #: via `scripts/bootstrap_rbac.py`) and configures its id here; `None`
    #: is a valid, deliberately fail-closed default (`voiceagent.runtime.privacy`
    #: refuses to call `authorize_data_access()` without it, rather than
    #: guessing a UUID that would fail its own foreign key at write time).
    system_actor_user_id: uuid.UUID | None = None
    #: The `core.identity.ServiceAccount.name` the runtime resolves, *per
    #: tenant*, for `core.rbac.can()` authorization checks
    #: (`voiceagent.tools.gateway`, Phase 2.4) -- a **name**, not a fixed
    #: `uuid.UUID`, and deliberately so: unlike `system_actor_user_id` above
    #: (a `core.users.id`, which is not tenant-bound -- one user row can hold
    #: separate memberships in many tenants), a `core.identity.ServiceAccount`
    #: row's `tenant_id` is fixed permanently at creation. One global service
    #: account id therefore cannot authorize calls across more than the one
    #: tenant it was created in -- a real design error caught during Phase
    #: 2.4's own integration testing (a cross-tenant `core.rbac.can()` check
    #: correctly failed closed, but writing its *audit* entry then violated
    #: `core.audit_log`'s own `fk_audit_log_tenant_service_account` constraint,
    #: which exists specifically to foreclose cross-tenant attribution --
    #: see `docs/PHASE-2.4-STATUS.md`). An operator instead provisions one
    #: service account *per tenant*, all sharing this one configured name
    #: (e.g. via `scripts/bootstrap_rbac.py --service-account-name`), and
    #: `ToolGateway.execute()` resolves the right row for the call's own
    #: tenant at authorization time (`core.identity.list_service_accounts()`).
    #: A tenant with no matching, active service account fails closed --
    #: audited as a `SYSTEM` actor (no id to attribute cross-tenant), never
    #: silently skipped.
    system_service_account_name: str = "voiceagent-runtime"


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
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

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


def _parse_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got: {raw!r}") from exc


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got: {raw!r}") from exc


def _parse_uuid(name: str, raw: str | None) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a UUID, got: {raw!r}") from exc


def settings_from_env(platform: PlatformSettings | None = None) -> Settings:
    """Build `Settings` from the environment. Pure apart from `os.environ`:
    no database, no network, no secret store. Tests call it directly with
    `monkeypatch.setenv(...)` instead of clearing a cache."""
    esl_port_raw = os.environ.get("VOICEAGENT_FREESWITCH_ESL_PORT")
    defaults = RuntimeSettings()

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
            eligible_providers=_parse_csv(
                os.environ.get("VOICEAGENT_AI_ELIGIBLE_PROVIDERS"),
                AiProviderSettings().eligible_providers,
            ),
            allowed_data_classifications=_parse_csv(
                os.environ.get("VOICEAGENT_AI_ALLOWED_DATA_CLASSIFICATIONS"),
                AiProviderSettings().allowed_data_classifications,
            ),
            allowed_purposes=_parse_csv(
                os.environ.get("VOICEAGENT_AI_ALLOWED_PURPOSES"),
                AiProviderSettings().allowed_purposes,
            ),
        ),
        runtime=RuntimeSettings(
            max_concurrent_calls=(
                _parse_int("VOICEAGENT_RUNTIME_MAX_CONCURRENT_CALLS", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_MAX_CONCURRENT_CALLS")) is not None
                else defaults.max_concurrent_calls
            ),
            to_thread_pool_size=(
                _parse_int("VOICEAGENT_RUNTIME_TO_THREAD_POOL_SIZE", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_TO_THREAD_POOL_SIZE")) is not None
                else defaults.to_thread_pool_size
            ),
            heartbeat_interval_seconds=(
                _parse_float("VOICEAGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS"))
                is not None
                else defaults.heartbeat_interval_seconds
            ),
            heartbeat_ttl_seconds=(
                _parse_float("VOICEAGENT_RUNTIME_HEARTBEAT_TTL_SECONDS", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_HEARTBEAT_TTL_SECONDS")) is not None
                else defaults.heartbeat_ttl_seconds
            ),
            reconciliation_interval_seconds=(
                _parse_float("VOICEAGENT_RUNTIME_RECONCILIATION_INTERVAL_SECONDS", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_RECONCILIATION_INTERVAL_SECONDS"))
                is not None
                else defaults.reconciliation_interval_seconds
            ),
            system_actor_user_id=_parse_uuid(
                "VOICEAGENT_RUNTIME_SYSTEM_ACTOR_USER_ID",
                os.environ.get("VOICEAGENT_RUNTIME_SYSTEM_ACTOR_USER_ID"),
            ),
            system_service_account_name=os.environ.get(
                "VOICEAGENT_RUNTIME_SYSTEM_SERVICE_ACCOUNT_NAME",
                defaults.system_service_account_name,
            ),
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
