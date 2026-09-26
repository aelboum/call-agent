# Phase 2.22: Call Orchestration & Real Call Startup

Checkpoint: `68b4ee7 feat: add FreeSWITCH telephony transport integration`
(Phase 2.21) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
No commit exists yet for this phase's own work; per its own instructions,
this phase implements and reports only -- it does not commit or push.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.

This phase builds the "Call Orchestrator" `voiceagent/runtime/errors.py`
and `scripts/run_call_runtime.py` had both, since Phase 2.13, named as the
one missing piece: the application-level bridge that turns Phase 2.21's
real FreeSWITCH `OFFERED` event into an authoritatively-routed, authorized,
exactly-once-started `CallSession` running on the existing lifecycle state
machine, `CallRuntime`, and `ConversationEngine` abstractions -- without
redesigning any of them.

## 1. Repository baseline (inspected before any change)

- **`voiceagent/runtime/telephony_events.py`** (Phase 2.21): the one
  consumer of `TelephonyProvider.events()`, demultiplexing by `call_ref` to
  per-subscriber queues; a `call_ref` with no subscriber was silently
  dropped, by design, but that same design meant *no* code path existed for
  a brand-new inbound call's own `OFFERED` event to ever be seen at all --
  it can never be "subscribed" before something first decides to route it.
- **`voiceagent/runtime/assignment.py`**: `assign_call_to_runtime()` already
  existed, self-documented as "This is Call Orchestrator code" -- selects
  the least-loaded runtime from heartbeats and calls
  `claim_runtime_ownership()`. It does not create the `CallSession` itself
  and was never called from anywhere in the repository.
- **`voiceagent/calls/service.py`**: `create_call_session()` had no
  `fs_channel_uuid` field and no idempotency story -- every existing caller
  was test code building calls directly. `claim_runtime_ownership()`'s own
  docstring already documents its `SELECT ... FOR UPDATE` exclusivity and
  same-instance-reclaim idempotency (ADR-0008 point 10).
- **`voiceagent/agents/service.py`**: `select_agent_version_id()` already
  existed, explicitly documented "for the future Call Orchestrator" --
  unused until this phase.
- **`voiceagent/tenancy/context.py`**: `TenantContext`'s own module
  docstring already anticipated "the call-session context the Call
  Orchestrator builds server-side" as a legitimate, non-HTTP source.
  `voiceagent/followups/worker.py` and `voiceagent/call_intelligence
  /worker.py` both established the precedent for building one
  synthetically (`actor_id`/`membership_id` as fresh UUIDs, since neither
  carries a foreign key any orchestrator codepath reaches).
- **`voiceagent/telephony/freeswitch/provider.py`**: `start_media_stream()`
  and `stop_media_stream()` existed (Phase 2.21) but had never been called
  by anything -- Phase 2.21's own "Known limitations" named this
  explicitly. `start_media_stream()` took a caller-supplied `media_url`,
  which would have required this orchestrator to import
  `voiceagent.telephony.freeswitch.media_transport.mint_media_ticket()`
  directly, violating the telephony boundary (section 8 below).
- **Nothing in this repository resolved which tenant/agent an inbound
  `to_number` belongs to.** `voiceagent.phone_numbers.service` reads/writes
  `PhoneNumber` rows, but every read goes through `tenant_session_scope()`
  -- Row-Level Security requires `app.tenant_id` to already be set, which
  is exactly the value an inbound call has not yet resolved. This is the
  central problem this phase's own new primitive (section 3) solves.

## 2. Design: three convergence properties, not new coordination

`CallOrchestrator` runs inside every `call-runtime` process, one instance
per process, each reacting independently to the identical broadcast
FreeSWITCH event stream its own `ManagedEslConnection` receives. There is
no separate orchestrator process and no new cross-process message bus.
Correctness under a multi-process fleet, and under any duplicate/replayed
event, rests entirely on three existing-or-newly-added database guarantees,
never on an in-memory dict:

