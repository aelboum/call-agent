/** Agents list (Phase 2.15 brief §9). */
import { Link } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useAgents } from "../lib/hooks";

export function AgentsPage() {
  const agents = useAgents();
  return (
    <div className="page agents-page">
      <h1>Agents</h1>
      <QueryState query={agents} emptyMessage="No agents yet.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Published version</th>
                <th>Draft version</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((agent) => (
                <tr key={agent.id}>
                  <td>
                    <Link to={`/agents/${agent.id}`}>{agent.name}</Link>
                  </td>
                  <td>
                    <span className={`badge badge-${agent.status}`}>{agent.status}</span>
                  </td>
                  <td>{agent.published_version_id ? "Published" : "None"}</td>
                  <td>{agent.draft_version_id ? "Draft pending" : "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </div>
  );
}
