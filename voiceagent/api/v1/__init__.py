"""The versioned `/v1` API surface.

Routers are aggregated here and mounted once by
`voiceagent.api.build_app()` under the configured prefix, so the prefix lives
in exactly one place. Phase 2.1 adds `agents`, `phone_numbers` and
`call_sessions` -- exactly the endpoints in
`docs/PHASE-2.0-ARCHITECTURE.md` §23.9, no more. Phase 2.5 adds
`conversations` (`docs/PHASE-2.5-STATUS.md`), the one durable-conversation
read endpoint. Phase 2.6 adds `contacts`, `calendars` and `calendar-events`
(`docs/PHASE-2.6-STATUS.md`). Phase 2.7 adds `follow-ups` -- plus the
outcome and call-scoped follow-up routes nested on `call-sessions`
(`docs/PHASE-2.7-STATUS.md`). Tool and workflow resources belong to later
phases and must not be anticipated here.
"""

from __future__ import annotations

from fastapi import APIRouter

from voiceagent.api.v1.agents import router as agents_router
from voiceagent.api.v1.calendar_events import router as calendar_events_router
from voiceagent.api.v1.calendars import router as calendars_router
from voiceagent.api.v1.call_sessions import router as call_sessions_router
from voiceagent.api.v1.contacts import router as contacts_router
from voiceagent.api.v1.conversations import router as conversations_router
from voiceagent.api.v1.follow_ups import router as follow_ups_router
from voiceagent.api.v1.meta import router as meta_router
from voiceagent.api.v1.phone_numbers import router as phone_numbers_router

router = APIRouter()
router.include_router(meta_router)
router.include_router(agents_router)
router.include_router(phone_numbers_router)
router.include_router(call_sessions_router)
router.include_router(conversations_router)
router.include_router(contacts_router)
router.include_router(calendars_router)
router.include_router(calendar_events_router)
router.include_router(follow_ups_router)

__all__ = ["router"]
