# Phase 2.11 Status: Agent Knowledge & Context

Checkpoint: Phase 2.10 ("feat: add controlled call workflows") is committed
as the branch's tip commit at the start of this phase. SaaS-OS remains
pinned at
`ff550010e5eafecace7311038aadc99fcecfbe3d`, consumed only as an external
dependency and never modified.

## 1. Objective

Give a published `AgentVersion` a controlled way to use tenant-owned
business knowledge (hours, pricing, policies, FAQs) during a call, without
ever giving the LLM unrestricted database access:

```text
Tenant-owned knowledge (KnowledgeSource / KnowledgeItem)
        |
        v
Knowledge retrieval service (voiceagent.knowledge.retrieval.search_items)
        |
        v
Tool Gateway (knowledge.search) / ContextAssembler
        |
        v
ConversationEngine
        |
        v
LLM
```

This is not a vector database, RAG platform, or document pipeline. It is a
small, bounded, text-first knowledge boundary -- the same "smallest
architecture that satisfies the brief" discipline Phase 2.10 (Controlled
Call Workflows) already established.

## 2. Knowledge model

Two tables (`migrations/versions/0009_create_knowledge_tables.py`,
`voiceagent/knowledge/models.py`):

- **`KnowledgeSource`**: `id`, `tenant_id`, `name`, `description`, `status`
  (`active`/`archived`), timestamps. A label grouping items; never itself
  retrieved or sent to an LLM.
- **`KnowledgeItem`**: `id`, `tenant_id`, `source_id`, `title` (<=200 chars),
  `content` (<=20,000 chars), `status` (`draft`/`active`/`archived`),
  timestamps. No JSON metadata field was added -- the brief explicitly asks
  not to add one "merely for future flexibility," and nothing in this phase
  needs one.

No generic document store, no vector table, no embedding queue.

## 3. AgentVersion interaction / versioning strategy

**Choice A (immutable/versioned knowledge items), not choice B
(content-snapshotting)** -- the smaller of the two architectures the brief
offers.

A `KnowledgeItem`'s `title`/`content` are mutable only while
`status='draft'`. `activate_item()` (`draft -> active`) freezes them for the
row's entire remaining lifetime; `app.forbid_knowledge_item_content_update`
(a trigger installed by migration `0009`, mirroring
`app.forbid_published_agent_version_update` from migration `0002`) enforces
this at the database layer, not merely in application code, and permits
exactly one further status transition (`active -> archived`).

`AgentVersion.config["knowledge"]` (`voiceagent.knowledge.config
.KnowledgeConfig`, a new optional field on `voiceagent.agents.config
.AgentConfig`, alongside the existing `workflow` field) is therefore a
**bounded reference list** -- `item_ids`, at most
`MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION` (50), deduplicated -- never a copy of
item content. Because an id referenced there can never come to mean
different text later, a published `AgentVersion`'s knowledge configuration
stays deterministic without needing to duplicate potentially many kilobytes
of tenant text into `AgentVersion.config` on every publish.

**What can still change after publish, and why that is not a determinism
violation:** deactivating an item (`active -> archived`) does not alter its
frozen `title`/`content`, but does remove it from future retrieval results
(`voiceagent.knowledge.retrieval.search_items()` filters on
`status='active'` on every call, never cached). The *reference* in
`config["knowledge"]["item_ids"]` stays valid and unchanged; only whether it
currently *resolves to visible content* can narrow over time. This mirrors
`workflow.advance`'s own relationship to the Tool Gateway allowlist: a
reference that stays syntactically valid forever, whose runtime
availability can still be gated by other, independently-changing state.

**Existence/ownership validation** happens once, at draft-creation time
(`voiceagent.agents.service.create_draft_version()`), the identical two-step
shape Phase 2.10 established for `tool_id`:
`voiceagent.knowledge.service.resolve_active_item_ids()` fetches the
tenant-owned, `status='active'` subset of the requested ids (its own
`tenant_scope()`), and the pure, DB-free
`voiceagent.knowledge.validation.validate_item_references()` compares. An
unknown, foreign-tenant, or still-`draft` item id fails the draft-creation
call with `InvalidAgentConfigError` -- never silently dropped, never
deferred to first use in a call.

