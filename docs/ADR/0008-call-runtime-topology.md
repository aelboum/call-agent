# ADR-0008: Call runtime process topology

Status: Accepted (Phase 2.0)
Date: 2026-09-21
Resolves: Phase 0 report §19 OD-5

## Context

ADR-0002 fixed FreeSWITCH as the telephony core and split call control
(`TelephonyProvider`) from media transport (`MediaProvider`). Phase 0 report
§4.2 already named the process split (`control-api`, `call-runtime`, `worker`)
and the reason for it: a slow synchronous query or a GC pause in the process
serving the dashboard must never be audible as a glitch in a call hosted by
that same process.

What was not yet decided is the *internal* shape of `call-runtime`: how many
calls live in one process, how many processes exist, how a call's audio
socket finds the right process, and what happens when that process dies mid
call. This ADR resolves OD-5 for the parts that are architecture (must be
right before Phase 2.1 code is written) and defers the parts that are pure
operations (replica counts, autoscaling triggers) to deployment configuration.

Constraint carried over from Phase 0 report §2.4 G-1, reconfirmed by direct
inspection of the pinned SaaS-OS commit
(`ff550010e5eafecace7311038aadc99fcecfbe3d`): `infra.db` exposes no async
engine, no `AsyncSession`, no `asyncpg`. Every database touch from an async
process must leave the event loop.

## Decision

1. **One process, one event loop, many concurrent calls (asyncio).** A single
   `call-runtime` process runs one `asyncio` event loop and hosts N concurrent
   `EngineSession`s as independent tasks. **Not** one process per call
   (prohibitive process-startup and memory cost at realistic concurrency, and
   FreeSWITCH's own ESL connection is naturally a shared, per-process
   resource) and **not** one shared event loop across multiple OS processes
   (not a real option in CPython). A hybrid — multiple worker processes, each
   running its own event loop and its own bounded share of calls — is how
   horizontal scaling is achieved (point 6), so "hybrid" in the brief's sense
   is realized across processes, not within one.
2. **Every database or SaaS-OS call from `call-runtime` crosses
   `asyncio.to_thread()`** into a bounded thread pool, wrapping a synchronous
   `infra.db.tenant_session_scope()` block (Phase 0 report §4.2, reaffirmed).
   **No database access ever happens on the audio path itself** — only at call
   start (session/tool-gateway setup), at a tool-call boundary, and at call
   end. The thread pool's size is a tunable operational parameter (point 15),
   not an architectural one.
3. **Each `call-runtime` process holds its own ESL connection(s)** to the
   FreeSWITCH box(es) it is configured against. A process does not proxy
   another process's ESL connection; there is no shared "ESL broker" process
   in this design. (This keeps the control-plane failure domain identical to
   the media failure domain — see point 9.)
4. **A `CallRuntimeAssignment` is the authoritative, persisted binding** from
   one `CallSession` to the `call-runtime` instance currently responsible for
   it. Written by the Call Orchestrator (running in `control-api`, not in the
   runtime) at the moment a runtime is selected, before media is attached.
   Runtime instance identity is a stable value the process derives at startup
   (hostname + a random suffix is sufficient; it is an opaque string to
   everything except the reconciler in point 11) and re-announces on every
   heartbeat.
5. **Runtime selection is least-loaded, not sticky.** The Call Orchestrator
   queries each registered runtime's current call count (point 8) and assigns
   to the least-loaded one with capacity below its configured maximum. No
   session affinity is needed because a call, once assigned, lives entirely on
   one runtime for its duration (point 1) — there is nothing to be "sticky"
   about after assignment.
6. **Horizontal scaling is adding `call-runtime` processes**, each
   independently connected to FreeSWITCH's ESL and independently capable of
   accepting `MediaProvider` attachments. A process's maximum concurrent-call
   count is a configured ceiling (point 15); the Call Orchestrator refuses new
   assignments to a runtime at its ceiling and returns "no capacity" (point
   14) rather than overcommitting it.
