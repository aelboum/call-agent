/**
 * Reusable state primitives (Phase 2.15 brief §18: "establish reusable
 * primitives for loading/empty/error/permission denied/not found... avoid
 * each page inventing a completely different pattern").
 *
 * `ErrorState` is the one place an `ApiError` becomes UI: it branches on
 * `kind`, never on `error.message` for anything but the final safe-to-show
 * `detail` string, which is already normalized by `apiClient.ts` (never a
 * raw exception, stack trace, or SQL error -- brief §18/§21).
 */
import type { ReactNode } from "react";

import { ApiError } from "../../lib/apiClient";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state state-loading" role="status">
      {label}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="state state-empty" role="status">
      {children}
    </div>
  );
}

export function NotFoundState({ label = "Not found." }: { label?: string }) {
  return (
    <div className="state state-not-found" role="status">
      {label}
    </div>
  );
}

export function PermissionDeniedState() {
  return (
    <div className="state state-denied" role="alert">
      You don't have permission to view this. If you believe this is wrong, contact your
      administrator.
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  if (error instanceof ApiError) {
    if (error.kind === "forbidden") return <PermissionDeniedState />;
    if (error.kind === "not_found") return <NotFoundState />;
    if (error.kind === "unauthorized") {
      return (
        <div className="state state-error" role="alert">
          Your session has expired. Please sign in again.
        </div>
      );
    }
    return (
      <div className="state state-error" role="alert">
        {error.message}
      </div>
    );
  }
  return (
    <div className="state state-error" role="alert">
      Something went wrong. Please try again.
    </div>
  );
}

/** Like `QueryState`, but a 404 is treated as "this optional resource does
 * not exist yet" (e.g. a call with no outcome set, no analysis built yet)
 * rather than an alarming error banner -- the correct reading for every
 * call-scoped sub-resource on `CallDetailPage`, which is created lazily. */
export function OptionalQueryState<T>({
  query,
  notYetMessage,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; error: unknown; data: T | undefined };
  notYetMessage: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.isLoading) return <LoadingState />;
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.kind === "not_found") {
      return <EmptyState>{notYetMessage}</EmptyState>;
    }
    return <ErrorState error={query.error} />;
  }
  if (query.data === undefined) return <LoadingState />;
  return <>{children(query.data)}</>;
}

/** Wraps a React Query result: loading -> `LoadingState`, error ->
 * `ErrorState`, empty array -> `emptyMessage`, otherwise `children(data)`.
 * The one place a page needs to reason about all four states at once. */
export function QueryState<T>({
  query,
  emptyMessage,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; error: unknown; data: T | undefined };
  emptyMessage?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.isLoading) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} />;
  if (query.data === undefined) return <LoadingState />;
  if (emptyMessage !== undefined && Array.isArray(query.data) && query.data.length === 0) {
    return <EmptyState>{emptyMessage}</EmptyState>;
  }
  return <>{children(query.data)}</>;
}
