/**
 * Knowledge management (Phase 2.15 brief §14). Respects the backend's
 * immutability model: only a `draft` item exposes an action here
 * (`activate`), and only an `active` one exposes `deactivate` -- there is
 * no edit action for anything else, matching `update_item()`'s own
 * `KnowledgeItemNotDraftError` (a non-draft item is not editable server
 * -side, so this page never offers to edit one).
 */
import { useState } from "react";

import { QueryState } from "../components/states/States";
import { useKnowledgeItems, useKnowledgeSources } from "../lib/hooks";

export function KnowledgePage() {
  const sources = useKnowledgeSources();
  const [selectedSourceId, setSelectedSourceId] = useState<string | undefined>(undefined);
  const items = useKnowledgeItems(selectedSourceId);

  return (
    <div className="page knowledge-page">
      <h1>Knowledge</h1>
      <div className="knowledge-grid">
        <section>
          <h2>Sources</h2>
          <QueryState query={sources} emptyMessage="No knowledge sources yet.">
            {(rows) => (
              <ul className="dashboard-list">
                {rows.map((source) => (
                  <li key={source.id}>
                    <button
                      type="button"
                      className={source.id === selectedSourceId ? "selected" : undefined}
                      onClick={() => setSelectedSourceId(source.id)}
                    >
                      {source.name}
                    </button>
                    <span className={`badge badge-${source.status}`}>{source.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </section>

        <section>
          <h2>Items{selectedSourceId ? "" : " (select a source)"}</h2>
          {selectedSourceId && (
            <QueryState query={items} emptyMessage="No items in this source yet.">
              {(rows) => (
                <table>
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((item) => (
                      <tr key={item.id}>
                        <td>{item.title}</td>
                        <td>
                          <span className={`badge badge-${item.status}`}>{item.status}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </QueryState>
          )}
        </section>
      </div>
    </div>
  );
}
