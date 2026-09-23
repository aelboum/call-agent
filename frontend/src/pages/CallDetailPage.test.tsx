/** Phase 2.15 brief §23 "Core screens" (AI analysis) and §23
 * "Security/privacy": safe rendering of AI-generated content, no sensitive
 * data in persistent browser storage or the URL.
 */
import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installFetchMock, renderWithProviders } from "../test/testUtils";
import { CallDetailPage } from "./CallDetailPage";

const CALL: Record<string, unknown> = {
  id: "call-1",
  direction: "inbound",
  status: "completed",
  from_e164: "+15551234567",
  to_e164: "+15559876543",
  phone_number_id: "pn-1",
  agent_id: "agent-1",
  agent_version_id: "av-1",
  contact_id: null,
  started_at: "2026-01-01T00:00:00Z",
  answered_at: "2026-01-01T00:00:01Z",
  ended_at: "2026-01-01T00:05:00Z",
  duration_ms: 300000,
  hangup_cause: "normal",
  end_reason: "completed",
  created_at: "2026-01-01T00:00:00Z",
};

function mockFetchByPath(responses: Array<[string, number, unknown]>) {
  const fetchMock = installFetchMock();
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    const match = responses.find(([path]) => url.includes(path));
    if (!match) {
      return new Response(JSON.stringify({ detail: `unmocked: ${url}` }), { status: 500 });
    }
    const [, status, body] = match;
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return fetchMock;
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CallDetailPage: AI analysis", () => {
  it("shows a 'not yet generated' state and a rebuild action when no analysis exists", async () => {
    mockFetchByPath([
      ["/ai-analysis", 404, { detail: "Not found." }],
      ["/outcome", 404, { detail: "Not found." }],
      ["/analysis", 404, { detail: "Not found." }],
      ["/workflow-execution", 404, { detail: "Not found." }],
      ["/conversation", 200, []],
      [`/call-sessions/call-1`, 200, CALL],
    ]);
    renderWithProviders(<CallDetailPage />, { route: "/calls/call-1", path: "/calls/:callId" });
    await waitFor(() =>
      expect(screen.getByText(/no ai analysis has been generated/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /rebuild analysis/i })).toBeInTheDocument();
  });

  it("renders a completed analysis result as plain text, never as executed HTML", async () => {
    const maliciousSummary = "<img src=x onerror=alert('xss')>Customer called about billing";
    mockFetchByPath([
      [
        "/ai-analysis",
        200,
        {
          id: "analysis-1",
          call_session_id: "call-1",
          version: 1,
          status: "completed",
          schema_version: "1",
          prompt_version: "1",
          provider: "fake",
          model: "fake-model",
          attempt_count: 1,
          requested_at: "2026-01-01T00:00:00Z",
          completed_at: "2026-01-01T00:01:00Z",
          failure_reason: null,
          result: {
            summary: maliciousSummary,
            customer_intent: "billing question",
            key_topics: ["billing"],
            action_items: ["Follow up with invoice"],
            escalation: { required: false, reason: null },
            sentiment: { overall: "neutral" },
            confidence: 0.87,
          },
        },
      ],
      ["/outcome", 404, { detail: "Not found." }],
      ["/analysis", 404, { detail: "Not found." }],
      ["/workflow-execution", 404, { detail: "Not found." }],
      ["/conversation", 200, []],
      [`/call-sessions/call-1`, 200, CALL],
    ]);
    renderWithProviders(<CallDetailPage />, { route: "/calls/call-1", path: "/calls/:callId" });

    await waitFor(() => expect(screen.getByText(maliciousSummary)).toBeInTheDocument());
    // Rendered as inert text content, not injected markup -- no <img> element
    // was created, and therefore its onerror handler never ran.
    expect(document.querySelectorAll("img").length).toBe(0);
    expect(screen.getByText("Follow up with invoice")).toBeInTheDocument();
    expect(screen.getByText(/confidence: 87%/i)).toBeInTheDocument();
  });
});

describe("CallDetailPage: privacy", () => {
  it("never writes conversation content to localStorage", async () => {
    mockFetchByPath([
      ["/ai-analysis", 404, { detail: "Not found." }],
      ["/outcome", 404, { detail: "Not found." }],
      ["/analysis", 404, { detail: "Not found." }],
      ["/workflow-execution", 404, { detail: "Not found." }],
      [
        "/conversation",
        200,
        [
          {
            id: "turn-1",
            sequence: 1,
            role: "user",
            content: "My social security number is 123-45-6789",
            tool_payload: null,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
      ],
      [`/call-sessions/call-1`, 200, CALL],
    ]);
    renderWithProviders(<CallDetailPage />, { route: "/calls/call-1", path: "/calls/:callId" });

    await waitFor(() =>
      expect(screen.getByText("My social security number is 123-45-6789")).toBeInTheDocument(),
    );

    // The transcript is on screen (an authorized, in-memory render) but must
    // never have been written to persistent storage.
    expect(window.localStorage.length).toBe(0);
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      const value = key ? window.sessionStorage.getItem(key) : null;
      expect(value ?? "").not.toContain("123-45-6789");
    }
    // Nor into the URL.
    expect(window.location.href).not.toContain("123-45-6789");
  });
});