7. **How FreeSWITCH's media reaches the assigned runtime.** The Call
   Orchestrator, at assignment time, mints a short-lived signed media ticket
   (Phase 0 report §14.3) encoding the assigned runtime's own media listener
   address (`VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL`-style, but per-runtime, not
   a single fixed value — point 15's operational detail is *how* the
   orchestrator learns each runtime's address, via the same heartbeat as
   point 8). The ESL command that attaches `mod_audio_stream`
   (`uuid_audio_stream ... start <wss-url> ...`) is issued with *that* URL, so
   FreeSWITCH connects the media socket directly to the assigned runtime — no
   proxy hop, no re-routing after the fact.
8. **Runtime registration and liveness is a heartbeat into Redis**, not a
   database table: `infra.db` is sync-only (point 2), and liveness is
   inherently ephemeral, transient state — exactly what `infra.jobs`'s own
   Redis usage pattern (dead-letter tracking) already models. Each runtime
   writes `runtime:{instance_id} -> {address, capacity, current_load,
   last_heartbeat}` on a short TTL, refreshed continuously; the Call
   Orchestrator reads this set to pick an assignee (point 5) and a
   reconciler (point 11) reads it to detect staleness.
9. **A runtime process crash is detected by heartbeat expiry, not by a
   session callback.** When a runtime's Redis key expires, every
   `CallRuntimeAssignment` still pointing at that instance is stale. The
   reconciler (point 11) marks each such `CallSession` `interrupted` and
   enqueues its finalization job. FreeSWITCH itself does not know the runtime
   died until its media socket drops — that drop is FreeSWITCH's own signal to
   hang up the call (or, for a graceful drain, to trigger the fallback
   behavior configured on the agent version), independent of the reconciler,
   which exists to close the *bookkeeping*, not to save the call. A call
   cannot be silently resumed on a different runtime mid-session: the engine
   session's in-memory conversation state is not replicated (point 12).
10. **Ownership is exclusive and provable.** At most one `call-runtime`
    process may hold an active `EngineSession` for a given `call_session_id` at
    any time. This falls out of points 4, 5 and 7 by construction (the
    orchestrator assigns exactly once, before media exists) rather than being
    enforced by a runtime-side lock — there is only ever one path by which a
    runtime comes to own a call, and no path by which two runtimes could.
