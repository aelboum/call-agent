# Phase 0 — Architecture & Discovery Report

**Product**: multi-tenant AI call-agent platform (working package name
`callagent`, see §19 OD-1).
**Phase**: 0 — architecture and discovery only. No implementation.
**Date**: 2026-09-21.
**Dependency under assessment**: `saas-os` @ `ff550010e5eafecace7311038aadc99fcecfbe3d`.

This report is evidence-based: every claim about SaaS-OS, Dograh and Fonio below
was taken from direct inspection of source, documentation or the vendor's own
public product pages during this phase, not from assumption. Where something was
*not* verified, it is stated as unverified and carried into §19 (Open decisions)
rather than asserted.

---

## 1. Repository assessment

**Finding: the product repository does not exist yet.**

`C:\Users\imran\Documents\ai-agent` was empty at the start of this phase: no
files, no `.git` directory, no tooling, no dependency manifest, no CI. Per the
Phase 0 brief, the correct output is therefore a *recommended* foundation
structure plus a Phase 1 creation plan, not an implementation.

What exists on the machine around it, and matters to this product:

| Path | What it is | Relevance |
|---|---|---|
| `~/Documents/saas-os` | The SaaS-OS platform repository, `main`, clean tree | The pinned dependency (§2) |
| `~/Documents/dograh-freeswitch` | A working clone of Dograh including its FreeSWITCH provider and a vendored Pipecat fork | Reference product — studied, never copied (§3.6) |
| `~/Documents/mod_audio_stream` | The FreeSWITCH media-streaming module Dograh's FreeSWITCH provider depends on | Candidate media bridge (§10) |

### 1.1 Recommended repository layout (to be created in Phase 1, not now)

Mirrors SaaS-OS's own conventions deliberately — same toolchain, same
validation-script pattern, same ADR discipline — so a developer moving between
the two repositories does not have to learn a second set of habits.

```
callagent/                  Product package (the only shipped Python package)
  agents/                   Agent, AgentVersion, publish/pin logic
  telephony/                FreeSWITCH boundary: ESL client, event handling,
                            dialplan contract, channel<->session correlation
  runtime/                  AI call runtime: media session, turn loop,
                            barge-in, engine orchestration  (Phase 2+)
  providers/                ConversationEngine + STT/LLM/TTS/Voice adapter
                            interfaces; concrete adapters live in subpackages
  tools/                    Tool Gateway + tool definitions + schemas
  workflow/                 Call-workflow model and interpreter  (Phase 3+)
  contacts/                 Native Contacts (deliberately small)
  calendar/                 Native Calendar + CalendarProvider abstraction
  conversations/            CallSession, Conversation, transcript, recording ref
  api/                      Product HTTP routes mounted onto SaaS-OS's
                            build_platform_app()
  migrations/               This product's OWN Alembic history (ADR-0016 of
                            SaaS-OS: independent from the platform's)
  purge.py                  This product's TenantPurgeParticipant registration
  app.py                    This product's composition root (create_app())
  worker.py                 This product's ARQ worker entrypoint
docs/
  PHASE-0-ARCHITECTURE.md   This report
  ADR/                      Product-level architecture decisions
deploy/                     docker-compose, FreeSWITCH config, Dockerfiles
scripts/                    check-backend.sh / check-security.sh / check-all.sh
tests/
  architecture/             Import-boundary contracts (import-linter)
.github/workflows/          CI running the same scripts as local dev
pyproject.toml              Pins saas-os by exact commit SHA
```

### 1.2 Tooling the product must match

Fixed by SaaS-OS's own accepted stack (its ADR-0011) and by the fact that the
product imports it in-process: Python **3.13+**, FastAPI, SQLAlchemy 2.x via
`infra.db` only, Alembic, PostgreSQL, Redis, ARQ, plain `pip` + `setuptools`
(no Poetry, no uv). Add on the product side: `import-linter` contracts,
`ruff`, `pyright`, `pytest`, `detect-secrets`.

---

## 2. SaaS-OS dependency assessment

### 2.1 The exact SHA to pin

```
saas-os @ git+https://github.com/aelboum/saas-os@ff550010e5eafecace7311038aadc99fcecfbe3d
```

Verification performed this phase:

- `HEAD` = `ff550010e5eafecace7311038aadc99fcecfbe3d`, authored
  `2026-09-19T23:48:34+02:00`, subject
  `ci: opt disposable TLS-pinned control_plane fixtures out of S-05 TLS`.
- Branch `main`; `git rev-list --left-right --count origin/main...HEAD` = `0 0`
  — the commit is the tip of the tracked remote branch, i.e. it is pushed and
  fetchable, not a local-only commit.
- `git status --porcelain` empty — the inspected tree *is* that commit, with no
  uncommitted work that a pin would silently omit.
- `git tag` returns nothing: **SaaS-OS has published no release tag.** A commit
  SHA is therefore the only available pin, which happens to be what SaaS-OS's
  own ADR-0015 rule 8 prefers anyway ("pin an exact commit SHA rather than a
  mutable tag").

This pin is a *decision*, not a default: it must be re-pinned deliberately, with
a human reading the intervening SaaS-OS changes, never by a bot or a
`pip install -U`.

### 2.2 The consumption model is already decided by SaaS-OS, and it matches this product

SaaS-OS ADR-0015 (Accepted) is binding on us and says, in substance: SaaS-OS is
a business-domain-agnostic foundation distributed as a Python package; consuming
projects live in **their own repositories**, own their **own database,
deployment, secrets and domain code**; a consumer depends on SaaS-OS and SaaS-OS
must never depend on a consumer; consumption is a **pinned Git/VCS dependency**
with an exact commit SHA preferred. Forking, vendoring, submodules and a
shared network runtime were each considered and explicitly rejected there.

Consequence for us: the brief's constraints ("pin an exact SHA", "do not fork",
"do not copy, do not modify") are not extra rules layered on top — they are
exactly the dependency's own accepted contract. Nothing in this product's design
needs to argue for them.

ADR-0018's `examples/reference-consumer/` is a working, CI-exercised proof of a
consumer, and is the single most useful artifact in the dependency for us: it
demonstrates the five integration points a consumer actually uses
(composition root, secured route, RBAC-gated tool, tenant-scoped data access,
purge participant) in ~200 lines. It is a *fixture*, not a product, and must be
read as a pattern, never copied wholesale.

### 2.3 Primitives available, and what each gives this product

Verified by reading each module's `__all__` at the pinned commit.

| SaaS-OS surface | Selected exports | What the call platform gets |
|---|---|---|
| `core.tenancy` | `Tenant`, `TenantStatus`, `create_tenant`, `get_ancestor_ids`, `purge_tenant`, `purge_participants`, retention classes | Tenant hierarchy, lifecycle, suspended/purging states, and the hook by which our call data is deleted on tenant purge |
| `core.rbac` | `can`, `register_permission`, `create_role`, `assign_role`, `assign_service_account_role`, `create_delegation`, `create_deny`, `RoleScope` | Authorization for every product route and every tool execution; delegated administration (agency manages client tenant) comes free |
| `core.identity` | `User`, `Session`, `TenantMembership`, `ServiceAccount`, OIDC login/session machinery | Human auth for the dashboard; **`ServiceAccount` is the principal the call runtime acts as** (§14.4) |
| `core.api_keys` | `create_service_account_api_key`, `validate_api_key`, rotate/revoke | Machine authentication for the runtime and for tenant-facing API access |
| `core.audit_log` | `record`, `get`, `list`, `ActorType`, `AuditOutcome`, metadata validation | Every auditable action in §15 writes here — we do not build an audit store |
| `core.idempotency` | `run_idempotent`, `begin/finalize_idempotent_operation`, fingerprinting | Exactly-once semantics for booking, outbound dial, and every mutating tool call |
| `core.webhooks` | `subscribe`, `trigger_event`, signing, replay protection | Tenant-facing outbound events (`call.completed`, `appointment.created`) |
| `core.usage` / `core.billing` | `ingest_event`, `consume_quota_idempotent`, `check_quota`, `get_entitlements`, `require_entitlement`, `BillingProvider` (+ Stripe adapter) | Per-minute call metering, concurrency entitlements, plan gating |
| `core.feature_flags` | `evaluate_flag`, per-tenant overrides | Staged rollout of runtime/provider changes per tenant |
| `core.notifications` / `core.email` | `dispatch_notification`, SMTP provider | Post-call email/summary delivery (Fonio-style, §3.5) |
| `core.crypto` | `EncryptionService.encrypt/decrypt` — AES-GCM envelope, versioned keys, associated-data support | Field encryption for transcripts and for per-tenant provider credentials, with `tenant_id` bound as AAD |
| `infra.db` | `session_scope`, `tenant_session_scope`, `acquire_tenant_advisory_lock`, `Base` + ORM primitives, `tenant_rls_statements`, `validate_application_role`, migration runner | The single DB chokepoint, RLS per tenant, and the advisory lock the calendar needs |
| `infra.jobs` | `enqueue_job`, `register_job`, `build_worker`, dead-letter inspection | Post-call processing pipeline |
| `infra.observability` | `configure_logging/tracing`, `bind_correlation_context`, `redact`, `is_sensitive_key` | Structured logs, OTel traces, PII redaction |
| `infra.secrets` | `SecretsProvider.get/get_required` | Deployment-level secrets (see gap G-3 below) |
| `infra.ratelimit` | (used via `api.dependencies`) | Per-tenant API rate limiting |
| `api.platform` | `build_platform_app(title, version)` | Our FastAPI app builder: lifespan, correlation middleware, health + OIDC routers, and the fail-closed unsafe-DB-role guard |
| `api.dependencies` / `api.context` / `api.errors` | `require_permission(resource, action)`, `RequestContext`, non-leaking errors | The ingress chain: auth → tenant resolution → rate limit → RBAC, with `RequestContext.tenant_id` as the only trusted tenant |
| `control_plane.orchestration` | `ToolDefinition`, `ToolRegistry`, `invoke_tool`, `ToolExecutionContext` | Reference shape for tool metadata; **not** the realtime execution path (§8.2) |
| `control_plane.data_authorization` | `authorize_data_access`, `TenantAIDataPolicy`, `ProviderEligibilityPolicy`, classifications | The gate that governs sending caller data to an external AI provider (§15.5) |
| `contracts` | schema/catalog | Product-descriptor contracts; not load-bearing for us yet |