1. **`create_call_session()`'s partial unique index on `fs_channel_uuid`**
   (migrations/0012) -- every process's handler converges on the *same*
   `CallSession` row for the same external call, no matter how many raced
   to create it first. A second `create_call_session()` call for an
   already-seen `fs_channel_uuid` returns the existing row rather than
   raising.
2. **`claim_runtime_ownership()`'s pre-existing `SELECT ... FOR UPDATE`
   exclusivity** (ADR-0008 point 10) -- exactly one runtime instance ends
   up as `CallSession.runtime_instance_id`, no matter how many processes'
   `assign_call_to_runtime()` calls raced. A process that computes a
   winner other than itself does nothing further; the winning process's own
   orchestrator independently reaches the same conclusion about itself from
   the identical broadcast event -- `select_runtime_for_assignment()`'s own
   selection is a pure function of the heartbeats every process reads, so
   this convergence holds regardless of which process's `_handle_offer()`
   happens to execute first.
3. **`claim_call_for_activation()`'s `SELECT ... FOR UPDATE`-backed
   `initiated -> ringing` claim** (this phase's own new primitive,
   `voiceagent/calls/service.py`), taken *after* property 2 above confirms
   this process as the owner, never before it -- the row lock, not a
   snapshot read of `status`, is what lets exactly one of any number of
   concurrent handlers *this one process* runs for the same `CallSession`
   proceed to `answer()`. Two bugs, both caught by this phase's own
   concurrency tests and both fixed before this design was settled, are
   recorded in section 10.

## 3. Authoritative tenant/agent resolution: `app.inbound_call_routes`

An inbound call's `to_number` cannot be resolved through
`voiceagent.phone_numbers.service` because that path requires
`app.tenant_id` already set -- exactly the value resolution is supposed to
produce. `migrations/versions/0012_inbound_call_routing.py` adds:

- **`app.inbound_call_routes`**: `phone_number_id` (PK), `tenant_id`,
  `e164` (unique), `agent_id`, `inbound_enabled`, `updated_at`.
  **Deliberately not Row-Level-Secured** -- it carries no data beyond
  `e164 -> tenant_id/agent_id` routing (never PII, never conversation/call
  content), and readability with *no* tenant context set is the entire
  point.
- **A database trigger** (`app.sync_inbound_call_route()`, `AFTER INSERT OR
  UPDATE OR DELETE` on `app.phone_numbers`) keeps this table in sync
  automatically -- application code never writes to it directly, so there
  is no second code path that could drift from `phone_numbers`' own state.
- **A backfill** for phone numbers that already existed.
- **A partial unique index** replacing `call_sessions.fs_channel_uuid`'s
  plain index with `uq_call_sessions_fs_channel_uuid` (unique where not
  null) -- the idempotency primitive `create_call_session()` now depends
  on.

`voiceagent/calls/routing.py` adds `resolve_inbound_route(e164) ->
ResolvedRoute | None`, a plain `session_scope()` (non-RLS) read of exactly
this table -- the only new code in this phase that runs before a
`TenantContext` exists.

**Empirically verified against real PostgreSQL** (not asserted from
migration source alone): applied via `alembic upgrade head`; confirmed via
`psql \d` that RLS is disabled on `inbound_call_routes` and enabled+forced
on `phone_numbers`; inserted/updated/deleted `phone_numbers` rows and
confirmed the trigger correctly propagated each change; confirmed that the
`saas_os_app` role with **no** `app.tenant_id` set can read
`inbound_call_routes` but reads **zero** rows from `app.phone_numbers`;
confirmed the downgrade path drops cleanly and re-upgrade is idempotent.

## 4. `CallOrchestrator` (`voiceagent/runtime/orchestrator.py`)

