# ADR-0003: All agent actions are mediated by a product-owned Tool Gateway

Status: Accepted (Phase 0)
Date: 2026-09-21

## Context

An AI agent on a live call must be able to act — look up a contact, check
availability, book an appointment, transfer the call. Those actions touch
tenant data and cost money, and their parameters come from a language model
whose input includes whatever a caller chose to say. An agent must therefore
never hold database access, and must never supply its own identity or scope.

SaaS-OS already establishes tool-mediated access as a platform invariant
(its ADR-0004) and ships an implementation in
`control_plane.orchestration`: `ToolDefinition`, `ToolRegistry`,
`invoke_tool()`, plus an approvals subsystem and a data-authorization gate. The
obvious move is to execute in-call tools through `invoke_tool()`. Inspection of
that code at the pinned SHA shows it cannot serve the realtime path:

- `invoke_tool()` raises `TierRequiresApprovalError` for any tool with
  `autonomy_tier >= 1`; such tools must be proposed through
  `control_plane.approvals` and await a human decision. A caller on the phone
  cannot wait. Declaring every in-call tool tier 0 to get past this would make
  the tier field decorative rather than protective.
- It requires `agent_user_id: uuid.UUID` — a *user* principal. The call runtime
  acts as a `core.identity.ServiceAccount`.
- `ToolDefinition` carries no input schema, no output schema, no timeout, no
  retry policy and no idempotency policy; its handler receives an unvalidated
  `Mapping[str, object]`. The brief requires all of these, and unvalidated model
  arguments are exactly what must be stopped.

Modifying SaaS-OS to fit is prohibited by ADR-0001 and would be wrong for the
platform in any case.

## Decision

1. **The product owns `callagent.tools.gateway`**, the single mediation point
   for every action an agent takes. No other path from the runtime to domain
   data or external effects exists.
2. **The Gateway is composed from SaaS-OS primitives**, not a parallel
   platform: `core.rbac.can` for authorization, `core.rbac.register_permission`
   for permission declaration, `core.idempotency.run_idempotent` for
   exactly-once semantics, `core.audit_log.record` for audit,
   `core.usage`/`core.billing` for quota and entitlement, `infra.db` for all
   data access.
3. **The same tools are additionally registered as
   `control_plane.orchestration.ToolDefinition`s** for non-realtime, operator
   and back-office agent use, where approval gates are appropriate. One tool
   implementation; two callers; two policies. The realtime path never goes
   through `invoke_tool()`.
4. **Identity and scope are never taken from model output.** `tenant_id`,
   `agent_version_id`, `call_session_id` and the acting principal come from the
   in-memory call context established server-side at call start. Model
   arguments supply domain parameters only.
5. **Every tool declares** input schema, output schema, permission, tenant
   scope, side-effect class, timeout, retry policy, idempotency policy, audit
   policy and data classification. A tool without all of these is not
   registrable.
6. **Two independent gates must both pass**: the tool must be listed in the
   *published AgentVersion's* allowlist, **and** the acting principal must hold
   the permission. Holding the permission does not grant use of a tool the
   agent's published configuration does not include.
7. **Execution order is fixed** (context → allowlist → entitlement/quota →
   schema validation → authorization → idempotency → timed execution → output
   validation and redaction → audit → persisted `ToolCall` record). Every step
   may deny.
8. **Failures are values at the model boundary.** The model receives a
   structured, non-leaking `{"error": {"code", "retryable"}}` so the agent can
   recover conversationally. Internal detail never crosses that line.
9. **Generic HTTP/webhook/custom-API/MCP tools do not exist** until each has
   passed its own security review. Tenant-authored egress is a distinct threat
   class (SSRF, credential exfiltration, an uncontrolled second tool supply
   chain), not an increment of the existing tool model.

## Rejected alternatives

- **Route in-call tools through `control_plane.orchestration.invoke_tool()`** —
  incompatible with realtime for the reasons above.
- **Modify SaaS-OS to add a realtime execution path** — forbidden by ADR-0001.
- **Give the runtime direct application-service access, skipping a gateway** —
  loses the single point where authorization, validation, idempotency and audit
  are guaranteed, which is the whole property being bought.
- **Let the model supply `tenant_id` and validate it afterwards** — a
  validation that can be forgotten in one tool is not a boundary.

## What would be difficult to change later

Point 4. Once a single tool accepts an identifier from model output, every
subsequent tool author reasonably assumes it is allowed, and the invariant
cannot be restored by review.

## What is deliberately not decided here

The concrete schema-validation library; whether tool-call audit is sampled at
high volume (Phase 0 report §19 OD-7); whether the Gateway is also exposed as a
public tenant-facing HTTP API (OD-8); and the eventual shape of the deferred
HTTP/MCP tool classes.

## Related

SaaS-OS ADR-0004 (tool-mediated access), ADR-0013 (external model boundary);
`docs/PHASE-0-ARCHITECTURE.md` §8, §14.