### 2.4 Gaps in the dependency this product must cover itself

These are the findings that actually shape the architecture. None is a defect in
SaaS-OS; each is simply outside its scope.

- **G-1 — `infra.db` is synchronous only.** Verified: no `create_async_engine`,
  no `AsyncSession`, no `asyncpg` anywhere in `infra`, `core`, or `api`.
  `tenant_session_scope()` is a sync context manager. A realtime media loop is
  necessarily `async`. **Every database touch from the call runtime must be
  pushed off the event loop** (`asyncio.to_thread` around a sync
  `tenant_session_scope` block), or done in a different process. This is the
  single biggest constraint on the runtime design (§4.2, R-1).
- **G-2 — no object-storage service.** `boto3` is a dependency, but only
  `infra/db/backup` uses it, and that is platform-team tooling, explicitly not
  an application-runtime capability (an import-linter contract enforces this).
  **Call recording storage is the product's own component** (§13.3).
- **G-3 — secrets are deployment-scoped, not tenant-scoped.**
  `SecretsProvider.get(name) -> str | None` has no tenant dimension. Per-tenant
  credentials (a tenant's own ElevenLabs key, SIP trunk password, external
  calendar OAuth token) are therefore **product-owned rows encrypted with
  `core.crypto`**, with `tenant_id` as associated data so a ciphertext cannot be
  replayed into another tenant's row. `infra.secrets` still owns the *master*
  key material and all deployment-level secrets.
- **G-4 — `infra.db` exports no `JSONB`, `ARRAY`, `LargeBinary` or `Enum`.**
  Only `JSON`, `String`, `Text`, `Integer`, `Numeric`, `Boolean`, `DateTime`,
  `ForeignKey`, and the constraint/index helpers. Since products must not import
  SQLAlchemy directly (a hard, enforced boundary), the product either lives
  within `JSON` + `String` (workable: `JSON` maps to Postgres `json`; tags
  become a child table; enums become `String` + `CheckConstraint`, which is what
  Dograh ended up preferring anyway) or requests an upstream export. Decision in
  §19 OD-4.
- **G-5 — no realtime/WebSocket ingress helpers.** `api.dependencies` is a
  request-scoped HTTP chain. The media WebSocket needs its own authentication
  path (§14.3).
- **G-6 — the Control Plane's tool executor is not a realtime executor.**
  `invoke_tool()` refuses any tool with `autonomy_tier >= 1`
  (`TierRequiresApprovalError` — those require a human approval round-trip),
  requires an `agent_user_id` that is a real user UUID, and `ToolDefinition`
  carries **no input schema, no output schema, no timeout and no retry policy**.
  A caller on a 300 ms latency budget cannot use it as-is. See §8.2.
- **G-7 — README drift.** SaaS-OS's `README.md` still says
  "no other business module yet" for `core/`, while `core/` in fact contains
  fifteen implemented modules. Read the modules, not the README. Minor, but it
  means the README is not a reliable capability inventory for pinning decisions.

---

## 3. Reference-product study: adopt / adapt / reject

### 3.1 Dograh — what was actually inspected

The local clone (`22e8b939…`, a merge of `origin/main`) was read directly:
`api/services/telephony/` (provider ABC, factory, registry, and the
`providers/freeswitch/` package including its `DESIGN.md`),
`api/services/workflow/` (graph, node specs, DTOs, engine, tools),
`api/services/pipecat/service_factory.py`, and `api/db/models.py`.

### 3.2 Dograh — ideas to **adopt** (as concepts; no code is taken)

1. **ESL as the FreeSWITCH control channel, in *inbound* mode** — the product
   connects out to FreeSWITCH's `mod_event_socket`; FreeSWITCH does not dial
   back into a per-call listener. Dograh's `DESIGN.md` records the rejected
   alternatives with reasons that hold for us too: a raw SIP/RTP bridge means
   reimplementing a softswitch; `mod_xml_rpc` has no ARI-equivalent REST control
   plane and little ecosystem support.
2. **Park-then-resolve for inbound calls.** The dialplan routes a DID to
   `park()` while still ringing; the product observes `CHANNEL_PARK`, resolves
   the *called* number to a tenant and agent **server-side**, then answers
   (`uuid_answer`) and attaches media. This gives a clean, auditable point at
   which tenant context is established before any audio or AI cost is incurred.
3. **A parked-channel reaper.** Dograh's design documents a real 2026-08
   incident: ~999 scanner-originated channels sat parked for ~6 days while their
   ESL manager was disconnected, saturating FreeSWITCH's session table. Their
   fix — a periodic rescan that hangs up unanswered parked channels past a
   timeout, with an in-flight "claim" marker and a never-touch rule for
   `ACTIVE` channels — is a correctness requirement, not a nicety, and we should
   design it in from day one rather than after our own incident.
4. **Pinning the media module and verifying its wire protocol from source.**
   `mod_audio_stream` (>= v1.0.3) is asymmetric: FreeSWITCH sends **raw binary
   L16 PCM** frames with no envelope, while playback back into the channel is
   **JSON text frames** (`{"type":"streamAudio","data":{...base64 L16...}}`).
   It is also easily confused with `mod_audio_fork`, a different module with a
   different ESL command and different framing. Verify against module source,
   pin the version, and write a protocol conformance test.
5. **Immutable, pinned definition rows for versioning.** Dograh's
   `workflow_definitions` has `status` (`draft|published|archived`),
   `version_number`, `published_at`, and each `workflow_run` stores the
   `definition_id` it executed against. That is the right shape, and it directly
   validates the AgentVersion model in §7.
6. **Transitions expressed as LLM tool calls.** Each outgoing edge of a node
   becomes a generated function the model may call, with a uniqueness check on
   generated tool names. This is a genuinely good idea: it removes a separate
   "router" LLM call and makes transitions observable as tool invocations.
7. **Storing the telephony mode as a plain string, not a Postgres ENUM**, with
   the canonical value set in application code — a new provider needs no
   migration. Their own code comment states this rationale. It also happens to
   sidestep our G-4 constraint.

### 3.3 Dograh — ideas to **adapt**

- **Provider abstraction, but around the right seam.** Dograh's
  `TelephonyProvider` ABC has ~25 abstract methods because it must satisfy
  Twilio-style webhook/CPaaS semantics *and* PBX semantics (ESL/ARI) through one
  interface: `verify_webhook_signature`, `parse_status_callback`,
  `get_call_cost`, `provision_phone_number`, `handle_external_websocket`,
  `supports_answering_machine_detection`, … We adapt the *idea* of a provider
  seam but put it where the variance actually is: **below** FreeSWITCH (SIP
  trunks, DID vendors), not **around** it (§10.2).
- **Post-call artifacts.** Recording URL, transcript URL, `gathered_context`,
  `usage_info`, `cost_info`, `annotations` on the run row is the right set of
  outputs — but we normalize them instead of keeping six JSON blobs (§5).

### 3.4 Dograh — ideas to **deliberately reject**

- **Vendoring a forked Pipecat.** The clone carries `pipecat/` in-tree plus
  `PIPECAT_PROVENANCE.md`, `PIPECAT_REBASE_PLAN.md`, `UPSTREAM_1_43_TO_1_45.md`
  and `UPSTREAM_COMPATIBILITY.md` — the permanent maintenance tax of a fork. If
  we use Pipecat at all it is as a **pinned upstream dependency behind our own
  `ConversationEngine` interface** (§11, OD-2), never a fork.
- **Provider breadth as a goal.** `service_factory.py` imports STT/LLM/TTS
  classes for roughly two dozen vendors. Breadth is a maintenance liability, not
  an architectural virtue. We ship *one* provider at launch behind an interface
  proven by a *second*, deliberately different one (see §11.4).
- **Treating FreeSWITCH as one provider among eight.** Dograh's own
  `WorkflowRunMode` enum lists `ari|plivo|twilio|vonage|vobiz|cloudonix|telnyx|freeswitch|webrtc|…`.
  For us FreeSWITCH is the core, and the brief says so explicitly. One
  telephony core means one call model, one transfer semantics, one recording
  path — rather than an interface that is the union of eight vendors'
  vocabularies and the intersection of none.
- **Organization-scoped rather than tenant-scoped rows.** Dograh's models scope
  by `organization_id` reached through parent relationships, with integer
  primary keys and no row-level security. We use SaaS-OS's `tenant_id` + UUID +
  RLS-per-table model, which is non-negotiable for us (§14.2).
- **Wide JSON blobs as the primary model.** `workflow_runs` carries `extra`,
  `usage_info`, `cost_info`, `initial_context`, `gathered_context`, `logs`,
  `annotations` all as JSON. Convenient early; unqueryable, unvalidatable and
  unmigratable later. JSON is right for the *immutable agent snapshot*, wrong
  for operational data.
- **Marketing/campaign/lead machinery.** `campaigns`, `queued_runs`, lead
  dispositions, `folders`, embed tokens, text-chat sessions. Out of scope by the
  brief; each would pull the product toward being an outbound-marketing tool.

### 3.5 Fonio — product patterns worth adopting

From Fonio's own public product pages (fonio.ai): agent configuration is
deliberately shallow and non-technical — **voice, language, custom greeting,
call behavior per scenario**; calls are answered 24/7, **forwarded to the right
contact** (rules by urgency, keyword, department, location, and by business
hours vs after hours); appointments are **booked during the conversation**, via
a built-in scheduler *or* an external calendar (Google, Outlook, Calendly,
cal.com), with reschedule and cancel supported; every call produces
**post-processing** — transcript, summary, email delivery, and an automated SMS
follow-up to the caller; numbers are either provisioned by the vendor or the
customer's own phone system is integrated; setup is explicitly no-code and
minutes-long.

Adopt, as product requirements:

1. **A no-workflow happy path.** An agent that is only
   *instructions + greeting + voice + language + a handful of tools* must be
   fully functional. The workflow builder is an escape hatch for the minority
   of tenants that need determinism — not the primary authoring surface. This
   inverts Dograh's emphasis and is the single most important product decision
   in this report.
