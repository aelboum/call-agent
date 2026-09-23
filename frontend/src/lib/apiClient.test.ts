import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getJson, postJson } from "./apiClient";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("getJson", () => {
  it("sends credentials so the platform session cookie is attached", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    await getJson("/v1/meta");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("include");
  });

  it("appends tenant_id as a query parameter, never a header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    await getJson("/v1/agents", { tenantId: "tenant-a" });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("tenant_id=tenant-a");
  });

  it("merges additional query parameters and omits undefined ones", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    await getJson("/v1/call-sessions", {
      tenantId: "tenant-a",
      query: { status: "completed", limit: 50, offset: undefined },
    });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("status=completed");
    expect(url).toContain("limit=50");
    expect(url).not.toContain("offset");
  });

  it("returns parsed JSON on success", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { name: "Console" }));
    await expect(getJson("/v1/meta")).resolves.toEqual({ name: "Console" });
  });

  it.each([
    [401, "unauthorized"],
    [403, "forbidden"],
    [404, "not_found"],
    [409, "conflict"],
    [422, "validation"],
    [500, "server_error"],
  ] as const)("normalizes a %i response to kind %s", async (status, kind) => {
    fetchMock.mockResolvedValueOnce(jsonResponse(status, { detail: "backend said so" }));
    await expect(getJson("/v1/agents/x")).rejects.toMatchObject({ kind, status, message: "backend said so" });
  });

  it("falls back to a generic message when the body has no detail field", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("not json", { status: 500 }),
    );
    const error = await getJson("/v1/agents/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe("Something went wrong. Please try again.");
  });

  it("never surfaces a raw exception/stack trace as the error message", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(500, {
        detail: "Internal Server Error",
        traceback: "Traceback (most recent call last): ...",
      }),
    );
    const error = (await getJson("/v1/agents/x").catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe("Internal Server Error");
    expect(error.message).not.toContain("Traceback");
  });

  it("maps a network failure to a network_error kind", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const error = (await getJson("/v1/meta").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe("network_error");
  });

  it("maps an aborted request to a cancelled kind, not a generic error", async () => {
    fetchMock.mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
    const error = (await getJson("/v1/meta").catch((e: unknown) => e)) as ApiError;
    expect(error.kind).toBe("cancelled");
  });
});

describe("postJson", () => {
  it("sends a JSON body and Content-Type header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { id: "1" }));
    await postJson("/v1/agents", { name: "New agent" }, { tenantId: "tenant-a" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(init.body).toBe(JSON.stringify({ name: "New agent" }));
  });
});
