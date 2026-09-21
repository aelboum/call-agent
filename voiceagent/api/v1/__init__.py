"""The versioned `/v1` API surface.

Routers are aggregated here and mounted once by
`voiceagent.api.build_app()` under the configured prefix, so the prefix lives
in exactly one place. Phase 2.1 adds `agents`, `phone_numbers` and
`call_sessions` -- exactly the endpoints in
`docs/PHASE-2.0-ARCHITECTURE.md` §23.9, no more. Contact, calendar, tool and
workflow resources belong to later phases and must not be anticipated here.
"""

from __future__ import annotations

from fastapi import APIRouter

from voiceagent.api.v1.agents import router as agents_router
from voiceagent.api.v1.call_sessions import router as call_sessions_router
from voiceagent.api.v1.meta import router as meta_router
from voiceagent.api.v1.phone_numbers import router as phone_numbers_router

router = APIRouter()
router.include_router(meta_router)
router.include_router(agents_router)
router.include_router(phone_numbers_router)
router.include_router(call_sessions_router)

__all__ = ["router"]
