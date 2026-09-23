/**
 * Header bar: display name, active-context selector, and user controls
 * (Phase 2.15 brief §6: "account/user controls where appropriate").
 *
 * Sign-out goes through `useSession().signOut()` -- never a bare call to
 * `authApi.logout()` here -- specifically so a shared/kiosk browser never
 * retains the previous user's active tenant id or cached query results
 * after they sign out (Phase 2.16 security audit finding: see
 * `SessionContext.tsx`'s own `signOut()` docstring).
 */
import { config } from "../../config";
import { useSession } from "../../context/SessionContext";
import { ContextSelector } from "./ContextSelector";

export function TopBar({ displayName }: { displayName: string | null }) {
  const { auth, signOut } = useSession();

  return (
    <header className="app-topbar">
      <span className="app-topbar-name">{displayName ?? config.fallbackDisplayName}</span>
      <ContextSelector />
      <div className="app-topbar-user">
        {auth.status === "authenticated" && (
          <>
            <span className="app-topbar-user-id" title={auth.user.user_id}>
              {auth.user.user_id.slice(0, 8)}…
            </span>
            <button type="button" onClick={() => void signOut()}>
              Sign out
            </button>
          </>
        )}
      </div>
    </header>
  );
}
