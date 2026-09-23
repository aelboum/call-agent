/** Phase 2.15 brief §23 "Application shell": authenticated / unauthenticated
 * / loading states, plus the active-context gate. */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SessionContext } from "../../context/SessionContext";
import { makeSessionValue } from "../../test/testUtils";
import { RequireActiveContext, RequireAuth } from "./Guards";

function withSession(value: ReturnType<typeof makeSessionValue>, children: React.ReactNode) {
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

describe("RequireAuth", () => {
  it("shows a loading state while identity is resolving", () => {
    render(
      withSession(makeSessionValue({ auth: { status: "loading" } }), (
        <RequireAuth>
          <span>protected</span>
        </RequireAuth>
      )),
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("protected")).not.toBeInTheDocument();
  });

  it("shows a sign-in prompt, never the protected content, when unauthenticated", () => {
    render(
      withSession(makeSessionValue({ auth: { status: "unauthenticated" } }), (
        <RequireAuth>
          <span>protected</span>
        </RequireAuth>
      )),
    );
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.queryByText("protected")).not.toBeInTheDocument();
  });

  it("renders the protected content once authenticated", () => {
    render(
      withSession(makeSessionValue(), (
        <RequireAuth>
          <span>protected</span>
        </RequireAuth>
      )),
    );
    expect(screen.getByText("protected")).toBeInTheDocument();
  });
});

describe("RequireActiveContext", () => {
  it("prompts for a context when none is selected, never rendering children", () => {
    render(
      withSession(makeSessionValue({ activeTenantId: null }), (
        <RequireActiveContext>
          <span>tenant data</span>
        </RequireActiveContext>
      )),
    );
    expect(screen.queryByText("tenant data")).not.toBeInTheDocument();
    expect(screen.getByText(/select a tenant context/i)).toBeInTheDocument();
  });

  it("renders children once a context is active", () => {
    render(
      withSession(makeSessionValue({ activeTenantId: "tenant-a" }), (
        <RequireActiveContext>
          <span>tenant data</span>
        </RequireActiveContext>
      )),
    );
    expect(screen.getByText("tenant data")).toBeInTheDocument();
  });
});