2. **Transfer rules as first-class configuration**, not as workflow nodes:
   conditions (business hours, intent, keyword, caller), destination, and
   fallback when the destination does not answer.
3. **Business hours and timezone as agent-level configuration**, driving both
   greeting selection and transfer behavior.
4. **Post-call processing as a guaranteed pipeline stage** (summary, structured
   extraction, notification), not as an optional workflow node — a call that
   ends in any state still produces its artifacts.
5. **Native scheduler *or* external calendar**, chosen per tenant. This directly
   justifies the `CalendarProvider` abstraction the brief asks for (§13).

### 3.6 Copying boundary

No Dograh source is copied into this repository. Nothing in this report is a
transcription of Dograh code; the adopted items are architectural concepts
(ESL inbound mode, park-then-resolve, reaper, definition pinning,
transitions-as-tools) plus two verifiable external facts (the `mod_audio_stream`
wire protocol and its version floor). The product will not depend on Dograh at
runtime or at build time.

---

## 4. Recommended architecture

### 4.1 Layering

```
                         PSTN / SIP trunks
                                 |
                          +--------------+
                          | FreeSWITCH   |  SIP, RTP, codecs, DTMF,
                          | (PBX core)   |  bridging, recording, transfer
                          +--------------+
                            |ESL        |media (WebSocket, L16 PCM)
                            v           v
  +---------------------------------------------------------------+
  |                        PRODUCT                                 |
  |  Telephony Boundary  ->  Call Orchestrator  ->  Call Runtime    |
  |                                  |                    |        |
  |                            Tool Gateway  <------------+        |
  |                                  |                             |
  |   Application services: agents, conversations, contacts,       |
  |   calendar, phone numbers, workflow                            |
  +---------------------------------------------------------------+
        |                    |                          |
        v                    v                          v
   +---------+     +------------------+        +------------------+
   | SaaS-OS |     | AI providers     |        | Object storage   |
   | core.*  |     | (STT/LLM/TTS or  |        | (recordings)     |
   | infra.* |     |  realtime S2S)   |        +------------------+
   +---------+     +------------------+
        |
   PostgreSQL (RLS) + Redis
```

Binding dependency rule, to be enforced by import-linter contracts in
`tests/architecture/` exactly as SaaS-OS enforces its own:

```
callagent.*  ->  core.* / infra.* / api.* / control_plane.*      ALLOWED
core.* / infra.*  ->  callagent.*                                FORBIDDEN, always
callagent.*  ->  sqlalchemy / psycopg                            FORBIDDEN (use infra.db)
callagent.domain / callagent.tools  ->  callagent.runtime        FORBIDDEN
callagent.providers.<vendor>  imported outside callagent.providers  FORBIDDEN
```

The last two matter: domain and tool code must never import the realtime
runtime (so tools stay testable and reusable from the HTTP API), and a vendor
SDK must never leak past its adapter — the same discipline SaaS-OS applies to
Stripe in `core/billing`.

### 4.2 Process topology

Three processes from one image, following SaaS-OS's own backend/worker split,
plus FreeSWITCH as separate infrastructure:

1. **`control-api`** — FastAPI built with `api.platform.build_platform_app()`,
   plus this product's routers. Serves the dashboard/API: agent CRUD, publish,
   phone numbers, contacts, calendar, conversation history. Synchronous DB use
   throughout; no media, no AI.
2. **`call-runtime`** — the async process that owns ESL and the media
   WebSockets. One event loop per process, N concurrent calls per process,
   horizontally scaled. Because of **G-1**, every database or SaaS-OS call it
   makes goes through a small `blocking` helper
   (`await asyncio.to_thread(...)` around a sync `tenant_session_scope` block)
   with an explicitly sized thread pool. It never opens a DB session on the
   event loop, and it never does DB work on the audio path — only at
   call-start, at tool-call time, and at call-end.
3. **`worker`** — ARQ worker via `infra.jobs.build_worker()`, running the
   post-call pipeline (transcript persistence, summarization, recording move
   and encryption, webhook delivery, usage/billing events) plus retention jobs.

Separating `call-runtime` from `control-api` is not optional: a GC pause or a
slow synchronous query in the API process would be audible as a glitch in every
concurrent call hosted by the same process.

### 4.3 Data ownership

The product owns its own PostgreSQL database and its own Alembic history
(SaaS-OS ADR-0016), applying the platform's migrations through the installed
package (`infra.db.migration_runner.run_core_migrations()` or the
`saas-os-migrate` console script) and its own on top. Product tables live in a
dedicated schema (`callagent`), each with `tenant_id` and RLS established via
`infra.db.tenant_rls_statements`, exactly as the reference consumer does.

---

## 5. Component boundaries

| Component | Owns | Must never |
|---|---|---|
| **FreeSWITCH** | SIP signalling, RTP/codecs, DTMF, bridging, hold, transfer execution, recording capture, channel lifecycle, trunk registration | Hold product state; decide which agent answers; talk to an AI provider; be trusted as an authority on tenant identity |
| **Telephony Boundary** (`callagent.telephony`) | The ESL connection, event normalization, channel↔session correlation, the dialplan contract, the parked-channel reaper | Contain business rules; call an AI provider; be imported by domain code |
| **Call Orchestrator** | Resolving tenant + phone number + published AgentVersion; creating `CallSession`; enforcing concurrency/entitlements; issuing the media session ticket; end-of-call finalization | Touch audio; embed provider-specific logic |
| **Call Runtime** (`callagent.runtime`) | The per-call turn loop: audio in/out, VAD/barge-in, engine orchestration, transcript assembly, tool-call requests, timers | Access the database directly; construct its own tenant context; call a domain service without going through the Tool Gateway |
| **Tool Gateway** (`callagent.tools`) | Authorization, schema validation, idempotency, timeouts, redaction and audit for every action an agent takes | Trust any identifier supplied by the model; expose raw DB access; require a human approval round-trip on the realtime path |
| **Application services** | All domain logic and all DB access (via `infra.db`) | Be called directly by the model or the runtime, except through the Tool Gateway |
| **AI providers** (`callagent.providers.*`) | Vendor protocol, streaming, reconnection, vendor error mapping | Know about tenants, agents, tools-as-domain-actions, or the database |
| **SaaS-OS** | Tenancy, identity, RBAC, audit, idempotency, secrets, jobs, webhooks, billing, DB chokepoint, observability | Know this product exists (ADR-0015 rule 5) |

**Trust boundaries** (each is a point where input becomes untrusted until
re-resolved server-side): PSTN→FreeSWITCH; FreeSWITCH→Telephony Boundary;
Runtime→Tool Gateway; Product→AI provider; external webhook→Product.

---

## 6. Domain model (proposal)

All product tables: UUID primary keys, `tenant_id UUID NOT NULL REFERENCES
core.tenants(id)`, RLS enabled, `created_at`/`updated_at`. Every query runs
inside `infra.db.tenant_session_scope(tenant_id)` with a *verified* tenant.

| Entity | Purpose | Key fields | Notes |
|---|---|---|---|
| **TenantIntegration** | Per-tenant external credentials and settings (AI provider keys, SIP trunk, calendar OAuth) | `kind`, `status`, `config JSON`, `secret_ciphertext` | Secrets encrypted via `core.crypto` with `tenant_id` as AAD (G-3) |
| **Agent** | The stable, tenant-visible identity of an assistant. Mutable **pointer**, not configuration | `name`, `description`, `draft_version_id`, `published_version_id`, `status` | Carries no behavior itself |
| **AgentVersion** | **Immutable** published snapshot of everything that determines behavior | `agent_id`, `version_number`, `status(draft\|published\|archived)`, `config JSON`, `config_hash`, `published_at`, `published_by` | `config` holds instructions, greeting, language, voice ref, tool allowlist, transfer rules, business hours, limits, workflow graph, knowledge refs. Append-only (§7) |
| **PhoneNumber** | A DID owned by a tenant and routed to an agent | `e164`, `label`, `agent_id`, `version_pin_mode(follow_published\|pinned)`, `pinned_version_id`, `inbound_enabled`, `outbound_caller_id`, `trunk_id` | The inbound routing key; uniqueness enforced **globally**, not per tenant (§14.1) |
| **SipTrunk** | A SIP carrier/gateway registration | `name`, `gateway_name`, `direction`, `config JSON`, `credential_ref` | FreeSWITCH-side name is the join key |
| **CallSession** | One telephony call, from setup to teardown | `direction`, `status`, `from_e164`, `to_e164`, `phone_number_id`, `agent_id`, `agent_version_id`, `fs_channel_uuid`, `fs_call_uuid`, `started_at`, `answered_at`, `ended_at`, `duration_ms`, `hangup_cause`, `end_reason`, `recording_id`, `contact_id` | `agent_version_id` is **resolved once, at call start, and never re-read** |
| **Conversation** | The semantic layer over a CallSession | `call_session_id`, `summary`, `outcome`, `intent`, `structured_data JSON`, `sentiment`, `language` | 1:1 with CallSession today; separate so a future channel (chat) can reuse it |
| **ConversationTurn** | One transcript turn | `conversation_id`, `seq`, `role(caller\|agent\|system)`, `text_ciphertext`, `started_at_ms`, `ended_at_ms`, `confidence` | Encrypted at rest (§16.2); `seq` unique per conversation |
| **ToolCall** | One tool execution attempt within a call | `conversation_id`, `tool_key`, `agent_version_id`, `arguments_redacted JSON`, `result_summary`, `status`, `error_code`, `latency_ms`, `idempotency_key`, `audit_entry_id` | The forensic record of what the AI actually did |
| **Recording** | Pointer to stored audio | `call_session_id`, `storage_key`, `bytes`, `format`, `duration_ms`, `encryption_key_ref`, `retention_expires_at`, `deleted_at` | Never the audio itself |
| **Contact** | Small native contact | `display_name`, `email`, `notes`, `tags` (child table), timestamps | §12 |
| **ContactPhone** | Normalized number for a contact | `contact_id`, `e164`, `kind`, `is_primary` | Unique `(tenant_id, e164)`; the caller-ID lookup index |
| **Calendar** | A bookable resource | `name`, `timezone`, `provider(native\|external)`, `provider_ref`, `slot_granularity_minutes`, `min_notice_minutes`, `max_advance_days` | §13 |
| **WorkingHours** | Availability template | `calendar_id`, `weekday`, `start_time`, `end_time` | Plus `CalendarException` rows for holidays/overrides |
| **Appointment** | A booking | `calendar_id`, `contact_id`, `conversation_id`, `starts_at`, `ends_at`, `status`, `title`, `notes`, `external_ref`, `idempotency_key` | §13.2 |
| **Tool** *(config, not code)* | Which tools an agent may use, and their per-tenant settings | `tool_key`, `enabled`, `config JSON`, `permission_ref` | The *definition* (schema, handler, policy) is code; this row is per-tenant enablement. §8 |
| **Workflow** | A call workflow graph | Stored **inside** the AgentVersion snapshot when published; a `WorkflowDraft` row while editing | §9 — not an independently versioned runtime entity |
| **KnowledgeSource** / **KnowledgeChunk** | Retrieval corpus | deferred to Phase 4 | Referenced by AgentVersion by id + content hash |

