/** Phase 2.15 brief §23 "Context": active context initialization, context
 * switching, stale-data invalidation, unauthorized context handling. */
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "../lib/queryClient";
import { SessionProvider, useSession } from "./SessionContext";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  window.sessionStorage.clear();
  queryClient.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

function Probe() {
  const { auth, activeTenantId } = useSession();
  return (
    <div>
      <span data-testid="auth-status">{auth.status}</span>
      <span data-testid="tenant-id">{activeTenantId ?? "none"}</span>
    </div>
  );
}

describe("SessionProvider: identity", () => {
  it("starts loading, then resolves to authenticated on a successful /auth/me", async () => {
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    );
    expect(screen.getByTestId("auth-status").textContent).toBe("loading");
    await waitFor(() => expect(screen.getByTestId("auth-status").textContent).toBe("authenticated"));
  });

  it("resolves to unauthenticated when /auth/me responds 401 (expired/no session)", async () => {
    fetchMock.mockReturnValueOnce(jsonResponse(401, { detail: "Unauthorized." }));
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("auth-status").textContent).toBe("unauthenticated"),
    );
  });

  it("resolves to unauthenticated on a network failure rather than crashing", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("auth-status").textContent).toBe("unauthenticated"),
    );
  });
});

describe("SessionProvider: active context", () => {
  it("initializes with no active context when nothing is stored", async () => {
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    );
    expect(screen.getByTestId("tenant-id").textContent).toBe("none");
  });

  it("restores a previously selected tenant from sessionStorage", async () => {
    window.sessionStorage.setItem("voiceagent.activeTenantId", "tenant-restored");
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    );
    expect(screen.getByTestId("tenant-id").textContent).toBe("tenant-restored");
  });

  it("switching context clears every cached query and updates storage", async () => {
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));
    const clearSpy = vi.spyOn(queryClient, "clear");

    function Switcher() {
      const { setActiveTenantId } = useSession();
      return (
        <button type="button" onClick={() => setActiveTenantId("tenant-b")}>
          switch
        </button>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <Probe />
          <Switcher />
        </SessionProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("auth-status").textContent).toBe("authenticated"));
    act(() => {
      screen.getByText("switch").click();
    });

    expect(clearSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("tenant-id").textContent).toBe("tenant-b");
    expect(window.sessionStorage.getItem("voiceagent.activeTenantId")).toBe("tenant-b");
  });

  it("a query issued under context A is never returned for a query under context B", async () => {
    // Reproduces ADR-0010 §11's own required test: context A -> context B
    // must never leak cached data.
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));

    function ResourceProbe() {
      const { activeTenantId } = useSession();
      const query = useQuery({
        queryKey: ["widgets", activeTenantId],
        queryFn: () => Promise.resolve(`widgets-for-${activeTenantId}`),
        enabled: activeTenantId !== null,
      });
      return <span data-testid="resource">{query.data ?? "none"}</span>;
    }

    function Switcher() {
      const { setActiveTenantId } = useSession();
      return (
        <>
          <button type="button" onClick={() => setActiveTenantId("tenant-a")}>
            a
          </button>
          <button type="button" onClick={() => setActiveTenantId("tenant-b")}>
            b
          </button>
        </>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <ResourceProbe />
          <Switcher />
        </SessionProvider>
      </QueryClientProvider>,
    );

    act(() => screen.getByText("a").click());
    await waitFor(() => expect(screen.getByTestId("resource").textContent).toBe("widgets-for-tenant-a"));

    act(() => screen.getByText("b").click());
    // Immediately after the switch the old value must not still be shown.
    expect(screen.getByTestId("resource").textContent).not.toBe("widgets-for-tenant-a");
    await waitFor(() => expect(screen.getByTestId("resource").textContent).toBe("widgets-for-tenant-b"));
  });

  it("clearing the active context (e.g. logout) removes the stored tenant id", async () => {
    fetchMock.mockReturnValueOnce(jsonResponse(200, { user_id: "user-1" }));
    window.sessionStorage.setItem("voiceagent.activeTenantId", "tenant-a");

    function Clearer() {
      const { setActiveTenantId } = useSession();
      return (
        <button type="button" onClick={() => setActiveTenantId(null)}>
          clear
        </button>
      );
    }

    render(
      <SessionProvider>
        <Probe />
        <Clearer />
      </SessionProvider>,
    );

    act(() => screen.getByText("clear").click());
    expect(screen.getByTestId("tenant-id").textContent).toBe("none");
    expect(window.sessionStorage.getItem("voiceagent.activeTenantId")).toBeNull();
  });
});