## 4. Retrieval design

`voiceagent.knowledge.retrieval.search_items(context, agent_version, *,
query, limit)` is bounded, deterministic, normalized-text matching -- not
PostgreSQL full-text search, not embeddings.

**Why not `ILIKE`/full-text search at the database layer:** `voiceagent.db`
(ADR-0007) re-exports no boolean combinator (`sqlalchemy.or_`/`and_`) a
multi-token `WHERE` clause would need -- only the primitives SaaS-OS's own
`infra.db` sanctions. Rather than reaching around that seam (which exists
specifically to prevent an RLS-bypass class of primitive from re-entering
this codebase), retrieval fetches the small, already-bounded candidate set a
query could ever match against -- at most 50 rows, the same ceiling that
already bounds one `AgentVersion`'s approved item count -- and matches
lowercased, alphanumeric tokens against `title`/`content` in Python. With
the candidate set capped at 50 rows this is exactly as fast and exactly as
deterministic as a database-side match, and needs nothing this seam does
not already provide.

**Every bound is enforced before any query reaches the database:**

| Bound | Value | Enforced by |
|---|---|---|
| Query length | 500 chars | `MAX_QUERY_LENGTH`, checked first |
| Query tokens | 10 | `MAX_QUERY_TOKENS`, after normalization |
| Result count | 5 | `MAX_RESULT_LIMIT`, clamps a caller-requested `limit` -- never trusted upward |
| Snippet length | 500 chars | `MAX_SNIPPET_LENGTH`, applied per matched item |
| Approved items per agent | 50 | `MAX_KNOWLEDGE_ITEMS_PER_AGENT_VERSION`, bounds the whole candidate scan |

**Association + status are re-checked on every call, never cached:** a
result must be in `agent_version.config["knowledge"]["item_ids"]` *and*
`KnowledgeItem.status == 'active'` *and* its own `KnowledgeSource.status ==
'active'`, all filtered through `tenant_scope()`/RLS on `context.tenant_id`.

Result ordering is `(title, id)` -- deterministic across repeated identical
queries against identical data, never raw database row order.

## 5. ContextAssembler

`voiceagent.knowledge.context.assemble_call_context()` combines only
already-persisted, already-tenant-scoped sources into one bounded,
inspectable `CallContext`:

- the governing `AgentVersion`'s own `instructions` (bounded to
  `MAX_AGENT_INSTRUCTIONS_LENGTH` = 4,000 chars, a defensive ceiling
  independent of whatever bound that field gets in the future),
- the call's lifecycle `status` and outcome (if any),
- its associated contact, as the same minimal `ContactRef`-shaped fields
  `voiceagent.tools.handlers` already exposes (never a raw `Contact` row),
- a bounded knowledge search, only if `knowledge_query` is explicitly given.

It performs no database query of its own -- every field comes from an
existing application service (`voiceagent.calls.service`,
`voiceagent.contacts.service`, `voiceagent.followups.service`,
`voiceagent.knowledge.retrieval`), each independently tenant-scoped.

**No conversation dump, no memory.** There is no conversation-turn field.
Nothing this function returns is written back anywhere -- it is assembled
fresh and discarded per call.

## 6. Privacy boundary

`voiceagent.runtime.privacy.authorize_call_data_access()` already runs once
per call, before that call's `ConversationEngine` ever starts (the ordering
invariant Phase 2.0's report establishes). If `assemble_call_context()` or
`knowledge.search` is reachable at all, that gate has already passed for the
call in question. Neither adds a second privacy check -- this mirrors the
exact precedent Phase 2.6's own Contact/Calendar tool handlers already set
(`_lookup_contact_by_phone`/`_check_availability` do not re-check
`authorize_data_access()` either, for the identical reason). No bypass is
introduced; the existing one-per-call gate is relied upon, not duplicated or
weakened.

## 7. Prompt-injection boundary

