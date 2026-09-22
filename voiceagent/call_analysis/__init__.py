"""`voiceagent.call_analysis` -- a small, deterministic, derived post-call
analysis foundation (Phase 2.8).

`CallAnalysis` is **not** a new source of truth. It is a rebuildable,
tenant-scoped snapshot of facts already persisted by other domains
(`voiceagent.calls`, `voiceagent.conversations`, `voiceagent.followups`,
`voiceagent.contacts`) -- see `voiceagent.call_analysis.service`'s own
module docstring for the exact metric rules and which domain remains
authoritative for each one.
"""

from __future__ import annotations
