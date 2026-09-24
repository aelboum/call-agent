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
    "CallIntelligenceSettings",
    "ConfigurationError",
    "DEPLOYMENT_STAGES",
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
    #: Bound on every ESL command (`FreeSwitchTelephonyProvider._command()`),
    #: mirroring that class's own constructor default (Phase 2.19). No ESL
    #: password field exists here, deliberately: there is still no real TCP
    #: transport to `mod_event_socket` in this repository (`esl.py`'s own
    #: module docstring), and an ESL credential is a secret in any case --
    #: read through `infra.secrets` by a future real transport, never by
    #: this module.
    command_timeout_seconds: float = 10.0

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
    #: Per-call bound on `voiceagent.runtime.conversation_persistence
    #: .ConversationPersistence`'s own queue (Phase 2.5 brief section 19:
    #: "never allow an unbounded queue"). A queue this full for one call
    #: means persistence is falling behind that call's own event rate;
    #: further turns are dropped and logged, never blocked on, and never
    #: grown past this bound.
    conversation_persistence_queue_size: int = 256
    #: How many times a single conversation turn's write is retried (with a
    #: linear backoff, `conversation_persistence_retry_backoff_seconds`)
    #: before the failure is logged as final and the turn is given up on
    #: (brief section 7: "bounded retry where appropriate").
    conversation_persistence_max_retries: int = 3
    #: The backoff multiplier between retries above.
    conversation_persistence_retry_backoff_seconds: float = 0.5
    #: How long `run_call_task()`'s own teardown waits for a call's
    #: conversation-persistence worker to drain its already-queued turns
    #: before force-cancelling it (brief section 6: "deterministic shutdown
    #: behavior") -- bounds how much a slow/unavailable database can delay
    #: one call's own teardown.
    conversation_persistence_drain_timeout_seconds: float = 5.0
    #: `voiceagent.runtime.stuck_calls` thresholds (Phase 2.14 brief section
    #: 13/15). Conservative placeholders, like every other duration in this
    #: dataclass (module docstring, ADR-0008 point 15) -- not a benchmarked
    #: production value. A call still `initiated`/`ringing` past this many
    #: seconds is reported stuck in startup.
    stuck_call_startup_threshold_seconds: float = 60.0
    #: A call still `answered`/`in_progress` past this many seconds is
    #: reported stuck active. Deliberately generous: real calls can
    #: legitimately run long, and this is a detection signal for an
    #: operator to investigate, never an automatic cutoff (this module never
    #: cancels a call itself).
    stuck_call_active_threshold_seconds: float = 3600.0


@dataclass(frozen=True, slots=True)
class CallIntelligenceSettings:
    """`voiceagent.call_intelligence.worker.CallAiAnalysisWorker` process
    tunables (Phase 2.12). Deliberately its own dataclass, not a widening of
    `RuntimeSettings`: this worker is a background process, sharing neither
    the call-runtime's own `to_thread_pool_size`-bounded `DatabaseBoundary`
    nor its `system_actor_user_id` (see `voiceagent.followups.worker
    .FollowUpWorker`'s own module docstring for why a background worker
    keeps its own separate system-actor value rather than sharing that
    dataclass).

    `provider`/`model` select the post-call analysis provider
    (`voiceagent.providers.call_intelligence.registry`) -- `"fake"` by
    default, matching every other AI provider selector in this product
    (`AiProviderSettings.default_engine`). `system_actor_user_id` is `None`
    by default (fail-closed: `voiceagent.call_intelligence.analyzer
    ._authorize()` cannot call `authorize_data_access()` without one, the
    identical discipline `RuntimeSettings.system_actor_user_id` already
    establishes).
    """

    provider: str = "fake"
    model: str = "fake-model"
    #: Vendor API endpoint override (Phase 2.19). `None` means "the
    #: provider's own documented default" (e.g.
    #: `GroqCallIntelligenceConfig.endpoint`'s `https://api.groq.com/openai/v1`)
    #: -- staging only needs this when pointing at something other than a
    #: vendor's public endpoint (a proxy, a regional endpoint).
    endpoint: str | None = None
    timeout_seconds: float = 30.0
    system_actor_user_id: uuid.UUID | None = None
    poll_interval_seconds: float = 30.0
    max_claims_per_tenant_per_tick: int = 5
    max_concurrent_tenants: int = 4