```text
FreeSWITCH OFFERED event
    -> TelephonyEventRouter (Phase 2.21, unmodified apart from one
       additive callback, section 5)
    -> CallOrchestrator._handle_offer()                        (this phase)
        -> resolve_inbound_route()                 (tenant/agent resolution)
        -> get_agent / select_agent_version_id / get_agent_version
                                                        (unmodified, existed)
        -> create_call_session(fs_channel_uuid=...)  (idempotent, section 2)
        -> authorize_call_data_access()                    (unmodified)
        -> assign_call_to_runtime()          (unmodified, ownership claim)
        -> claim_call_for_activation()      (exactly-once claim, section 10)
        -> FreeSwitchTelephonyProvider.answer()/.start_media_stream()
                                                      (Phase 2.21, first use)
        -> CallRuntime.start_call()                        (unmodified)
```

Constructed once per `call-runtime` process (`scripts/run_call_runtime.py`,
section 6). `handle_unrouted_offer()` is the synchronous callback the
router calls for an `OFFERED` event with no subscriber; it never blocks --
it schedules `_handle_offer()` as a tracked background task
(`self._background_tasks`, section 9) and returns immediately, so routing
work (database calls, telephony commands) never delays the router's own
event pump.

**Authorization ordering is structural, not merely tested**:
`authorize_call_data_access()` is called, and must succeed, strictly before
`answer()`/`start_media_stream()`/`CallRuntime.start_call()` -- there is no
path through `_handle_offer()`/`_activate()` that reaches media/runtime
code without having passed authorization first (`_handle_offer()` is one
linear function, deliberately not split across helpers a later edit could
reorder; `noqa: C901` records why).

## 5. `TelephonyEventRouter.on_unrouted_offer` (additive, Phase 2.21 file)

Rather than let `CallOrchestrator` open a second, independent consumer of
`TelephonyProvider.events()` (which would silently reintroduce the exact
event-splitting bug `TelephonyEventRouter` exists to prevent), the router
gained one optional, public, mutable attribute:
`on_unrouted_offer: Callable[[CallEvent], None] | None = None`, called for
exactly one case -- an `OFFERED` event with no current subscriber. Every
other unrouted event type is still silently dropped exactly as before.
`None` by default (every pre-2.22 caller/test), so existing behavior is
byte-for-byte unchanged unless a caller opts in. Public and
post-construction-settable (not constructor-only) because the real wiring
is necessarily `router = TelephonyEventRouter(telephony); orchestrator =
CallOrchestrator(telephony_events=router, ...); router.on_unrouted_offer =
orchestrator.handle_unrouted_offer` -- the orchestrator needs the router as
its own dependency, so it must be constructed second. All 10 pre-existing
tests plus 4 new ones (callback invoked for `OFFERED`, not invoked for an
already-subscribed `call_ref`, not invoked for a non-`OFFERED` event,
default `None` preserves old behavior) pass.

## 6. Media attachment: the missing caller, closed at the right layer

Phase 2.21 left `start_media_stream()`/`stop_media_stream()` uncalled by
anything. Wiring this orchestrator as their first real caller surfaced a
genuine import-boundary violation: minting a media ticket
(`mint_media_ticket()`) lives in `voiceagent.telephony.freeswitch
.media_transport`, and the orchestrator calling it directly would import
FreeSWITCH internals, violating "FreeSWITCH internals stay behind
`voiceagent.telephony.freeswitch`" (import-linter). **Fixed
architecturally, not suppressed**: `FreeSwitchTelephonyProvider` now takes
`media_public_base_url`/`media_ticket_secret_provider`/
`media_ticket_ttl_seconds` at construction and mints its own ticket
internally; `start_media_stream(call_ref)` no longer takes a caller-built
URL. The orchestrator calls it through a structural `Protocol`
(`_MediaStreamCapableTelephonyProvider`), never importing anything
FreeSWITCH-specific.

`scripts/run_call_runtime.py` now passes `settings.freeswitch
.media_public_url` (already existed, unused since Phase 2.21) and a
secrets-backed provider for `FREESWITCH_MEDIA_TICKET_SECRET` at
construction.

## 7. Runtime ownership and exactly-once startup

