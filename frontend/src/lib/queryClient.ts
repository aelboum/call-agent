import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

/**
 * ADR-0010 §11's required cache-isolation strategy, applied literally:
 * `query key = resource + active_context_id + relevant parameters`.
 *
 * Every query hook in this codebase builds its key through this function --
 * never a bare array literal -- so a query issued under one tenant can never
 * be served, from cache, to a component rendering under a different one.
 * `tenantId` is always the *first* dynamic segment (after the fixed
 * `resource` name) precisely so `SessionContext`'s context-switch handler
 * can reason about "everything keyed under the old tenant" as one prefix,
 * even though in practice it clears the whole cache rather than trying to
 * selectively evict by prefix (see that module's own comment for why).
 */
export function contextKey(
  tenantId: string | null,
  resource: string,
  params?: Record<string, unknown>,
): readonly unknown[] {
  return params && Object.keys(params).length > 0
    ? [resource, tenantId, params]
    : [resource, tenantId];
}
