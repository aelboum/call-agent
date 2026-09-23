/**
 * The active-context selector (Phase 2.15 brief §5: "current context is
 * visible", "context can be changed without full-page reload where
 * practical").
 *
 * **Manual entry, not a dropdown of authorized tenants** -- and that is a
 * documented backend gap, not a shortcut: no endpoint anywhere in this
 * product (or in the platform's own `core.identity`/`core.tenancy`) can
 * enumerate which tenants the current user may act in (see `README.md`'s
 * "Known backend gaps"). Brief §5 is explicit about the correct response to
 * that: "If the backend currently lacks a safe endpoint needed to
 * enumerate/select contexts, do not invent a client-side workaround.
 * Document the backend dependency/gap instead." This selector does not
 * pretend to authorize anything -- typing an id you do not have access to
 * simply makes every subsequent request 404 (`voiceagent.tenancy
 * .require_tenant()`'s own non-enumerating failure mode), exactly as if you
 * had typed it directly into a `tenant_id` query parameter yourself.
 */
import { useState } from "react";

import { useSession } from "../../context/SessionContext";

export function ContextSelector() {
  const { activeTenantId, setActiveTenantId } = useSession();
  const [draft, setDraft] = useState(activeTenantId ?? "");

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const trimmed = draft.trim();
    setActiveTenantId(trimmed.length > 0 ? trimmed : null);
  }

  return (
    <form className="context-selector" onSubmit={handleSubmit} aria-label="Active tenant context">
      <label htmlFor="active-tenant-id">Tenant context</label>
      <input
        id="active-tenant-id"
        type="text"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Tenant ID"
        spellCheck={false}
        autoComplete="off"
      />
      <button type="submit">Use</button>
      {activeTenantId && (
        <span className="context-selector-active" title={activeTenantId}>
          Active: {activeTenantId.slice(0, 8)}…
        </span>
      )}
    </form>
  );
}