No second ownership scheme and no takeover-on-crash were introduced.
`assign_call_to_runtime()`/`claim_runtime_ownership()` are called exactly
as they already existed. Three explicit outcomes are handled distinctly:
`NoRuntimeCapacityError` (transitions the call to `failed`/`no_capacity`,
rejects the channel), `CallSessionAlreadyOwnedError` (a different runtime
already won -- this process does nothing further, `ownership_lost`), and
"this process computed a winner that isn't itself" (same outcome, same
reasoning -- the actual winner's own orchestrator is independently
processing the identical event).

**`claim_call_for_activation()`** (new, `voiceagent/calls/service.py`),
called only after the ownership check above confirms this process the
owner, is the exactly-once gate two concurrency tests together proved
necessary in exactly this position (section 10): `SELECT ... FOR
UPDATE`-serialized `initiated -> ringing`, returning `True` for exactly one
of any number of concurrent callers. Every other caller -- already claimed
by a concurrent racer in this same process -- gets `False` and returns
without calling `answer()`.

## 8. Boundaries preserved

- **Telephony boundary**: `orchestrator.py` imports no ESL wire-frame type,
  no FreeSWITCH event object, and no `mod_audio_stream` framing detail --
  only `voiceagent.telephony.contracts` and the one structural `Protocol`
  named above. `lint-imports` confirms: "FreeSWITCH internals stay behind
  voiceagent.telephony.freeswitch" -- KEPT.
- **AI boundary**: the orchestrator builds a `ConversationEngine` via
  `voiceagent.providers.engines.factory.build_conversation_engine()`,
  driven entirely by `AgentVersion.config` -- no provider name, model name,
  or prompt is hard-coded anywhere in this file. Because that factory
  transitively reaches every vendor adapter through the STT/LLM/TTS
  registries (exactly like `voiceagent.providers.engines.factory` itself
  already does), the same import-linter exemption already granted to that
  module was extended, by explicit submodule enumeration (excluding
  `orchestrator`), to the four vendor-confinement contracts in
  `pyproject.toml` -- `lint-imports` confirms all 8 contracts KEPT, 0
  broken.
- **Database boundary**: every database call from `_handle_offer()`/
  `_activate()` goes through `self._db.run(...)`
  (`voiceagent.runtime.db.DatabaseBoundary`), never a direct sync call from
  the event loop.
- **Lifecycle/auth/tenant isolation**: none of `voiceagent.calls.lifecycle`,
  `voiceagent.runtime.privacy`, or SaaS-OS's own RLS/tenancy primitives were
  modified. SaaS-OS remains pinned and untouched.

## 9. Terminal cleanup

`CallOrchestrator.shutdown()` cancels every still-in-flight
`_handle_offer()` background task and awaits them (bounded, not an
unbounded drain) -- called from `scripts/run_call_runtime.py`'s own
shutdown sequence, before `CallRuntime.shutdown()`. Every spawned task is
tracked in `self._background_tasks` (added on creation, discarded via
`add_done_callback`), so an untracked task can never be silently
garbage-collected mid-flight. `_activate()`'s `finally` always calls
`telephony_events.unsubscribe(call_ref)`, whether the call reached
`answered` or failed at the media-attach step.

## 10. Two real concurrency bugs found and fixed

**Bug 1 -- duplicate `answer()` within one process.**
`test_duplicate_offered_events_for_the_same_call_start_exactly_once`
(section 12) emits the identical `OFFERED` event for the identical
`call_ref` twice in immediate succession, in one process. Before
`claim_call_for_activation()` existed, the guard was `if call.status !=
"initiated": duplicate_ignored; return` -- a plain snapshot read.
Both concurrent handlers called `create_call_session()` and both received
the same freshly-created row with `status == "initiated"` (neither had
advanced it yet), so both passed this check and both called `answer()`:
`answer_count == 2`, confirmed by the failing test before the fix.
Root cause: `claim_runtime_ownership()`'s own documented same-instance
reclaim idempotency (a deliberate feature, for legitimate retries) does not
signal "a concurrent handler in this same process already claimed this," so
it cannot be relied on to close this particular race on its own.

