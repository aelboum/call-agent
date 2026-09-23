/**
 * The authenticated application shell (Phase 2.15 brief §6): nav, active
 * context, header, and a content outlet. Responsive via CSS only (`styles
 * .css`'s `app-shell` rules) -- no separate mobile component tree.
 */
import { Outlet } from "react-router-dom";

import { useMeta } from "../../lib/hooks";
import { RequireActiveContext } from "./Guards";
import { Nav } from "./Nav";
import { TopBar } from "./TopBar";

export function AppShell() {
  const meta = useMeta();
  return (
    <div className="app-shell">
      <TopBar displayName={meta.data?.name ?? null} />
      <div className="app-shell-body">
        <Nav />
        <main className="app-content">
          <RequireActiveContext>
            <Outlet />
          </RequireActiveContext>
        </main>
      </div>
    </div>
  );
}
