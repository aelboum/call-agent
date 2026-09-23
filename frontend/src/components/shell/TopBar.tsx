/** Header bar: display name, active-context selector, and user controls
 * (Phase 2.15 brief §6: "account/user controls where appropriate"). */
import { config } from "../../config";
import { useSession } from "../../context/SessionContext";
import { logout } from "../../lib/authApi";
import { ContextSelector } from "./ContextSelector";

export function TopBar({ displayName }: { displayName: string | null }) {
  const { auth, refreshIdentity } = useSession();

  async function handleSignOut(): Promise<void> {
    await logout();
    refreshIdentity();
  }

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
            <button type="button" onClick={() => void handleSignOut()}>
              Sign out
            </button>
          </>
        )}
      </div>
    </header>
  );
}