**Bug 2 -- the first fix's own placement stranded calls across processes.**
The first fix placed `claim_call_for_activation()` immediately after
`create_call_session()`, before authorization/assignment.
`test_two_runtimes_race_the_same_call_only_one_wins_ownership` (section 12)
then failed with a timeout: two independent `CallOrchestrator`/`CallRuntime`
pairs (simulating two processes), both fed the identical `OFFERED` event,
and *neither* ever started the call. Root cause: that placement is a plain
first-come-first-served race, unrelated to which runtime
`assign_call_to_runtime()`'s deterministic least-loaded selection would
actually assign the call to. Whichever process's handler happened to win it
by pure execution-order luck could be the *wrong* one -- it would proceed,
call `assign_call_to_runtime()`, discover the *other* runtime is the true
least-loaded winner, correctly back off as `ownership_lost` -- while the
*true* winner's own handler had already been rejected as a false duplicate
earlier and never even attempted `assign_call_to_runtime()`. Nobody ever
called `answer()`; the call was stranded, ownership claimed in the database
by a runtime whose own orchestrator process had already exited.

**The fix**: move `claim_call_for_activation()` to *after* the ownership
check (`winning_instance_id == self._call_runtime.instance_id`), immediately
before `_activate()`, not before authorization/assignment. This preserves
both properties: `assign_call_to_runtime()`'s deterministic, heartbeats-only
selection still lets every process reach the same conclusion about itself
regardless of execution order (so at most one process's own `_handle_offer()`
can ever pass the ownership check for a given external call -- cross-process
duplication is already impossible past that point, without any extra gate);
and the row lock in `claim_call_for_activation()`, applied only to calls that
already passed that check, closes the one race that check does not --
this same process independently confirming itself the owner twice for two
truly concurrent duplicate events (same-instance reclaim being
idempotent-success is exactly what makes that possible). Both tests pass
after this fix, and `test_two_runtimes_race_the_same_call_only_one_wins
_ownership` was re-run 5 additional times with no flakiness.

## 11. Failure semantics (verified by test, section 12)

| Scenario | Outcome | Call ends as | Channel |
|---|---|---|---|
| Unknown `to_number` | `unknown_route` | (no `CallSession` created) | hung up |
| Route disabled (`inbound_enabled=False`) | `route_disabled` | (none created) | hung up |
| Route has no `agent_id`, or agent not `active` | `unknown_agent` | (none created) | hung up |
| Agent has no published version | `agent_version_unavailable` | (none created) | hung up |
| Duplicate/replayed `OFFERED` for an already-claimed or terminal call | `duplicate_ignored` | unchanged | untouched |
| Data-authorization denied | `authorization_denied` | `failed` | hung up |
| No runtime has capacity | `no_capacity` | `failed` | hung up |
| A different runtime already owns the call | `ownership_lost` | unchanged (owned elsewhere) | untouched |
| `start_media_stream()`/`answer()` fails, or no `ANSWERED` within the timeout | `media_unavailable` | `failed` | hung up |
| Unhandled exception anywhere in `_handle_offer()` | `error` (logged) | unchanged | left for FreeSWITCH's own timeout, never blindly hung up from a codepath that may not know the call is still live |

Every non-`error` outcome is recorded via
`voiceagent.metrics.record_orchestration_event(outcome)` -- one bounded
counter, `voiceagent.orchestration.events`, labeled only by outcome (no
call id, no phone number, no tenant id: bounded cardinality).

## 12. Tests added

**`tests/integration/test_orchestrator_integration.py`** (new, 14 tests,
real PostgreSQL + real RLS, `pytest -m integration`):

- `test_valid_inbound_route_creates_and_starts_a_call`
- `test_unknown_route_is_rejected_and_hung_up`
- `test_disabled_route_is_rejected_and_hung_up`
- `test_route_with_no_agent_is_rejected`
- `test_inactive_agent_route_is_rejected`
- `test_agent_with_no_published_version_is_rejected`
- `test_authorization_denial_happens_before_media_activation`
- `test_no_runtime_capacity_is_rejected`
- `test_media_unavailable_ends_the_call_deterministically`
- `test_terminal_call_session_is_never_restarted`
- `test_duplicate_offered_events_for_the_same_call_start_exactly_once`
- `test_two_runtimes_race_the_same_call_only_one_wins_ownership` (two real
  `CallOrchestrator`/`CallRuntime` instances, two independent
  `FakeTelephonyProvider`s simulating two processes' own ESL connections,
  both fed the identical `OFFERED` event -- exactly one ends up with
  `current_load == 1`)
