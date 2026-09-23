/**
 * Every data-fetching/mutation hook the pages use. One file, so "does a
 * hook for resource X already exist" is always answerable by scanning here
 * (Phase 2.15 brief §15: "one consistent request mechanism").
 *
 * Every query is built through `useTenantQuery()`, which enforces three
 * things uniformly: the query key is namespaced by the active tenant
 * (`contextKey()`, ADR-0010 §11), the query never runs without an
 * authenticated identity and a selected context, and cancellation is wired
 * automatically (`getJson()` receives React Query's own `signal`).
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import { useSession } from "../context/SessionContext";
import { getJson, postJson, putJson } from "./apiClient";
import { contextKey } from "./queryClient";
import type {
  Agent,
  AgentVersion,
  Calendar,
  CalendarEvent,
  CallAiAnalysis,
  CallAnalysis,
  CallOutcome,
  CallSession,
  CallWorkflowExecution,
  Contact,
  ConversationTurn,
  FollowUp,
  KnowledgeItem,
  KnowledgeSource,
  Meta,
  PhoneNumber,
} from "./types";

/** Unauthenticated, tenant-agnostic (`voiceagent.api.v1.meta`'s own
 * docstring: "deliberately not included: the environment, database/Redis
 * state, ..."). Used only to show the deployment's configured display
 * name before/while identity resolves. */
