/** Shared test helpers: a deterministic `SessionContext` value (never the
 * real provider's async `/auth/me` round trip) and a fetch-mocking utility
 * that lets a test script exact HTTP responses. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

import { SessionContext, type SessionContextValue } from "../context/SessionContext";

export function makeSessionValue(overrides: Partial<SessionContextValue> = {}): SessionContextValue {
  return {
    auth: { status: "authenticated", user: { user_id: "11111111-1111-1111-1111-111111111111" } },
    activeTenantId: "22222222-2222-2222-2222-222222222222",
    setActiveTenantId: vi.fn(),
    refreshIdentity: vi.fn(),
    signOut: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

/** `path` is the route *pattern* (e.g. `/calls/:callId`, defaulting to
 * `route` itself for pages with no params) -- needed so a page that calls
 * `useParams()` actually receives one. */
export function renderWithProviders(
  ui: ReactElement,
  {
    session = makeSessionValue(),
    route = "/",
    path = route,
  }: { session?: SessionContextValue; route?: string; path?: string } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={session}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path={path} element={ui} />
          </Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

/** Queues one `fetch` response. Call once per expected request, in order. */
export function mockFetchOnce(status: number, body?: unknown, headers: Record<string, string> = {}) {
  const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementationOnce(
    async () =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json", ...headers },
      }),
  );
}

export function installFetchMock() {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
