/**
 * The one context mechanism this application has (Phase 2.15 brief §4:
 * "Create one clear context mechanism/store/provider for the active tenant
 * context" -- "do not encode tenant IDs throughout arbitrary component
 * state"). Every other part of the app reads identity and active context
 * through `useSession()`; nothing else stores either independently.
 *
 * This provider distinguishes exactly the pillars ADR-0010 §6 names:
 *
 * - **Identity** (`auth`): who is authenticated, from the platform's own
 *   `GET /auth/me` (`user_id` only -- see `authApi.ts`).
 * - **Active context** (`activeTenantId`): the one tenant this session is
 *   currently acting in.
 * - **Capabilities**: deliberately *not* modeled here. No backend endpoint
 *   reports "what can the current principal do" (Phase 2.15 inspection: no
 *   such route exists anywhere in `voiceagent/api/v1/`) -- inventing a
 *   capability flag on the frontend without a backend source for it would
 *   mean either fabricating authorization data or silently duplicating
 *   `core.rbac`'s own decisions in TypeScript, both of which brief §17
 *   forbids ("never rely on frontend checks to protect data"; "do not
 *   duplicate the entire RBAC hierarchy"). See `README.md`'s "Known backend
 *   gaps" section. The UI instead reacts to a real 403 from the API
 *   (`components/states/PermissionDeniedState.tsx`), which is the backend's
 *   own authoritative answer, not a guess.
 * - **Application data**: not this provider's concern at all -- every
 *   resource hook in `lib/hooks.ts` fetches it, scoped by `activeTenantId`
 *   through `queryClient.contextKey()`.
 *
 * **No "available contexts" list.** The backend has no reverse "which
 * tenants can this user act in" lookup (Phase 2.15 inspection: neither
 * `core.identity`, `core.tenancy`, nor `core.rbac` exposes one). This
 * provider therefore cannot offer a dropdown of authorized tenants -- only
 * a manual-entry selector (`components/shell/ContextSelector.tsx`). Setting
 * `activeTenantId` here is never itself an authorization decision: every
 * subsequent request still carries it only as `tenant_id` on the wire, and
 * `voiceagent.tenancy.require_tenant()` independently re-verifies real
 * membership on every request, failing closed (404, matching "no such
 * tenant") if the id is wrong or unauthorized -- exactly ADR-0010 §12's "a
 * locator, not a grant."
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { fetchCurrentUser, type CurrentUser } from "../lib/authApi";
import { queryClient } from "../lib/queryClient";

const ACTIVE_TENANT_STORAGE_KEY = "voiceagent.activeTenantId";

export type AuthState =
  | { status: "loading" }
  | { status: "unauthenticated" }
  | { status: "authenticated"; user: CurrentUser };

interface SessionContextValue {
  auth: AuthState;
  activeTenantId: string | null;
  setActiveTenantId: (tenantId: string | null) => void;
  refreshIdentity: () => void;
}

// Exported (not just `SessionProvider`/`useSession`) so tests can render a
// subtree under a fully deterministic, synchronous value -- never through
// the real provider's async `/auth/me` round trip, which is exactly the
// kind of network dependency a hermetic test must not have.
export const SessionContext = createContext<SessionContextValue | null>(null);
export type { SessionContextValue };

function readStoredTenantId(): string | null {
  // A tenant id is an opaque locator, not a secret (ADR-0010 §12) -- safe in
  // `sessionStorage`, unlike anything from Phase 2.15 brief §19's list
  // (transcripts, tokens, prompts, ...), none of which this provider ever
  // touches. `sessionStorage` (not `localStorage`) so it does not outlive
  // the browser tab/session, and reads/writes are wrapped: storage access
  // can throw (private browsing, blocked site data), and absence is always
  // a valid state, never a fatal one.
  try {
    return window.sessionStorage.getItem(ACTIVE_TENANT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredTenantId(tenantId: string | null): void {
  try {
    if (tenantId) {
      window.sessionStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, tenantId);
    } else {
      window.sessionStorage.removeItem(ACTIVE_TENANT_STORAGE_KEY);
    }
  } catch {
    // Non-fatal: the active context still changes for this page's lifetime.
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });
  const [activeTenantId, setActiveTenantIdState] = useState<string | null>(readStoredTenantId);
  const [refreshCounter, setRefreshCounter] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setAuth({ status: "loading" });
    fetchCurrentUser(controller.signal)
      .then((user) => setAuth({ status: "authenticated", user }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // Any failure here -- 401 (not signed in), a network error, an
        // expired session -- is the same UI state: not authenticated. This
        // provider does not distinguish "definitely logged out" from
        // "could not confirm" because the app behaves identically either
        // way: show the sign-in entry point, never a broken authenticated
        // shell.
        setAuth({ status: "unauthenticated" });
      });
    return () => controller.abort();
  }, [refreshCounter]);

  function setActiveTenantId(tenantId: string | null): void {
    // ADR-0010 §7/§11: a context switch must invalidate every
    // context-sensitive query rather than let the previous tenant's cached
    // data answer a request issued under the new one. Clearing the whole
    // cache is the simplest implementation that actually guarantees this --
    // every entry is already keyed by the *previous* tenant id
    // (`contextKey()`), so none of it could be reused under the new one
    // regardless; clearing also stops an in-flight request for the old
    // context from writing a result a still-mounted component might read.
    queryClient.clear();
    writeStoredTenantId(tenantId);
    setActiveTenantIdState(tenantId);
  }

  const value = useMemo<SessionContextValue>(
    () => ({
      auth,
      activeTenantId,
      setActiveTenantId,
      refreshIdentity: () => setRefreshCounter((n) => n + 1),
    }),
    // `setActiveTenantId`/`refreshIdentity` close only over stable module
    // references and state setters, so omitting them here does not risk a
    // stale closure -- it only avoids giving every render a new context
    // value.
    [auth, activeTenantId],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useSession() must be used within a SessionProvider");
  }
  return context;
}
