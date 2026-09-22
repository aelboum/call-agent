# Phase 2.6 Status: Contacts & Minimal Internal Calendar

## 1. Objective

Give a call agent the minimum it needs to look up who is calling and manage
appointments during a phone call: an optional `CallSession` ↔ `Contact`
association, a small internal calendar/appointment model, and four new Tool
Gateway tools so an AI agent can use both from inside a call. Not a CRM, not
a general calendar product (see §19 Non-Goals).

## 2. Implementation Summary

Two new domain packages (`voiceagent.contacts`, `voiceagent.calendars`),
each following the exact `models.py` / `errors.py` / `permissions.py` /
`service.py` shape every existing domain package
(`voiceagent.phone_numbers`, `voiceagent.calls`) already uses. One nullable
column (`call_sessions.contact_id`) plus one new application-service
function (`associate_call()`) added to the existing `voiceagent.calls`
package — the `CallSession` lifecycle itself is untouched. Four new
`ToolDefinition`s registered into the existing `TOOL_REGISTRY`, reusing the
existing `ToolGateway`/allowlist/RBAC/audit machinery unchanged except for
one additive seam: `ToolExecutionContext` gained two optional fields
(`tenant_context`, `db`) so a Contact/Calendar handler can call its
application service through the existing `DatabaseBoundary`. One migration
(`0004_contacts_calendar`). Six new REST endpoints plus one addition to the
existing `call-sessions` router.

## 3. Schema

```text
app.contacts
  id, tenant_id, name, phone_e164, email, created_at, updated_at
  UNIQUE(tenant_id, phone_e164)   UNIQUE(id, tenant_id)
  CHECK phone_e164 ~ '^\+[1-9][0-9]{1,14}$'

app.calendars
  id, tenant_id, name, timezone, is_active, created_at, updated_at
  UNIQUE(id, tenant_id)

app.calendar_events
  id, tenant_id, calendar_id, contact_id (nullable), title,
  start_at, end_at, status, created_at, updated_at
  FK (calendar_id, tenant_id) -> calendars(id, tenant_id)
  FK (contact_id, tenant_id)  -> contacts(id, tenant_id)
  CHECK start_at < end_at
  CHECK status IN ('scheduled', 'cancelled')

app.call_sessions
  + contact_id (nullable)
  FK (contact_id, tenant_id) -> contacts(id, tenant_id)
```

Migration: `migrations/versions/0004_create_contacts_calendar_tables.py`
(`0004_contacts_calendar`, `down_revision = 0003_conversation_turns`). Every
new table has `tenant_id`, RLS `ENABLE` + `FORCE`, the standard
`GRANT SELECT, INSERT, UPDATE, DELETE` to the application role, and every
FK into a Phase 2.6 table is the established composite tenant-aware pattern
(`(child_id, tenant_id) REFERENCES (parent.id, parent.tenant_id)`), never
application-side tenant filtering.

## 4. Contact Model

`voiceagent/contacts/models.py`. `phone_e164` is **tenant-locally** unique
(`UNIQUE(tenant_id, phone_e164)`) — deliberately the opposite of
`PhoneNumber.e164`'s global uniqueness (Phase 2.1): a DID is a platform
routing resource; a contact's phone number is not, so the same number
belonging to two different tenants' contacts is ordinary data, not a
tenant-isolation failure.

`voiceagent/contacts/service.py` provides `create_contact`, `get_contact`,
`list_contacts`, `lookup_contact_by_phone`, and the pure, DB-free
`normalize_phone_e164()` (strips spaces/hyphens/parentheses, then requires
already-valid E.164 — never guesses a missing country code).

## 5. Call ↔ Contact Relationship

`call_sessions.contact_id` (nullable, composite tenant-aware FK). Set only
through `voiceagent.calls.service.associate_call(context, call_session_id,
contact_id)`, which reads both rows inside one `tenant_scope()` session — a
`contact_id` belonging to a different tenant is invisible under RLS there,
so it raises the identical `ContactNotFoundError` a nonexistent id would
(no cross-tenant lookup mechanism exists). Never called from the audio hot
path; the existing `CallSession` lifecycle (`voiceagent.calls.lifecycle`) is
untouched — verified directly in
`tests/integration/test_contacts_calendar_integration.py
::test_call_lifecycle_is_unaffected_by_association`.

## 6. Calendar Model

`voiceagent/calendars/models.py`. A tenant may own multiple `Calendar` rows
(no "one calendar per tenant" assumption). `timezone` is a plain string
column; validity is an application-boundary concern (§7 below), not a
database constraint — an invalid string cannot be distinguished from a
valid one by a `CHECK` alone without a lookup table this phase does not
add.

