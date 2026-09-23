"""`voiceagent.ops` -- operator-only operational diagnostics (Phase 2.14).

Not a tenant-facing domain: nothing here stores or serves customer data. It
exposes exactly the two cross-process-safe operational signals the API
process can honestly report without inventing a new mechanism (see
`voiceagent.runtime.diagnostics`'s own module docstring for why a live
`CallRuntime` snapshot is not one of them): the Redis runtime-heartbeat set
(`voiceagent.runtime.heartbeat`) and a DB-based stuck-call scan
(`voiceagent.runtime.stuck_calls`).
"""

from __future__ import annotations
