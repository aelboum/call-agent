# ADR-0004: Agent configuration is executed as an immutable published version

Status: Accepted (Phase 0)
Date: 2026-09-21

## Context

A tenant edits an agent while calls are in progress. If a call reads live
configuration, editing a prompt, swapping a voice, or removing a tool changes
the behavior of conversations already under way — producing incidents that are
unreproducible by construction, because the configuration that caused them no
longer exists.

The brief requires: agent configuration supports immutable published versions;
a production call executes against a specific published version; changing a
draft must not change an active production call.

Dograh's `workflow_definitions` table (`status` draft/published/archived,
`version_number`, `published_at`, with each `workflow_run` storing the
`definition_id` it ran against) validates the general shape. Its weaknesses are
instructive: immutability is a convention rather than an enforced property, and
a published row is an ordinary updatable row.

## Decision

1. **`Agent` is a mutable pointer; `AgentVersion` is immutable configuration.**
   The `Agent` row carries identity, `draft_version_id` and
   `published_version_id`, and no behavior of its own.
2. **`AgentVersion` rows are append-only.** Publishing inserts a new row and
   moves the pointer in one transaction; it never mutates an existing row. The
   only permitted mutation of a published row is the single
   `published → archived` status transition.
3. **A version's `config` is a complete behavioral snapshot**: instructions,
   greeting, language, voice reference, tool allowlist, transfer rules, business
   hours, call limits, workflow graph, and knowledge-source references by id and
   content hash. Nothing that determines behavior is resolved from a mutable
   row at call time.
4. **`config_hash`** is SHA-256 over a canonical JSON serialization of `config`,
   stored on the row, recorded in the publish audit entry, and used to answer
   "did anything actually change?" and to verify a replay.
5. **Immutability is enforced in three layers**, not by discipline alone:
   application (no update path exists), database (a trigger or rule rejecting
   `UPDATE` on a published row beyond the archive transition), and runtime (the
   snapshot is loaded once into memory for the life of the call, so even a
   successful hostile update cannot reach a call in progress).
6. **Version selection happens once, at call start.**
   `CallSession.agent_version_id` is written then and never re-read. Selection
   follows the `PhoneNumber`'s pin mode: `follow_published` (default) or
   `pinned` to a specific version — the latter exists for canarying on one
   number and for holding a known-good version during an incident.
7. **Rollback is re-pointing, never editing.** `published_version_id` moves back
   to an earlier row; the bad version is archived, not altered or deleted,
   because it is the evidence for the incident it caused.
8. **Validation happens at publish time, not at call time**: tools exist and are
   entitled, the voice exists at the configured provider, the workflow graph is
   well-formed with every transition resolvable and no duplicate generated tool
   names, transfer destinations are valid E.164, business hours parse in the
   agent's timezone. A caller must never hear the result of a configuration
   error that could have been caught at publish.
9. **Publishing is audited** (`agent.published`, with `version_number` and
   `config_hash`) and may emit a tenant-facing webhook event.

## Rejected alternatives

- **Live configuration read per call** — the failure this ADR exists to prevent.
- **Copy-on-call (snapshot the config into the call row)** — duplicates the
  full configuration per call, loses the ability to say "these 4,000 calls ran
  the same version", and makes a version's behavior unauditable in one place.
- **Version by mutable tag or "current" flag alone** — ambiguous under
  concurrent publishes, and offers no evidence of what actually ran.
- **Application-level immutability only** — a single missed code path silently
  destroys the property, and nothing detects it.

## What would be difficult to change later

Point 6. Once any code path re-reads agent configuration mid-call, "a call
executes against one version" stops being true, and every downstream guarantee
built on it (reproducibility, audit, safe rollback, canarying) degrades quietly
rather than failing loudly.

## What is deliberately not decided here

Version-retention policy (how many archived versions are kept, and for how
long); whether the canonical JSON serialization is a documented public format;
diff/compare UX between versions; and whether knowledge-source content is
snapshotted by value or only referenced by content hash.

## Related

`docs/PHASE-0-ARCHITECTURE.md` §7, §6, §9.2.
