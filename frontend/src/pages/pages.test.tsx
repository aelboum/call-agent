/** Phase 2.15 brief §23 "Core screens": calls, agents, contacts, calendar,
 * follow-ups, knowledge (AI analysis is covered in `CallDetailPage.test
 * .tsx`, which also exercises the richer authorization/rendering cases).
 * Each test here proves the same minimal contract: the page renders real
 * fetched data through the one API client, under the active tenant
 * context.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installFetchMock, mockFetchOnce, renderWithProviders } from "../test/testUtils";
import { AgentDetailPage } from "./AgentDetailPage";
import { AgentsPage } from "./AgentsPage";
import { CalendarPage } from "./CalendarPage";
import { CallsPage } from "./CallsPage";
import { ContactsPage } from "./ContactsPage";
import { FollowUpsPage } from "./FollowUpsPage";
import { KnowledgePage } from "./KnowledgePage";

beforeEach(() => {
  installFetchMock();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CallsPage", () => {
  it("renders fetched calls", async () => {
    mockFetchOnce(200, [
      {
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
      },
    ]);
    renderWithProviders(<CallsPage />);
    await waitFor(() => expect(screen.getByText("+15551234567")).toBeInTheDocument());
  });

  it("shows a permission-denied state when the API returns 403", async () => {
    mockFetchOnce(403, { detail: "Forbidden." });
    renderWithProviders(<CallsPage />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/permission/i);
  });
});

describe("AgentsPage", () => {
  it("renders fetched agents", async () => {
    mockFetchOnce(200, [
      {
        id: "agent-1",
        name: "Front Desk Agent",
        description: null,
        status: "active",
        draft_version_id: null,
        published_version_id: "v-1",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    renderWithProviders(<AgentsPage />);
    await waitFor(() => expect(screen.getByText("Front Desk Agent")).toBeInTheDocument());
  });
});

describe("AgentDetailPage: authorization UX (allowed vs. hidden action)", () => {
  it("hides the publish action when there is no draft version to publish", async () => {
    mockFetchOnce(200, {
      id: "agent-1",
      name: "No Draft Agent",
      description: null,
      status: "active",
      draft_version_id: null,
      published_version_id: "v-1",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    renderWithProviders(<AgentDetailPage />, { route: "/agents/agent-1", path: "/agents/:agentId" });
    await waitFor(() => expect(screen.getByText("No Draft Agent")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /publish draft version/i })).not.toBeInTheDocument();
  });

  it("shows the publish action when a draft version exists", async () => {
    mockFetchOnce(200, {
      id: "agent-1",
      name: "Has Draft Agent",
      description: null,
      status: "active",
      draft_version_id: "v-2",
      published_version_id: "v-1",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    renderWithProviders(<AgentDetailPage />, { route: "/agents/agent-1", path: "/agents/:agentId" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /publish draft version/i })).toBeInTheDocument(),
    );
  });
});

describe("ContactsPage", () => {
  it("renders fetched contacts", async () => {
    mockFetchOnce(200, [
      {
        id: "contact-1",
        name: "Ada Lovelace",
        phone_e164: "+15551234567",
        email: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    renderWithProviders(<ContactsPage />);
    await waitFor(() => expect(screen.getByText("Ada Lovelace")).toBeInTheDocument());
  });
});

describe("CalendarPage", () => {
  it("renders fetched calendars and events", async () => {
    mockFetchOnce(200, [
      { id: "cal-1", name: "Front Desk", timezone: "UTC", is_active: true, created_at: "x", updated_at: "x" },
    ]);
    mockFetchOnce(200, [
      {
        id: "event-1",
        calendar_id: "cal-1",
        contact_id: null,
        title: "Checkup",
        start_at: "2026-01-02T10:00:00Z",
        end_at: "2026-01-02T11:00:00Z",
        status: "scheduled",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    renderWithProviders(<CalendarPage />);
    await waitFor(() => expect(screen.getByText("Checkup")).toBeInTheDocument());
  });
});

describe("FollowUpsPage", () => {
  it("renders fetched follow-ups and offers actions only for pending ones", async () => {
    mockFetchOnce(200, [
      {
        id: "fu-1",
        call_session_id: "call-1",
        contact_id: null,
        type: "callback",
        status: "pending",
        due_at: "2026-01-03T00:00:00Z",
        calendar_event_id: null,
        description: null,
        attempt_count: 0,
        next_attempt_at: null,
        last_attempted_at: null,
        completed_at: null,
        failure_reason: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    renderWithProviders(<FollowUpsPage />);
    await waitFor(() => expect(screen.getByText("callback")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^complete$/i })).toBeInTheDocument();
  });
});

describe("KnowledgePage", () => {
  it("renders fetched sources and loads items once one is selected", async () => {
    mockFetchOnce(200, [
      {
        id: "source-1",
        name: "FAQ",
        description: null,
        status: "active",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    mockFetchOnce(200, [
      {
        id: "item-1",
        source_id: "source-1",
        title: "Return policy",
        content: "30 days.",
        status: "active",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<KnowledgePage />);
    await waitFor(() => expect(screen.getByText("FAQ")).toBeInTheDocument());
    await user.click(screen.getByText("FAQ"));
    await waitFor(() => expect(screen.getByText("Return policy")).toBeInTheDocument());
  });
});