- `test_ten_concurrent_inbound_calls_do_not_cross_talk` (10 simultaneous
  inbound offers, one process, capacity 20 -- exactly 10 distinct
  `call_ref`s, exactly 10 `answer()` calls)
- `test_different_tenants_calling_simultaneously_stay_isolated` (two real
  tenants, simultaneous calls -- each tenant's own `list_call_sessions()`
  sees exactly its own one call)

**`tests/telephony/freeswitch/test_provider.py`** (Phase 2.21 file):
updated `test_start_media_stream_issues_the_expected_esl_command` for the
new construction-time-ticket signature; added
`test_start_media_stream_without_configuration_raises`.

**`tests/runtime/test_telephony_events.py`** (Phase 2.21 file): 4 new tests
for `on_unrouted_offer` (section 5).

**`voiceagent/telephony/fakes.py`**: `FakeTelephonyProvider` gained
`start_media_stream()`/`stop_media_stream()` no-ops, and its
`offer_inbound()`/`originate()`/`transfer()` call-ref generation was
changed from a plain per-instance counter (`fake-call-1`, `fake-call-2`,
...) to a per-instance random token plus counter
(`fake-call-<8 hex>-1`). **Why**: a real-PostgreSQL integration test
persists `CallSession.fs_channel_uuid` rows for real, across separate test
*process* invocations against one long-lived shared database; a plain
counter deterministically re-mints the same call_ref on every run,
colliding with another run's still-present row on `create_call_session()`'s
own uniqueness guarantee -- discovered directly (a genuine stale-row
collision was hit and diagnosed during this phase's own testing, not
hypothesized). No test asserts an exact `offer_inbound()`-generated value
(only hardcoded literal `call_ref=` fixtures do), so this changes nothing
any existing test depends on.

**`tests/integration/test_domain_rls_integration.py`** (pre-existing
security-invariant test, updated): its full-table RLS inventory now
includes `"inbound_call_routes": (False, False)`, with an explanation
tying the exception back to section 3's own reasoning -- the alternative
(leaving it out of the inventory) makes the test fail on an *unlisted*
table rather than document a reviewed, deliberate exception.

## 13. Real vs. simulated FreeSWITCH validation

**Real**: every claim in sections 3 and 12 about PostgreSQL/RLS behavior
(trigger sync, RLS enabled/forced flags, cross-role read visibility,
partial unique index conflict/idempotency behavior, concurrent-claim row
locking) was verified by actually running migrations and queries against
the real `voiceagent-test-pg` container and by running the 14 new
integration tests against it -- not asserted from source reading alone.

**Simulated**: no real FreeSWITCH/ESL server or real `mod_audio_stream`
connection was available or used this phase (matching Phase 2.21's own
documented posture). Every telephony-side test uses `FakeTelephonyProvider`
(`_AutoAnsweringTelephonyProvider`, `_MediaUnavailableTelephonyProvider`),
which is a fake, not a mock -- it holds real state (`live_calls`,
`commands`) and enforces real preconditions (`UnknownCallError` for a
non-live `call_ref`), but it is not a real ESL wire connection. The ESL
command-shape assertions in `tests/telephony/freeswitch/test_provider.py`
(section 6) exercise the real `FreeSwitchTelephonyProvider` translation
logic over `FakeEslConnection`, which is likewise not a live FreeSWITCH
process. This distinction is not new to this phase; it is preserved exactly
as Phase 2.21 established and documented it.

## 14. Known limitations / deferred (explicitly out of this phase's scope)

- Outbound-call origination requests are not built (this phase is inbound
  routing only, per its own brief).