## 7. Appointment Model

`CalendarEvent`. `contact_id` is optional. `status` is `scheduled` /
`cancelled` — cancellation is a state transition
(`voiceagent.calendars.service.cancel_event()`), idempotent, never a row
deletion; there is no delete API and no service function that removes a
row (`tests/calendars/test_calendar_models.py
::test_no_hard_delete_api_exists`).

## 8. Timezone Semantics

- `Calendar.timezone` must be a real IANA identifier (e.g.
  `Europe/Amsterdam`, `Africa/Casablanca`, `America/New_York`), validated by
  `voiceagent.calendars.service.validate_timezone()` using the standard
  library's `zoneinfo` (backed by the `tzdata` package) — never a
  hand-rolled allowlist.
- Every `start_at`/`end_at` this product accepts, at every boundary
  (application service, REST API, Tool Gateway tool input), must be
  timezone-aware. A naive datetime is **rejected**, never silently
  interpreted as UTC or as the calendar's own timezone.
  - The service layer raises `NaiveDatetimeError` (checked *before* any
    database session is opened, so this path is hermetically unit-tested
    with no real PostgreSQL — `tests/calendars/test_calendar_service.py`).
  - The REST API uses Pydantic's `AwareDatetime` type on every
    request/query field that carries a datetime, so a naive value is
    rejected as a `422` validation error before the route body ever runs.
  - The `calendar.check_availability` / `calendar.create_appointment` tool
    inputs use the same `AwareDatetime` type, so a naive value from the
    model is a `ToolResult(error_code="invalid_arguments")`, never silently
    accepted.
- Comparison is always by instant (`datetime` equality/ordering in Python
  is timezone-correct across differing offsets) — never by wall-clock
  string. `CalendarEvent.start_at`/`end_at` are stored as PostgreSQL
  `timestamptz`, which normalizes to a single instant regardless of the
  offset the value arrived with.
- Nothing in this phase relies on the server's local timezone.

## 9. Overlap / Availability Semantics

Two `scheduled` events on the **same calendar** conflict exactly when:

```text
existing.start_at < requested.end_at AND existing.end_at > requested.start_at
```

Consequences, all verified against real PostgreSQL
(`tests/integration/test_contacts_calendar_integration.py`):

- Adjacent appointments (`10:00–10:30`, `10:30–11:00`) do **not** conflict.
- A `cancelled` event never blocks availability.
- Only events belonging to the *requested* calendar are considered — a
  busy slot on one calendar never affects another.
- `check_availability()` returns a provider-neutral
  `AvailabilityResult(available: bool, conflict_start_at, conflict_end_at)`
  — on conflict, only the conflicting interval is disclosed, never the
  conflicting event's id, title, or contact.
- `create_event()` performs the identical check before insert and raises
  `CalendarEventConflictError` (mapped to HTTP `409` / tool error code
  `"conflict"`) rather than silently double-booking.

## 10. APIs

```text
POST   /v1/contacts
GET    /v1/contacts/{id}
GET    /v1/contacts/by-phone/{phone_e164}

POST   /v1/calendars
GET    /v1/calendars
GET    /v1/calendars/{id}
GET    /v1/calendars/{id}/availability?start_at=...&end_at=...

POST   /v1/calendar-events
GET    /v1/calendar-events/{id}
POST   /v1/calendar-events/{id}/cancel

POST   /v1/call-sessions/{id}/contact
```

Availability is `GET` with query parameters — the existing style for a
read-only, filterable query (`GET /v1/call-sessions?status=...`), not a
`POST` for what performs no write. No `DELETE` route exists for a calendar
event. Domain validation failures (invalid E.164, invalid timezone, naive
or inverted interval) map to `422`; a phone/appointment conflict maps to
`409` via the existing `api.errors.conflict()` helper; not-found maps to
`404` via the existing `api.errors.not_found()` helper — no new error
shape was invented.

## 11. RBAC

```text
voiceagent.contacts          read, create
voiceagent.calendars         read, create
voiceagent.calendar_events   read, create, cancel
voiceagent.call_sessions     + "associate" (new action on the existing
                                resource — see §17 deviation)
voiceagent.tools             + contact.lookup_by_phone,
                                calendar.check_availability,
                                calendar.create_appointment,
                                calendar.cancel_appointment
                                (one permission per tool, matching the
                                Phase 2.4 pattern exactly)
```

All registered via each package's own `permissions.py::register()` (never
called at import/build time, per the established pattern) and wired into
`voiceagent.rbac_bootstrap.PERMISSIONS`/`register_permissions()`.

