import { Route, Routes } from "react-router-dom";

import { AppShell } from "./components/shell/AppShell";
import { RequireAuth } from "./components/shell/Guards";
import { NotFoundState } from "./components/states/States";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { AgentsPage } from "./pages/AgentsPage";
import { CalendarPage } from "./pages/CalendarPage";
import { CallDetailPage } from "./pages/CallDetailPage";
import { CallsPage } from "./pages/CallsPage";
import { ContactDetailPage } from "./pages/ContactDetailPage";
import { ContactsPage } from "./pages/ContactsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { FollowUpsPage } from "./pages/FollowUpsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { SettingsPage } from "./pages/SettingsPage";

/**
 * The application root (Phase 2.15). `RequireAuth` gates the entire routed
 * tree on identity; `AppShell` (mounted for every route below it) gates on
 * active context. Every route here is a real, implemented product surface
 * (brief §6: "do not create dead navigation") -- the unmatched fallback
 * shows a not-found state rather than silently rendering nothing.
 */
export function App() {
  return (
    <RequireAuth>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route path="calls" element={<CallsPage />} />
          <Route path="calls/:callId" element={<CallDetailPage />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="agents/:agentId" element={<AgentDetailPage />} />
          <Route path="contacts" element={<ContactsPage />} />
          <Route path="contacts/:contactId" element={<ContactDetailPage />} />
          <Route path="calendar" element={<CalendarPage />} />
          <Route path="follow-ups" element={<FollowUpsPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundState label="Page not found." />} />
        </Route>
      </Routes>
    </RequireAuth>
  );
}
