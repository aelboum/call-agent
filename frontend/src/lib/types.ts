/**
 * Frontend-owned mirrors of the backend's response schemas
 * (`voiceagent/api/v1/*.py`'s `*Out` Pydantic models).
 *
 * No OpenAPI/codegen mechanism exists in this repository yet (Phase 2.15
 * inspection: `voiceagent/api/v1/` has no schema-export step, and nothing
 * generates TypeScript from it) -- these types are hand-written and must be
 * kept in sync with their Python source by hand. Every field name and
 * optionality below is copied verbatim from the corresponding `*Out` class;
 * do not add a field here that the backend does not actually return.
 */

export type UUID = string;
export type ISODateTime = string;

// -- meta ---------------------------------------------------------------

export interface Meta {
  name: string;
  version: string;
  api_version: string;
}

// -- agents ---------------------------------------------------------------

export type AgentStatus = "active" | "archived";
export type AgentVersionStatus = "draft" | "published" | "archived";

export interface Agent {
  id: UUID;
  name: string;
  description: string | null;
  status: string;
  draft_version_id: UUID | null;
  published_version_id: UUID | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface AgentVersion {
  id: UUID;
  agent_id: UUID;
  version_number: number;
  status: string;
  config_hash: string;
  published_at: ISODateTime | null;
  created_at: ISODateTime;
}

// -- contacts ---------------------------------------------------------------

export interface Contact {
  id: UUID;
  name: string;
  phone_e164: string;
  email: string | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// -- calendars ---------------------------------------------------------------

export interface Calendar {
  id: UUID;
  name: string;
  timezone: string;
  is_active: boolean;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface Availability {
  available: boolean;
  conflict_start_at: ISODateTime | null;
  conflict_end_at: ISODateTime | null;
}

export interface CalendarEvent {
  id: UUID;
  calendar_id: UUID;
  contact_id: UUID | null;
  title: string;
  start_at: ISODateTime;
  end_at: ISODateTime;
  status: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// -- call sessions ---------------------------------------------------------------

export type CallStatus =
  | "initiated"
  | "ringing"
  | "answered"
  | "in_progress"
  | "completed"
  | "failed"
  | "interrupted";

export interface CallSession {
  id: UUID;
  direction: string;
  status: string;
  from_e164: string;
  to_e164: string;
  phone_number_id: UUID;
  agent_id: UUID;
  agent_version_id: UUID;
  contact_id: UUID | null;
  started_at: ISODateTime | null;
  answered_at: ISODateTime | null;
  ended_at: ISODateTime | null;
  duration_ms: number | null;
  hangup_cause: string | null;
  end_reason: string | null;
  created_at: ISODateTime;
}

export interface CallOutcome {
  id: UUID;
  call_session_id: UUID;
  contact_id: UUID | null;
  outcome: string;
  notes: string | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface FollowUp {
  id: UUID;
  call_session_id: UUID;
  contact_id: UUID | null;
  type: string;
  status: string;
  due_at: ISODateTime | null;
  calendar_event_id: UUID | null;
  description: string | null;
  attempt_count: number;
  next_attempt_at: ISODateTime | null;
  last_attempted_at: ISODateTime | null;
  completed_at: ISODateTime | null;
  failure_reason: string | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface CallAnalysis {
  id: UUID;
  call_session_id: UUID;
  status: string;
  turn_count: number;
  user_turn_count: number;
  assistant_turn_count: number;
  tool_call_count: number;
  tool_result_count: number;
  duration_ms: number | null;
  had_transfer: boolean;
  had_hold: boolean;
  contact_associated: boolean;
  outcome: string | null;
  follow_up_count: number;
  appointment_follow_up_count: number;
  open_follow_up_count: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface CallWorkflowExecution {
  id: UUID;
  call_session_id: UUID;
  agent_version_id: UUID;
  status: string;
  current_step_id: string;
  steps_executed: number;
  started_at: ISODateTime;
  ended_at: ISODateTime | null;
  failure_reason: string | null;
}

export interface ConversationTurn {
  id: UUID;
  sequence: number;
  role: string;
  content: string | null;
  tool_payload: Record<string, unknown> | null;
  created_at: ISODateTime;
}

// -- AI post-call intelligence ---------------------------------------------------------------

export interface EscalationIndicator {
  required: boolean;
  reason: string | null;
}

export type SentimentValue = "positive" | "neutral" | "negative" | "mixed";

export interface SentimentSignal {
  overall: SentimentValue;
}

export interface CallAiAnalysisResult {
  summary: string;
  customer_intent: string;
  key_topics: string[];
  action_items: string[];
  escalation: EscalationIndicator;
  sentiment: SentimentSignal | null;
  confidence: number;
}

export interface CallAiAnalysis {
  id: UUID;
  call_session_id: UUID;
  version: number;
  status: string;
  schema_version: string;
  prompt_version: string;
  provider: string;
  model: string;
  attempt_count: number;
  requested_at: ISODateTime;
  completed_at: ISODateTime | null;
  failure_reason: string | null;
  result: CallAiAnalysisResult | null;
}

// -- knowledge ---------------------------------------------------------------

export interface KnowledgeSource {
  id: UUID;
  name: string;
  description: string | null;
  status: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface KnowledgeItem {
  id: UUID;
  source_id: UUID;
  title: string;
  content: string;
  status: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// -- phone numbers ---------------------------------------------------------------

export interface PhoneNumber {
  id: UUID;
  e164: string;
  label: string | null;
  agent_id: UUID | null;
  version_pin_mode: string;
  pinned_version_id: UUID | null;
  inbound_enabled: boolean;
  outbound_caller_id: string | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// -- operator diagnostics (voiceagent/api/v1/ops.py) ------------------------

export interface RuntimeHeartbeat {
  instance_id: string;
  capacity: number;
  current_load: number;
  has_capacity: boolean;
  seconds_since_heartbeat: number;
}

export interface RuntimeHeartbeatsResponse {
  runtimes: RuntimeHeartbeat[];
  redis_reachable: boolean;
}

export interface StuckCall {
  call_session_id: UUID;
  status: string;
  phase: string;
  seconds_since_progress: number;
}

export interface StuckCallsResponse {
  stuck_calls: StuckCall[];
}