Relationships that carry the product's value: `Contact ↔ Conversation ↔
Appointment`. A call resolves (or creates) a Contact, produces a Conversation,
and may produce an Appointment — those three joins are what a tenant actually
looks at the morning after.

---

## 7. Agent lifecycle and versioning

### 7.1 States

```
draft ──publish──▶ published ──supersede──▶ archived
  ▲                    │
  └──edit (new draft)──┘
```

- **Agent** is a pointer with `draft_version_id` and `published_version_id`.
- **AgentVersion** rows are **append-only**. Publishing never mutates a row; it
  inserts a new one and moves the pointer in the same transaction.
- `config_hash` = SHA-256 over a canonical JSON serialization of `config`.
  Two publishes of identical configuration are still two rows (distinct
  `version_number`, distinct `published_at`) but share a hash — which makes
  "did anything actually change?" answerable and makes replaying a call against
  its exact configuration verifiable.

### 7.2 Immutability enforcement — three layers, not one

Application-level discipline alone is not sufficient for the property the brief
demands ("changing a draft agent must not change an active production call"):

1. **Application**: no service function updates a `published` AgentVersion;
   publish is `INSERT` + pointer move inside one transaction.
2. **Database**: a `CHECK`/trigger (or a `BEFORE UPDATE` rule) rejecting any
   `UPDATE` to a row whose `status = 'published'` other than the single
   `status → 'archived'` transition.
3. **Runtime**: `CallSession.agent_version_id` is written at call start and the
   snapshot is **loaded once into memory for the life of the call**. Even a
   successful hostile update would not reach a call in progress.

### 7.3 Publishing

`publish(agent_id, actor)`:
validate the draft (tools exist and are entitled; voice exists at the configured
provider; workflow graph is well-formed and every transition resolves; transfer
destinations are valid E.164; business hours parse in the agent's timezone) →
`INSERT` AgentVersion `status='published'`, `version_number = max+1` →
move `agent.published_version_id` → `core.audit_log.record('agent.published', …)`
including `config_hash` and `version_number` → optionally
`core.webhooks.trigger_event('agent.published')`.

Validation happens at publish time, not at call time: a caller must never hear
the result of a configuration error that could have been caught minutes earlier.

### 7.4 Version selection at call time

```
PhoneNumber.version_pin_mode
  = follow_published -> agent.published_version_id   (default)
  = pinned           -> phone_number.pinned_version_id
```

`pinned` exists so a tenant can canary a new version on one number, and so a
regression can be pinned back without unpublishing. **Rollback is re-pointing,
never editing**: `published_version_id` moves back to an earlier row.

---

## 8. Tool model

### 8.1 The boundary

```
AgentVersion tool allowlist
        |
     AI model  --requests-->  Tool Gateway  -->  Application service  -->  infra.db / external
                                   |
                        tenant context (server-side)
                        authorization (core.rbac)
                        input/output schema validation
                        entitlement + quota (core.billing/usage)
                        idempotency (core.idempotency)
                        timeout + retry policy
                        redaction + audit (core.audit_log)
```

**The AI never has database access, and never has a tenant identifier it can
choose.** The Gateway derives `tenant_id`, `agent_version_id`,
`call_session_id` and the acting principal from the in-memory call context that
the Orchestrator created at call start — the model's arguments contribute
*domain* parameters only, never identity, never scope.

### 8.2 Why the product owns its own Gateway rather than calling `control_plane.orchestration.invoke_tool()`

This is the most consequential integration decision in the report, so the
evidence is stated explicitly (all verified at the pinned SHA):

- `invoke_tool()` raises `TierRequiresApprovalError` for any tool with
  `autonomy_tier >= 1`; tier ≥ 1 requires a proposal through
  `control_plane.approvals` and a human decision. A caller on the phone cannot
  wait for that. Every in-call tool would have to be declared tier 0, which
  would make the tier field meaningless rather than protective.
- It requires `agent_user_id: uuid.UUID` — a *user*. Our acting principal is a
  `ServiceAccount` (§14.4).
- `ToolDefinition` has no `input_schema`, no `output_schema`, no `timeout`, no
  `retry_policy`, no `idempotency_policy`. The brief requires all of these.
- Its handler contract is a plain `Mapping[str, object]` payload with no
  validation step — exactly the place where a hallucinated argument must be
  stopped.

**Recommendation**: build `callagent.tools.gateway`, composed from SaaS-OS
primitives (`core.rbac.can`, `core.idempotency.run_idempotent`,
`core.audit_log.record`, `core.usage.consume_quota_idempotent`,
`core.billing.require_entitlement`), and *additionally* register the same tools
as `ToolDefinition`s in the Control Plane registry for non-realtime, operator or
back-office agent use, where approval gates are appropriate and welcome. One
tool implementation, two callers, two policies. Do **not** modify SaaS-OS to
make `invoke_tool` realtime-capable — that would be a change to the dependency,
which is forbidden, and arguably wrong for the platform anyway.

### 8.3 Tool definition (product-side)

```
ToolDefinition:
  key                 "calendar.book"
  version             1                      # bumped on any schema change
  description         model-facing text
  input_schema        JSON Schema (strict, additionalProperties: false)
  output_schema       JSON Schema
  permission          (resource, action) registered via core.rbac.register_permission
  tenant_scope        "tenant"               # always; no cross-tenant tool exists
  side_effect         read_only | mutating
  timeout_ms          e.g. 1500 read / 4000 mutating
  retry_policy        none | idempotent_retry(max=2, backoff)
  idempotency         none | key_from(call_session_id, tool_call_id)
  audit               always | on_mutation
  data_classification none | tenant_data | pii | sensitive_pii
  entitlement         optional feature key
  handler             async wrapper -> sync application service via to_thread