## 12. Tool Gateway Integration

Four new `ToolDefinition`s registered into the existing, single
`TOOL_REGISTRY` from `voiceagent/tools/handlers.py` (no second tool
execution path):

```text
contact.lookup_by_phone      -> voiceagent.contacts.service.lookup_contact_by_phone
calendar.check_availability  -> voiceagent.calendars.service.check_availability
calendar.create_appointment  -> voiceagent.calendars.service.create_event
calendar.cancel_appointment  -> voiceagent.calendars.service.cancel_event
```

Every handler: resolves through the existing `ToolGateway.execute()`
pipeline unchanged (registry lookup → AgentVersion allowlist → idempotency
replay → input validation → RBAC → timed execution → output validation →
audit); calls its application service via `ctx.db.run(...)`
(`voiceagent.runtime.db.DatabaseBoundary` — never `voiceagent.db`/
`voiceagent.tenancy.tenant_scope` directly, enforced by
`tests/architecture/test_tool_gateway_isolation.py
::test_no_tool_module_imports_voiceagent_db_directly`); and normalizes
every domain error (`ContactNotFoundError`, `CalendarNotFoundError`,
`CalendarEventConflictError`, …) to a fixed `ToolExecutionError` code —
never a raw exception, never a message containing tenant-sensitive content.

**The one structural addition**: `ToolExecutionContext` (`voiceagent/tools/
definitions.py`) gained two new, optional (default `None`) fields —
`tenant_context: TenantContext | None` and `db: DatabaseBoundary | None` —
populated by `ToolGateway.execute()` from the `context`/`db` values it was
already handed by `voiceagent.runtime.call_task`. This is the minimum
extension needed for a DB-touching tool to exist at all; the four Phase 2.4
call-control handlers ignore both fields and are unaffected (verified by
`tests/tools/test_handlers.py`'s unmodified existing tests, still passing
unchanged).

## 13. AgentVersion Integration

No new mechanism: the four Phase 2.6 tools are gated by the exact same
`AgentVersion.config.tools` allowlist (`voiceagent.tools.allowlist
.is_tool_allowed()`) every Phase 2.4 tool already uses. A published
`AgentVersion` that does not list `calendar.create_appointment` cannot
execute it — verified against real RBAC in
`tests/integration/test_contacts_calendar_integration.py
::test_calendar_tool_not_in_allowlist_is_denied`.

## 14. Tenant/RLS Security

Every new table: RLS `ENABLE` + `FORCE`, verified against real PostgreSQL
(`tests/integration/test_domain_rls_integration.py
::test_row_level_security_is_enabled_and_forced_for_every_table`, extended
this phase). Verified, against real PostgreSQL, in
`tests/integration/test_contacts_calendar_integration.py`:

1. Tenant A cannot read Tenant B's contacts.
2. Tenant B cannot read Tenant A's calendars.
3. (Symmetric case) Tenant A cannot read Tenant B's appointments.
4. Cross-tenant call/contact association fails (`ContactNotFoundError`),
   both directions (wrong contact, wrong call).
5. Tenant-local duplicate phone is rejected; the identical phone number is
   allowed for a different tenant's contact.
6. `CalendarEvent` FK isolation: a cross-tenant `calendar_id` and a
   cross-tenant `contact_id` both fail at the database constraint layer
   (`IntegrityError`), not merely hidden by RLS.
7. RLS is enforced under the restricted `saas_os_app` role for every new
   table (the entire integration suite runs as that role, per
   `tests/integration/README.md`).
8. Overlapping appointments are rejected; adjacent ones are not.
9. A cancelled appointment does not block availability.
10. `ToolGateway` → service → DB executes correctly under real tenant
    authorization for both new read and new write tools.
11. A tool not present in the `AgentVersion` allowlist is denied even with
    full RBAC authorization.

## 15. Privacy / Logging

No contact name/phone/email, appointment title, or raw tool
argument/output is logged anywhere in the new code. `ToolGateway`'s
existing audit call is unchanged and untouched by this phase — its
`metadata` still carries only `tool_id`/`call_session_id`/`status`, never
tool arguments or results (`tests/tools/test_gateway.py
::test_audit_metadata_never_carries_tool_arguments`, unmodified and still
passing). Every new domain error class carries a fixed, static message —
none of them echoes a phone number, name, email, or title back into an
exception string.

## 16. Tests

**Hermetic** (`pytest`, no PostgreSQL): 430 passed (baseline 375 + 55 new:
`tests/contacts/`, `tests/calendars/`, `tests/tools/test_handlers.py`
additions, `tests/tools/test_gateway.py` addition, plus updates to three
pre-existing tests that needed to change for the new schema/tool count —
see §17).

