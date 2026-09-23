/**
 * Contact detail (Phase 2.15 brief §10). Shows only the contact's own
 * fields. **No call history / follow-up / calendar relationship shown
 * here** -- the backend has no "calls for this contact" or "follow-ups for
 * this contact" query (`voiceagent.calls.service.list_call_sessions()`
 * filters by `status` only, never `contact_id`), and building that by
 * downloading every call and filtering client-side would be exactly the
 * unbounded client-side join brief §10 forbids. A documented gap
 * (`README.md`), not a workaround.
 */
import { useParams } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useContact } from "../lib/hooks";

export function ContactDetailPage() {
  const { contactId } = useParams<{ contactId: string }>();
  const contact = useContact(contactId);

  return (
    <div className="page contact-detail-page">
      <h1>Contact detail</h1>
      <QueryState query={contact}>
        {(row) => (
          <dl className="detail-grid">
            <dt>Name</dt>
            <dd>{row.name}</dd>
            <dt>Phone</dt>
            <dd>{row.phone_e164}</dd>
            <dt>Email</dt>
            <dd>{row.email ?? "—"}</dd>
          </dl>
        )}
      </QueryState>
    </div>
  );
}
