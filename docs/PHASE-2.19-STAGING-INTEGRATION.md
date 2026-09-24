# Phase 2.19: Staging & External Integration Foundation

Checkpoint: `de0073e feat: establish production deployment foundation`
(Phase 2.18) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified at
`ff550010e5eafecace7311038aadc99fcecfbe3d`. No commit exists yet for this
phase's own work. `docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched, as
in every prior phase.

This phase makes the Phase 2.18 deployment foundation usable against a real
staging environment and real external providers -- explicit configuration,
startup validation, and documentation -- without redesigning the
architecture, modifying SaaS-OS, or adding product features.

## 1. Baseline (what was inspected before anything changed)

- **`core.config.Settings.environment`** (SaaS-OS) is hard-locked to exactly
  `{"development", "test", "production"}` -- enforced in its own
  `__post_init__`, and independently re-enforced by
  `infra.secrets.config._provider_from_env`'s own `_VALID_ENVIRONMENTS`.
  **There is no `"staging"` value anywhere in the platform, and ADR-0001
  forbids adding one by modifying SaaS-OS.** This is the single fact that
  shapes every other decision in this phase: see section 3.
- **OIDC authentication already fully exists in SaaS-OS** -- this is not a
  fake or a stub. `api/auth/routes.py` (mounted automatically by
  `api.platform.build_platform_app()`, confirmed at
  `voiceagent/api/app.py:14`) implements a complete Authorization
  Code + PKCE flow (`/auth/login`, `/auth/callback`, `/auth/logout`,
  `/auth/me`) against `core.identity`/`core.identity.oidc`, with real
  RS256/ES256 JWT verification and standards-based OIDC discovery
  (ADR-0005: provider-agnostic despite the `ZITADEL_*` variable names).
  There is no dev/fake-identity bypass anywhere in `voiceagent`'s own code
  (`voiceagent/tenancy/context.py` calls the platform's real
  `require_permission`).
