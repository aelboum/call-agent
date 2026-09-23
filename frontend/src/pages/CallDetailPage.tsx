/**
 * Call detail (Phase 2.15 brief §8/§13): metadata, outcome, deterministic
 * analysis, AI post-call analysis (with rebuild), workflow execution, and
 * the durable conversation transcript -- every one of them behind its own
 * already-authorized endpoint (`voiceagent.conversations.permissions
 * .RESOURCE`, a narrower grant than call metadata itself, brief §8: "do not
 * bypass privacy/data-access controls").
 *
 * The transcript is rendered as plain text nodes only (`{turn.content}`),
 * never `dangerouslySetInnerHTML` -- brief §21: "AI-generated content must
 * never be treated as trusted HTML by default." Nothing on this page is
 * ever written to `localStorage`/`sessionStorage`, and none of it appears
 * in a URL beyond the call's own opaque id (brief §19).
 */
import { useParams } from "react-router-dom";

import { OptionalQueryState, QueryState } from "../components/states/States";
import {
  useCallAiAnalysis,
  useCallAnalysis,
  useCallOutcome,
  useCallSession,
  useCallWorkflowExecution,
  useConversationTurns,
  useRebuildCallAiAnalysis,
} from "../lib/hooks";

export function CallDetailPage() {
  const { callId } = useParams<{ callId: string }>();
  const call = useCallSession(callId);
  const outcome = useCallOutcome(callId);
  const analysis = useCallAnalysis(callId);
  const aiAnalysis = useCallAiAnalysis(callId);
  const workflowExecution = useCallWorkflowExecution(callId);
  const conversation = useConversationTurns(callId);
  const rebuildAiAnalysis = useRebuildCallAiAnalysis();

  return (
    <div className="page call-detail-page">
      <h1>Call detail</h1>
      <QueryState query={call}>
        {(row) => (
          <dl className="detail-grid">
            <dt>Direction</dt>
            <dd>{row.direction}</dd>
            <dt>From</dt>
            <dd>{row.from_e164}</dd>
            <dt>To</dt>
            <dd>{row.to_e164}</dd>
            <dt>Status</dt>
            <dd>
              <span className={`badge badge-${row.status}`}>{row.status}</span>
            </dd>
            <dt>Started</dt>
            <dd>{row.started_at ? new Date(row.started_at).toLocaleString() : "—"}</dd>
            <dt>Ended</dt>
            <dd>{row.ended_at ? new Date(row.ended_at).toLocaleString() : "—"}</dd>
            <dt>Hangup cause</dt>
            <dd>{row.hangup_cause ?? "—"}</dd>
          </dl>
        )}
      </QueryState>

      <section>
        <h2>Outcome</h2>
        <OptionalQueryState query={outcome} notYetMessage="No outcome recorded for this call yet.">
          {(row) => (
            <p>
              <strong>{row.outcome}</strong>
              {row.notes ? ` — ${row.notes}` : null}
            </p>
          )}
        </OptionalQueryState>
      </section>

      <section>
        <h2>Deterministic analysis</h2>
        <OptionalQueryState query={analysis} notYetMessage="No analysis built for this call yet.">
          {(row) => (
            <dl className="detail-grid">
              <dt>Turns</dt>
              <dd>{row.turn_count}</dd>
              <dt>Duration</dt>
              <dd>{row.duration_ms !== null ? `${Math.round(row.duration_ms / 1000)}s` : "—"}</dd>
              <dt>Follow-ups</dt>
              <dd>{row.follow_up_count}</dd>
              <dt>Transferred</dt>
              <dd>{row.had_transfer ? "Yes" : "No"}</dd>
            </dl>
          )}
        </OptionalQueryState>
      </section>

      <section>
        <h2>AI post-call analysis</h2>
        {/* The rebuild action is offered whether or not a (possibly failed)
            analysis row already exists -- brief §13: "rebuild semantics" --
            so it appears both in the "not yet generated" empty state and
            alongside an existing result below. */}
        <OptionalQueryState
          query={aiAnalysis}
          notYetMessage={
            <>
              No AI analysis has been generated for this call yet.{" "}
              {callId && <RebuildAiAnalysisButton callId={callId} mutation={rebuildAiAnalysis} />}
            </>
          }
        >
          {(row) => (
            <div>
              <p>
                Status: <span className={`badge badge-${row.status}`}>{row.status}</span> (version{" "}
                {row.version})
              </p>
              {row.result && (
                <>
                  <p>{row.result.summary}</p>
                  <p>
                    <strong>Intent:</strong> {row.result.customer_intent}
                  </p>
                  {row.result.key_topics.length > 0 && (
                    <p>
                      <strong>Topics:</strong> {row.result.key_topics.join(", ")}
                    </p>
                  )}
                  {row.result.action_items.length > 0 && (
                    <ul>
                      {row.result.action_items.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  )}
                  {row.result.escalation.required && (
                    <p className="badge badge-escalation">Escalation recommended</p>
                  )}
                  {row.result.sentiment && <p>Sentiment: {row.result.sentiment.overall}</p>}
                  <p>Confidence: {Math.round(row.result.confidence * 100)}%</p>
                </>
              )}
              {row.failure_reason && <p role="alert">Failed: {row.failure_reason}</p>}
              {callId && <RebuildAiAnalysisButton callId={callId} mutation={rebuildAiAnalysis} />}
            </div>
          )}
        </OptionalQueryState>
      </section>

      <section>
        <h2>Workflow execution</h2>
        <OptionalQueryState
          query={workflowExecution}
          notYetMessage="No workflow execution recorded for this call."
        >
          {(row) => (
            <p>
              <span className={`badge badge-${row.status}`}>{row.status}</span> at step{" "}
              {row.current_step_id} ({row.steps_executed} steps executed)
            </p>
          )}
        </OptionalQueryState>
      </section>

      <section>
        <h2>Conversation</h2>
        <QueryState query={conversation} emptyMessage="No conversation turns recorded.">
          {(turns) => (
            <ol className="conversation-transcript">
              {turns.map((turn) => (
                <li key={turn.id} className={`turn turn-${turn.role}`}>
                  <span className="turn-role">{turn.role}</span>
                  <span className="turn-content">{turn.content ?? "(no text content)"}</span>
                </li>
              ))}
            </ol>
          )}
        </QueryState>
      </section>
    </div>
  );
}

function RebuildAiAnalysisButton({
  callId,
  mutation,
}: {
  callId: string;
  mutation: ReturnType<typeof useRebuildCallAiAnalysis>;
}) {
  return (
    <button
      type="button"
      disabled={mutation.isPending}
      onClick={() => mutation.mutate({ callSessionId: callId })}
    >
      {mutation.isPending ? "Requesting…" : "Rebuild analysis"}
    </button>
  );
}