```

### 8.4 Execution pipeline (ordered; every step can deny)

1. **Resolve context** from the call, never from arguments.
2. **Allowlist check** — is `tool_key` in the *published AgentVersion's* tool
   list? A model that invents a tool name is denied here.
3. **Entitlement/quota** — `require_entitlement`, `consume_quota_idempotent`.
4. **Schema validation** — strict; unknown properties rejected, not dropped.
5. **Authorization** — `core.rbac.can(principal, resource, action, scope)`.
6. **Idempotency** — `run_idempotent` keyed on
   `(call_session_id, tool_call_id)`, so a model retry or a runtime reconnect
   cannot double-book an appointment or double-create a contact.
7. **Execute** with timeout, off the event loop.
8. **Validate + redact output**, then return a model-safe result — never raw
   rows, never internal ids the model has no use for.
9. **Audit**: `core.audit_log.record(action='tool.executed', …)` with
   `tool_key`, `agent_version_id`, `call_session_id`, outcome, latency —
   **never** the argument or result values for PII-classified tools.
10. **Persist** a `ToolCall` row for the conversation record.

Failure is a *value*, not an exception, at the model boundary: the runtime hands
the model a structured `{"error": {"code": ..., "retryable": bool}}` so the
agent can say "I couldn't book that, may I take a message?" instead of going
silent. Internal detail never crosses that line.

### 8.5 Initial tool set

Phase 1 (defined, implemented in Phase 2–3): `call.transfer`, `call.end`,
`contact.lookup`, `contact.create`, `contact.update`,
`calendar.get_availability`, `calendar.book`, `calendar.reschedule`,
`calendar.cancel`, `knowledge.search`.

Deferred, and each needs its own security review before existing: generic
`http.request` / custom API tools (SSRF, credential exfiltration, and
tenant-authored egress), webhook tools, MCP tools (an entire second,
tenant-controlled tool supply chain).

---

## 9. Workflow model

### 9.1 Position

Workflows are **optional and secondary**. The primary authoring surface is a
prompt-and-tools agent (§3.5). A workflow exists to make a specific call
deterministic where a tenant needs it: a compliance script, a fixed
qualification sequence, a regulated disclosure.

### 9.2 Shape

A directed graph, authored as data, **serialized into the AgentVersion snapshot
at publish time** — so a workflow is never independently mutable while a call
is running, and no second versioning system is needed.

Node kinds (the full eventual set; Phase 3 implements `start`, `agent`, `tool`,
`transfer`, `end`, `error`):

| Node | Behavior |
|---|---|
| `start` | Entry; selects greeting by business hours/language |
| `say` | Deterministic utterance (TTS), no model turn |
| `agent` | A bounded LLM conversation segment with its own instructions and tool subset |
| `collect` | Gather a typed value (name, date, number) with validation and retries |
| `tool` | Deterministic tool invocation through the Gateway |
| `condition` | Branch on collected variables or call metadata |
| `transfer` | Hand off to a human; destination + ring timeout + fallback edge |
| `wait` | Bounded silence/hold |
| `end` | Terminal, with an outcome label |
| `error` | Fallback target for any node's failure edge |

**Transitions are tool calls** (adopted from Dograh, §3.2.6): each outgoing edge
of an `agent` node is exposed to the model as a named function; edge-name
uniqueness is validated at publish time. This makes every transition appear in
the same `ToolCall` record as every other agent action — one observability
story, not two.

### 9.3 Invariants

- Every node has an error edge, explicit or inherited from a graph-level
  `error` node. A call must never be able to reach a state with no exit.
- Total call duration, total turns, and total tool calls are bounded by the
  AgentVersion's call limits; exhausting a bound routes to `error`, which by
  default transfers or takes a message rather than hanging up on a human.
- Graph validation at publish: reachability, no unreachable terminal, all
  transitions resolve, no duplicate generated tool names, referenced tools are
  in the allowlist.
- Variables collected during a call live in the runtime's in-memory context and
  are persisted to `Conversation.structured_data` at end-of-call — they are
  never a side channel into the database.

### 9.4 Not now

No visual builder, no generic step/trigger engine, no cross-call orchestration,
no campaign runner, no user-authored code execution. SaaS-OS's own ADR-0007
already declines to build a general workflow engine; duplicating one at the
product level would be a strictly worse version of the same mistake.

---

## 10. FreeSWITCH integration boundary

### 10.1 Division of responsibility

| FreeSWITCH owns | The product owns |
|---|---|
| SIP registration/trunks, NAT, codecs, RTP, jitter, DTMF, early media, bridging, hold/park, recording capture, hangup causes, channel limits | Who owns this DID, which AgentVersion answers, what is said, which tools may run, what is stored, what is billed |

The product never speaks SIP or RTP and never parses SDP. FreeSWITCH never calls
an AI provider and never holds product state.

### 10.2 Where the *telephony* provider seam actually goes

Not around FreeSWITCH (that is Dograh's shape, rejected in §3.4) but **below**
it: SIP trunks and DID vendors are the varying part. A carrier change is a
`SipTrunk` row and a FreeSWITCH gateway profile, not a new Python provider
class. The product's abstraction for "how do I make a call happen" is exactly
one implementation — `FreeSwitchTelephony` — and that is a feature, not
technical debt. If a hosted CPaaS ever becomes a requirement, it enters as a SIP
trunk into FreeSWITCH, not as a parallel provider interface.

### 10.3 Control channel

**ESL, inbound mode** (product connects out to `mod_event_socket`; FreeSWITCH
does not connect back per call). Rationale is Dograh's, independently sound: ESL
is FreeSWITCH's only first-class, ARI-equivalent control channel;
`mod_xml_rpc` is a weak bolt-on; outbound-mode ESL requires a bespoke `socket`
application wired into every extension, where inbound mode needs only a
`park()` step.

The ESL connection is an operator-configured, network-restricted, credentialed
link. Anything arriving on it is **input**, not authority (§14.1).

### 10.4 Media

`mod_audio_stream` (pin ≥ v1.0.3; verify the wire protocol against module
source, not the README, and do not confuse it with `mod_audio_fork`).
Asymmetric protocol: FreeSWITCH → product is raw binary L16 PCM with no
envelope; product → FreeSWITCH is JSON text frames carrying base64 L16.
Started per call via `uuid_audio_stream <uuid> start <wss-url> mono <rate> <metadata>`.

The `<wss-url>` carries a **short-lived signed session ticket**, not a tenant id
(§14.3). Sample rate: 8 kHz on the PSTN leg is the realistic default; whether to
upsample for the provider is a provider-adapter concern, benchmarked in Phase 2.

### 10.5 Call flows

**Inbound**: DID arrives → dialplan `park()`s while ringing → product observes
`CHANNEL_PARK` → resolves called number → `PhoneNumber` → tenant + agent →
resolves the AgentVersion per pin mode → checks tenant status, entitlement and
concurrency → creates `CallSession` and mints a media ticket →
`uuid_answer` → `uuid_audio_stream start` → runtime takes over.

**Outbound**: an authorized request (API or tool) creates a `CallSession` first,
then `bgapi originate` with a product-generated `origination_uuid` and stamped
channel variables → `CHANNEL_PARK`/answer on our own UUID is the "attach media"
trigger. Channel variables are used to *correlate*, never to *authorize*: the
session row that the product created is the authority (§14.1).

**Transfer**: FreeSWITCH has no Asterisk-style bridge object. Originate the
destination leg, correlate its `CHANNEL_ANSWER` to the transfer id, then
`uuid_bridge` the caller to the answered destination. Policy — whether the agent
announces, whether it waits for answer, what happens on no-answer — is the
product's, in the AgentVersion's transfer rules.

**Teardown**: `CHANNEL_HANGUP` / `CHANNEL_HANGUP_COMPLETE` finalizes the session,
releases the concurrency slot, clears ephemeral Redis mappings, and enqueues the
post-call job.

### 10.6 Resilience requirements (design-in, not retrofit)

- **Parked-channel reaper** (§3.2.3): periodic rescan; never touch `ACTIVE`
  channels; honor an in-flight claim marker; hang up unclaimed parked channels
  past a timeout with an explicit cause. This is the defense against ESL
  downtime, missed events across reconnects, and SIP scanners.
- **ESL reconnection with resynchronization**: on reconnect, enumerate live
  channels and reconcile against open `CallSession` rows in both directions
  (orphan channel → reap; orphan session → finalize as `interrupted`).
- **Runtime crash**: a call whose media socket dies must be hung up by
  FreeSWITCH within a bounded time, and the session finalized by the reconciler
  — never left billing or "in progress" forever.
- **Concurrency limits at two layers**: FreeSWITCH-level (protects the box) and
  product-level per tenant (protects the plan). They are different limits with
  different owners and must not be conflated.

---

## 11. AI provider abstraction

### 11.1 The key insight: STT + LLM + TTS is not a sufficient abstraction

A three-box pipeline cannot express a realtime speech-to-speech provider
(ElevenLabs Agents, OpenAI Realtime, Gemini Live), where transcription,
reasoning, tool-calling and synthesis happen inside one stateful bidirectional
session and cannot be decomposed. Any architecture whose top-level interface is
`(STT, LLM, TTS)` must either exclude those providers or bolt them on as a
special case — and bolting them on later means rewriting the runtime, which the
brief explicitly forbids.

**Recommendation**: the runtime depends on **`ConversationEngine`**, with two
implementations:

```
ConversationEngine            (what the Call Runtime knows about)
├── PipelinedEngine(stt, llm, tts)     composes the three interfaces below
└── RealtimeEngine(provider)           one stateful duplex vendor session
```

Both expose the same event stream to the runtime, so the runtime is written once:

```
ConversationEngine:
  async start(session_config) -> EngineSession
EngineSession:
  async send_audio(pcm_frame)
  async interrupt()                     # barge-in
  async submit_tool_result(id, value)
  async close()
  events: AudioOut(frame) | PartialTranscript | FinalTranscript
        | SpeechStarted | SpeechEnded | ToolCallRequested(id, name, args)
        | UsageReported | EngineError
```

### 11.2 The component interfaces (used by `PipelinedEngine`)

```
SttProvider:     async stream(audio) -> AsyncIterator[Transcript(partial|final, text, confidence, ts)]
                 capabilities: languages, sample_rates, diarization, endpointing, vad
LlmProvider:     async stream_turn(messages, tools) -> AsyncIterator[TextDelta | ToolCallRequest | TurnEnd]
                 capabilities: streaming, parallel tool calls, context window, json mode
TtsProvider:     async synthesize(text, voice_ref) -> AsyncIterator[AudioFrame]
                 capabilities: streaming, sample rates, ssml, latency class
VoiceCatalog:    async list_voices(tenant) -> [Voice(id, name, language, gender, preview_url)]
```

Cross-cutting requirements every adapter must satisfy: streaming-first; explicit
cancellation (barge-in must stop synthesis mid-frame, and a cancelled TTS bill
must still be metered); typed error taxonomy
(`auth | rate_limit | transient | invalid_request | provider_down`) so the
runtime's fallback policy is provider-agnostic; usage reporting in provider-
neutral units; deterministic fake implementations for tests.

### 11.3 Voice as a reference, not a value

`AgentVersion.config.voice` stores `{provider, voice_id, settings}` — a
reference, resolved through `VoiceCatalog` at publish-time validation. Swapping
providers is then a config migration, not a code change.

### 11.4 Proving the abstraction

An interface with one implementation is an assumption. ElevenLabs is a
reasonable first provider, but Phase 2 must ship a **second, deliberately
dissimilar** implementation — ideally one `PipelinedEngine` (separate STT/LLM/TTS
vendors) and one `RealtimeEngine` — before the interface is declared stable. A
local/offline engine also makes CI hermetic and keeps development free.

### 11.5 Data boundary

Sending caller audio or transcript to an external provider crosses SaaS-OS
ADR-0013's external-model boundary. See §15.5 — this is an authorization
decision, not just a configuration one.

---

## 12. Contacts architecture

Intentionally small. The purpose is call context, not customer relationship
management.

- `Contact`: `id`, `tenant_id`, `display_name`, `email`, `notes`, timestamps.
- `ContactPhone`: normalized **E.164**, `kind`, `is_primary`; unique
  `(tenant_id, e164)` — the index that makes caller-ID lookup a single hit.
- `ContactTag`: a child table (avoids G-4's missing `ARRAY`, and makes
  tag filtering indexable).

Behavior:

- **Normalization is mandatory and central.** One function turns any inbound
  number into E.164 given the tenant's default region. Every write and every
  lookup goes through it; a number never enters the database in any other form.
- **Resolution at call start** is a *convenience*, never an authorization input:
  a matched contact personalizes the greeting and pre-fills context; a caller-ID
  match authorizes nothing (caller ID is trivially spoofed — §14.1).
- `contact.create` / `contact.update` are Gateway tools with strict schemas and
  idempotency; merge/dedupe is an operator action in the dashboard, never an
  agent action.
- Deliberately absent: pipelines, deals, custom fields, lists, segments, imports
  at scale, activity timelines beyond the Conversation join. If a tenant needs
  those, the answer is an integration, not a feature.

---

## 13. Calendar architecture

### 13.1 Model

`Calendar` (name, IANA timezone, provider, granularity, min-notice,
max-advance) → `WorkingHours` (per weekday) + `CalendarException` (holidays,
one-off closures/openings) → `Appointment`.

### 13.2 Availability and booking

- Availability is computed **server-side** in the calendar's timezone, always
  stored and compared in UTC, and returned as explicit slots. The model never
  does date arithmetic — it picks from offered slots. (Dates and timezones are
  where LLMs fail most reliably and most invisibly.)
- Booking is **idempotent** and **race-free**: `calendar.book` runs inside
  `core.idempotency.run_idempotent` keyed on `(call_session_id, tool_call_id)`,
  and takes `infra.db.acquire_tenant_advisory_lock(session, tenant_id, f"calendar:{calendar_id}")`
  before the availability re-check and insert — so two simultaneous calls cannot
  book the same slot.
- Availability is **re-validated at booking time**, never trusted from the
  earlier `get_availability` result.
- `reschedule` and `cancel` are separate tools with their own permissions;
  cancel is soft (`status='cancelled'`), never a delete.

### 13.3 CalendarProvider abstraction

```
CalendarProvider:
  async get_availability(calendar, window) -> [Slot]
  async book(slot, attendee, metadata) -> ExternalAppointmentRef
  async reschedule(ref, new_slot) -> ExternalAppointmentRef
  async cancel(ref) -> None
  capabilities: supports_reschedule, supports_attendees, min_notice, writes_back
