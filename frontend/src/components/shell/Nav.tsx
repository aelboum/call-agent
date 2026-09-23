/**
 * Primary navigation (Phase 2.15 brief §6): exactly the implemented
 * product surfaces, no dead links to hypothetical future modules.
 *
 * **Not capability-filtered.** There is no backend endpoint reporting what
 * the current principal may do (`SessionContext.tsx`'s own docstring), so
 * this list cannot safely be narrowed per-user without either guessing or
 * duplicating RBAC in TypeScript -- both of which brief §17 forbids. Every
 * implemented area is always listed; an unauthorized visit fails through
 * the real API response instead (`components/states/PermissionDeniedState`).
 * Visibility here is navigation, not a security boundary (ADR-0010 §9).
 */
import { NavLink } from "react-router-dom";

const ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/calls", label: "Calls" },
  { to: "/agents", label: "Agents" },
  { to: "/contacts", label: "Contacts" },
  { to: "/calendar", label: "Calendar" },
  { to: "/follow-ups", label: "Follow-ups" },
  { to: "/knowledge", label: "Knowledge" },
  { to: "/settings", label: "Settings" },
] as const;

export function Nav() {
  return (
    <nav className="app-nav" aria-label="Primary">
      <ul>
        {ITEMS.map((item) => (
          <li key={item.to}>
            <NavLink to={item.to} end={"end" in item ? item.end : false}>
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
