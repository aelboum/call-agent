/** Calls list (Phase 2.15 brief §8). Respects the API's own pagination
 * (`useCallSessions()` passes `limit`; the backend orders newest-first) and
 * status filter -- no client-side re-implementation of either. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useCallSessions } from "../lib/hooks";
import type { CallStatus } from "../lib/types";

const STATUSES: CallStatus[] = [
  "initiated",
  "ringing",
  "answered",
  "in_progress",
  "completed",
  "failed",
  "interrupted",
];

export function CallsPage() {
  const [status, setStatus] = useState<string>("");
  const calls = useCallSessions(status || undefined);

  return (
    <div className="page calls-page">
      <h1>Calls</h1>
      <label>
        Status
        <select value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">All</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <QueryState query={calls} emptyMessage="No calls match this filter.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>Direction</th>
                <th>From</th>
                <th>To</th>
                <th>Status</th>
                <th>Started</th>
                <th>Ended</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((call) => (
                <tr key={call.id}>
                  <td>{call.direction}</td>
                  <td>{call.from_e164}</td>
                  <td>{call.to_e164}</td>
                  <td>
                    <Link to={`/calls/${call.id}`} className={`badge badge-${call.status}`}>
                      {call.status}
                    </Link>
                  </td>
                  <td>{call.started_at ? new Date(call.started_at).toLocaleString() : "—"}</td>
                  <td>{call.ended_at ? new Date(call.ended_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </div>
  );
}
