# Phase 2.15 Status: Frontend / Unified UX

Checkpoint: Phase 2.14 ("feat: add observability and operational
reliability") is the branch's tip commit at the start of this phase.
SaaS-OS remains pinned and unmodified. No commit exists yet for this
phase's own work.

## 1. Objective

Build the first coherent product frontend, establishing the architectural
foundation for "One Frontend, Multiple User Contexts" (ADR-0010) -- not the
full ADR-0010/Phase 4.4 vision, which that ADR and `docs
/PHASE-0-ARCHITECTURE.md` §23 both explicitly scope as a *later*,
not-yet-designed phase (the product API's own authentication/session model
is recorded there as "not yet designed" as of this ADR's writing; Phase
2.15's own inspection found it now exists at the platform layer -- see §4
below -- which is what makes a real, working (if partial) implementation
possible this phase, without overstepping ADR-0010's own non-goals in §15).

## 2. Files added

**Frontend** (`frontend/src/`):

- `lib/types.ts`, `lib/apiClient.ts` (+ `.test.ts`), `lib/authApi.ts`,
  `lib/queryClient.ts`, `lib/hooks.ts`
- `context/SessionContext.tsx` (+ `.test.tsx`)
- `components/shell/{AppShell,Nav,TopBar,ContextSelector,Guards}.tsx` (+
  `Guards.test.tsx`)
- `components/states/States.tsx` (+ `.test.tsx`)
- `pages/{Dashboard,Calls,CallDetail,Agents,AgentDetail,Contacts,
  ContactDetail,Calendar,FollowUps,Knowledge,Settings}Page.tsx` (+
  `pages.test.tsx`, `CallDetailPage.test.tsx`)
- `test/setup.ts`, `test/testUtils.tsx`
- `styles.css`
- `README.md`

**Backend** (narrow additions, see §11):

- None as new files -- both additions extend existing modules.

**Tests**: `tests/integration/test_contacts_calendar_integration.py`
extended (5 new tests: `list_contacts`/`list_events`, real-PostgreSQL only,
not executable in this environment -- see §22).

**Docs**: this file.

## 3. Files modified

- `frontend/package.json`/`package-lock.json` -- new dependencies (§21).
- `frontend/vite.config.ts` -- vitest `test` config block, `/auth` added to
  the dev proxy.
- `frontend/src/App.tsx`, `main.tsx` -- rewritten (router + provider tree).
- `frontend/src/api.ts` -- **deleted**, superseded by `lib/apiClient.ts` +
  `lib/authApi.ts` + `lib/hooks.ts`.
- `voiceagent/contacts/service.py` -- `list_contacts()` gains optional
  `limit`/`offset` (mirrors `list_call_sessions()`'s own shape).
- `voiceagent/api/v1/contacts.py` -- new `GET /v1/contacts` route.
- `voiceagent/calendars/service.py` -- new `list_events()` function.
- `voiceagent/api/v1/calendar_events.py` -- new `GET /v1/calendar-events`
  route (required, bounded date range).

## 4. Architecture

**Framework/tooling**: React 19 + TypeScript (`strict`) + Vite 7,
unchanged from the Phase 1 scaffold. `react-router-dom` (routing) and
`@tanstack/react-query` (server-state cache) are the two new runtime
dependencies (§21). No UI component library, no CSS framework -- one
hand-written stylesheet (`styles.css`).

**Routing**: `App.tsx`, a single `<Routes>` tree. Every authenticated route
is nested under one `<Route element={<AppShell />}>` so navigation, the
active-context gate, and the content outlet are structural, not repeated
per page.

**Application shell**: `components/shell/AppShell.tsx` composes `TopBar`
(display name, `ContextSelector`, sign-out), `Nav` (the eight product
surfaces, unfiltered -- see §7), and a content area gated by
`RequireActiveContext`. `RequireAuth` (in `App.tsx`, above the shell) gates
the whole tree on identity first.

**API client**: `lib/apiClient.ts` is the only module that calls `fetch()`.
Every request sends `credentials: "include"`; errors are normalized into
one `ApiError` class with a bounded `kind` (`unauthorized`/`forbidden`/
`not_found`/`conflict`/`validation`/`server_error`/`network_error`/
`cancelled`), `detail` taken only from the backend's own safe `{detail}`
string or a generic fallback -- never a raw exception. `lib/hooks.ts` wraps
every resource in one `useTenantQuery()`/`useTenantMutation()` pair, so
tenant propagation and cache invalidation are uniform across all ~15
resource hooks.

**Authentication/session handling**: entirely delegated to the platform's
real OIDC flow (`api.auth`, mounted by SaaS-OS's `build_platform_app()`,
discovered during this phase's own backend investigation -- see §4's own
opening note). `GET /auth/login` redirects to the IdP; `GET /auth/callback`
completes the exchange server-side and sets an `HttpOnly`/`Secure`/
`SameSite=Lax` session cookie; `GET /auth/me` returns `{user_id}`;
`POST /auth/logout` revokes it. The frontend never sees, stores, or
forwards a token -- `lib/authApi.ts` is a thin wrapper, not a second
mechanism.

**Active tenant context**: see §5.

**Capability/authorization UX**: see §7.

**State management**: React Query for all server state (with the explicit,
tested cache-isolation strategy ADR-0010 §11 requires -- §5); one React
Context (`SessionContext`) for identity + active context: no Redux, no
second state library. Page-local UI state (a text filter, a selected
source id) is plain `useState`, matching "follow the existing frontend
technology rather than introducing another state-management framework
unnecessarily" (brief §4) -- there was none to follow, so the smallest
correct addition was made, not the largest available one.

**Component architecture**: `components/shell/*` (layout), `components
/states/*` (loading/empty/error/denied/not-found primitives, reused by
every page), `pages/*` (one file per product surface, each a thin
composition of hooks + state primitives + plain markup -- no page
reimplements its own loading/error handling).

## 5. One Frontend / Multiple User Contexts

**Identity, active context, capabilities, and application data are kept
distinct** (brief §4), in `context/SessionContext.tsx`:

- *Identity*: `auth` (`loading`/`unauthenticated`/`authenticated{user}`),
  from `GET /auth/me`.
- *Active context*: `activeTenantId`, one tenant id, held in the provider
  and mirrored to `sessionStorage` (a locator, not a secret -- ADR-0010
  §12 -- so this is safe, unlike anything in brief §19's list).
- *Capabilities*: deliberately **not** a field here at all -- see §7.
- *Application data*: not this provider's concern; every resource hook in
  `lib/hooks.ts` fetches it, scoped by `activeTenantId`.

**How a context change affects API/data state** (this is the one place
this phase's own test suite proves ADR-0010 §11's required property):
`setActiveTenantId()` calls `queryClient.clear()` before updating the
stored/held tenant id. Every query key is built by `queryClient
.contextKey(tenantId, resource, params)` -- `[resource, tenantId, ...]` --
so even without the explicit clear, no query issued under the old tenant
id could ever satisfy a lookup under the new one; the clear additionally
guarantees an in-flight request for the old context cannot write a result a
still-mounted component reads after the switch. `context/SessionContext
.test.tsx::"a query issued under context A is never returned for a query
under context B"` exercises exactly this against a real `QueryClient`
instance.

Every subsequent request still carries `activeTenantId` only as the
`tenant_id` query parameter on the wire -- never trusted as authorization:
`voiceagent.tenancy.require_tenant()` independently re-verifies real
tenant membership on every single request, server-side, failing closed
(404) for a wrong or unauthorized id. Setting the active context on the
frontend is therefore never itself an authorization decision (ADR-0010
§12: "a locator, not a grant").

**What is not implemented, and why (a documented, not silent, gap)**: a
dropdown of the user's own authorized tenants. No endpoint anywhere in the
platform (`core.identity`, `core.tenancy`, `core.rbac`) or in `voiceagent`
enumerates which tenants an authenticated user may act in. Per brief §5's
own explicit instruction ("If the backend currently lacks a safe endpoint
needed to enumerate/select contexts, do not invent a client-side
workaround. Document the backend dependency/gap instead"), this phase does
not add that endpoint. `ContextSelector.tsx` is manual-entry instead --
still fully safe, because the backend's own per-request verification is
what actually decides whether a given id is honored, not this selector.

## 6. Product surfaces implemented

| Surface | State |
|---|---|
| Dashboard | Real data: recent calls, pending follow-ups, upcoming calendar events (7-day window). No fabricated aggregate metric -- none of these has a backend aggregate endpoint, so each section is a bounded slice of an existing list endpoint (brief §7's own prescribed fallback). |
| Calls | List (status filter, pagination as the API provides) + detail (metadata, outcome, deterministic analysis, AI analysis with rebuild, workflow execution, conversation transcript). |
| Agents | List + detail (lifecycle pointers, publish action). No version history (backend gap, §11) or visual builder (out of scope, brief §9/§25). |
| Contacts | List (Phase 2.15 backend addition, §11) + detail (own fields only -- no call/follow-up/calendar relationship; backend gap, §11). |
| Calendar | Agenda view over a bounded date range (Phase 2.15 backend addition, §11) + cancel action. |
| Follow-ups | List (status filter) + complete/cancel actions. |
| Knowledge | Sources + items (read-only; activate/deactivate omitted from the UI this phase -- see §23 deviations). |
| Settings | Session/active-context summary + phone numbers. |

## 7. Capability/authorization UX -- reactive, not precomputed

No endpoint reports what the current principal may do (checked: no route
in `voiceagent/api/v1/` resembles "my capabilities", and adding one was
considered and deliberately not done -- see §23 deviations for the
reasoning). Consequence, applied consistently:

- **Navigation is never capability-filtered.** `Nav.tsx` always lists all
  eight implemented surfaces. Visibility here is navigation, not a
  security boundary (ADR-0010 §9) -- exactly the distinction brief §17
  itself draws ("frontend authorization is presentation; backend
  authorization is enforcement").
- **A denied action is a real `403` from the API**, rendered by the shared
  `PermissionDeniedState` (`components/states/States.tsx`), reused
  everywhere a query can fail that way -- never a frontend-computed guess.
- **The one "hidden action" case implemented** (brief §17's own example)
  is data-driven, not permission-driven: `AgentDetailPage` shows "Publish
  draft version" only when `draft_version_id` is non-null -- there is
  nothing to publish otherwise, independent of what the caller is
  authorized to do. Tested in `pages/pages.test.tsx`.

No part of `core.rbac`'s decision logic is reimplemented in TypeScript
anywhere in this codebase.

## 8. API integration

**Consumed** (every route listed already existed before this phase, except
the two marked *new*): `/auth/{login,callback,me,logout}`, `/v1/meta`,
`/v1/agents[/{id}[/versions/{id}/publish]]`, `/v1/contacts[/{id}]` (list
*new*, §11), `/v1/calendars[/{id}]`, `/v1/calendar-events[/{id}/cancel]`
(list *new*, §11), `/v1/call-sessions/{id}[/outcome|/analysis|
/workflow-execution|/conversation]`, `/v1/call-sessions/{id}/ai-analysis[
/rebuild]`, `/v1/follow-ups[/{id}/complete|/cancel]`, `/v1/knowledge/
sources[/{id}/items]`, `/v1/phone-numbers`.

**New endpoints added this phase**: `GET /v1/contacts` (list, tenant-scoped,
paginated), `GET /v1/calendar-events` (list, tenant-scoped, bounded date
range, optional `calendar_id` filter). See §11 for the justification and
scope of each.

## 9. Security/privacy audit

- **Tenant isolation**: every `/v1/*` request carries `tenant_id`; the
  backend independently re-verifies it on every request
  (`require_tenant()`). The frontend never computes or assumes
  authorization from tenant hierarchy.
- **Authorization**: reactive only (§7). No RBAC duplication in
  TypeScript.
- **Token/session handling**: no token is ever held, read, or forwarded by
  frontend code -- the session lives entirely in an `HttpOnly`/`Secure`
  cookie the browser manages. `credentials: "include"` on every request is
  the only thing this codebase does to participate in it.
- **Browser storage**: `sessionStorage` holds exactly one value, the
  active tenant id (a locator, not a secret). Nothing else is written to
  `sessionStorage` or `localStorage` anywhere in this codebase --
  `context/SessionContext.test.tsx` and `pages/CallDetailPage.test.tsx`
  assert this directly (the latter renders a conversation turn containing
  a sensitive-looking string and asserts neither storage nor
  `window.location` ever contains it).
- **URL exposure**: route params are opaque UUIDs only (`/calls/:callId`,
  `/agents/:agentId`, `/contacts/:contactId`); no transcript, prompt, or
  analysis content is ever placed in a URL, query string, or `<Link>`.
- **AI-generated content rendering**: every AI-derived field (analysis
  summary, intent, topics, action items, conversation turn content,
  knowledge item content) is rendered as a plain React text child, never
  `dangerouslySetInnerHTML`. `pages/CallDetailPage.test.tsx` proves this
  concretely: a summary containing an `<img onerror=...>` payload is
  asserted present as literal text and asserted to have created zero
  `<img>` DOM nodes.
- **Sensitive-data handling**: no transcript/recording/prompt/LLM-or-
  STT-or-TTS output/tool argument is cached to persistent storage, logged,
  or sent to any analytics/telemetry system -- none exists in this
  codebase (brief §20: no frontend observability platform was added).
- **Error handling**: normalized centrally (`ApiError`); no raw exception,
  stack trace, SQL error, or internal infrastructure detail is ever
  rendered -- the backend's own `voiceagent.api.errors` already guarantees
  this on its side (fixed `detail` strings only), and `apiClient.ts`'s
  `detailFromResponse()` falls back to a generic message for anything that
  is not exactly that shape.

## 10. Frontend observability

Not added. No existing frontend error-reporting/logging mechanism was
found, and brief §20 does not require inventing one ("do not create a
frontend analytics system unless already present and clearly required").
Errors surface in the UI (`ErrorState`) and, for genuine bugs, in the
browser's own console via unhandled promise rejections -- nothing beyond
that.

## 11. Backend changes

Both are narrow, reuse-only additions -- no new authorization concept, no
new table, no domain redesign (brief §24: "keep it narrowly scoped",
"do not redesign domain models merely for frontend convenience").

1. **`GET /v1/contacts`** (`voiceagent/api/v1/contacts.py`). Wires an
   *already-existing* service function, `voiceagent.contacts.service
   .list_contacts()`, to a route for the first time -- it existed but had
   no caller through the API. Extended with optional `limit`/`offset`
   (mirroring `list_call_sessions()`'s own established shape) to satisfy
   brief §10's "use pagination/bounded requests". Reuses the existing
   `voiceagent.contacts` `read` permission -- no new permission.
   Required because `ContactsPage` (brief §10: "list, search/filter") has
   no other way to enumerate a tenant's own contacts.
2. **`GET /v1/calendar-events`** (`voiceagent/api/v1/calendar_events.py`)
   + **`list_events()`** (`voiceagent/calendars/service.py`, new
   function). No prior service function did this at all (unlike
   contacts). Bounded by a required date range (mirrors
   `check_availability()`'s own `start_at`/`end_at` query-parameter
   shape) and an optional `calendar_id` filter -- never an unbounded
   full-calendar scan. Reuses the existing `voiceagent.calendar_events`
   `read` permission. Required because both `CalendarPage` (brief §11:
   "calendar/agenda view... upcoming events") and `DashboardPage` (brief
   §7: "upcoming calendar activity") have no other way to list events at
   all -- the backend previously exposed create/get/cancel for one event
   at a time only.

**Considered and explicitly not added**: a "list my tenant memberships"
endpoint and a "capabilities" endpoint (§5/§7); a "calls/follow-ups for
this contact" filter (§11 gap table); an "agent version history" endpoint.
Each is a documented gap (`frontend/README.md`'s "Known backend gaps"),
not a silent omission -- see §23 for why each was left for a future,
separately-reviewed phase rather than added here.

## 12. Testing

**Frontend** (`vitest`): 6 test files, 49 tests, all passing.

- `lib/apiClient.test.ts` (15): tenant-id propagation, additional query
  params, credentials, success parsing, all six `ApiError` kinds, generic
  fallback for a non-JSON/no-`detail` body, no raw exception ever
  surfaced, network-error and cancellation mapping, `postJson` body/header
  shape.
- `context/SessionContext.test.tsx` (8): identity loading/authenticated/
  unauthenticated (including on a network failure), active-context
  initialization (none stored, restored from `sessionStorage`), context
  switching (clears the query cache, updates storage), the ADR-0010 §11
  cache-isolation property (context A -> context B never leaks cached
  data), clearing the active context.
- `components/states/States.test.tsx` (9): `QueryState` loading/empty/
  data/403-as-permission-denied; `OptionalQueryState` 404-as-empty vs. a
  real error still surfacing; `ErrorState` never rendering a raw
  non-`ApiError` object, showing the normalized backend detail for a 409,
  and a distinct message for an expired (401) session.
- `components/shell/Guards.test.tsx` (5): `RequireAuth` loading/
  unauthenticated/authenticated; `RequireActiveContext` no-context/
  has-context.
- `pages/pages.test.tsx` (9): Calls (data + 403), Agents, AgentDetail
  (hidden vs. shown publish action -- the authorization-UX case), Contacts,
  Calendar, Follow-ups (action visibility tied to status), Knowledge
  (source selection loads items).
- `pages/CallDetailPage.test.tsx` (3): AI-analysis "not yet generated" +
  rebuild action, a completed result rendered safely (the XSS-shaped
  summary test, §9), and the storage/URL privacy assertions.

**Frontend typecheck**: clean (`tsc --noEmit`, `strict`).
**Frontend build**: clean (`vite build`, 109 modules, ~321 kB / ~99 kB
gzip).
**Frontend lint**: not applicable -- no ESLint configuration exists in
this repository (checked; none added).

**Backend** (`pytest`): 796 passed, 241 deselected (integration-marked),
0 failed -- identical pass count to the pre-Phase-2.15 baseline, confirming
no regression from the two narrow backend additions. 5 new integration
tests were added (`tests/integration/test_contacts_calendar_integration
.py`) for `list_contacts()`/`list_events()`; **not executable in this
environment** (no PostgreSQL available) -- verified instead by `ruff`,
`ruff format`, and `pyright` (all clean) plus the fact that both new
functions are structurally identical to already-integration-tested
siblings (`list_call_sessions()`, `check_availability()`). This is a
stated limitation, not a claimed pass.

**Backend gates**: `ruff check` clean (whole repo); `ruff format --check`
clean for every file this phase touched (one pre-existing, unrelated
non-conformance in `docs/PHASE-2.10-STATUS.md`, untouched by this phase);
`pyright` 0 errors/warnings; `import-linter` 8/8 contracts kept;
`detect-secrets-hook` exit 0 against every file this phase added or
modified. No migration: nothing in this phase adds or changes persistent
schema.

## 13. Dependencies

**Frontend runtime**: `@tanstack/react-query` (^5) -- the standard,
minimal server-state cache with exactly the cache-key/invalidation
primitives ADR-0010 §11 requires; hand-rolling this would mean
re-implementing request de-duplication, cancellation, and cache
invalidation ourselves, with real risk of getting the one
security-relevant part (context isolation) subtly wrong. `react-router-dom`
(^7) -- the standard router; brief §6 requires real navigation and
ADR-0010 §12 requires deep-link handling, both of which a hand-rolled
router would only reproduce with more code and more risk.

**Frontend dev-only**: `vitest` (^4.1.11, pinned above 4.1.10 specifically
to avoid a moderate `@vitest/mocker` path-traversal advisory --
`npm audit` clean after pinning), `@testing-library/react`,
`@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom` -- the
test foundation brief §23 requires and this repository had none of.

**Backend**: none. `pyproject.toml` is unchanged.

## 14. Deviations from the specification

- **No capabilities endpoint added**, despite brief §17 asking for
  capability-aware UI. Considered explicitly (§7); not added because (a)
  it would require a second new authorization-adjacent backend primitive
  in the same phase the context-enumeration gap is *also* being left
  undone per the brief's own explicit instruction, compounding scope in
  exactly the area brief §24 warns against expanding broadly, and (b) the
  reactive alternative (a real `403` -> `PermissionDeniedState`) already
  satisfies brief §17's actual constraint ("never rely on frontend checks
  to protect data") more directly than a precomputed flag would.
- **Context selector is manual-entry, not a dropdown** -- required by
  brief §5 itself, given the enumeration gap (§5 above).
- **Contact detail shows no call/follow-up/calendar relationship** (brief
  §10's own list) -- no backend query exists for any of the three, and
  building one from an unbounded client-side scan is explicitly forbidden
  by the same section.
- **No agent version history view** (brief §9) -- no backend list
  endpoint exists; only the two pointer fields `Agent` itself exposes.
- **No visual agent builder, no knowledge item editor UI** -- both
  explicitly out of scope (brief §9: "avoid creating a complex visual
  agent builder"; knowledge items are shown read-only past their
  immutability boundary, consistent with brief §14).
- **New integration tests unexecuted in this environment** (§12) -- no
  PostgreSQL available; documented, not silently skipped.
- **No frontend lint gate** -- none existed before this phase; adding
  ESLint was judged unnecessary tooling growth for what `tsc --strict` and
  the test suite already catch (brief §27: "do not introduce unnecessary
  dependencies").

## 15. Pre-existing issues (not this phase's)

- `docs/PHASE-2.10-STATUS.md` fails the current `ruff format` check
  (a single comment-spacing difference) -- pre-existing, untouched by this
  phase.
- No PostgreSQL instance is available in this environment, so no
  integration test (old or new) can be executed here -- a pre-existing
  environment limitation, not introduced by this phase.

## 16. Protected files

- `docs/PHASE-0-ARCHITECTURE.md` -- read for architectural guidance only;
  not edited, staged, or committed.
- `docs/ADR/0010-one-frontend-multiple-user-contexts.md` -- read for
  architectural guidance only; not edited, staged, or committed.

## 17. Confirmations

- SaaS-OS is unchanged.
- `pyproject.toml` is unchanged.
- `docs/PHASE-0-ARCHITECTURE.md` is untouched by this phase.
- `docs/ADR/0010-one-frontend-multiple-user-contexts.md` is untouched by
  this phase.
- No commit was created.
- Nothing was pushed.