Retrieved knowledge is treated as **untrusted tenant data, never
instructions**. `KnowledgeSearchResult`/`ContactContext`/`CallStateContext`
are returned as separate, typed, plain-data fields -- never concatenated
into `agent_instructions` or into any single string this module produces.
The intended layering (System instructions > Agent configuration >
Tool/security policy > Retrieved knowledge DATA > Conversation/user content)
is a caller responsibility when it finally assembles a prompt; this phase's
own contribution is keeping knowledge content structurally separate so that
layering is possible, and never itself interpreting, executing, or
sanitizing `KnowledgeItem.content` -- it is passed through as opaque text in
every direction.

A knowledge document cannot define a tool, a workflow step, or any
executable behavior: there is no such reference from `KnowledgeItem` to
anything action-shaped anywhere in this schema.

## 8. Tool Gateway integration

`knowledge.search` (`voiceagent.tools.handlers`) is the twelfth built-in
tool, registered in `TOOL_REGISTRY` exactly like every other tool. It:

- takes one input field, `query` (`min_length=1`, `max_length=500`) -- the
  model may not supply `tenant_id`, a result limit, a source/table name, or
  any filter shape;
- derives tenant and agent/version context from `ToolExecutionContext`
  (server-resolved, never from model-supplied arguments), the same pattern
  every other Phase 2.6+/2.10 tool already follows;
- clamps its own result limit to `MAX_RESULT_LIMIT` (5) regardless of
  anything the model could ask for;
- is authorized the identical way `workflow.advance` already is: through
  `voiceagent.tools.permissions` (computed from `TOOL_REGISTRY`, no
  hand-listed duplicate) plus the published `AgentVersion`'s own tool
  allowlist -- no new "execute knowledge search" permission was invented;
- is audited by the Tool Gateway's own existing mechanism
  (`core.audit_log`, `action="tool.knowledge.search"`) -- no bespoke
  `knowledge.search` audit path was added (see §10).

## 9. API

`voiceagent/api/v1/knowledge.py`, mounted at `/v1/knowledge`:

| Method | Path | Permission |
|---|---|---|
| `POST` | `/knowledge/sources` | `voiceagent.knowledge_sources:create` |
| `GET` | `/knowledge/sources` | `voiceagent.knowledge_sources:read` |
| `POST` | `/knowledge/sources/{source_id}/items` | `voiceagent.knowledge_items:create` |
| `GET` | `/knowledge/sources/{source_id}/items` | `voiceagent.knowledge_items:read` |
| `PATCH` | `/knowledge/items/{item_id}` | `voiceagent.knowledge_items:update` |
| `POST` | `/knowledge/items/{item_id}/activate` | `voiceagent.knowledge_items:activate` |
| `POST` | `/knowledge/items/{item_id}/deactivate` | `voiceagent.knowledge_items:deactivate` |

No delete route (not truly necessary -- `deactivate` already removes an item
from retrieval without breaking a published reference), no per-id `GET`
(the row is already in hand from `list_*`/the mutating call's own
response), no debug/admin retrieval endpoint, and **no separate
knowledge-association endpoint**: approving knowledge for an agent is done
through the *existing* `POST /v1/agents/{agent_id}/versions` route, whose
`AgentVersionCreateRequest.config` already carries the new optional
`knowledge` field. Adding a second, parallel association endpoint would
only duplicate that one.

## 10. RBAC

Two resources (`voiceagent/knowledge/permissions.py`), matching
`voiceagent.followups.permissions`'s own two-resources-one-package shape:

- `voiceagent.knowledge_sources`: `read`, `create`, `update`, `archive`
- `voiceagent.knowledge_items`: `read`, `create`, `update`, `activate`,
  `deactivate`

No `knowledge.admin` bypass resource. `knowledge.search` needs no separate
management-style permission -- it is authorized through the existing
`voiceagent.tools`-resource path (§8). `voiceagent/rbac_bootstrap.py`
registers both new resources' actions and includes them in the default
`voiceagent-runtime` role's grant set, the same as every other domain.

## 11. Persistence / RLS

Migration `0009_create_knowledge_tables.py`, continuing directly from
`0008_call_workflow_executions`. Both tables:

- carry `tenant_id` (`ForeignKey("core.tenants.id")`);
- have `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`
  (`infra.db.tenant_rls_statements()`, the same helper every other
  tenant-owned table uses);