#: Every `deployment_stage` value this product understands. `core.config
#: .Settings.environment` (SaaS-OS) is hard-locked to exactly
#: `{"development", "test", "production"}` -- there is no `"staging"`
#: `ENVIRONMENT` value anywhere in the platform, and it cannot be added
#: (ADR-0001). `deployment_stage` is therefore a second, voiceagent-owned,
#: additive axis: a staging deployment runs with `ENVIRONMENT=production`
#: underneath (the correct posture -- it gets the platform's strict
#: `EnvironmentSecretsProvider` and every other production hardening for
#: free) and layers `VOICEAGENT_DEPLOYMENT_STAGE=staging` on top so this
#: product's own validation (`voiceagent.config.validation`) can tell "real
#: production" and "staging" apart without weakening either.
DEPLOYMENT_STAGES: tuple[str, ...] = ("development", "staging", "production")


@dataclass(frozen=True, slots=True)
class Settings:
    """The product's configuration root.

    `platform` is SaaS-OS's own settings object, held rather than copied.
    """

    platform: PlatformSettings
    #: See `DEPLOYMENT_STAGES`. Defaults to `"development"` when constructed
    #: directly (e.g. by a test); `settings_from_env()` derives a
    #: production-safe default from `platform.environment` when the operator
    #: has not set `VOICEAGENT_DEPLOYMENT_STAGE` explicitly.
    deployment_stage: str = "development"
    app_display_name: str = "AI Call Agent"
    cors_allowed_origins: tuple[str, ...] = ()
    telemetry_service_name: str = "voiceagent"
    object_storage: ObjectStorageSettings = field(default_factory=ObjectStorageSettings)
    freeswitch: FreeSwitchSettings = field(default_factory=FreeSwitchSettings)
    ai_providers: AiProviderSettings = field(default_factory=AiProviderSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    call_intelligence: CallIntelligenceSettings = field(default_factory=CallIntelligenceSettings)

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
        if self.deployment_stage not in DEPLOYMENT_STAGES:
            raise ConfigurationError(
                f"VOICEAGENT_DEPLOYMENT_STAGE must be one of {DEPLOYMENT_STAGES}, "
                f"got: {self.deployment_stage!r}"
            )
        if self.deployment_stage in ("staging", "production") and self.environment != "production":
            raise ConfigurationError(
                f"VOICEAGENT_DEPLOYMENT_STAGE={self.deployment_stage!r} requires "
                "ENVIRONMENT=production (SaaS-OS has no separate 'staging' ENVIRONMENT "
                f"value); got ENVIRONMENT={self.environment!r}"
            )
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
        a misconfigured production process cannot start.

        This is *structural* validation only -- it reads nothing but this
        object's own already-parsed, non-secret fields (module docstring
        rule 2: `Settings` never reads a secret). Operational readiness
        checks that need to confirm a secret's *presence* (an OIDC client
        id, a vendor API key) without ever reading its value live in
        `voiceagent.config.validation.validate_deployment_readiness`
        instead, called explicitly by each process entrypoint -- not from
        here, so that constructing a `Settings` value (as every test in
        this package does) never touches `infra.secrets`.
        """
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


def _resolve_deployment_stage(platform: PlatformSettings) -> str:
    """`VOICEAGENT_DEPLOYMENT_STAGE`, explicit or derived.

    Unset is the common case for every deployment that predates Phase 2.19
    (including the Phase 2.18 Docker Compose stack, which sets
    `ENVIRONMENT=production` and knows nothing of this variable) -- it must
    keep working unchanged, so an unset value derives `"production"` from
    `ENVIRONMENT=production` rather than defaulting to `"development"` and
    silently skipping every staging/production-only check in
    `voiceagent.config.validation`.
    """
    raw = os.environ.get("VOICEAGENT_DEPLOYMENT_STAGE")
    if raw is not None:
        return raw
    return "production" if platform.environment == "production" else "development"


def settings_from_env(platform: PlatformSettings | None = None) -> Settings:
    """Build `Settings` from the environment. Pure apart from `os.environ`:
    no database, no network, no secret store. Tests call it directly with
    `monkeypatch.setenv(...)` instead of clearing a cache."""
    esl_port_raw = os.environ.get("VOICEAGENT_FREESWITCH_ESL_PORT")
    defaults = RuntimeSettings()
    freeswitch_defaults = FreeSwitchSettings()
    resolved_platform = platform if platform is not None else get_platform_settings()

    return Settings(
        platform=resolved_platform,
        deployment_stage=_resolve_deployment_stage(resolved_platform),
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
            command_timeout_seconds=(
                _parse_float("VOICEAGENT_FREESWITCH_COMMAND_TIMEOUT_SECONDS", raw)
                if (raw := os.environ.get("VOICEAGENT_FREESWITCH_COMMAND_TIMEOUT_SECONDS"))
                is not None
                else freeswitch_defaults.command_timeout_seconds
            ),
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
            conversation_persistence_queue_size=(
                _parse_int("VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_QUEUE_SIZE", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_QUEUE_SIZE"))
                is not None
                else defaults.conversation_persistence_queue_size
            ),
            conversation_persistence_max_retries=(
                _parse_int("VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_MAX_RETRIES", raw)
                if (
                    raw := os.environ.get("VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_MAX_RETRIES")
                )
                is not None
                else defaults.conversation_persistence_max_retries
            ),
            conversation_persistence_retry_backoff_seconds=(
                _parse_float(
                    "VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_RETRY_BACKOFF_SECONDS", raw
                )
                if (
                    raw := os.environ.get(
                        "VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_RETRY_BACKOFF_SECONDS"
                    )
                )
                is not None
                else defaults.conversation_persistence_retry_backoff_seconds
            ),
            conversation_persistence_drain_timeout_seconds=(
                _parse_float(
                    "VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_DRAIN_TIMEOUT_SECONDS", raw
                )
                if (
                    raw := os.environ.get(
                        "VOICEAGENT_RUNTIME_CONVERSATION_PERSISTENCE_DRAIN_TIMEOUT_SECONDS"
                    )
                )
                is not None
                else defaults.conversation_persistence_drain_timeout_seconds
            ),
            stuck_call_startup_threshold_seconds=(
                _parse_float("VOICEAGENT_RUNTIME_STUCK_CALL_STARTUP_THRESHOLD_SECONDS", raw)
                if (
                    raw := os.environ.get("VOICEAGENT_RUNTIME_STUCK_CALL_STARTUP_THRESHOLD_SECONDS")
                )
                is not None
                else defaults.stuck_call_startup_threshold_seconds
            ),
            stuck_call_active_threshold_seconds=(
                _parse_float("VOICEAGENT_RUNTIME_STUCK_CALL_ACTIVE_THRESHOLD_SECONDS", raw)
                if (raw := os.environ.get("VOICEAGENT_RUNTIME_STUCK_CALL_ACTIVE_THRESHOLD_SECONDS"))
                is not None
                else defaults.stuck_call_active_threshold_seconds
            ),
        ),
        call_intelligence=_call_intelligence_settings_from_env(),
    )


def _call_intelligence_settings_from_env() -> CallIntelligenceSettings:
    defaults = CallIntelligenceSettings()
    return CallIntelligenceSettings(
        provider=os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_PROVIDER", defaults.provider),
        model=os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_MODEL", defaults.model),
        endpoint=os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_ENDPOINT", defaults.endpoint),
        timeout_seconds=(
            _parse_float("VOICEAGENT_CALL_INTELLIGENCE_TIMEOUT_SECONDS", raw)
            if (raw := os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_TIMEOUT_SECONDS")) is not None
            else defaults.timeout_seconds
        ),
        system_actor_user_id=_parse_uuid(
            "VOICEAGENT_CALL_INTELLIGENCE_SYSTEM_ACTOR_USER_ID",
            os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_SYSTEM_ACTOR_USER_ID"),
        ),
        poll_interval_seconds=(
            _parse_float("VOICEAGENT_CALL_INTELLIGENCE_POLL_INTERVAL_SECONDS", raw)
            if (raw := os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_POLL_INTERVAL_SECONDS"))
            is not None
            else defaults.poll_interval_seconds
        ),
        max_claims_per_tenant_per_tick=(
            _parse_int("VOICEAGENT_CALL_INTELLIGENCE_MAX_CLAIMS_PER_TENANT_PER_TICK", raw)
            if (
                raw := os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_MAX_CLAIMS_PER_TENANT_PER_TICK")
            )
            is not None
            else defaults.max_claims_per_tenant_per_tick
        ),
        max_concurrent_tenants=(
            _parse_int("VOICEAGENT_CALL_INTELLIGENCE_MAX_CONCURRENT_TENANTS", raw)
            if (raw := os.environ.get("VOICEAGENT_CALL_INTELLIGENCE_MAX_CONCURRENT_TENANTS"))
            is not None
            else defaults.max_concurrent_tenants
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