- **Every AI provider family (LLM, STT, TTS, call intelligence) already has
  real, wired vendor adapters**, not stubs: `gemini`/`mistral`/`groq` (LLM),
  `deepgram`/`assemblyai` (STT), `elevenlabs`/`deepgram_aura` (TTS), `groq`
  (call intelligence). Each reads its own API key through
  `infra.secrets.get_secrets_provider()` at construction time and raises
  rather than silently falling back to `fake` on an unknown name
  (`voiceagent/providers/registry.py`). Per-agent engine/model selection is
  a database row (`AgentVersion.engine_selection`,
  `voiceagent/agents/config.py`), not a deployment env var --
  `AiProviderSettings.default_engine` has no real consumer today (confirmed
  by grep) and this phase does not change that; the one genuinely
  deployment-wide AI configuration surface is `AiProviderSettings
  .eligible_providers` (`voiceagent/runtime/privacy.py`'s policy allowlist).
- **Telephony has contracts and a working FreeSWITCH-shaped adapter, but no
  real transport.** `voiceagent/telephony/freeswitch/esl.py`'s own module
  docstring is explicit: establishing an actual TCP connection to
  `mod_event_socket` is out of scope, and no ESL password field exists
  anywhere in the codebase. `provider.py`/`media.py` correctly implement the
  ESL command/`mod_audio_stream` wire protocols against an *injected*
  connection -- real logic, fake transport. This phase does not change that;
  it only adds the configuration a real transport will need later
  (`command_timeout_seconds`) and is careful never to claim telephony is
  staging-operational.
- **`/healthz` and `/readyz` are platform-owned** (`api.platform
  .build_platform_app()`), not reimplemented here. Neither ever echoes
  configuration or a secret.
- **Existing test conventions**: `tests/config/test_settings.py`
  (`_platform()` helper building a bare `PlatformSettings`, a
  `test_provider_settings_hold_no_credentials` reflection test forbidding
  credential-shaped field names on any settings dataclass) and
  `tests/conftest.py` (`ENVIRONMENT=test` set globally via `setdefault`,
  dead-address `DATABASE_URL`/`REDIS_URL`, a `settings` fixture calling
  `settings_from_env()` fresh each time). Both are followed unchanged by
  this phase's own additions.
- Git state confirmed clean at phase start apart from the two pre-existing
  protected changes (`M docs/PHASE-0-ARCHITECTURE.md`,
  `?? docs/ADR/0010-one-frontend-multiple-user-contexts.md`).

## 2. The central design decision: `deployment_stage`

Because SaaS-OS's `ENVIRONMENT` cannot gain a fourth value, "staging" is
modeled as a second, additive, voiceagent-owned axis:
`VOICEAGENT_DEPLOYMENT_STAGE` (`voiceagent.config.settings.DEPLOYMENT_STAGES
= ("development", "staging", "production")`), read by
`voiceagent.config.settings.Settings.deployment_stage`.

- A staging deployment runs with **`ENVIRONMENT=production`** underneath --
  this is correct, not a compromise: it gets the platform's strict
  `EnvironmentSecretsProvider` (no `.env`-file secret reading), its
  production `DB_REQUIRE_TLS`/`REDIS_REQUIRE_TLS` defaults, and its
  mandatory `AUTH_COOKIE_SECURE` -- and layers `VOICEAGENT_DEPLOYMENT_STAGE
  =staging` on top.
- `Settings.__post_init__` enforces the combination structurally:
  `deployment_stage` must be one of the three known values, and
  `staging`/`production` requires `ENVIRONMENT=production` underneath (else
  `ConfigurationError`, naming both variables, never a value).
- Unset (`VOICEAGENT_DEPLOYMENT_STAGE` not set at all) derives from
  `ENVIRONMENT`: `"production"` if `ENVIRONMENT=production`, else
  `"development"`. This is deliberate backward compatibility: Phase 2.18's
  own `docker-compose.yml` sets `ENVIRONMENT=production` and knows nothing
  of this new variable -- it must keep getting the hardened validation path
  (section 4) rather than silently reverting to development leniency.
- Direct `Settings(...)` construction (every existing test in this
  repository) defaults to `"development"` regardless of `ENVIRONMENT`,
  so no pre-existing test changed behavior -- confirmed by running the full
  suite unchanged (section 12).

## 3. Environment separation

| `ENVIRONMENT` (SaaS-OS) | `VOICEAGENT_DEPLOYMENT_STAGE` | Secrets provider | Hardened validation |
|---|---|---|---|
| `development` | `development` (only valid value) | `EnvFileSecretsProvider` (`.env`) | No |
| `test` | `development` (only valid value; pytest) | `EnvironmentSecretsProvider` | No |
| `production` | `development` | `EnvironmentSecretsProvider` | Structural checks only (`Settings._validate_production`) |
| `production` | `staging` | `EnvironmentSecretsProvider` | Full (`validate_deployment_readiness`, section 4) |
| `production` | `production` | `EnvironmentSecretsProvider` | Full |

No insecure production default was introduced: `DEBUG`, CORS wildcard/HTTPS,
and cookie security were already fail-closed in production
(`Settings._validate_production`, `AuthHttpConfig`); this phase adds
provider-selection and secret-presence checks on top, gated on the new
`deployment_stage`, never weakening what already existed.

## 4. Configuration validation (`voiceagent.config.validation`)

Two deliberately separate layers, because `Settings` itself must stay pure
(module docstring rule: "it never reads a secret", verified by every test in
`tests/config/` constructing a bare `Settings()`):

1. **Structural** (`Settings.__post_init__`, always runs, no I/O): the
   `deployment_stage`/`ENVIRONMENT` combination (section 2), plus the
   pre-existing `DEBUG`/CORS checks.
2. **Operational readiness** (`voiceagent.config.validation
   .validate_deployment_readiness(settings)`, a no-op for
   `deployment_stage in ("development", "test")`, called explicitly by every
   process entrypoint -- never from `Settings` itself):
   - Rejects the unconfigured default AI provider policy
     (`VOICEAGENT_AI_ELIGIBLE_PROVIDERS=fake`, the dataclass default) and the
     unconfigured call-intelligence provider (`fake`) under
     staging/production -- section 6's "must not silently fall back to
     fakes."
   - If FreeSWITCH is configured at all (`VOICEAGENT_FREESWITCH_ESL_HOST`
     set), requires `VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL` and that it be
     `https://`/`wss://`. Unconfigured FreeSWITCH is not itself an error --
     see the known limitation in section 14.
   - Checks, by **presence only, never by value**
     (`infra.secrets.SecretsProvider.get(name) is not None`), that every
     secret a selected real provider needs is actually configured:
     `GROQ_API_KEY`/`GEMINI_API_KEY`/`MISTRAL_API_KEY`/`DEEPGRAM_API_KEY`/
     `ASSEMBLYAI_API_KEY`/`ELEVENLABS_API_KEY` for whichever names appear in
     `VOICEAGENT_AI_ELIGIBLE_PROVIDERS` or
     `VOICEAGENT_CALL_INTELLIGENCE_PROVIDER`.
   - Checks OIDC readiness: `OIDC_REDIRECT_URI` present and `https://`, and
     (by presence only) `ZITADEL_ISSUER_URL`/`ZITADEL_CLIENT_ID` configured
     in the secret store. SaaS-OS's own OIDC routes validate this **lazily,
     per request** (`api/auth/routes.py` catches `OIDCConfigurationError`
     and returns a bare `503`); this closes the gap at startup instead,
     satisfying the brief's "must fail clearly when staging is configured to
     require OIDC but required configuration is missing" without
     duplicating the platform's own parsing.

Wired into every process:

- `voiceagent/api/app.py`'s `build_app()` wraps the platform's own lifespan
  (`app.router.lifespan_context`) so the check runs when the server's
  lifespan actually starts -- never at import or at `build_app()` itself,
  preserving that function's documented "no I/O at import or build time"
  invariant. (Starlette's current version removed `add_event_handler`/
  `on_event` entirely -- composing the lifespan context manager is the
  supported replacement; see section 11 for the test breakage this caused
  and how it was fixed.)
- `scripts/run_call_runtime.py`, `scripts/run_followup_worker.py`,
  `scripts/run_call_intelligence_worker.py` call it directly, right after
  building `Settings`, before any other work.

Every `ConfigurationError` raised here names a variable, never a value --
identical to the pre-existing contract.

## 5. Secrets

No secret is committed, logged, or echoed anywhere in this phase's changes:

- `.env.staging.example` contains only placeholders (`CHANGE_ME`, empty
  strings) and is meant to be copied to a **gitignored** `.env.staging` that
  a real deployment fills in and never commits.
- `docker-compose.staging.yml` never hardcodes a credential -- every secret
  is an `${VAR}` reference resolved from the operator's own
  `--env-file`/shell, and every one that a real deployment cannot safely
  default uses the `${VAR:?message}` form so `docker compose` itself refuses
  to start rather than silently substituting an empty string.
- `validate_deployment_readiness` (section 4) reads secret *presence* only
  (`SecretsProvider.get(name) is not None`) and never a value; every
  `ConfigurationError` it raises names a variable, never its content.
- No frontend code was touched -- no secret was ever, or is now, reachable
  from `frontend/`.
- `/healthz`/`/readyz` remain platform-owned and were not modified; they
  already disclosed nothing about configuration (confirmed by the existing
  `tests/api/test_ops.py::test_ops_routes_are_never_reachable_without
  _authorization` style coverage, unchanged).

## 6. OIDC staging configuration

Nothing in `voiceagent` needed to change to make OIDC staging-configurable
-- it already reads real, non-hardcoded environment/secret configuration
(`api/auth/config.py:AuthHttpConfig`, `core/identity/provider.py`). This
phase's contribution is (a) documenting every variable in one place
(section 13's table) and (b) the startup presence check in section 4, since
the platform itself only validates lazily.

| Variable | Secret? | Purpose |
|---|---|---|
| `OIDC_REDIRECT_URI` | No | Absolute callback URL registered with the identity provider; must be `https://` once `ENVIRONMENT=production`. |
| `ZITADEL_ISSUER_URL` | Yes (via `infra.secrets`) | OIDC issuer; despite the name, any standards-compliant OIDC provider works (ADR-0005) -- discovery is `{issuer}/.well-known/openid-configuration`. |
| `ZITADEL_CLIENT_ID` | Yes | The registered OAuth client id. |
| `ZITADEL_CLIENT_SECRET` | Yes, optional | Leave unset for a public client using PKCE. |
| `AUTH_COOKIE_NAME` | No | Session cookie name (default `saas_os_session`). |
| `AUTH_COOKIE_SECURE` | No | Must be `true` under `ENVIRONMENT=production` -- the platform itself refuses `false` there. |
| `AUTH_POST_LOGIN_PATH` | No | Same-origin path only; no open-redirect surface. |
| `TRUST_PROXY_HEADERS` | No | Only `true` behind a trusted, header-setting reverse proxy. |

## 7. AI provider configuration

`voiceagent/config/settings.py`:

- `AiProviderSettings.eligible_providers` (already existed) is the
  deployment-wide policy surface -- comma-separated real provider names,
  consumed by `voiceagent.runtime.privacy`. Section 4's validation now
  rejects its unconfigured `("fake",)` default under staging/production.
- `CallIntelligenceSettings` gained one field, `endpoint: str | None = None`
  (`VOICEAGENT_CALL_INTELLIGENCE_ENDPOINT`), and its existing
  `timeout_seconds` is now actually threaded through to the provider's own
  config (`scripts/run_call_intelligence_worker.py` previously always
  passed an empty `{}`, silently discarding both -- a real, if minor, gap
  this phase closes; both are validated fields on `GroqCallIntelligenceConfig`
  already, so no provider-side change was needed).
- **No credential field was added to any `Settings` dataclass**, deliberately:
  `AiProviderSettings`'s own docstring and Phase 0 report section 2.4 G-3
  already establish that a deployment-global API-key field is exactly how a
  credential quietly becomes a default -- `tests/config
  /test_settings.py::test_provider_settings_hold_no_credentials` enforces
  this by reflection. Every vendor's real API key
  (`GROQ_API_KEY`/`GEMINI_API_KEY`/`MISTRAL_API_KEY`/`DEEPGRAM_API_KEY`/
  `ASSEMBLYAI_API_KEY`/`ELEVENLABS_API_KEY`) is supplied as a real
  environment variable at deploy time and read by each vendor adapter
  through `infra.secrets` exactly as it already was -- this phase changed
  none of that wiring, only added the startup presence check (section 4).
- `LlmProvider`/`SttProvider`/`TtsProvider`/`ConversationEngine` contracts,
  `PipelinedEngine`, `RealtimeEngine`, and the per-agent engine-selection
  mechanism (`AgentVersion.engine_selection`) are all unchanged.

## 8. Telephony configuration

`voiceagent/config/settings.py:FreeSwitchSettings` gained one field:
`command_timeout_seconds: float = 10.0`
(`VOICEAGENT_FREESWITCH_COMMAND_TIMEOUT_SECONDS`), mirroring
`FreeSwitchTelephonyProvider`'s own existing constructor default
(`voiceagent/telephony/freeswitch/provider.py`) -- previously fixed at
construction time, unconfigurable via environment.

Deliberately **not** added, and why:

- **An ESL password/credential field.** No real TCP transport to
  `mod_event_socket` exists in this repository (`esl.py`'s own module
  docstring). A credential is a secret in any case and belongs behind
  `infra.secrets`, read by a future real transport -- inventing an unused
  `Settings` field for it now would be dead, misleading configuration.
- **An ESL TLS setting.** The ESL protocol itself has no TLS mode;
  `docs/PHASE-0-ARCHITECTURE.md`'s own security framing for it is network
  isolation plus a password, not transport encryption. Adding a setting
  that maps to nothing real would be worse than adding nothing.

`voiceagent.telephony.contracts`, the FreeSWITCH adapter's command/media
wire-protocol logic, and the import boundary confining FreeSWITCH internals
to `voiceagent.telephony.freeswitch` (`tests/architecture
/test_import_boundaries.py`) are all unchanged -- confirmed by
`import-linter` and the full architecture test suite passing unmodified
(section 12).

**This phase does not make telephony staging-operational** -- see section 14.

## 9. PostgreSQL and Redis

Both are already fully configurable in a production-style way by SaaS-OS
itself (`infra/db/config.py`, `infra/jobs/config.py`), unchanged by this
phase: `DATABASE_URL`/`MIGRATIONS_DATABASE_URL` (secret), `DB_REQUIRE_TLS`
(defaults to `ENVIRONMENT == "production"`), pool tunables
(`DB_POOL_SIZE`/`DB_POOL_MAX_OVERFLOW`/`DB_POOL_TIMEOUT_SECONDS`/
`DB_POOL_RECYCLE_SECONDS`/`DB_POOL_PRE_PING`/`DB_CONNECT_TIMEOUT_SECONDS`),
`REDIS_URL` (secret), `REDIS_REQUIRE_TLS` (same production-default pattern,
requires `rediss://`). `docker-compose.staging.yml` sets
`DB_REQUIRE_TLS`/`REDIS_REQUIRE_TLS` to `"true"` explicitly for every
service, with no bundled local `postgres`/`redis` containers -- staging
points at real, externally-provisioned instances (section 11).

Redis remains strictly non-authoritative: nothing in this phase touches
`RedisHeartbeatStore`, job scheduling, or any ownership/ordering logic.
PostgreSQL remains the only authoritative runtime/business state. No
takeover/failover behavior was added.

## 10. Public URL / callback configuration

Every explicit public-URL knob this product needs already existed --
`OIDC_REDIRECT_URI` (OIDC callback), `VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL`
(telephony media), `VOICEAGENT_CORS_ALLOWED_ORIGINS` (frontend origin). This
phase deliberately did not invent an additional generic "public base URL"
setting with no real consumer -- doing so would be exactly the kind of dead
configuration section 8 already argues against for FreeSWITCH. All three are
now validated (section 4: OIDC redirect URI and, when FreeSWITCH is
configured, the media URL) or already were (CORS, `Settings
._validate_production`). None of this product's own code constructs a
security-sensitive URL from a request header; `TRUST_PROXY_HEADERS`
(platform-owned) governs only client-IP resolution for audit logging, not
URL construction.

## 11. Docker Compose

`docker-compose.staging.yml` (new, standalone -- not a merge override of
Phase 2.18's `docker-compose.yml`, to avoid Compose's merge-semantics
subtleties for removing a service's `depends_on`): `migrate`, `api`,
`call-runtime`, `follow-up-worker`, `call-intelligence-worker` (the latter
two behind the existing `workers` profile), and `frontend`. Same Dockerfile,
same per-process commands as Phase 2.18. No local `postgres`/`redis`
containers -- staging's PostgreSQL/Redis are real, externally-provisioned
infrastructure, referenced via `${DATABASE_URL}`/`${REDIS_URL}`. Every
required, non-defaultable value uses `${VAR:?message}` so `docker compose
config`/`up` refuses to start with a clear, per-variable message rather than
silently substituting an empty string -- verified directly (section 12).

`.env.staging.example` (new, root) documents and defaults every variable a
staging deployment needs; copied to a gitignored `.env.staging` and never
committed with real values.

No Kubernetes, no Terraform, no cloud-specific provisioning, no managed
takeover/failover -- exactly as scoped.

### Fix made along the way: `build_app()`'s startup-hook wiring broke on the installed Starlette version

Registering the new readiness check via `app.add_event_handler("startup",
...)` (the historically documented pattern) raised
`AttributeError: 'FastAPI' object has no attribute 'add_event_handler'` --
the installed Starlette (`1.6.0`) removed that API entirely in favor of
exactly one lifespan per app. Fixed by wrapping
`app.router.lifespan_context` (capture the platform's own lifespan, call
`validate_deployment_readiness` first, then delegate) instead of trying to
register a second, independent handler. This is a pre-existing-dependency
compatibility fix required to implement section 4's requirement at all, not
a scope expansion; `tests/api/test_app.py` (which caught the break
immediately) passes unchanged.

## 12. Tests

New: `tests/config/test_deployment_validation.py` (18 tests) --
development/test stages are a no-op; a fully-configured staging deployment
passes; the default fake AI provider is rejected; the default fake
call-intelligence provider is rejected; a missing vendor secret is rejected,
naming the variable; a missing/insecure `OIDC_REDIRECT_URI` is rejected; a
missing `ZITADEL_ISSUER_URL`/`ZITADEL_CLIENT_ID` is rejected; FreeSWITCH left
fully unconfigured is not required; FreeSWITCH partially configured (host
set, no/insecure media URL) is rejected; a properly-configured FreeSWITCH
passes.

Extended: `tests/config/test_settings.py` (+13 tests) -- `deployment_stage`
default derivation (development, and production-from-`ENVIRONMENT`),
explicit env override, invalid value rejection, the
staging-requires-production-`ENVIRONMENT` combination (both the rejected and
the accepted case), `FreeSwitchSettings.command_timeout_seconds` parsing/
default/invalid-value rejection, `CallIntelligenceSettings.endpoint`
default/parsing.

(Test module named `test_deployment_validation.py`, not `test_validation.py`
-- `tests/workflows/test_validation.py` already exists and this repository's
`tests/` tree has no `__init__.py` files, so pytest's rootless import mode
requires globally-unique test module basenames; this was caught immediately
by a full-suite collection error and fixed before proceeding.)

No test names, imports, or reads a real secret value anywhere; every
positive-path test in `test_deployment_validation.py` sets clearly-fake
placeholder values via `monkeypatch.setenv` for the duration of that test
only.

## 13. Staging smoke-test foundation

`scripts/smoke_test_staging.py` (new): a standalone, read-only operator tool
(not part of the pytest suite -- it needs a real, reachable deployment) that
checks, against `--base-url`: `/healthz` returns 200; `/readyz` returns 200;
`/auth/login` redirects (proving OIDC configuration resolved end-to-end,
without completing a real login); an unauthenticated `/auth/me` is rejected
(401/403); an unauthenticated `/v1/agents` request is rejected (401/403,
proving no tenant-isolation bypass). It never prints a response body or
header that could carry a secret.

**Verified for real** against a live instance of this repository's own API
(`docker compose up postgres redis migrate api`, no OIDC configured):
`/healthz` and `/readyz` returned 200; unauthenticated `/auth/me` and
`/v1/agents` both correctly returned `401`; `/auth/login` correctly returned
`503` (not a redirect) because that instance had no `ZITADEL_*`/
`OIDC_REDIRECT_URI` configured -- which is itself the exact "fails clearly
when OIDC is required but not configured" behavior section 4 is about,
observed directly rather than assumed. The redirect path itself (a real
identity provider actually configured) was not exercised, because no real
staging IdP is reachable from this environment -- see section 14.

**Explicit boundary (brief section 13: "do not create a fake end-to-end
telephony success test that claims to prove a real provider works")**: this
script never completes a real OIDC login, never places a real telephone
call, and never calls a real AI provider. The deployment sequence below
names each remaining manual step explicitly.

### Deployment sequence

1. Provision PostgreSQL and Redis (real, TLS-capable instances).
2. Register an OAuth client with a real OIDC provider; note its issuer,
   client id, (optional) client secret, and register the callback URL.
3. Choose and obtain credentials for at least one real AI provider (LLM/STT/
   TTS as needed, plus call-intelligence).
4. Decide on telephony: as of this phase, no real ESL/`mod_audio_stream`
   transport exists in this repository (section 8/14) -- this step remains
   a documented future phase, not something this deployment sequence can
   complete today.
5. `cp .env.staging.example .env.staging` and fill in every value from
   steps 1-3.
6. `docker compose -f docker-compose.staging.yml --env-file .env.staging
   build`
7. `docker compose -f docker-compose.staging.yml --env-file .env.staging up
   migrate` -- wait for exit code 0.
8. `docker compose -f docker-compose.staging.yml --env-file .env.staging up
   -d api call-runtime`
9. Bootstrap at least one tenant (`scripts/bootstrap_rbac.py`), then set
   `VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS`/
   `VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS`/their system-actor
   variables in `.env.staging`.
10. `docker compose -f docker-compose.staging.yml --env-file .env.staging
    --profile workers up -d follow-up-worker call-intelligence-worker`
11. `docker compose -f docker-compose.staging.yml --env-file .env.staging up
    -d frontend`
12. `python scripts/smoke_test_staging.py --base-url https://<staging-origin>`
13. Manually: complete one real OIDC login in a browser; manually exercise
    one authenticated API call as that user; manually verify a real AI
    provider call (e.g. trigger a call-intelligence analysis and confirm a
    non-fake result). Real telephony end-to-end is out of scope until a real
    ESL/media transport exists (section 14).

## 14. Known limitations

- **Telephony is not staging-operational.** There is still no real TCP ESL
  transport and no real `wss://` media listener anywhere in this repository
  (unchanged since Phase 2.0/2.1's own documented scope boundary). This
  phase adds configuration (`command_timeout_seconds`) for when a real
  transport exists, and validates FreeSWITCH configuration for internal
  consistency when an operator does set `VOICEAGENT_FREESWITCH_ESL_HOST` --
  it does not, and cannot, make a real phone call happen.
- **`AiProviderSettings.default_engine` remains dead configuration** (a
  pre-existing gap, not introduced here): the real per-conversation engine
  selection is `AgentVersion.engine_selection`, a database row, not a
  deployment env var. `eligible_providers` (the policy allowlist) is the
  genuinely live, deployment-wide AI configuration surface, and is what
  section 4's validation targets.
- **The OIDC redirect-flow smoke-test check was verified structurally
  (fails clearly with no IdP configured) but not against a real identity
  provider** -- none was reachable from this environment. An operator
  running the deployment sequence above will exercise it for real at step
  12/13.
- **`docker-compose.staging.yml` was validated with `docker compose config`
  against both an empty and a fully-populated placeholder `.env` file
  (section 12), not run end-to-end** -- doing so needs the same real
  external infrastructure named in steps 1-3 above, which this environment
  does not have.

## 15. Deviations from the brief

- Section 9's "public URL configuration ... where required" resulted in
  **no new setting** rather than one -- every genuinely-required public URL
  already had an owner (section 10). Adding one anyway would have been dead
  configuration, which section 7/8's own language elsewhere in the brief
  explicitly warns against for other subsystems; the same discipline was
  applied here for consistency.
- Section 7's "API credential/secret" for AI provider configuration was
  **not added as a `Settings` field** -- doing so would conflict with an
  existing, deliberate architectural decision
  (`AiProviderSettings`'s own docstring, Phase 0 report section 2.4 G-3, and
  an existing reflection test). Real vendor credentials continue to flow
  through `infra.secrets` at the adapter layer, unchanged; this phase adds
  only startup presence validation for them (section 4).
- `CallIntelligenceSettings.timeout_seconds` was already-existing
  configuration that silently had no effect (`{}` was always passed to the
  provider); this phase wires it through as part of closing the identical
  "request timeout" gap the brief asks for on `endpoint`. Scope-adjacent,
  not scope-expanding -- same field, same env var, made to do what its own
  docstring already claimed.

## 16. Quality gates

- Backend tests: **851 passed**, 241 deselected (up from Phase 2.18's 827 --
  24 new tests), 0 failed.
- Ruff: all checks passed on every changed file.
- Ruff format: all changed files already formatted.
- Pyright: 0 errors, 0 warnings on `voiceagent/` and `tests/` (its
  configured `include`).
- import-linter: 8 contracts kept, 0 broken (194 files, 927 dependencies --
  up from 194/921 pre-phase purely from the new module).
- `docker compose -f docker-compose.staging.yml config`: fails closed with a
  named-variable error when required values are unset; parses cleanly with
  every required value populated (verified both ways).
- Live verification: `docker compose up postgres redis migrate api` (Phase
  2.18's own stack) plus `scripts/smoke_test_staging.py` against it,
  confirming `/healthz`/`/readyz`/unauthenticated-rejection behavior for
  real (section 13).
- detect-secrets: clean on every new/changed file (two placeholder/test
  values required `# pragma: allowlist secret` annotations, added); baseline
  file itself untouched.
- pip-audit: no known vulnerabilities (`saas-os`/`voiceagent` correctly
  skipped, not on PyPI).
- Frontend tests: 6 files / 51 passed. Frontend typecheck: clean. Frontend
  production build: succeeded. `npm audit`: 0 vulnerabilities. (No frontend
  file was touched this phase; re-run anyway for a complete gate report.)

## 17. Recommendation

`READY FOR CHECKPOINT`
