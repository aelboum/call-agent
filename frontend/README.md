# Frontend

The first coherent product frontend for the AI Call Platform (Phase 2.15).
React + TypeScript + Vite, independent of the Python package: it shares no
code with it and talks to it only over HTTP (`/v1/*`, plus the platform's
own `/auth/*` and `/healthz`/`/readyz`).

## Running locally

```sh
npm install
npm run dev      # vite dev server, proxies /v1, /auth, /healthz, /readyz to :8000
npm run typecheck
npm run test      # vitest
npm run build      # tsc --noEmit && vite build
```

There is no `lint` script: this repository has no ESLint configuration for
the frontend (checked before adding one), and Phase 2.15 did not introduce
one -- `tsc --strict` plus the test suite are the real gates. `npm run
build` is what CI runs (`scripts/check-frontend.sh`).

## Architecture

- **Routing**: `react-router-dom` (`App.tsx`). One route tree; no
  per-role/per-tenant route sets.
- **Server state**: `@tanstack/react-query` (`lib/queryClient.ts`,
  `lib/hooks.ts`). Every query key is namespaced by the active tenant
  (`contextKey()`) -- see "Context isolation" below.
- **API client**: `lib/apiClient.ts` (the only place `fetch()` is called
  directly), `lib/authApi.ts` (the platform's own `/auth/*` session
  endpoints), `lib/hooks.ts` (typed, tenant-scoped resource hooks). No
  component calls `fetch()` on its own.
- **Session/context**: one provider, `context/SessionContext.tsx`
  (`useSession()`), holding identity (`auth`) and the active tenant context
  (`activeTenantId`) -- see its own module docstring for why "capabilities"
  is deliberately *not* a field there.
- **Shell**: `components/shell/` (`AppShell`, `Nav`, `TopBar`,
  `ContextSelector`, `Guards`).
- **State primitives**: `components/states/States.tsx` (`LoadingState`,
  `EmptyState`, `ErrorState`, `PermissionDeniedState`, `NotFoundState`,
  `QueryState`, `OptionalQueryState`).
- **Pages**: `pages/*.tsx`, one file per product surface (see below).
- **Types**: `lib/types.ts` -- hand-written mirrors of the backend's `*Out`
  Pydantic schemas. No OpenAPI/codegen mechanism exists in this repository
  (checked before writing these by hand); keep them in sync manually when a
  backend response shape changes.
- **Tests**: `vitest` + `@testing-library/react` (`src/test/setup.ts`,
  `src/test/testUtils.tsx`). No test framework existed before Phase 2.15;
  this is the first one, chosen because it is Vite's own native test
  runner (`vite.config.ts`'s `test` block) and needed no other build
  tooling.

## One frontend, multiple user contexts

Implements the architectural foundation ADR-0010
(`../docs/ADR/0010-one-frontend-multiple-user-contexts.md`) and Phase 0
report §23 describe -- **not** the full Phase 4.4 vision those documents
themselves say is not yet designed. Concretely:

- **Identity**: `GET /auth/me` (the platform's own OIDC session, an
  `HttpOnly`/`Secure` cookie this app never reads or holds).
- **Active context**: one tenant id, held by `SessionContext`, sent as
  `?tenant_id=...` on every `/v1/*` request (`apiClient.ts`'s own docstring
  explains why: no `/v1` route declares a `{tenant_id}` path segment, so
  the platform's tenant-resolution dependency binds it as a required query
  parameter). Selected manually (`ContextSelector.tsx`) because there is no
  enumeration endpoint (see "Known backend gaps" below) -- never a
  fabricated authorization decision: every request is still independently
  re-verified server-side (`voiceagent.tenancy.require_tenant()`), and an
  unauthorized/nonexistent id simply 404s.
- **Context switching + cache isolation**: `SessionContext
  .setActiveTenantId()` clears the entire React Query cache on every
  switch, and every query key is `[resource, tenantId, ...params]`
  (`queryClient.contextKey()`) -- ADR-0010 §11's own required strategy,
  literally. Tested in `context/SessionContext.test.tsx` (context A ->
  context B never leaks cached data; a switch is visible before the new
  context's own fetch resolves).
- **Capabilities**: not modeled on the frontend at all. See
  `SessionContext.tsx`'s own docstring and "Known backend gaps" below.

## Known backend gaps

Documented here rather than worked around (Phase 2.15 brief §5/§16: "do not
invent a client-side workaround... document the backend dependency/gap
instead"). None of these were added in this phase; each would need its own
reviewed, narrowly-scoped backend change.

1. **No "list my tenant memberships" endpoint.** Neither `core.identity`,
   `core.tenancy`, nor `core.rbac` exposes a reverse "given this
   authenticated user, which tenants may they act in" lookup. Consequence:
   `ContextSelector` is manual-entry, not a dropdown of authorized tenants.
2. **No capabilities/"what can I do" endpoint.** Nothing reports which
   actions the current principal is authorized for in the active context.
   Consequence: navigation is not capability-filtered (every implemented
   area is always listed, per ADR-0010 §9 -- visibility is not a security
   boundary); the UI instead reacts to a real `403` from the API
   (`PermissionDeniedState`).
3. **No cross-resource relationship queries.** `list_call_sessions()` and
   `list_follow_ups()`-by-status filter by `status` only, never by
   `contact_id`; there is no "calls for this contact" or "follow-ups for
   this contact" endpoint. Consequence: `ContactDetailPage` shows only the
   contact's own fields, not related calls/follow-ups/calendar events --
   building that client-side would mean downloading and filtering an
   unbounded list, which brief §10 explicitly forbids.
4. **No agent version history/list endpoint.** `Agent` exposes
   `draft_version_id`/`published_version_id` pointers only; there is no
   "list this agent's versions" route. Consequence: `AgentDetailPage` shows
   the current draft/published pointers, not a full version history.
5. **No knowledge item/source detail-by-id route** (only list + the
   mutating routes' own response). Not a blocker today -- list responses
   already carry full item/source objects -- but a future per-id deep link
   would need one.

## Product surfaces implemented

Dashboard, Calls (list + detail: outcome, deterministic analysis, AI
analysis with rebuild, workflow execution, conversation transcript), Agents
(list + detail with publish), Contacts (list + detail), Calendar
(agenda view + cancel, built on a Phase 2.15 backend addition -- see
`../docs/PHASE-2.15-STATUS.md`), Follow-ups (list + complete/cancel),
Knowledge (sources + items), Settings (session/context + phone numbers).

Explicitly not built this phase (see `../docs/PHASE-2.15-STATUS.md` for the
full list): a visual agent builder, a drag-and-drop workflow builder, a
full white-label editor, billing UI, and everything else Phase 2.15's own
brief §25 rules out.