```

`NativeCalendarProvider` is the first and only Phase-3 implementation; Google /
Microsoft / cal.com / Calendly adapters come later (Fonio's model, §3.5). The
abstraction is designed now precisely so external calendars are an adapter, not
a rewrite. Two-way sync, free/busy merging across multiple calendars, and
conflict resolution are explicitly **not** designed here — they are the hard
part and need their own phase.

---

## 14. Security model

### 14.1 Nothing outside the product's own database is an authority

Never trusted as identity or authorization:

| Input | Why not | What is trusted instead |
|---|---|---|
| Caller ID (`from`) | Trivially spoofable over SIP/PSTN | Nothing — it is a lookup hint only |
| Called number (`to`) | Untrusted string on the wire | The `PhoneNumber` row it resolves to, server-side |
| FreeSWITCH channel variables | Whatever the dialplan or an originate put there | The `CallSession` row the product created |
| `agent_id` / `workflow_id` / `tool_id` from a client | Client-controlled | RBAC-checked lookup under `RequestContext.tenant_id` |
| Any `tenant_id` in a URL, payload or LLM argument | Client- or model-controlled | `RequestContext.tenant_id` / the in-memory call context |
| External webhook payloads | Unauthenticated until proven | Signature verification + replay protection (`core.webhooks`) |

DID uniqueness is enforced **globally, not per tenant**: two tenants claiming
the same E.164 makes inbound routing ambiguous, which is a tenant-isolation
failure, not a UX annoyance. Number ownership must be verified before
activation.

### 14.2 Tenant isolation

Every product table carries `tenant_id` with RLS via
`infra.db.tenant_rls_statements`; every read and write happens inside
`infra.db.tenant_session_scope(verified_tenant_id)`; ownership is re-checked
explicitly on top of RLS (belt and braces, as the reference consumer does); a
foreign or nonexistent row is the same non-enumerating 404. The application
database role must not be a superuser or `BYPASSRLS` — `build_platform_app()`
already fails closed on this at startup via
`infra.db.validate_application_role()`.

### 14.3 Media-session authentication

The media WebSocket is an unauthenticated network endpoint by default, and it
carries the entire call. Requirements:

- At `CallSession` creation the Orchestrator mints a **short-lived, single-use,
  signed ticket** bound to `call_session_id`, `tenant_id`, the FreeSWITCH
  channel UUID, and an expiry of seconds, not minutes.
- The ticket is the only credential in the `wss://` URL passed to
  `uuid_audio_stream`. **No tenant id, agent id or phone number appears in that
  URL as a trusted value.**
- The runtime verifies signature, expiry, single-use (Redis), and
  channel-UUID match, then loads the call context from its own database.
- The media endpoint is additionally network-restricted to the FreeSWITCH hosts.

### 14.4 The acting principal

The call runtime acts as a **`core.identity.ServiceAccount`** per tenant (or a
platform service account with a tenant-scoped delegation), holding a narrow role
whose permissions are exactly the tool permissions
(`callagent.contacts:read`, `callagent.calendar:book`, …) registered through
`core.rbac.register_permission`. Consequences that come free: `core.rbac.can()`
works unchanged for AI actions; audit entries name a real principal; a tenant
can revoke the agent's ability to book without touching code; delegated
administration (an agency operating a client tenant) is already modeled by
`core.rbac.create_delegation`.

A tool the published AgentVersion does not list is refused **even if** the
principal has the permission — allowlist and authorization are independent
gates, and both must pass.

### 14.5 Prompt injection is an expected condition

A caller can say anything, and a knowledge document can contain anything.
Therefore: caller speech and retrieved knowledge are **data, never
instructions**; the system prompt comes only from the immutable AgentVersion; no
tool takes a tenant, agent, principal, calendar or contact identifier from model
output; every tool argument is strictly schema-validated; destructive or
irreversible actions are not exposed as in-call tools at all (§8.5). The mental
model: a hostile caller is a normal user of the system, not an attack to be
detected.

### 14.6 Outbound calling abuse

Outbound dialing is a weapon if unbounded. Per-tenant concurrency and
per-window call caps, destination allow/deny lists (premium-rate and
international prefixes denied by default), verified caller ID, an explicit
entitlement to dial outbound at all, and audit on every origination. This is
also a fraud-cost control, not only a compliance one.

---

## 15. Audit model

`core.audit_log.record()` is the only audit store; the product adds no second
one. Auditable actions (all tenant-scoped, all with a real actor):

`agent.created` · `agent.updated` · `agent.published` (with `version_number`,
`config_hash`) · `agent.version_pinned` · `agent.archived` ·
`tool.configured` · `tool.executed` · `phone_number.assigned` ·
`phone_number.released` · `trunk.configured` · `call.started` · `call.answered` ·
`call.transferred` · `call.ended` · `outbound_call.initiated` ·
`appointment.created` · `appointment.rescheduled` · `appointment.cancelled` ·
`contact.created` · `contact.updated` · `contact.deleted` ·
`recording.accessed` · `recording.downloaded` · `recording.deleted` ·
`transcript.accessed` · `integration.credential_set` · `retention.applied` ·
`data_authorization.decided` (written by SaaS-OS itself).

Two rules that matter more than the list: **audit metadata never contains call
content** (no transcript text, no recording bytes, no PII-bearing tool
arguments — only keys, classifications, ids, outcomes and latencies), and
**reading sensitive data is itself an audited action**. "Who listened to that
recording?" must be answerable.

Volume is a real design constraint: at a few tool calls per call and thousands
of calls per day, audit is a high-write table. Decide retention and partitioning
before launch (§19 OD-7).

---

## 16. Privacy model

Call recordings and transcripts are among the most sensitive data a SaaS product
can hold: biometric-adjacent voice data, plus whatever a caller volunteered.
Phase 0 records requirements; Phase 4 implements them.

### 16.1 Recordings

- Stored in object storage, **never in the database** — the DB holds only a
  `Recording` row pointing at a key.
- Per-tenant key prefix; server-side encryption at the bucket *plus* a
  product-held per-recording data key wrapped via `core.crypto` — so bucket
  compromise alone is insufficient.
- Access exclusively through short-lived, RBAC-checked, audited signed URLs.
  No public objects, ever. No permanent URL stored anywhere.