- use tenant-safe composite foreign keys (`knowledge_items.source_id` ->
  `(knowledge_sources.id, knowledge_sources.tenant_id)`, matching the
  tenant_id column, so a cross-tenant reference is rejected at the
  constraint layer, not merely hidden by RLS);
- have explicit uniqueness constraints:
  `uq_knowledge_sources_id_tenant`/`uq_knowledge_items_id_tenant` (the
  composite-FK targets) and `uq_knowledge_sources_tenant_name`/
  `uq_knowledge_items_tenant_source_title` (tenant-local name/title
  uniqueness);
- have the indexes their own access patterns need:
  `(tenant_id)`, `(status)`/`(tenant_id, status)` (the retrieval filter),
  and `(source_id)`.

No `CREATE EXTENSION`, no vector column type, no generic `embeddings`/
`documents` table -- verified directly by
`tests/test_migrations.py::test_phase_2_11_does_not_create_a_vector_or_generic_document_table`.

## 12. Audit

Management operations (`voiceagent/knowledge/service.py`):
`knowledge.source_created`, `knowledge.item_created`,
`knowledge.item_updated`, `knowledge.item_activated`,
`knowledge.item_deactivated`. `knowledge.association_changed` is recorded by
`voiceagent.agents.service.create_draft_version()` itself, only when
`config.knowledge.item_ids` is non-empty, with `metadata={"item_count":
...}` -- never the item ids or content.

**Runtime search auditing** deliberately uses no bespoke `knowledge.search`
audit path. `voiceagent.tools.gateway.ToolGateway` already writes one
`core.audit_log` entry per terminal outcome for every tool call, `action
="tool.knowledge.search"` -- adding a second, parallel audit write here
would duplicate that infrastructure for no additional security value, and
would risk drifting from the Tool Gateway's own metadata discipline (tool
arguments, and therefore a caller's raw query text, are never included in
audit metadata -- brief AUDIT: "Do not log... conversation text"). This is
not omission-by-oversight; it is the same choice `workflow.advance`
(Phase 2.10) already made for its own execution audit trail.

No audit entry, anywhere in this phase, carries `KnowledgeItem.content`,
`KnowledgeSearchResult.snippet`, or a raw search query -- only stable ids
and counts.

## 13. Tests

**Hermetic** (`tests/knowledge/`, `tests/tools/test_registry.py`,
`tests/test_migrations.py`):

- `test_knowledge_config.py`: `KnowledgeConfig` bounds, duplicates,
  frozen-ness (7 tests).
- `test_knowledge_validation.py`: `validate_item_references()` pure
  comparison logic (4 tests).
- `test_knowledge_retrieval.py`: `_tokenize`/`_truncate` pure helpers, and
  every `search_items()` path that rejects or short-circuits before
  touching the database (12 tests).
- `test_context_assembler.py`: `assemble_call_context()`'s own combination/
  bounding logic, with its four cross-domain service calls monkeypatched
  (6 tests).
- `test_registry.py::test_the_process_wide_registry_holds_exactly_the_twelve_built_in_tools`:
  updated for the new tool (was "eleven").
- `test_migrations.py`: five new tests -- table creation, RLS/FORCE,
  the content-immutability trigger's presence, uniqueness constraints, and
  the explicit "no vector/generic-document table" check.

**Integration** (`tests/integration/test_knowledge_integration.py`, 22
tests, all executed and passing against real PostgreSQL): source/item
lifecycle and status transitions; content-immutability enforced at both the
service layer and the database trigger; tenant isolation for both tables;
cross-tenant composite-FK rejection; cross-tenant and unknown/draft-status
knowledge-item rejection at draft-`AgentVersion`-creation time;
`resolve_active_item_ids()`'s own exclusion behavior; bounded retrieval
(approved-association scope, deactivated-item exclusion, and a defense-in-
depth proof that retrieval's own tenant filter still holds even against a
hand-crafted cross-tenant reference that could never occur through the real
validation path); `knowledge.search` executing end-to-end through the real
Tool Gateway and real RBAC (allowlist denial, unauthorized-tenant denial,
malformed-query rejection); and `assemble_call_context()` against real
persisted call/knowledge state.

