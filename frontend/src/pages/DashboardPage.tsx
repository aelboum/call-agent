/**
 * The initial dashboard (Phase 2.15 brief §7): real data from existing
 * endpoints only. No aggregate/stats endpoint exists anywhere in the
 * backend today, so nothing here is a computed metric -- every section is a
 * bounded slice of an existing list endpoint (recent calls, due follow-ups,
 * upcoming calendar events), exactly what brief §7 asks for when "an
 * aggregate endpoint does not exist."
 */
import { Link } from "react-router-dom";

import { useCallSessions, useCalendarEvents, useFollowUps } from "../lib/hooks";
import { QueryState } from "../components/states/States";

function nowIso(): string {
  return new Date().toISOString();
}

function daysFromNowIso(days: number): string {
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}

export function DashboardPage() {
  const recentCalls = useCallSessions();
  const dueFollowUps = useFollowUps("pending");
  const upcomingEvents = useCalendarEvents(nowIso(), daysFromNowIso(7));

  return (
    <div className="page dashboard-page">
      <h1>Dashboard</h1>
      <div className="dashboard-grid">
        <section>
          <h2>Recent calls</h2>
          <QueryState query={recentCalls} emptyMessage="No calls yet.">
            {(calls) => (
              <ul className="dashboard-list">
                {calls.slice(0, 8).map((call) => (
                  <li key={call.id}>
                    <Link to={`/calls/${call.id}`}>
                      {call.from_e164} → {call.to_e164}
                    </Link>
                    <span className={`badge badge-${call.status}`}>{call.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
          <Link to="/calls">View all calls →</Link>
        </section>

        <section>
          <h2>Pending follow-ups</h2>
          <QueryState query={dueFollowUps} emptyMessage="No pending follow-ups.">
            {(followUps) => (
              <ul className="dashboard-list">
                {followUps.slice(0, 8).map((followUp) => (
                  <li key={followUp.id}>
                    <Link to={`/calls/${followUp.call_session_id}`}>{followUp.type}</Link>
                    <span>{followUp.due_at ? new Date(followUp.due_at).toLocaleString() : "—"}</span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
          <Link to="/follow-ups">View all follow-ups →</Link>
        </section>

        <section>
          <h2>Upcoming calendar activity (next 7 days)</h2>
          <QueryState query={upcomingEvents} emptyMessage="Nothing scheduled in the next 7 days.">
            {(events) => (
              <ul className="dashboard-list">
                {events.slice(0, 8).map((event) => (
                  <li key={event.id}>
                    <span>{event.title}</span>
                    <span>{new Date(event.start_at).toLocaleString()}</span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
          <Link to="/calendar">View calendar →</Link>
        </section>
      </div>
    </div>
  );
}