11. **A reconciliation loop** (running in `control-api`'s worker, alongside
    the parked-channel reaper of ADR-0002 §10.6) periodically compares three
    views: FreeSWITCH's own live channel list (over ESL), the set of open
    `CallSession` rows, and the runtime heartbeat set. It is the source of
    truth-repair for three failure shapes: a parked channel with no
    `CallSession` (ADR-0002's reaper handles this one directly), a
    `CallSession` assigned to a heartbeat-expired runtime (point 9), and a
    `CallSession` with no corresponding live FreeSWITCH channel at all (marks
    it `completed`/`interrupted` as appropriate and audits the discrepancy).
12. **No in-flight call state is replicated or persisted for recovery.** A
    runtime process crash ends every call it was hosting; there is no
    warm-standby takeover of an `EngineSession` mid-conversation. This is a
    deliberate simplicity choice for the initial vertical slice, not an
    oversight — it is recorded as an explicit non-goal in the Phase 2.0
    report, with the operational mitigation being a short blast radius (bound
    the per-process call ceiling, point 6) rather than session replication.
13. **Backpressure is capacity refusal, never silent queuing on the audio
    path.** If every runtime is at its configured ceiling, the Call
    Orchestrator does not assign the call and does not wait for capacity to
    free up mid-ring. See point 14.
14. **No capacity is a defined outcome, not an unhandled case.** The Call
    Orchestrator's assignment step can fail with "no runtime capacity". For
    inbound calls this routes to the agent version's configured no-capacity
    behavior (busy tone, or a transfer/voicemail fallback — Phase 2.1 does not
    build that behavior yet, but the assignment API's contract accommodates a
    failure outcome from day one so it is never bolted on as an afterthought).
    For outbound calls it is a synchronous failure returned to the caller of
    the outbound-call API.
15. **Tunable, not fixed, parameters** (operational configuration, not
    architecture): per-runtime maximum concurrent calls, the `to_thread` pool
    size, the heartbeat interval and TTL, and the reconciliation loop's
    period. None is given a number by this ADR — each needs a benchmark
    against real load before a default is chosen (Phase 2.0 report §18).

## Sequence (informal; the Phase 2.0 report carries the full Mermaid diagram)

```
PSTN -> FreeSWITCH -> park() -> CHANNEL_PARK (ESL, to *a* runtime's control
  connection, or to control-api if runtimes do not hold the inbound-park
  connection themselves -- see the Phase 2.0 report's topology diagram for
  which process actually owns the inbound resolution step)
    -> tenant/agent resolution -> CallSession row -> runtime assignment
       (least-loaded, Redis heartbeat set) -> signed media ticket
    -> uuid_answer, uuid_audio_stream start <assigned-runtime-media-url>
    -> media socket opens directly to the assigned runtime
    -> ConversationEngine session begins
```

## Rejected alternatives

- **One process per call** — process-startup and memory cost at realistic
  concurrency, and duplicates the ESL connection per call for no benefit.
- **A single process, single event loop, no horizontal scaling** — a hard
  ceiling on total platform concurrency with no way to raise it short of a
  bigger box; does not survive one process crash without taking every active
  call down.
- **Sticky assignment with a consistent-hashing ring** — solves a rebalancing
  problem this design does not have, because a call never migrates between
  runtimes after assignment (point 1); adds real complexity for no captured
  benefit at this scale.
- **A shared "ESL broker" process that all runtimes proxy through** —
  reintroduces a single point of failure between FreeSWITCH and every call,
  exactly the kind of shared-runtime risk ADR-0002's control/media split
  exists to avoid.
- **Replicating in-flight session state for hot takeover** — real complexity
  (a distributed session store, a hand-off protocol) that the current scale
  does not justify; recorded as a Phase 3+ candidate if a crash's blast
  radius, measured in production, turns out to matter more than the
  mitigation in point 12 assumes.

## Consequences

Positive: horizontal scaling is "start another runtime process"; a runtime
crash has a bounded, cheaply-reasoned-about blast radius (the calls it was
hosting, nothing else); no distributed lock is needed anywhere in this design
(point 10); the reconciliation loop is one mechanism serving three failure
shapes, not three.

Negative and accepted: a call in progress cannot survive its runtime process
dying; the Call Orchestrator becomes a runtime-assignment authority and must
itself be highly available (an existing `control-api` concern, not a new
single point of failure introduced by this ADR — `control-api` already sits
in the request path for every authenticated action).

## What would be difficult to change later

Point 1 (one event loop per process, many calls) is the load-bearing
assumption behind points 4–11. Moving to one-process-per-call or to
session-replicated takeover later is possible but is a runtime rewrite in the
part of the system this platform's architecture works hardest to keep small
(ADR-0006) — so the ceiling in point 15 should be tuned generously rather than
treating this ADR's model as merely provisional.

## What is deliberately not decided here

The exact heartbeat interval, TTL, per-runtime call ceiling and thread-pool
size (point 15 — Phase 2.1 benchmarking). Whether `control-api` itself needs
multiple replicas for its own availability (an existing, unrelated concern).
Whether inbound `CHANNEL_PARK` events are received directly by a designated
runtime or by a separate control-plane ESL listener that only ever creates
`CallSession` rows and never hosts media — the Phase 2.0 report's topology
section states the recommended shape for Phase 2.1 but leaves the alternative
open pending the FreeSWITCH deployment topology's own operational testing.

## Related

ADR-0002 (FreeSWITCH boundary, the parked-channel reaper); ADR-0006
(engine/transport split); Phase 0 report §4.2, §10.6, §14.3; Phase 2.0 report
§5 (FreeSWITCH topology), §8 (call ownership model), §17 (failure/recovery).
