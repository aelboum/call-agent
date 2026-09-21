# Phase 1 — Foundation: STATUS

**Status**: IN PROGRESS (foundation complete, awaiting review; Phase 2 not started)
**Updated**: 2026-09-21
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (verified resolved)

Phase 0 and Phase 0.1 are complete and are not revised by this document. See
`PHASE-0-ARCHITECTURE.md` (sections 1–21 for the architecture, section 22 for
the blocker resolutions) and `ADR/`.

---

## 1. What Phase 1 built

| Area | Delivered |
|---|---|
| Package structure | `voiceagent` with `api/`, `config/`, `db/`, `tenancy/`, `telephony/` (+ empty `freeswitch/`), `providers/engines/`, `runtime/` (empty) |
| Dependency | `saas-os` pinned to an exact commit SHA; the product's only runtime dependency |
| Configuration | `voiceagent.config.Settings` — composes `core.config.Settings`, adds product settings, fails closed in production |
| Persistence | `voiceagent.db` — the seam over `infra.db`; no `text`, no `func`, no second ORM |
| Migrations | Top-level `migrations/` with its own Alembic history; `0001` creates the `app` schema and its grants; autogenerate disabled |
| Tenant context | `voiceagent.tenancy.TenantContext`, `tenant_scope()`, `require_tenant()` |
| Application shell | `voiceagent.api.build_app()` over `api.platform.build_platform_app()`; `/v1/meta`; ASGI entrypoint in `voiceagent.api.asgi` |
| Telephony boundary | `TelephonyProvider` + `MediaProvider` contracts and in-memory fakes |
| Engine boundary | `ConversationEngine`/`EngineSession` contract, event types, component protocols, and a deterministic `FakeConversationEngine` |
| Object storage | `ObjectStore` contract only — no implementation, no `boto3` anywhere |
| Observability | `voiceagent.observability` seam over `infra.observability` |
| Architecture fences | 3 import-linter contracts + 9 AST-based architecture tests, both verified to fail when violated |
| Tests | 78 passing, hermetic (no PostgreSQL, Redis, network, provider or FreeSWITCH) |
| CI | 4 jobs: backend, security, migrations, frontend — running the same scripts as local dev |
| Frontend | React + Vite scaffold; display name fetched from the API, never hardcoded |

## 2. What Phase 1 deliberately did NOT build

No `Agent`, `AgentVersion`, `PhoneNumber`, `CallSession`, `Conversation`,
`Contact`, `Calendar`, `Appointment`, `Tool`, `Workflow`, knowledge base, call
recording, STT, LLM, TTS, ElevenLabs, OpenAI, Pipecat runtime, FreeSWITCH, SIP,
RTP, ESL, `mod_audio_stream`, realtime audio, call routing, transfer, outbound
calling, CRM, marketing automation, accounting, billing, lead or campaign
management, or white-label functionality.

**No product tables exist.** `0001` creates a schema and grants, nothing more:
a table invented ahead of its domain is a migration that has to be rewritten.

`voiceagent/runtime/` and `voiceagent/telephony/freeswitch/` are intentionally
empty packages. They exist so the fences that protect them can exist *before*
there is anything to leak — which is the only time a fence is cheap to add.

## 3. Findings about the pinned platform

Discovered by building against it, recorded because each shapes Phase 2:

1. **Importing `api.dependencies` requires `REDIS_URL` to be set.**
   `core.identity.session_retention` registers an ARQ job at module scope, so
   the jobs configuration is read during import. Reading configuration is not
   connecting — the test suite sets both URLs to port 1 so that anything which
   actually connected would fail loudly.
2. **Migrations need `MIGRATIONS_DATABASE_URL`, separate from `DATABASE_URL`.**
   The platform deliberately separates the schema-owning role from the
   application role, because it refuses to become ready under a
   superuser/`BYPASSRLS` role. Both are documented in `.env.example`.
3. **`Base.metadata` is shared with every SaaS-OS table**, confirming ADR-0007:
   autogenerate must stay off, or Alembic would compare the platform's schema
   against this product's model set.
4. **SaaS-OS's `"Only infra/db may import SQLAlchemy"` contract lists
   `source_modules = ["core", "products", "control_plane"]`** — its own
   packages. It does not bind an external consumer, so the product imposes the
   equivalent rule on itself, for the security reason rather than out of
   deference to convention.
5. **`/healthz` does no I/O and `/readyz` aggregates dependency checks**, so
   liveness is assertable in a hermetic suite and readiness returns 503 (never
   500) with a body carrying only check names and statuses.
6. **This FastAPI version exposes an included router as an opaque object with
   no public `path`.** Route assertions read the generated OpenAPI schema
   instead of walking `app.routes`, so the tests are not coupled to framework
   internals.

None required a change to SaaS-OS, and none was made.

## 4. Verification performed

| Check | Result |
|---|---|
| `pip install -e ".[dev,security]"` in a clean venv | Succeeded |
| SaaS-OS commit actually resolved | `direct_url.json` → `ff550010e5eafecace7311038aadc99fcecfbe3d` |
| `pytest` | 78 passed |
| `ruff check` / `ruff format --check` | Clean |
| `pyright` | 0 errors |
| `lint-imports` | 3 contracts kept, 0 broken |
| Fences fail when violated | Verified: injecting `import sqlalchemy` + a FreeSWITCH import broke 2 contracts and 2 tests; reverted |
| Alembic offline SQL | Renders exactly the intended DDL; single head |
| `detect-secrets` | 0 findings |
| Frontend `tsc --noEmit` + `vite build` | Both succeeded |
| SaaS-OS working tree | Clean, unmodified, still at the pinned commit |

## 5. Deferred decisions (not blockers for Phase 2 start)

- **D-1 — Containerization.** No `Dockerfile` or compose file yet. Deployment
  shape is a Phase 2 concern and depends on OD-5 (FreeSWITCH topology).
- **D-2 — Pipecat version pin.** The `pipecat` extra is declared but empty; the
  exact version is chosen alongside the first real engine (ADR-0006).
- **D-3 — Frontend framework depth.** A React + Vite scaffold with no routing,
  state management, component library or design system — none of those should
  be chosen before there is a screen to build.
- **D-4 — Worker process.** `infra.jobs` is available but no product job
  exists, so no `voiceagent/worker.py` was created. It arrives with the first
  background job (post-call processing, Phase 2).
- **D-5 — Product RBAC permissions.** `require_tenant()` exists but registers
  no permission and authorizes no route: Phase 1 has no domain resource to
  authorize.
- **D-6 — Structured-logging conventions.** The seam exists; per-call
  correlation (`call_session_id`) starts in Phase 2 when calls do.

Phase 0's open decisions OD-3 and OD-5 (first AI providers, FreeSWITCH
topology) must be answered **before** Phase 2 work begins, not before Phase 1
is reviewed.

## 6. Phase 2 entry criteria

Phase 2 (FreeSWITCH boundary, media, first engine, call orchestration) may
begin when:

1. This foundation is reviewed and accepted.
2. OD-3 (first AI provider pair) is decided.
3. OD-5 (FreeSWITCH deployment topology, and how a media socket reaches the
   right runtime process) is decided.
4. The first domain migration's table set is agreed — because ADR-0004's
   immutability guarantees are enforced in DDL, not only in application code.
