# Phase 2.16: Production & Security Readiness

Checkpoint: Phase 2.15 (`aa1911b feat: add unified frontend and tenant
context`) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified. No commit exists yet for this phase's own
work.

This document is the production-security readiness record for the AI Call
Platform as it stands after Phase 2.15. It covers what was audited, what was
concretely hardened, and what remains a documented, intentional limitation.
It does not modify `docs/PHASE-0-ARCHITECTURE.md` or
`docs/ADR/0010-one-frontend-multiple-user-contexts.md`.

## 1. Architecture security boundaries (unchanged, reaffirmed)

`Frontend → Product API → SaaS Core → Infrastructure` and `Agent/LLM → Tool
Gateway → authorization/validation/idempotency/audit → application service`
remain exactly as established through Phase 2.15. Nothing in this phase
bypasses a SaaS-OS security primitive, introduces a second tenant-isolation
mechanism, moves authorization into the frontend, or gives an AI component
direct database access. Every fix below is additive validation, a narrow
route addition, or an index -- none redesigns a boundary.

## 2. Authentication / session

**Verified, not modified** (owned by SaaS-OS's `api.auth`, off-limits under
ADR-0001). The OIDC Authorization Code + PKCE flow (`GET /auth/login` →
`GET /auth/callback`) issues an `HttpOnly`, `Secure`, `SameSite=Lax` session
cookie the frontend never reads or holds; the login transaction itself is a
single-use, cookie-bound, state-checked token (replay- and CSRF-resistant by
construction); `/auth/login`/`/auth/callback` are rate-limited by IP
(`infra.ratelimit`, fail-closed 503 on backend failure); every failure path
returns one fixed, generic message (never which check failed, never a
provider body, token, code, or secret). `cookie_secure` defaults `True` and
cannot be disabled in production. Confirmed by reading `api/auth/routes.py`
and `api/auth/config.py` in full.

**Frontend**: no token is ever held, read, or forwarded by frontend code
(grepped `frontend/src/` for `localStorage`, `dangerouslySetInnerHTML`,
`eval(`; none found). `sessionStorage` holds exactly one non-secret value,
the active tenant id.

