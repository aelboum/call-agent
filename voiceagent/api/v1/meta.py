"""`/v1/meta` -- the versioned API's build-information endpoint.

The one route the Phase 1 foundation serves. It exists to prove the `/v1`
surface is mounted and reachable, and to give deployments a way to confirm
which build is running.

It is unauthenticated, so it discloses the minimum that is useful: the
configured display name and the product version. Deliberately **not**
included: the environment, the database or Redis state, the commit SHA of the
pinned platform dependency, feature flags, or anything else that tells an
unauthenticated caller about the deployment. Readiness has its own endpoint
(`/readyz`) and its own deliberately thin body.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["meta"])


@router.get("/meta")
def meta(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "name": settings.app_display_name,
        "version": request.app.version,
        "api_version": "v1",
    }
