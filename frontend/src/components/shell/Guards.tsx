/**
 * The two gates every authenticated route passes through, in order:
 * `RequireAuth` (identity), then `AppShell`'s own active-context check
 * (§4's "active context" pillar). Kept as two separate, composable checks
 * rather than one combined gate, matching ADR-0010 §6's own instruction not
 * to collapse identity and active context into a single field/state.
 */
import type { ReactNode } from "react";

import { useSession } from "../../context/SessionContext";
import { beginLogin } from "../../lib/authApi";
import { LoadingState } from "../states/States";

export function RequireAuth({ children }: { children: ReactNode }) {
  const { auth } = useSession();

  if (auth.status === "loading") {
    return <LoadingState label="Checking your session…" />;
  }

  if (auth.status === "unauthenticated") {
    return (
      <div className="unauthenticated-screen">
        <h1>Sign in required</h1>
        <p>You need to sign in to use this application.</p>
        <button type="button" onClick={beginLogin}>
          Sign in
        </button>
      </div>
    );
  }

  return <>{children}</>;
}

export function RequireActiveContext({ children }: { children: ReactNode }) {
  const { activeTenantId } = useSession();

  if (activeTenantId === null) {
    return (
      <div className="state state-empty" role="status">
        Select a tenant context above to continue. See this app's <code>README.md</code> for why
        there is no automatic list to choose from yet.
      </div>
    );
  }

  return <>{children}</>;
}