**Hardened this phase**: sign-out previously only re-fetched `/auth/me`
after calling the backend logout endpoint -- it never cleared the active
tenant context or the React Query cache. On a shared/kiosk browser, a second
user signing in after the first would have inherited the first user's
active tenant id and any still-cached query results for it (ADR-0010 §14
names this exact scenario -- "logout/login with a different context" -- as
required test coverage). Fixed: `SessionContext.signOut()` now clears the
active context (which also clears the whole query cache, reusing
`setActiveTenantId(null)`'s existing guarantee) and sets identity to
`unauthenticated` locally, unconditionally, even if the backend logout call
itself fails (network error). `TopBar.tsx` now calls this instead of a bare
`authApi.logout()`. Tested: `frontend/src/context/SessionContext.test.tsx`
(2 new tests -- clears context/cache/identity on success, and still clears
them if the backend call fails).

**CSRF**: analyzed, not re-solved. Every state-changing `voiceagent`
endpoint authenticates through the same `SameSite=Lax` session cookie the
platform's own reasoning already covers (`api/auth/routes.py`'s own
docstring: "no CSRF framework exists in this repository to reuse; none is
invented") -- a cross-site `POST`/`PATCH`/`PUT` does not carry a
`SameSite=Lax` cookie in any current browser, so a forged cross-origin
request cannot authenticate. CORS (§6 below) is the second layer: with no
origins configured (the default), a browser blocks a cross-origin `fetch`
outright regardless of cookies. No gap found; no framework added.

## 3. Tenant isolation

**Verified across the audited surface**: every `voiceagent/api/v1/*.py`
route is authorized through `voiceagent.tenancy.require_tenant()`, which
resolves tenant context from a verified session (never a client-supplied
value trusted without re-verification) and re-checks real, `ACTIVE`
membership on every single request -- confirmed for the two Phase 2.15
additions (`GET /v1/contacts`, `GET /v1/calendar-events`) and the two Phase
2.14 operator-diagnostics routes. Every service-layer query reviewed
(`calls`, `contacts`, `calendars`, `followups`, `knowledge`,
`call_intelligence`) filters by `context.tenant_id` inside
`tenant_scope()`, which sets `app.tenant_id` for Row-Level Security to
enforce independently -- confirmed application-level filtering is
belt-and-braces on top of RLS, never the only boundary.

**Database-level (migration audit, fork investigation)**: RLS `ENABLE` +
`FORCE` is applied to all ten `voiceagent`-owned tables, with no gap.
Composite tenant-aware foreign keys (`(child_id, tenant_id) → (parent.id,
parent.tenant_id)`) are used consistently everywhere one `voiceagent` table
references another; the supporting `UNIQUE(id, tenant_id)` constraint two
tables were initially missing (`call_sessions`, `calendar_events`) was
already retroactively fixed in earlier phases (`0003`, `0007`'s
predecessor) -- confirmed present, not a new finding.
`app.phone_numbers.e164`'s uniqueness is correctly **global**, not
tenant-scoped -- verified against `voiceagent/phone_numbers/errors.py`'s own
documented intent (a real phone number can only be claimed by one tenant
platform-wide); this is the intended cross-tenant conflict-detection
behavior, not a bug.

**Redis**: the only Redis usage anywhere in `voiceagent/` is the runtime
heartbeat mechanism (`voiceagent/runtime/heartbeat.py` +
`voiceagent/api/v1/ops.py`) -- confirmed by exhaustive grep. Its one key
shape, `voiceagent:runtime:{instance_id}`, is keyed by runtime instance (a
process serving many tenants), not by tenant, by design; its payload is
pure liveness/capacity metadata, never business data. No tenant-specific
business fact is ever stored in Redis; PostgreSQL remains the sole
authoritative store, exactly as `heartbeat.py`'s own module docstring
already states. No change needed.

**Frontend context isolation**: see §6 (frontend) and ADR-0010 §11's own
required test, exercised directly in `SessionContext.test.tsx` (context A →
context B never returns context A's cached data).

**Hardened this phase (connection hygiene, not isolation)**:
`voiceagent/api/v1/ops.py`'s two diagnostics routes built a fresh
`RedisHeartbeatStore`/`redis.asyncio.Redis` client per request and never
closed it -- relying entirely on the client library's own `__del__`-based
finalizer, a documented `redis-py` anti-pattern and a real
connection-exhaustion risk under concurrent load to those two routes
(fork-agent finding). Fixed: `HeartbeatStore` protocol gained a `close()`
method; `RedisHeartbeatStore.close()` releases and resets the lazily-built
client (idempotent, safe even if nothing was ever opened);
`FakeHeartbeatStore.close()` is a harmless no-op for protocol conformance;
both `ops.py` routes now close the store in a `finally` block. Tested: 4 new
cases in `tests/runtime/test_heartbeat.py`.

**Real indexing gap found and fixed**: `app.call_sessions` had separate
single-column indexes on `tenant_id` and `status` but no composite covering
both, despite being the table behind this product's highest-pressure
recurring query (`list_non_terminal_call_sessions()`, called once per
tenant per scan by *both* `voiceagent.runtime.reconciliation` and
`voiceagent.runtime.stuck_calls`) -- in contrast to `follow_up_actions` and
`call_ai_analyses`, which both already received purpose-built composite
indexes for their own equivalent claim-lookup queries. Fixed: migration
`0011_call_sessions_tenant_status_index` adds
`ix_call_sessions_tenant_status (tenant_id, status)`; purely additive (no
column, constraint, or data change), the `voiceagent.calls.models.CallSession`
model updated to match. See §11 for rollout considerations. Tested: 3 new
hermetic offline-SQL tests in `tests/test_migrations.py`. `follow_up_actions
.status`/`list_follow_ups_by_status`'s equivalent composite index and
`call_sessions`'s own API-facing `list_call_sessions(status=...)` path are
lower-priority instances of the same pattern, deferred (§14).

## 4. Authorization / RBAC

**Verified**: no route grants broad operator permissions merely to make
itself usable. `voiceagent.ops.permissions.RESOURCE` (Phase 2.14) remains
deliberately never auto-granted to the runtime's own service account --
confirmed still true, unchanged this phase. No frontend-only authorization
exists anywhere (confirmed in the Phase 2.15 frontend re-audit, §6): every
UI-level "hidden action" is data-driven (e.g. `AgentDetailPage` hides
"Publish" only when there is genuinely no draft to publish), never a
capability the frontend computed and trusted. A denied action surfaces
through a real `403` from the API (`PermissionDeniedState`), never a
frontend guess.

**Rate limiting -- discovered already fully wired, no framework added**:
`api.dependencies.require_permission()` composes authentication → tenant
resolution → **rate limiting** (`infra.ratelimit.enforce_rate_limit()`,
keyed `f"{tenant_id}:{path}"`, fail-closed 503 on backend failure) → the
RBAC check, ahead of every single handler. Since every `voiceagent` route
uses `require_tenant()` → `require_permission()`, **every tenant-scoped
route in this product is already rate-limited by tenant and path** --
confirmed by reading `api/dependencies.py` in full. This satisfies brief
§22 directly ("do not create a generalized rate-limiting framework unless
the existing architecture already has one") -- it does, and nothing further
was added.

## 5. API security

**Verified**: every mutating route's request model uses
`ConfigDict(extra="forbid")`; every list route accepts `limit`/`offset`
with `Query(ge=..., le=...)` bounds (the two Phase 2.15 additions included);
error responses use `voiceagent.api.errors`' fixed `detail` strings, never
an exception message; `debug=False` unconditionally (`api.platform`, no
flag to enable it).

**Hardened this phase (unbounded tool/request fields)**: four fields had no
application-level size bound at all -- `LookupContactByPhoneInput.phone_e164`
(no validation whatsoever), `CreateAppointmentInput.title` (DB column is
`String(200)`, but nothing enforced that pre-write, so an oversized value
would have surfaced as a raw, uncaught database error instead of a clean
`ToolExecutionError`), `SetOutcomeInput.notes` and `CreateFollowUpInput
.description` (both backed by unbounded `Text` columns). Fixed:
`phone_e164` now uses the same E.164 pattern `TransferInput.destination_e164`
already enforces; `title` is bounded to 200 chars (matching its column);
`notes`/`description` are bounded to 2000 chars (an application ceiling,
since their columns have none). The two human-facing API request models for
the same underlying columns (`CallOutcomeSetRequest.notes`,
`FollowUpCreateRequest.description`) were given the identical bound, so
both entry points to each column agree. Tested: 4 new cases in
`tests/tools/test_handlers.py`.

**CORS**: audited and corrected. `allow_methods` was `["GET", "POST",
"PATCH", "DELETE"]` -- derived by hand, not from the actual route table --
and had drifted: no `/v1` route uses `DELETE` (0 routes), and one uses
`PUT` (`PUT /v1/call-sessions/{id}/outcome`) that was missing from the
list entirely. A cross-origin browser request to that one route would have
failed CORS preflight even though the route itself works correctly for
same-origin/server-to-server calls. Fixed: `allow_methods` corrected to
`["GET", "POST", "PATCH", "PUT"]`. CORS itself is off unless origins are
explicitly configured, credentials are only sent with an explicit
allowlist, and `"*"` is rejected outright in production -- confirmed
unchanged and correct. Tested: 1 new test deriving the expected method set
directly from the generated OpenAPI schema (so it cannot silently drift
again if a future route adds a new verb) plus the existing wildcard/absent
-by-default tests, unchanged and still passing.

**Security headers -- added**: neither `api.platform` nor any
`voiceagent` module set any response security header before this phase.
Added, at the product layer (`voiceagent/api/app.py`, since `api.platform`
is off-limits): `X-Content-Type-Options: nosniff` unconditionally (always
safe for a pure JSON API -- this backend serves no HTML/static assets at
all, confirmed: no `StaticFiles` mount anywhere in this app or the
platform builder), and `Strict-Transport-Security` only when
`environment == "production"` (matching every other environment-conditional
security control this product already has -- asserting HSTS in development
would force HTTPS a local deployment may not have). `Content-Security-Policy`
and `X-Frame-Options` were deliberately **not** added -- see §12. Tested: 3
new tests (`x-content-type-options` present on every response; HSTS absent
outside production; HSTS present with the correct value in a
production-configured app).

## 6. Frontend / browser security

**Re-audited after Phase 2.15's own build.** Grepped all of `frontend/src/`
(excluding tests) for `dangerouslySetInnerHTML`, `innerHTML`,
`document.write`, `eval(`, `localStorage`: **zero matches for all five**.
`window.location` usage is limited to two safe call sites: `new
URL(path, window.location.origin)` (origin only, never attacker-influenced)
and `window.location.assign("/auth/login")` (a hardcoded literal, not a
redirect target read from any query parameter -- grepped for
`URLSearchParams`/`redirect`/`returnTo`/`next=` across the whole frontend:
no open-redirect vector exists anywhere). Every AI-derived/user-derived
field (analysis summary, transcript content, knowledge item content) is
rendered as a plain React text child -- proven concretely in
`pages/CallDetailPage.test.tsx`, which renders an `<img onerror=...>`
-shaped analysis summary and asserts it appears as literal text with zero
`<img>` DOM nodes created. Storage: `sessionStorage` holds only the active
tenant id; `pages/CallDetailPage.test.tsx` also asserts a rendered
transcript turn never appears in `localStorage`, `sessionStorage`, or
`window.location.href`. Browser refresh during a context switch is safe by
construction, not merely untested: React Query's cache is in-memory only
(no persistence plugin), so any reload starts with an empty cache
regardless of what was cached before, and `activeTenantId` is re-read from
`sessionStorage` on the next render -- no code path can show stale-context
data across a reload.

**Fixed this phase**: the logout/context-clearing gap, §2.

## 7. Call / media privacy

**Verified, unchanged**: `authorize_call_data_access()` executes inside
`voiceagent.runtime.call_task.run_call_task()` before media is attached or
`ConversationEngine.start()` is ever called (confirmed by re-reading the
function: the authorization call and its `DataAuthorizationDeniedError`
branch, which finalizes the call as `failed`/`authorization_denied` and
`return`s, both precede `deps.media.attach()`). No sensitive call content
(transcript text, prompts, model output) is logged, metriced, or placed in
a trace attribute anywhere in `voiceagent/metrics.py` or the Phase 2.14
structured-logging call sites -- re-confirmed unchanged this phase (no
`voiceagent` module touched by Phase 2.16 logs call content; the telephony
provider changes add only bounded operation-name/outcome metrics, never
phone numbers or DTMF content as metric labels). Cancellation/teardown
paths in `call_task.py`'s single `finally` block always run regardless of
where cancellation lands, so a call cannot skip finalization or leak a
non-terminal row.

## 8. AI / Tool Gateway security

**Verified via dedicated investigation** (fork agent, full read of
`voiceagent/tools/{gateway,definitions,handlers,registry}.py`): tool
dispatch is a plain dict lookup (`ToolRegistry.resolve()`), never
`eval`/`exec`/caller-string-driven dynamic dispatch. `ToolCallRequested
.arguments` (LLM-generated) is used exactly once, as data passed to
Pydantic `model_validate()` -- never reinterpreted as instructions or code.
No tool handler makes an outbound HTTP request or opens an arbitrary file
path (grepped `handlers.py` for `requests.`/`httpx`/`urllib`/`open(`/
`Path(`: zero matches) -- every handler is either a bounded
`TelephonyProvider` call against a server-resolved `call_ref`, or an
application-service call against validated, typed arguments. Tool output
is bounded: `knowledge.search` is hard-capped to 5 results / 500 chars per
snippet server-side (the tool input doesn't even expose a caller-controlled
limit); every other tool's output is a small fixed-shape dict validated
against its own `output_model`. Authorization/idempotency/audit ordering
(`resolve → allowlist → idempotency replay → validate → authorize → execute
→ audit`) is unchanged and was re-confirmed by re-reading `gateway.py` in
full. `call_ref` is always FreeSWITCH-issued (`Unique-ID` or a `bgapi` job
response), never model/caller-supplied.

**Hardened this phase**: see §5's unbounded-field fixes (`phone_e164`,
`title`, `notes`, `description`) -- these are the concrete "maximum
argument size" gap brief §11 asks about; everything else audited in this
section had no gap.

## 9. Telephony provider security

**Investigated and hardened**: `FreeSwitchTelephonyProvider.originate()`
and `.send_dtmf()` build ESL command strings by raw f-string interpolation
with **zero validation** of `OriginateRequest.to_number`/`from_number`
(`OriginateRequest` is a plain, unvalidated `dataclass`) or `send_dtmf()`'s
`digits` at the contract level. Concretely: **neither method is reachable
from anywhere in this codebase today** (confirmed by exhaustive grep --
`originate()`/`OriginateRequest(` and `send_dtmf(` have no call site
outside the protocol definition, the implementation, and test doubles; no
tool exposes DTMF sending to the model at all) -- so this is a latent
injection risk, not a currently-exploitable one. `transfer()`'s
`destination` parameter, the one phone-number-shaped value a model *can*
influence (via the `call.transfer` tool), is already correctly validated
pre-handler by `TransferInput.destination_e164`'s E.164 regex, confirmed
concretely (the regex admits only `+` and digits -- no space, quote, brace,
or command separator can pass it).

**Fixed defensively, not reactively**: rather than leave `originate()`/
`send_dtmf()` unvalidated until something eventually calls them,
`FreeSwitchTelephonyProvider` now validates both at the one place they
actually reach an ESL command string -- `_require_e164()` (the identical
E.164 pattern, enforced a second, independent time) for `to_number`/
`from_number`, and `_require_dtmf_digits()` (FreeSWITCH's own accepted DTMF
alphabet: digits, `*`, `#`, `w`/`W`, bounded to 32 chars) for `digits`.
Invalid input raises the existing `TransportError` before the ESL
connection is ever touched. Tested: 12 new cases in
`tests/telephony/freeswitch/test_provider_security.py`, including
command-separator-shaped and ESL-brace-shaped injection attempts, each
asserted to never reach the fake ESL connection at all.

`call_ref` never originates from model or caller input anywhere (§8).
Provider failures (`TransportError`, a timeout) are caught and normalized
by `_command()`'s own bounded-timeout wrapper (Phase 2.13/2.14, unchanged)
-- they cannot bypass `authorize_call_data_access()`, which runs earlier
and independently in `call_task.py` (§7).

## 10. Webhooks / idempotency

**Not applicable -- confirmed absent, not assumed.** Grepped the entire
`voiceagent/` tree and the vendored `api/` platform package
(`.venv/Lib/site-packages/api/`) for "webhook"/"callback": the only hits
are Python-callback-semantics comments (`asyncio.Task.add_done_callback`,
a synchronous-callback design-tradeoff comment in `tools/gateway.py`) and
`voiceagent.telephony.contracts`' own module docstring, which explicitly
records "webhook signature verification" and "status-callback parsing" as
deliberately excluded, out-of-scope concepts. No FastAPI route in this
codebase, or in the platform package, matches any webhook/status-callback
shape. Inbound call signaling is confirmed to be an ESL TCP socket event
stream (`voiceagent/telephony/freeswitch/esl.py`), never HTTP. Per brief
§18's own instruction ("do not manufacture tests for controls that do not
exist"), no fix and no test were added for this section.

## 11. Database / RLS

Covered in full under §3 (tenant isolation). Summary: RLS `ENABLE`+`FORCE`
complete on all ten tables; composite tenant-aware FKs consistent; one real
indexing gap found and fixed (`call_sessions`, migration `0011`).

**Rollout consideration for migration `0011`** (documented per brief §15,
not solved here): `CREATE INDEX` without `CONCURRENTLY` takes a brief
`SHARE`-level lock blocking concurrent writes to `call_sessions` for the
build's duration. Negligible on a small/moderate table; on a very large
production table, an operator should consider running the equivalent
`CREATE INDEX CONCURRENTLY` by hand outside this migration's transaction
(Alembic's default mode runs every migration inside one transaction, which
`CONCURRENTLY` cannot participate in). This migration uses plain
`CREATE INDEX`, matching every other index this product's migrations
already create the same way (`0005`, `0007`, `0009`, `0010`) -- introducing
`CONCURRENTLY` here alone would be an inconsistent one-off, not a real fix
to the underlying operational question (which belongs to a real deployment
runbook, not one migration file).

## 12. Deployment / container security

**No Docker or Compose configuration exists anywhere in this repository**
(confirmed: `find . -iname "*docker*"` returns nothing outside `.venv`/
`node_modules`). This product's CI (`.github/workflows/ci.yml`) runs
directly on GitHub-hosted runners via `scripts/check-*.sh`, with no
container image built or published. There is therefore nothing to harden
under this section -- no non-root-user directive, no exposed-port
configuration, no health-check stanza, no resource limit exists to audit,
because no container definition exists. This is stated as a genuine gap in
this product's deployment artifacts, not a false "N/A": whatever
eventually deploys this product (a container image, a PaaS buildpack, a
bare-metal process manager) will need its own security review at that
time, using this document's application-level findings as an input.

This also directly informs §5's security-headers scope: since this backend
is never served as, or alongside, static HTML (confirmed: no `StaticFiles`
mount anywhere), `Content-Security-Policy`/`X-Frame-Options`/
`frame-ancestors` are the responsibility of whatever serves the frontend's
built `dist/` output (a separate, currently undefined deployment artifact)
-- not something this FastAPI backend can meaningfully set on its own JSON
responses.

## 13. Secrets / configuration

**Verified, unchanged this phase**: no hardcoded secret, API key, token,
or credential was found in any file this phase added or modified
(`detect-secrets-hook` run against every changed file -- see §Q). Every
placeholder/fake credential in test fixtures (`postgresql+psycopg://
unused:unused@127.0.0.1:1/unused`, `owner:unused@...`) is a pre-existing,
deliberately-unreachable value, matching the established test convention
(`tests/conftest.py`'s own docstring: "Both URLs point at port 1
deliberately"). `voiceagent.config.settings` still carries no credential
field of any kind (confirmed unchanged); `ENVIRONMENT` is still fail-closed
at import time for any process that reaches `infra.secrets` without it set
(unchanged, platform-owned). `.env` is confirmed not tracked by git
(`scripts/check-security.sh`'s own check, re-run this phase -- see §Q).

## 14. Known limitations

**Environment-dependent** (this sandbox has no PostgreSQL, Redis, or
FreeSWITCH instance):
- The 5 integration tests added in Phase 2.15
  (`tests/integration/test_contacts_calendar_integration.py`) and every
  other `pytest.mark.integration` test remain unexecuted here -- verified
  by static analysis (ruff, pyright) and, for the new migration, by the
  hermetic offline-SQL rendering (§V), never claimed as "passed" beyond
  that.
- Migration `0011`'s actual lock behavior on a real, populated
  `call_sessions` table (its own rollout consideration, §11) cannot be
  observed in this environment.
- Whatever eventually deploys this product (container, PaaS, bare metal)
  has no security posture recorded here, because no such configuration
  exists in the repository yet (§12).

**Intentionally deferred** (real findings, explicitly out of the "genuinely
required" bar for this phase):
- `follow_up_actions.status`/`list_follow_ups_by_status`'s composite index
  and `call_sessions`'s own API-facing `list_call_sessions(status=...)`
  path are the same indexing pattern as the fixed `call_sessions`
  reconciliation-path gap, at lower query-frequency priority -- not fixed
  this phase to keep the one migration narrowly scoped to the
  highest-pressure, explicitly-flagged case.
- `Content-Security-Policy`/`X-Frame-Options`/`Referrer-Policy` are a
  frontend-hosting-layer concern (§12), not implementable meaningfully in
  this JSON-only backend.

**Pre-existing** (present before this phase, not introduced by it):
- `docs/PHASE-2.10-STATUS.md` fails the current `ruff format` check (one
  comment-spacing difference) -- untouched by this phase.
- `.secrets.baseline`'s `"results"` section was effectively never
  populated/audited for this repository's pre-existing files (a Phase
  2.14 finding, reconfirmed this phase -- see §Q): editing any
  long-unaudited file surfaces its own pre-existing false positives, as
  happened with `tests/test_migrations.py:25` (a placeholder database URL,
  present before this phase, flagged only because appending new tests to
  that file caused it to be rescanned in full).

**Newly discovered this phase** (all fixed, listed in full in §A):
tenant/index gap (`call_sessions`), Redis connection-hygiene gap (`ops.py`),
four unbounded tool/request-model fields, CORS `allow_methods` drift,
missing security headers, telephony command-construction validation gap
(latent, unreachable today), and the frontend logout/context-clearing gap.

## 15. Production checklist

| Item | Status |
|---|---|
| Tenant isolation enforced server-side on every route | Verified |
| RLS `ENABLE`+`FORCE` on every product table | Verified |
| Composite tenant-aware FKs | Verified |
| `call_sessions` tenant/status index | Hardened |
| OIDC session security (cookie flags, PKCE, rate limiting) | Verified (platform-owned) |
| Frontend never holds a token | Verified |
| Frontend clears context/cache on logout | Hardened |
| CSRF exposure | Verified (SameSite + CORS, no gap) |
| Per-tenant API rate limiting | Verified (already platform-wide) |
| Tool/request field size bounds | Hardened |
| CORS method list accuracy | Hardened |
| Security response headers | Hardened |
| Telephony command-construction validation | Hardened (defense in depth; currently unreachable) |
| Redis connection hygiene (`ops.py`) | Hardened |
| Webhooks | Not applicable (none exist) |
| Secrets in source/tests | Verified |
| Container/deployment hardening | Environment-dependent (no container config exists) |
| Integration-test execution | Environment-dependent (no PostgreSQL/Redis/FreeSWITCH here) |