**Pre-existing test corrected, not silently:** `tests/integration
/test_domain_rls_integration.py::test_row_level_security_is_enabled_and_forced_for_every_table`
's hand-maintained table inventory was missing `call_workflow_executions`
(a gap Phase 2.10's own real-PostgreSQL verification pass found and
reported rather than fixing, since that verification pass was scoped to
read-only checking). Implementing Phase 2.11 is the appropriate place to
correct it, following the exact convention every earlier phase already
used for this same test (each phase adds its own new tables to this one
inventory) -- both `call_workflow_executions` and this phase's own
`knowledge_sources`/`knowledge_items` are added together.

## 14. Quality gates

All run from the product's own `.venv` (Python 3.13):

- **Hermetic suite**: full `pytest` run, all tests pass (Phase 2.11 adds 29
  new hermetic tests: 7 + 4 + 12 + 6, plus the updated registry/migration
  tests).
- **Integration suite** (real PostgreSQL, `pytest -m integration`): full
  suite passes, including the new 22-test knowledge file and the corrected
  RLS-inventory test. One pre-existing, unrelated test --
  `tests/integration/test_runtime_integration.py
  ::test_one_call_failing_does_not_affect_others` -- is a known timing-
  sensitive flake in `voiceagent/runtime/supervisor.py` (a file this phase
  never touches); it was already flagged, unfixed and out of scope, during
  Phase 2.10's own verification pass, and remains so here.
- **`ruff check`** / **`ruff format --check`**: clean on every file this
  phase added or touched.
- **`pyright`**: 0 errors, 0 warnings on every file this phase added or
  touched.
- **`lint-imports`** (import-linter): all 8 architecture contracts kept --
  `voiceagent.knowledge` introduces no new forbidden edge (it does not
  import SQLAlchemy/psycopg directly, and nothing in
  `voiceagent.providers.engines.*` reaches it transitively through anything
  but the already-permitted `voiceagent.agents.config` field).
- **`detect-secrets`**: scanned; no finding in any file this phase added.
  The tool's own baseline regeneration surfaced a large, pre-existing
  diff across unrelated files (`.env.example`, CI workflow, earlier phase
  status docs, existing tests) that predates this phase entirely --
  apparent local plugin/version drift against the committed baseline, not
  a Phase 2.11 finding. That baseline diff was reverted so this phase's
  working tree carries only its own changes.
- **Migration validation**: `alembic upgrade head`/`downgrade -1`/`upgrade
  head` all applied cleanly against a real PostgreSQL instance; RLS+FORCE
  confirmed directly via `pg_class` before and after.

## 15. Known limitations

- Retrieval is deterministic substring matching, not ranked relevance --
  acceptable for the bounded, small-corpus MVP this phase targets, and
  explicitly not meant to compete with real search/RAG.
- `resolve_active_item_ids()`'s draft-time validation is a point-in-time
  snapshot, not transactional with the surrounding `create_draft_version()`
  write -- an item deactivated in the narrow window between the two is an
  accepted, narrow gap (the identical class of race
  `validate_tool_references()`/`TOOL_REGISTRY` already accepts), not a
  security boundary: retrieval re-checks `status='active'` on every call
  regardless.
- `ContextAssembler` has no hermetic test for its DB-crossing callees
  themselves (only for its own combination logic, monkeypatched) --
  consistent with this codebase's existing convention that DB-touching
  service functions are proven in integration tests only
  (`tests/contacts/test_contact_service.py`'s own module docstring already
  states this for `voiceagent.contacts.service`).

## 16. Explicitly deferred / non-goals

Per the brief, none of the following exist anywhere in this phase, and
their absence is verified where practical (migration test, import-linter):
vector database, embeddings, external search engine, autonomous memory,
long-term conversational memory, knowledge agents, self-learning,
fine-tuning, RAG platform, website/email/cloud-drive ingestion, arbitrary
document processing pipeline, generic document management, MCP, arbitrary
tools/code execution/expressions, generic workflow engine, CRM, marketing
automation, campaigns, Phase 2.12+ work, and a frontend knowledge-management
UI.
