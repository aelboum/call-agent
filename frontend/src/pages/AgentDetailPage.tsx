/**
 * Agent detail (Phase 2.15 brief §9). Shows exactly the lifecycle pointers
 * the backend exposes (`draft_version_id`/`published_version_id`) and lets
 * an authorized caller publish the current draft -- never implies a
 * published version can be edited: there is no "edit version" endpoint to
 * call, and this page adds no client-side pretense of one.
 *
 * **No version history list.** `voiceagent.agents` exposes no "list this
 * agent's versions" endpoint (only create/publish/archive by id) -- a
 * documented gap (`README.md`), not something this page fabricates by
 * guessing at version numbers.
 */
import { useParams } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useAgent, usePublishAgentVersion } from "../lib/hooks";

export function AgentDetailPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const agent = useAgent(agentId);
  const publish = usePublishAgentVersion();

  return (
    <div className="page agent-detail-page">
      <h1>Agent detail</h1>
      <QueryState query={agent}>
        {(row) => (
          <>
            <dl className="detail-grid">
              <dt>Name</dt>
              <dd>{row.name}</dd>
              <dt>Description</dt>
              <dd>{row.description ?? "—"}</dd>
              <dt>Status</dt>
              <dd>
                <span className={`badge badge-${row.status}`}>{row.status}</span>
              </dd>
              <dt>Published version</dt>
              <dd>{row.published_version_id ?? "None"}</dd>
              <dt>Draft version</dt>
              <dd>{row.draft_version_id ?? "None"}</dd>
            </dl>
            {row.draft_version_id && (
              <button
                type="button"
                disabled={publish.isPending}
                onClick={() =>
                  agentId &&
                  row.draft_version_id &&
                  publish.mutate({ agentId, versionId: row.draft_version_id })
                }
              >
                {publish.isPending ? "Publishing…" : "Publish draft version"}
              </button>
            )}
            {publish.isError && (
              <p role="alert">{publish.error instanceof Error ? publish.error.message : "Failed to publish."}</p>
            )}
          </>
        )}
      </QueryState>
    </div>
  );
}
