/** Follow-ups (Phase 2.15 brief §12): the existing lifecycle, read-only
 * plus the two operator actions the API already exposes (complete/cancel).
 * No client-side workflow engine -- every state transition is a direct
 * call to the existing endpoint; the durable worker remains authoritative. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useCancelFollowUp, useCompleteFollowUp, useFollowUps } from "../lib/hooks";

const STATUSES = ["pending", "processing", "completed", "cancelled", "failed"] as const;

export function FollowUpsPage() {
  const [status, setStatus] = useState<string>("");
  const followUps = useFollowUps(status || undefined);
  const completeFollowUp = useCompleteFollowUp();
  const cancelFollowUp = useCancelFollowUp();

  return (
    <div className="page follow-ups-page">
      <h1>Follow-ups</h1>
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
      <QueryState query={followUps} emptyMessage="No follow-ups match this filter.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Status</th>
                <th>Due</th>
                <th>Call</th>
                <th>Failure</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((followUp) => (
                <tr key={followUp.id}>
                  <td>{followUp.type}</td>
                  <td>
                    <span className={`badge badge-${followUp.status}`}>{followUp.status}</span>
                  </td>
                  <td>{followUp.due_at ? new Date(followUp.due_at).toLocaleString() : "—"}</td>
                  <td>
                    <Link to={`/calls/${followUp.call_session_id}`}>View call</Link>
                  </td>
                  <td>{followUp.failure_reason ?? "—"}</td>
                  <td>
                    {followUp.status === "pending" && (
                      <>
                        <button
                          type="button"
                          disabled={completeFollowUp.isPending}
                          onClick={() => completeFollowUp.mutate({ followUpId: followUp.id })}
                        >
                          Complete
                        </button>{" "}
                        <button
                          type="button"
                          disabled={cancelFollowUp.isPending}
                          onClick={() => cancelFollowUp.mutate({ followUpId: followUp.id })}
                        >
                          Cancel
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </div>
  );
}
