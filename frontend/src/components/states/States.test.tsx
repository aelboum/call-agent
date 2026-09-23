import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../lib/apiClient";
import { ErrorState, OptionalQueryState, QueryState } from "./States";

describe("QueryState", () => {
  it("shows a loading state while the query is loading", () => {
    render(
      <QueryState query={{ isLoading: true, isError: false, error: null, data: undefined }}>
        {() => <span>data</span>}
      </QueryState>,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows the empty state for an empty array when emptyMessage is given", () => {
    render(
      <QueryState
        query={{ isLoading: false, isError: false, error: null, data: [] as unknown[] }}
        emptyMessage="Nothing here."
      >
        {() => <span>data</span>}
      </QueryState>,
    );
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
  });

  it("renders children with the resolved data", () => {
    render(
      <QueryState query={{ isLoading: false, isError: false, error: null, data: "hello" }}>
        {(data) => <span>{data}</span>}
      </QueryState>,
    );
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("renders a permission-denied state for a 403 ApiError", () => {
    const error = new ApiError("forbidden", 403, "Forbidden.");
    render(
      <QueryState query={{ isLoading: false, isError: true, error, data: undefined }}>
        {() => <span>data</span>}
      </QueryState>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/permission/i);
  });
});

describe("OptionalQueryState", () => {
  it("treats a 404 as an empty state, not an error", () => {
    const error = new ApiError("not_found", 404, "Not found.");
    render(
      <OptionalQueryState
        query={{ isLoading: false, isError: true, error, data: undefined }}
        notYetMessage="Nothing yet."
      >
        {() => <span>data</span>}
      </OptionalQueryState>,
    );
    expect(screen.getByText("Nothing yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("still surfaces a non-404 error", () => {
    const error = new ApiError("server_error", 500, "Something went wrong. Please try again.");
    render(
      <OptionalQueryState
        query={{ isLoading: false, isError: true, error, data: undefined }}
        notYetMessage="Nothing yet."
      >
        {() => <span>data</span>}
      </OptionalQueryState>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("never renders a raw non-ApiError object as text", () => {
    render(<ErrorState error={{ some: "raw object", stack: "at foo.js:1:1" }} />);
    expect(screen.getByRole("alert").textContent).toBe("Something went wrong. Please try again.");
  });

  it("shows the normalized backend detail for a 409 conflict", () => {
    const error = new ApiError("conflict", 409, "Phone number unavailable.");
    render(<ErrorState error={error} />);
    expect(screen.getByText("Phone number unavailable.")).toBeInTheDocument();
  });

  it("shows a session-expired message for a 401, not the raw detail", () => {
    const error = new ApiError("unauthorized", 401, "Not authenticated");
    render(<ErrorState error={error} />);
    expect(screen.getByRole("alert").textContent).toMatch(/session has expired/i);
  });
});