**Integration** (`pytest -m integration`, real PostgreSQL 16): 92 passed
(baseline 68 + 24 new, `tests/integration
/test_contacts_calendar_integration.py`, covering exactly the §14 list
above), plus one pre-existing test updated for the three new tables (§17).

**Total: 522 passed, 0 failed.** No existing test was removed or weakened;
every changed pre-existing test is documented in §17 with why.

## 17. Quality Gates

All run and green in this environment:

- `ruff check .` — clean.
- `ruff format --check .` — clean.
- `pyright` — 0 errors, 0 warnings.
- `lint-imports` — 8 contracts kept, 0 broken.
- `detect-secrets scan` on every file this phase added or modified — 0
  findings. (A repo-wide `detect-secrets scan --baseline .secrets.baseline`
  surfaces pre-existing false positives in files this phase never touched —
  e.g. a dummy `postgresql+psycopg://owner:unused@...` URL in
  `tests/test_migrations.py`'s already-existing code, and `_SECRET_NAME`
  constants in the Phase 2.3 LLM/STT/TTS adapters — unrelated to Phase 2.6
  and out of this phase's scope to touch; `.secrets.baseline` was left
  exactly as it was at the start of this phase.)
- `pytest` (hermetic) and `pytest -m integration` (real PostgreSQL) — both
  green, see §16.
- No frontend files were touched; the frontend check was not run.

## 18. Deviations

- **`voiceagent.calls.permissions` gained a new `"associate"` action** on
  the existing `voiceagent.call_sessions` resource, rather than reusing
  `"read"` for the new association write (a role that can only read call
  sessions must not thereby be able to mutate one) — the brief's §12 lists
  recommended actions for the three *new* resources only and is silent on
  this one; extending the nearest existing resource, the same way Phase 2.4
  added a tool-scoped permission to the existing `voiceagent.tools`
  resource, was judged the minimal correct choice over inventing a fourth
  new resource.
- **No dedicated HTTP-level (`TestClient`) route tests** were added for the
  six new endpoints. This mirrors the codebase's own established
  convention exactly: `voiceagent/api/v1/phone_numbers.py` and
  `call_sessions.py` (Phase 2.1) have zero dedicated route tests today,
  either hermetic or integration — `tests/api/test_app.py` only asserts
  application-shell properties (route wiring, CORS, debug flag), and every
  domain module's actual behavior (including RBAC, tenant isolation,
  validation, not-found, cross-tenant rejection) is tested at the service
  layer and, for tools, at the `ToolGateway` layer — the same boundary the
  API route is a thin, untested-independently wrapper over (§12's own "API
  and Tool Gateway service operations must converge on the same
  authorization boundary" is exactly why this substitution is sound, not a
  gap). This phase followed that convention rather than introducing a new
  one unilaterally.
- **`ToolExecutionContext` gained two new optional fields** (§12) — the
  smallest structural change that let a Contact/Calendar tool exist at all
  without a second tool-execution path or a new database-access seam.

## 19. Limitations

- `GET /v1/contacts` has no list endpoint (only create/get/lookup-by-phone,
  exactly the brief's §15 list) and `GET /v1/calendars` returns every
  calendar for a tenant unpaginated — acceptable at the expected scale of
  "a tenant's own calendars" but would need pagination if that assumption
  changes.
- Rescheduling is not implemented (explicitly optional per brief §8).
- `normalize_phone_e164()` is intentionally minimal (punctuation-stripping
  plus E.164 validation) — it is not a full phone-parsing library and
  never infers a missing country code.

## 20. Explicit Non-Goals (confirmed absent)

CRM, lead management, opportunities, sales pipelines, campaigns, marketing
automation, recurring appointments, attendee management, invitations,
reminders, SMS/email notifications, Google Calendar / Microsoft Graph /
CalDAV, OAuth, external calendar synchronization or webhooks, any calendar
provider SDK, a `CalendarProvider` abstraction (deferred until a second
real implementation justifies it, per brief §18), and no change to
SaaS-OS, previous migrations, or the existing `CallSession` lifecycle.

## 21. Phase 2.7 Readiness

The Contact/Calendar foundation is fully reachable from an AI agent through
the existing Tool Gateway, gated by the existing AgentVersion allowlist and
RBAC, with real-PostgreSQL-verified tenant isolation. A future phase adding
an external calendar provider can introduce a `CalendarProvider`
abstraction at the service boundary without touching the Tool Gateway,
RBAC, or schema shape established here — no empty provider registry was
built ahead of that need.
