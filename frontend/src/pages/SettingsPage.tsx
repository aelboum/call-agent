/** Settings/context (Phase 2.15 brief §6/§30.E): the identity and active
 * context this session is running under, plus phone number configuration
 * (the one remaining backend domain with no more specific home in the
 * navigation). */
import { useSession } from "../context/SessionContext";
import { QueryState } from "../components/states/States";
import { usePhoneNumbers } from "../lib/hooks";

export function SettingsPage() {
  const { auth, activeTenantId } = useSession();
  const phoneNumbers = usePhoneNumbers();

  return (
    <div className="page settings-page">
      <h1>Settings</h1>
      <section>
        <h2>Session</h2>
        <dl className="detail-grid">
          <dt>User ID</dt>
          <dd>{auth.status === "authenticated" ? auth.user.user_id : "—"}</dd>
          <dt>Active tenant context</dt>
          <dd>{activeTenantId ?? "None selected"}</dd>
        </dl>
      </section>

      <section>
        <h2>Phone numbers</h2>
        <QueryState query={phoneNumbers} emptyMessage="No phone numbers registered.">
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>Number</th>
                  <th>Label</th>
                  <th>Inbound</th>
                  <th>Version pin</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((number) => (
                  <tr key={number.id}>
                    <td>{number.e164}</td>
                    <td>{number.label ?? "—"}</td>
                    <td>{number.inbound_enabled ? "Enabled" : "Disabled"}</td>
                    <td>{number.version_pin_mode}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </section>
    </div>
  );
}
