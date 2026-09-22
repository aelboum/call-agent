"""`voiceagent.call_intelligence` -- durable, provider-independent AI
post-call intelligence (Phase 2.12).

Extends Phase 2.8's deterministic `voiceagent.call_analysis` without
replacing it. The separation is exact and load-bearing:

```text
voiceagent.call_analysis (Phase 2.8)   voiceagent.call_intelligence (Phase 2.12)
-------------------------------------  -------------------------------------------
deterministic facts, computed          AI-derived interpretation, produced by an
synchronously from already-persisted   external provider, asynchronously, after
rows -- duration, turn counts, tool    the call is durable -- summary, customer
counts, had_transfer/had_hold,         intent, key topics, action items,
outcome/follow-up references           escalation indicator, confidence
never invented, never AI-derived       never authoritative for a deterministic
                                        fact Phase 2.8 already owns
```

`CallAiAnalysis` never overwrites `CallAnalysis`'s columns, and
`voiceagent.call_intelligence.prompt` *reads* `CallAnalysis` (rebuilding it
if necessary) as one of its deterministic input sources -- the one
intentional coupling between the two phases, in the direction Phase 2.8
already established as safe (a rebuild is idempotent, DB-only, no AI
provider call of its own).

**Never on the live audio path.** Nothing in this package is imported by
`voiceagent.runtime.call_task`, `voiceagent.providers.engines.*`, or
`voiceagent.tools.*`. It is reached only through
`voiceagent.call_intelligence.service` (an ordinary application service,
`voiceagent.api.v1.call_ai_analysis`'s own caller) and
`voiceagent.call_intelligence.worker.CallAiAnalysisWorker` (a bounded,
independently-owned polling loop, exactly mirroring
`voiceagent.followups.worker.FollowUpWorker`'s own "must not share the call
audio execution path" discipline). A failed or unavailable AI provider never
affects a live call.

**No second privacy mechanism.** `voiceagent.call_intelligence.analyzer`
reuses `voiceagent.runtime.privacy.authorize_call_data_access()` (the same
`control_plane.data_authorization.authorize_data_access()` gate the live
call runtime already uses) for a distinct `purpose="post_call_analysis"` --
never a parallel or weaker check.
"""

from __future__ import annotations
