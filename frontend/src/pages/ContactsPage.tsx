/** Contacts list (Phase 2.15 brief §10). No client-side search: the
 * backend has no full-text/fuzzy search endpoint (only exact
 * `GET /contacts/by-phone/{e164}`, not used for this list view) -- a
 * documented gap, not something this page fakes with an unbounded
 * client-side filter over every contact. */
import { Link } from "react-router-dom";

import { QueryState } from "../components/states/States";
import { useContacts } from "../lib/hooks";

export function ContactsPage() {
  const contacts = useContacts();
  return (
    <div className="page contacts-page">
      <h1>Contacts</h1>
      <QueryState query={contacts} emptyMessage="No contacts yet.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Phone</th>
                <th>Email</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((contact) => (
                <tr key={contact.id}>
                  <td>
                    <Link to={`/contacts/${contact.id}`}>{contact.name}</Link>
                  </td>
                  <td>{contact.phone_e164}</td>
                  <td>{contact.email ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </div>
  );
}