- Consent/announcement configuration per agent and per jurisdiction ("this call
  is recorded"), enforced at call start, not left to the prompt.
- Recording can be disabled per tenant, per agent, or per number — and *must* be
  suppressible mid-call (a caller reading out a card number is the canonical
  case).

### 16.2 Transcripts

Field-encrypted at rest via `core.crypto.EncryptionService` with `tenant_id`
(and ideally `conversation_id`) as associated data, so a ciphertext cannot be
moved between tenants or rows. Redaction of obvious high-risk patterns (card
numbers, national ids) before persistence, with the raw form never written.
Transcript access is RBAC-gated and audited.

### 16.3 Retention and deletion

- Per-tenant, per-artifact retention policy (recording, transcript, structured
  data may differ), with a platform maximum the tenant cannot exceed.
- A scheduled `infra.jobs` retention job enforces expiry: delete the object,
  null the transcript ciphertext, keep the metadata row (duration, outcome,
  billing facts) so history and invoices remain coherent.
- **Tenant purge**: register a `TenantPurgeParticipant` (the reference consumer
  shows the exact shape) that deletes this product's tenant data — including
  object-storage keys, which no database cascade will reach. Idempotent, safe to
  retry.
- Per-subject deletion ("delete everything about this caller") must be designed
  as a real capability, not improvised later: it spans contacts, conversations,
  transcripts, recordings and appointments.

### 16.4 AI provider boundary

Sending audio or transcript to an external provider is a cross-boundary data
flow under SaaS-OS ADR-0013. Requirements that follow directly: per-tenant AI
data policy (allowed providers, allowed data classes, retention requirements);
unclassified data is denied by default; data minimization (send the current
turn plus bounded context, not the customer's record); provider eligibility as
configuration, never a hardcoded trust assumption.

### 16.5 Making the gate compatible with realtime

`control_plane.data_authorization.authorize_data_access()` writes exactly one
audit entry per decision. Evaluating it per audio frame would be both
latency-fatal and an audit flood. **Authorize once per `CallSession`, at call
start, before any audio reaches a provider** — the decision is then an
in-memory allow for the life of the call, referenced by `decision_id` on the
session row. A denial means the call never reaches the AI at all: it goes to
voicemail, a transfer, or a configured message. This is the correct granularity
anyway: the policy question ("may this tenant's caller audio go to provider X
for purpose Y?") does not change between frames.

---

## 17. Observability requirements

- **Correlation**: `call_session_id` is the correlation id for everything a call
  touches, bound via `infra.observability.bind_correlation_context` and carried
  into ESL commands, provider requests, tool executions and jobs. One id, one
  call, end to end.
- **Metrics that predict user-perceived quality** (the ones that matter):
  answer latency (ring → audio), time-to-first-token (LLM), time-to-first-audio
  (TTS), end-of-speech → agent-speech gap, barge-in count and barge-in cut
  latency, ASR final-transcript latency, tool latency p50/p95/p99 by tool,
  tool error rate, provider error/retry rate, concurrent calls per tenant and
  per FreeSWITCH node, media socket drops, transfer success rate.
- **Business metrics**: calls by outcome, containment rate (resolved without
  transfer), appointments booked per call, average duration, cost per call by
  provider component.
- **Traces**: one span per call with child spans per turn, per provider request,
  per tool execution. OTel is already SaaS-OS's accepted standard.
- **Logging discipline**: structured only; no transcript text, no recording
  bytes, no caller identity in logs — `infra.observability.redact` /
  `is_sensitive_key` exist for this. A debug log line containing call content is
  a privacy incident, not a debugging convenience.
- **Reconciliation dashboards**: open `CallSession` rows vs live FreeSWITCH
  channels; unclaimed parked channels; ARQ dead-letter depth
  (`infra.jobs.count_dead_letters`). These are the early-warning signals for the
  failure modes in §10.6.
- **Cost attribution** per call and per tenant, per provider component — without
  it, pricing is guesswork and a runaway tenant is invisible until the invoice.

---

## 18. Phase 1 implementation plan

Phase 1 is a **thin vertical slice with no AI and no audio**. The goal is to
prove the boundaries — dependency, tenancy, authorization, audit, migrations,
versioning — while they are still cheap to move.

**P1.0 — Decisions.** Answer §19 OD-1 (product/package name), OD-2 (engine
strategy), OD-4 (JSON/ORM primitives). Nothing else starts first.

**P1.1 — Repository foundation.** `git init`; the §1.1 layout; `pyproject.toml`
pinning `saas-os @ git+…@ff550010e5eafecace7311038aadc99fcecfbe3d`; ruff,
pyright, pytest, detect-secrets, import-linter contracts (§4.1); `scripts/check-*.sh`;
CI running the same scripts. *Done when*: a clean checkout installs the pinned
SaaS-OS and `check-all.sh` passes.

**P1.2 — Composition root.** `callagent/app.py` calling
`api.platform.build_platform_app()`, mounting a product router, registering the
purge participant. `callagent/worker.py` via `infra.jobs.build_worker()`.
*Done when*: `/health` serves and an authenticated route returns
`RequestContext.tenant_id`.

**P1.3 — Migrations and first tables.** The product's own Alembic history in
`callagent/migrations/`, independent of the platform's; create schema
`callagent`; `agents`, `agent_versions`, `phone_numbers` with `tenant_id`, RLS
via `infra.db.tenant_rls_statements`, and the published-row immutability
trigger. *Done when*: a CI job applies platform migrations then product
migrations against a throwaway PostgreSQL and a cross-tenant read returns
nothing.

**P1.4 — Agent lifecycle.** Draft CRUD, validation, publish, `config_hash`,
version pinning, archive, rollback-by-repointing; RBAC permissions registered;
audit on publish. *Done when*: a published version cannot be mutated by any code
path or by direct `UPDATE`, proven by a test.

**P1.5 — Phone numbers and routing resolution.** `PhoneNumber` CRUD with global
E.164 uniqueness and ownership verification; a pure
`resolve_inbound(to_number) -> (tenant, agent_version)` function with no
telephony dependency. *Done when*: resolution is unit-tested including the
suspended-tenant, unassigned-number, and unknown-number cases.

**P1.6 — Tool Gateway skeleton with two real tools.** The full §8.4 pipeline,
with `contact.lookup` (read) and `contact.create` (mutating, idempotent) as the
first two tools, plus `Contact`/`ContactPhone` and E.164 normalization. Invoked
in tests by a fake caller, not by a model. *Done when*: schema violation,
missing permission, not-in-allowlist, quota exhaustion and duplicate
idempotency key each produce the right denial and the right audit entry.

**P1.7 — Conversation skeleton.** `CallSession`, `Conversation`,
`ConversationTurn`, `ToolCall` tables and state machine, exercised by a
simulated call driver — no FreeSWITCH, no audio. *Done when*: a scripted
conversation produces a complete, queryable, tenant-isolated record.

**P1.8 — Provider interfaces, defined not implemented.** `ConversationEngine`,
`EngineSession`, `SttProvider`, `LlmProvider`, `TtsProvider`, `VoiceCatalog` as
protocols, with deterministic fakes used by P1.7's driver. No vendor SDK, no
network call. *Done when*: the simulated call runs end to end against fakes.

**Explicitly not in Phase 1**: FreeSWITCH, ESL, media, any AI provider, the
workflow engine, calendar, knowledge, recordings, the UI.

Phase 2 (after Phase 1 review): FreeSWITCH boundary + real media + one engine +
the reaper. Phase 3: calendar + transfer + workflow subset. Phase 4: knowledge,
privacy/retention implementation, external calendars, UI.

---

## 19. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R-1 | **Sync-only `infra.db` (G-1) under an async realtime loop** | A single blocking query stalls every call in the process; thread-pool exhaustion under load | Dedicated runtime process; all DB work via `asyncio.to_thread` with a sized pool; no DB on the audio path; load-test thread saturation in Phase 2 |
| R-2 | **Latency budget** — ~800 ms end-of-speech to first audio is the perceptual limit; ASR + LLM + TTS + tool + network must fit | Product feels broken regardless of correctness | Measure from day one (§17); prefer streaming everywhere; consider a realtime S2S engine; keep tool timeouts strict and speak filler while a tool runs |
| R-3 | **FreeSWITCH operational expertise** — it is powerful, sharp-edged, and unfamiliar territory for most teams | Outages, one-way audio, NAT/codec debugging, SIP-scanner abuse | Treat FreeSWITCH config as versioned infrastructure; the §10.6 resilience set is mandatory; restrict SIP exposure; budget real learning time |
| R-4 | **Media module dependency** — `mod_audio_stream` is a community module, easily confused with `mod_audio_fork` | Wrong module or version silently produces garbled or one-way audio | Pin ≥ v1.0.3; verify against module source; protocol conformance test in CI; document the distinction in the operator guide |
| R-5 | **Pinned SHA with no release tags** — SaaS-OS publishes no tags and its README already drifts from its code (G-7) | Upgrades become archaeology; a security fix could be missed | Scheduled human-reviewed re-pin; record the diff reviewed in an ADR; pin only tips of `main` with clean CI |
| R-6 | **Control-plane tool executor mismatch (G-6)** | Either an unusable approval gate on the phone, or the temptation to modify SaaS-OS | §8.2's dual-registration design; never modify SaaS-OS; revisit only if the platform grows a realtime execution path |
| R-7 | **Prompt injection via callers and knowledge documents** | Unauthorized tool use, data disclosure | §14.5: model output is never an identifier; allowlist + RBAC + strict schemas; destructive tools simply do not exist |
| R-8 | **Recording/transcript privacy exposure** | Regulatory and reputational harm, potentially existential | §16 implemented as designed; signed short-lived URLs; encryption; audited access; retention jobs |
| R-9 | **Cost runaway** — per-minute AI costs, a looping agent, or an abusive tenant | Margin destroyed silently | Per-call and per-tenant caps enforced by `core.usage` quotas; hard duration/turn/tool bounds in AgentVersion; per-call cost attribution (§17) |
| R-10 | **Outbound abuse / toll fraud** | Direct financial loss, carrier termination | §14.6 controls from the first outbound call, not after the first incident |
| R-11 | **Provider abstraction leaks** — building it around one vendor's shape | "Replaceable without rewriting the runtime" quietly becomes false | Two dissimilar engines before the interface is stable (§11.4); vendor SDK confined to its adapter by an import contract |
| R-12 | **Scope creep toward a CRM / marketing platform** | Dilution; the product stops being a call platform | §20 non-goals are binding; Contacts and Calendar stay deliberately small |
| R-13 | **Timezone and DST errors in booking** | Wrong-day appointments, which destroy trust instantly | All storage UTC; all display in calendar timezone; slots computed server-side; model never does date arithmetic; DST-boundary tests |
| R-14 | **Object storage is the product's own responsibility (G-2)** | Recording durability, lifecycle and encryption are easy to get wrong late | Design the `RecordingStore` interface in Phase 2 with the §16.1 requirements as its contract, not as later hardening |

---

## 20. Open decisions

Each needs a human answer; none should be resolved by drift.

- **OD-1 — Product and package name.** ~~`callagent` is a placeholder used
  throughout this report. Renaming is cheap now and expensive after migrations
  and a schema name exist. *Blocks P1.1.*~~ **RESOLVED 2026-09-21 — see §22 and
  ADR-0005.** Package `voiceagent`; schema `app`; commercial name deliberately
  undecided. Wherever this report writes `callagent.*`, read `voiceagent.*`.
- **OD-2 — Engine strategy.** Adopt Pipecat as a pinned upstream dependency
  behind `ConversationEngine`, or build a minimal in-house turn loop? Pipecat
  brings VAD, interruption handling, transports and dozens of vendor
  integrations for free, and a large surface to track; in-house is smaller,
  fully understood, and slower to reach parity. **Never a fork, in either case**
  (§3.4). *Blocks P1.8's shape, though not its interfaces.* **RESOLVED
  2026-09-21 — see §22 and ADR-0006.** Hybrid: Pipecat bounded to
  `PipelinedEngine` as an optional, fenced, unforked dependency;
  `RealtimeEngine` product-owned.
- **OD-3 — First AI provider(s).** ElevenLabs is the stated candidate; the
  second, dissimilar implementation required by §11.4 is unchosen. Realtime S2S
  or pipelined for launch?
- **OD-4 — ORM primitive gap (G-4).** Live within `JSON`/`String` +
  `CheckConstraint` + child tables, or request `JSONB`/`ARRAY` exports upstream
  in `infra.db`? Recommendation: live within what exists — it costs little and
  keeps the dependency untouched. **RESOLVED 2026-09-21 — see §22 and
  ADR-0007.** Live within `infra.db` in the application layer (the omissions
  are documented RLS-bypass defenses, not oversights); full SQLAlchemy in the
  migration layer only; no upstream change requested.
- **OD-5 — FreeSWITCH deployment topology.** Single node or a pool; who owns the
  SIP edge; how a call's media socket finds the right runtime process when
  runtimes scale horizontally (sticky ticket routing vs a shared registry).
  *Blocks Phase 2.*
- **OD-6 — Object storage backend.** S3, MinIO, or an S3-compatible provider;
  and whether the product reuses the `boto3` already present transitively or
  declares its own client dependency.
- **OD-7 — Audit and transcript volume.** Retention window, partitioning, and
  whether tool-call audit is sampled at high volume (and if so, what is never
  sampled).
- **OD-8 — Tenant-facing API surface.** Does the platform expose a public API
  and webhooks to tenants in v1, or only a dashboard? This changes how much of
  §8's tool layer must also be a public HTTP surface.
- **OD-9 — SMS/messaging.** Fonio's post-call SMS is a genuinely valuable
  pattern (§3.5), but SMS is a separate regulatory and provider domain. In or
  out for v1?
- **OD-10 — Knowledge implementation.** Vector store choice (pgvector in the
  product database vs external), chunking, and whether `knowledge.search` ships
  before Phase 4.
- **OD-11 — Multi-region / data residency.** Whether EU-resident audio
  processing is a launch requirement — it constrains provider choice
  (§11) and FreeSWITCH placement (OD-5) far more cheaply if decided now.
- **OD-12 — Re-pin cadence for SaaS-OS.** Who reviews, how often, and what
  evidence is recorded (R-5).

---

## 21. Explicit non-goals

Not built in Phase 0, and not to be started without a decision that supersedes
this report:

**Phase 0 build non-goals** (from the brief, restated as binding): no Agent
Runtime, no FreeSWITCH integration, no AI provider implementations, no workflow
engine, no UI, no database schema, no application code of any kind.

**Product non-goals** (durable, not phase-scoped):

- Not a CRM. Contacts stay minimal: no pipelines, deals, custom fields,
  segments, or activity feeds.
- Not a marketing or outbound-campaign platform. No campaign runner, no lead
  lists, no dialer, no drip sequences.
- Not a general business-automation or iPaaS platform. Workflows are **call**
  workflows only.
- Not an accounting or invoicing product; billing is SaaS-OS's
  `core.billing`/`core.usage` and a payment provider.
- Not a general-purpose PBX or softswitch — FreeSWITCH is, and the product does
  not reimplement SIP, RTP, or call control.
- Not a CPaaS abstraction layer over many telephony vendors (§10.2).
- Not a fork or vendor of SaaS-OS, Dograh, or Pipecat.
- Not a chat/omnichannel platform in v1. `Conversation` is modeled so a second
  channel is possible later; nothing more is promised.
- No tenant-authored code execution, and no generic outbound HTTP tool, until
  each has passed its own security review (§8.5).

---

## 22. Phase 0.1 — Architecture Blocker Resolution

*Added 2026-09-21. Resolves the three blockers §19 recorded as gating Phase 1.
This section records decisions; the reasoning lives in the ADRs it cites.
Nothing in §1–§21 is rewritten — the `callagent` placeholder is left in place as
history, with the mapping in OD-1 below as the authority.*

**Dependency pin re-verified.** The SaaS-OS pin is unchanged:
`ff550010e5eafecace7311038aadc99fcecfbe3d`. (A variant SHA ending `…fcecf3e3d`
was quoted when this work was requested; `git cat-file` reports no such object.
The pin in §2.1 is the correct one.) SaaS-OS was inspected read-only during this
phase and its working tree remains clean and unmodified.

### OD-1 — Product and package naming: **RESOLVED** (ADR-0005)

Four names, decoupled so the expensive ones never depend on the cheap ones:

| Layer | Value | Rationale |
|---|---|---|
| Commercial/product name | **Deliberately undecided** | Nothing in the codebase depends on it; it will live in `APP_DISPLAY_NAME` and the frontend only |
| Repository name | `ai-agent` (existing) | Already neutral; renaming buys nothing |
| Python package / distribution | **`voiceagent`** | Domain-descriptive, vendor-free, brand-free, valid identifier, no collision with the names `saas-os` occupies (`core`, `infra`, `api`, `control_plane`, `contracts`) |
| Database schema | **`app`** — frozen permanently | The most expensive name to change, so it is deliberately given no product or domain identity; reads unambiguously against `core.*` |
| API path namespace | `/v1/<resource>` | No brand, no package name, no product name in any URL |

`callagent` is retired. Wherever §1–§21 writes `callagent.*`, read
`voiceagent.*`. No code exists under the placeholder, so there is nothing to
migrate. A package rename, if it ever happens, must remain an import rewrite
only — never a schema migration or an API path change.

### OD-2 — Pipecat vs in-house realtime turn loop: **RESOLVED** (ADR-0006)

**Option C — hybrid, narrowly bounded.** The `ConversationEngine` abstraction
from §11 is unchanged and is not reopened.

- The `ConversationEngine` / `EngineSession` contract is **product-owned and
  Pipecat-free**: no framework type appears in it or in anything the Call
  Runtime sees.
- Pipecat may be the internal implementation of **`PipelinedEngine` only** —
  where VAD, endpointing, interruption plumbing, frame backpressure and the
  STT/LLM/TTS adapter catalogue are real, non-differentiating work.
- **`RealtimeEngine` is product-owned**: a direct adapter over the vendor's
  duplex session. For speech-to-speech providers the vendor already owns VAD,
  turn detection and interruption, so a framework in that path adds a hop and an
  impedance mismatch exactly where tool-call events must reach the Tool Gateway.
- Pipecat is an **optional extra** (`voiceagent[pipecat]`), exact-version
  pinned, **never forked, never monkeypatched, never subclassed to change
  behavior**; an import fence confines it to
  `voiceagent.providers.engines.pipecat/` and CI enforces the fence.
- A **non-Pipecat path always exists in CI** (`FakeEngine` + the product-owned
  `RealtimeEngine`), which is the structural guarantee that Pipecat is
  removable rather than load-bearing.
- **Exit criteria are written down** (fork/patch required; attributable latency
  regression; re-pins routinely breaking; boundary concepts leaking upward).
- **Engine is not the media transport**: the FreeSWITCH media socket belongs to
  `MediaProvider`, not to the engine.

This is what prevents the runtime rewrite §1–§21 prohibits: the runtime depends
on a contract this product owns, and no engine implementation is load-bearing
for that contract's existence.

### OD-4 — SaaS-OS ORM primitive boundary: **RESOLVED** (ADR-0007)

Direct inspection of the pinned dependency materially changed this question.
**The missing primitives are deliberate security decisions with recorded
live-audit evidence, not oversights.** `infra/db/orm.py` documents that
`sqlalchemy.func` and `sqlalchemy.text` are withheld because either one lets
ordinary Core/Product code execute `set_config('app.tenant_id', …)` inside a
tenant-scoped session — bypassing Row-Level Security for the rest of the
transaction and, with `is_local=false`, poisoning the pooled connection past
`COMMIT`.

Decision:

- **Application layer** (models, services, tools, runtime) imports persistence
  primitives only through one product module, `voiceagent.db`, which re-exports
  `infra.db`. It never imports `sqlalchemy`, `psycopg`, or a
  `sqlalchemy.dialects.*` name. **`func` and `text` are never reintroduced by
  any route** — the single non-negotiable rule.
- **Migration layer** (`voiceagent/migrations/versions/*.py`) imports
  `sqlalchemy as sa` and uses any PostgreSQL type or raw DDL it needs. This is
  the platform's own sanctioned split, demonstrated by
  `examples/reference-consumer/`: its model imports only `infra.db`, its
  migration imports `sa` freely. A migration emits DDL in its own transaction
  and never runs inside a tenant-scoped session, so it cannot bypass a policy.
- **The migration is the source of truth for a column's physical type**; the ORM
  type only needs to be compatible. Models are declared on `infra.db.Base`, DDL
  is never generated from them, `create_all()` is never called, and **Alembic
  autogenerate stays disabled** (`target_metadata = None`) because
  `Base.metadata` is shared with SaaS-OS's own tables.
- Gap answers: `JSON` by default (JSONB only where a query needs it); child
  tables instead of `ARRAY`; `String` + `CheckConstraint` instead of `Enum`;
  minutes-from-midnight `Integer` instead of `Time`; `Numeric` instead of
  `Float`; no `LargeBinary` need (recordings live in object storage). `UUID`
  columns need no type import at all.
- **The compatibility layer is one module**, not a second ORM: re-exports plus a
  small `tenant_table_args(...)` helper. Its value is a single seam if the
  upstream surface changes on a future re-pin.
- **No upstream change is requested**, and SaaS-OS is not modified.

### FreeSWITCH status: **unchanged, boundary made explicit** (ADR-0002 amendment)

The blocker analysis surfaced **no technical contradiction** with ADR-0002.
FreeSWITCH remains the initial telephony/media core. The amendment adds two
narrow, product-shaped interfaces — **`TelephonyProvider`** (call control) and
**`MediaProvider`** (media transport) — with `FreeSwitchTelephonyProvider` (ESL)
and `FreeSwitchMediaProvider` (`mod_audio_stream`) as their only
implementations. Domain code never sees an ESL command, a channel-UUID
semantic, or a FreeSWITCH hangup cause; an import fence confines them to
`voiceagent.telephony.freeswitch`. The interfaces are deliberately *not* a
vendor union (the §3.4 failure mode) and carry no portability promise.

### Blocker status

**All three Phase 0 blockers are resolved.** OD-1, OD-2 and OD-4 are closed by
ADR-0005, ADR-0006 and ADR-0007 respectively; the FreeSWITCH boundary is
explicit and unchanged; SaaS-OS is untouched, unforked and uncopied.

The remaining §19 entries (OD-3, OD-5 through OD-12) are **not** Phase 1
blockers: none of P1.1–P1.8 depends on an AI provider choice, FreeSWITCH
topology, object-storage backend, audit retention policy, public-API decision,
SMS, knowledge implementation, data residency, or the re-pin cadence. Each must
be answered before the phase that needs it — OD-3 and OD-5 before Phase 2,
OD-6 before recordings ship, OD-11 before any provider contract is signed.

Two verification tasks are tracked and non-blocking: the Phase 2 spike must
confirm that Pipecat's transport model does not demand ownership of the media
socket (ADR-0006 point 9), and P1.3 must confirm 32-bit integer sufficiency
before any column would need BIGINT (ADR-0007).
