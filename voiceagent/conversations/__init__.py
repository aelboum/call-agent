"""Durable conversation history for a `CallSession` (Phase 2.5).

Provider-neutral turns only -- no raw provider payload, no vendor message
object, no audio. See `voiceagent.conversations.models`,
`voiceagent.conversations.service`, and `voiceagent.runtime
.conversation_persistence` for how a turn gets here from a running call
without ever touching the audio/media pump.
"""

from __future__ import annotations

__all__: list[str] = []