export function useMeta() {
  return useQuery<Meta>({
    queryKey: ["meta"],
    queryFn: ({ signal }) => getJson<Meta>("/v1/meta", { signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

type QueryParams = Record<string, string | number | boolean | undefined>;

function useTenantQuery<T>(
  resource: string,
  path: string,
  params: QueryParams = {},
  options: { enabled?: boolean } = {},
): UseQueryResult<T, Error> {
  const { auth, activeTenantId } = useSession();
  const enabled =
    (options.enabled ?? true) && auth.status === "authenticated" && activeTenantId !== null;
  return useQuery<T>({
    queryKey: contextKey(activeTenantId, resource, params),
    queryFn: ({ signal }) =>
      getJson<T>(path, { tenantId: activeTenantId ?? undefined, query: params, signal }),
    enabled,
  });
}

/** Every mutation hook invalidates the same tenant-scoped `resource` key it
 * just wrote to, so a page showing that list refetches instead of showing
 * stale data -- never a manual "remember to also update the list" step at
 * each call site. */
function useTenantMutation<TArgs, TResult>(
  resource: string,
  mutationFn: (args: TArgs & { tenantId: string }) => Promise<TResult>,
) {
  const { activeTenantId } = useSession();
  const queryClient = useQueryClient();
  return useMutation<TResult, Error, TArgs>({
    mutationFn: (args) => {
      if (activeTenantId === null) {
        return Promise.reject(new Error("No active tenant context is selected."));
      }
      return mutationFn({ ...args, tenantId: activeTenantId });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [resource, activeTenantId] });
    },
  });
}

// -- agents ---------------------------------------------------------------

export function useAgents() {
  return useTenantQuery<Agent[]>("agents", "/v1/agents");
}

export function useAgent(agentId: string | undefined) {
  return useTenantQuery<Agent>("agent", `/v1/agents/${agentId}`, {}, { enabled: !!agentId });
}

export function usePublishAgentVersion() {
  return useTenantMutation<{ agentId: string; versionId: string }, AgentVersion>(
    "agents",
    ({ agentId, versionId, tenantId }) =>
      postJson<AgentVersion>(`/v1/agents/${agentId}/versions/${versionId}/publish`, undefined, {
        tenantId,
      }),
  );
}

// -- calls ---------------------------------------------------------------

export function useCallSessions(status?: string) {
  return useTenantQuery<CallSession[]>("call_sessions", "/v1/call-sessions", { status, limit: 50 });
}

export function useCallSession(callSessionId: string | undefined) {
  return useTenantQuery<CallSession>(
    "call_session",
    `/v1/call-sessions/${callSessionId}`,
    {},
    { enabled: !!callSessionId },
  );
}

export function useCallOutcome(callSessionId: string | undefined) {
  return useTenantQuery<CallOutcome>(
    "call_outcome",
    `/v1/call-sessions/${callSessionId}/outcome`,
    {},
    { enabled: !!callSessionId },
  );
}

export function useCallAnalysis(callSessionId: string | undefined) {
  return useTenantQuery<CallAnalysis>(
    "call_analysis",
    `/v1/call-sessions/${callSessionId}/analysis`,
    {},
    { enabled: !!callSessionId },
  );
}

export function useCallWorkflowExecution(callSessionId: string | undefined) {
  return useTenantQuery<CallWorkflowExecution>(
    "call_workflow_execution",
    `/v1/call-sessions/${callSessionId}/workflow-execution`,
    {},
    { enabled: !!callSessionId },
  );
}

/** The durable conversation turns for one call (brief §8: "post-call
 * analysis where available"; §21: this content must never be cached to
 * persistent storage -- React Query's default cache is in-memory only, and
 * nothing in this codebase adds a persistence plugin). */
export function useConversationTurns(callSessionId: string | undefined) {
  return useTenantQuery<ConversationTurn[]>(
    "conversation",
    `/v1/call-sessions/${callSessionId}/conversation`,
    {},
    { enabled: !!callSessionId },
  );
}

export function useCallAiAnalysis(callSessionId: string | undefined) {
  return useTenantQuery<CallAiAnalysis>(
    "call_ai_analysis",
    `/v1/call-sessions/${callSessionId}/ai-analysis`,
    {},
    { enabled: !!callSessionId },
  );
}

export function useRebuildCallAiAnalysis() {
  return useTenantMutation<{ callSessionId: string }, CallAiAnalysis>(
    "call_ai_analysis",
    ({ callSessionId, tenantId }) =>
      postJson<CallAiAnalysis>(`/v1/call-sessions/${callSessionId}/ai-analysis/rebuild`, undefined, {
        tenantId,
      }),
  );
}

export function useSetCallOutcome() {
  return useTenantMutation<
    { callSessionId: string; outcome: string; notes?: string | null },
    CallOutcome
  >("call_outcome", ({ callSessionId, outcome, notes, tenantId }) =>
    putJson<CallOutcome>(
      `/v1/call-sessions/${callSessionId}/outcome`,
      { outcome, notes: notes ?? null },
      { tenantId },
    ),
  );
}

// -- contacts ---------------------------------------------------------------

export function useContacts() {
  return useTenantQuery<Contact[]>("contacts", "/v1/contacts", { limit: 100 });
}

export function useContact(contactId: string | undefined) {
  return useTenantQuery<Contact>(
    "contact",
    `/v1/contacts/${contactId}`,
    {},
    { enabled: !!contactId },
  );
}

// -- calendar ---------------------------------------------------------------

export function useCalendars() {
  return useTenantQuery<Calendar[]>("calendars", "/v1/calendars");
}

export function useCalendarEvents(startAt: string, endAt: string, calendarId?: string) {
  return useTenantQuery<CalendarEvent[]>("calendar_events", "/v1/calendar-events", {
    start_at: startAt,
    end_at: endAt,
    calendar_id: calendarId,
    limit: 100,
  });
}

export function useCancelCalendarEvent() {
  return useTenantMutation<{ eventId: string }, CalendarEvent>(
    "calendar_events",
    ({ eventId, tenantId }) =>
      postJson<CalendarEvent>(`/v1/calendar-events/${eventId}/cancel`, undefined, { tenantId }),
  );
}

// -- follow-ups ---------------------------------------------------------------

export function useFollowUps(status?: string) {
  return useTenantQuery<FollowUp[]>("follow_ups", "/v1/follow-ups", { status });
}

export function useCompleteFollowUp() {
  return useTenantMutation<{ followUpId: string }, FollowUp>(
    "follow_ups",
    ({ followUpId, tenantId }) =>
      postJson<FollowUp>(`/v1/follow-ups/${followUpId}/complete`, undefined, { tenantId }),
  );
}

export function useCancelFollowUp() {
  return useTenantMutation<{ followUpId: string }, FollowUp>(
    "follow_ups",
    ({ followUpId, tenantId }) =>
      postJson<FollowUp>(`/v1/follow-ups/${followUpId}/cancel`, undefined, { tenantId }),
  );
}

// -- knowledge ---------------------------------------------------------------

export function useKnowledgeSources() {
  return useTenantQuery<KnowledgeSource[]>("knowledge_sources", "/v1/knowledge/sources");
}

export function useKnowledgeItems(sourceId: string | undefined) {
  return useTenantQuery<KnowledgeItem[]>(
    "knowledge_items",
    `/v1/knowledge/sources/${sourceId}/items`,
    {},
    { enabled: !!sourceId },
  );
}

// -- phone numbers ---------------------------------------------------------------

export function usePhoneNumbers() {
  return useTenantQuery<PhoneNumber[]>("phone_numbers", "/v1/phone-numbers");
}
