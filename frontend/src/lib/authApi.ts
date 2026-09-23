/**
 * The platform's own session endpoints (`api.auth`, mounted by
 * `api.platform.build_platform_app()` -- not a `voiceagent`-owned route, and
 * not under `/v1`). This module is the one place the frontend touches them;
 * everything else goes through `apiClient`/`sessionApi`.
 *
 * There is no second authentication mechanism here (Phase 2.15 brief §16:
 * "do not invent a second authentication mechanism") -- `login()`/`logout()`
 * are thin wrappers over the platform's real OIDC session flow.
 */
import { getJson, postJson } from "./apiClient";

export interface CurrentUser {
  user_id: string;
}

/** `GET /auth/me` returns only `{user_id}` -- no email, no display name, no
 * tenant list (the backend gap Phase 2.15 documents in `README.md`'s
 * "Known backend gaps" section: there is no reverse "which tenants can this
 * user act in" lookup anywhere in the platform yet). A 401 here is the
 * normal "not signed in" state, not a failure this function itself
 * reports -- callers (`SessionProvider`) branch on it explicitly rather
 * than treating every rejection as an error to surface. */
export function fetchCurrentUser(signal?: AbortSignal): Promise<CurrentUser> {
  return getJson<CurrentUser>("/auth/me", { signal });
}

/** Redirects the browser to the platform's real OIDC login flow. Not a
 * `fetch()` call: `/auth/login` itself 303-redirects to the identity
 * provider, which only a full navigation (never `fetch`, which would just
 * follow the redirect invisibly and hand back an opaque response) can
 * complete correctly. */
export function beginLogin(): void {
  window.location.assign("/auth/login");
}

export async function logout(): Promise<void> {
  await postJson<void>("/auth/logout");
}
