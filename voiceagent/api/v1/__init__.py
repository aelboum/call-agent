"""The versioned `/v1` API surface.

Routers are aggregated here and mounted once by
`voiceagent.api.build_app()` under the configured prefix, so the prefix lives
in exactly one place. Phase 1 serves a single route (`/v1/meta`); agent,
phone-number, call, contact and calendar resources belong to later phases and
must not be anticipated here.
"""

from __future__ import annotations

from fastapi import APIRouter

from voiceagent.api.v1.meta import router as meta_router

router = APIRouter()
router.include_router(meta_router)

__all__ = ["router"]