- No crash-takeover/reassignment of an abandoned call's ownership --
  unchanged from ADR-0008 point 10 / Phase 2.0 report OQ-4.
- No load test against real concurrency beyond 10 simulated simultaneous
  calls in a fake-telephony test.
- No live FreeSWITCH/`mod_audio_stream` validation (section 13) --
  unchanged limitation from Phase 2.21.
- `voiceagent.runtime.reconciliation`'s existing stale-ownership scan was
  not modified and was out of this phase's scope.

## 15. Quality gates (all run against this phase's own working tree)

- **Backend tests, hermetic** (`pytest -q`, default run, excludes
  `-m integration`): full suite passes.
- **Backend tests, real PostgreSQL** (`pytest -m integration -q`,
  `tests/integration/`): full suite passes, including the pre-existing
  `test_domain_rls_integration.py` (with its one new, intentional,
  documented `inbound_call_routes` inventory entry).
- **Ruff** (`ruff check .`): all checks passed.
- **Ruff format** (`ruff format . --check`): all files formatted.
- **Pyright** (`voiceagent`, `tests`, `scripts`): 0 errors, 0 warnings.
- **import-linter** (`lint-imports`): 8 contracts kept, 0 broken.
- **detect-secrets**: re-scanned against the existing baseline; zero new
  findings in any file this phase touched or added (confirmed by diffing
  the file-set of a fresh scan against the existing baseline -- no file
  from this phase appears). The baseline itself was left untouched
  (unrelated pre-existing entries from a full-repo rescan were discarded
  rather than committed, as out of this phase's scope).
- **pip-audit**: no known vulnerabilities in any resolvable dependency (the
  two local packages, `saas-os`/`voiceagent`, are correctly skipped as not
  on PyPI).
- Frontend gates: not applicable -- no frontend file was touched this
  phase.

## 16. Files added

- `migrations/versions/0012_inbound_call_routing.py`
- `voiceagent/calls/routing.py`
- `voiceagent/runtime/orchestrator.py`
- `tests/integration/test_orchestrator_integration.py`
- `docs/PHASE-2.22-CALL-ORCHESTRATION.md` (this file)

## 17. Files modified

- `voiceagent/calls/service.py` -- `create_call_session(fs_channel_uuid=)`,
  `get_call_session_by_fs_channel_uuid()`, `claim_call_for_activation()`.
- `voiceagent/calls/models.py` -- `fs_channel_uuid` index documentation
  updated to match the migration's partial unique index (DDL is
  migration-owned; this is the ORM-side mirror).
- `voiceagent/metrics.py` -- `record_orchestration_event()`.
- `voiceagent/runtime/telephony_events.py` -- `on_unrouted_offer`.
- `voiceagent/telephony/freeswitch/provider.py` -- `start_media_stream()`
  mints its own ticket (section 6).
- `voiceagent/telephony/fakes.py` -- `start_media_stream`/
  `stop_media_stream` no-ops; collision-safe call-ref generation.
- `scripts/run_call_runtime.py` -- constructs `CallOrchestrator` and wires
  `router.on_unrouted_offer` whenever FreeSWITCH is configured (section 6);
  closes it and its `DatabaseBoundary` on shutdown.
- `tests/telephony/freeswitch/test_provider.py`,
  `tests/runtime/test_telephony_events.py`,
  `tests/integration/test_domain_rls_integration.py` -- see sections 5, 6,
  12.
- `pyproject.toml` -- the four vendor-confinement import-linter contracts
  now enumerate `voiceagent.runtime`'s submodules explicitly (excluding
  `orchestrator`), mirroring the pre-existing `providers.engines.factory`
  exemption (section 8). No package/dependency was added or removed.

## 18. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 19. Protected files

`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` were not opened,
read for editing, or written to at any point during this phase.

## 20. Final status

Implementation and verification complete for this phase's scope. Per this
phase's own instructions, no commit has been created and nothing has been
pushed -- `git status --short` at the end of this phase shows only this
phase's own working-tree changes (plus the pre-existing, already-dirty
state of the two files named in section 19, present before this phase
began and untouched by it).
