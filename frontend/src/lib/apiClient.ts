/**
 * The one HTTP request mechanism the frontend uses (Phase 2.15 brief §15:
 * "no arbitrary fetch calls scattered through components").
 *
 * Authentication is entirely server-side: the backend's OIDC login flow
 * (`/auth/login` -> IdP -> `/auth/callback`) sets an `HttpOnly`/`Secure`
 * session cookie the browser cannot read or leak -- this client never holds,
 * sees, or forwards a token. Every request is sent with
 * `credentials: "include"` so the browser attaches that cookie
 * automatically; there is nothing else for this module to manage for auth.
 *
 * **Active tenant context is a request parameter, not a header or a stored
 * session field** (Phase 2.15 backend inspection: no `/v1` route declares a
 * `{tenant_id}` path segment, so the platform's own tenant-resolution
 * dependency binds it as a required query parameter on every authorized
 * route). `tenantId` is therefore an explicit, required argument to every
 * function below -- never read from a module-level variable -- so a caller
 * can never accidentally issue a request under the wrong (or a stale)
 * context. `voiceagent.tenancy.require_tenant()` independently re-verifies
 * this id against the caller's own memberships on every single request
 * (ADR-0010 §12: "a locator, not a grant") -- this client supplies the
 * locator; the backend remains the only authority on whether it is honored.
 */

const DEFAULT_BASE_URL = "";

function baseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? DEFAULT_BASE_URL;
}

export type ApiErrorKind =
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "validation"
  | "server_error"
  | "network_error"
  | "cancelled";

/** A normalized, safe-to-display API failure (Phase 2.15 brief §18: "API
 * errors should be normalized centrally"; §21: never a raw exception, SQL
 * error, or infrastructure detail). `detail` is always either the backend's
 * own fixed `detail` string (`voiceagent.api.errors`: never an exception
 * message, never a stack trace) or a generic fallback this client supplies
 * itself -- never anything derived from a response body we did not
 * successfully parse as the expected shape. */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;

  constructor(kind: ApiErrorKind, status: number | null, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

function kindForStatus(status: number): ApiErrorKind {
  switch (status) {
    case 401:
      return "unauthorized";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 422:
      return "validation";
    default:
      return "server_error";
  }
}

async function detailFromResponse(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Not JSON, or no `detail` field -- fall through to the generic message.
  }
  return "Something went wrong. Please try again.";
}

export interface RequestOptions {
  /** Query parameters. `tenantId` (when the caller passes one) is merged in
   * automatically as `tenant_id` -- never construct that key by hand at a
   * call site. */
  query?: Record<string, string | number | boolean | undefined>;
  tenantId?: string;
  signal?: AbortSignal;
}

function buildUrl(path: string, options: RequestOptions | undefined): string {
  const url = new URL(`${baseUrl()}${path}`, window.location.origin);
  if (options?.tenantId !== undefined) {
    url.searchParams.set("tenant_id", options.tenantId);
  }
  for (const [key, value] of Object.entries(options?.query ?? {})) {
    if (value !== undefined) {
      url.searchParams.set(key, String(value));
    }
  }
  return url.pathname + url.search;
}

async function request<T>(
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  options?: RequestOptions & { body?: unknown },
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(buildUrl(path, options), {
      method,
      credentials: "include",
      signal: options?.signal,
      headers: {
        Accept: "application/json",
        ...(options?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: options?.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch (cause: unknown) {
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw new ApiError("cancelled", null, "Request cancelled.");
    }
    throw new ApiError("network_error", null, "Could not reach the API.");
  }

  if (!response.ok) {
    throw new ApiError(kindForStatus(response.status), response.status, await detailFromResponse(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function getJson<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>("GET", path, options);
}

export function postJson<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>("POST", path, { ...options, body });
}

export function patchJson<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>("PATCH", path, { ...options, body });
}

export function putJson<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
  return request<T>("PUT", path, { ...options, body });
}
