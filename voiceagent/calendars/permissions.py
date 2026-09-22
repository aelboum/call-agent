"""RBAC permission declarations for `Calendar`/`CalendarEvent` (Phase 2.6
brief §12): two resources, so a role can hold `voiceagent.calendars:read`
without also getting `voiceagent.calendar_events:cancel`. See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["CALENDARS_RESOURCE", "CALENDAR_EVENTS_RESOURCE", "register"]

CALENDARS_RESOURCE = "voiceagent.calendars"
CALENDAR_EVENTS_RESOURCE = "voiceagent.calendar_events"


def register() -> None:
    for action in ("read", "create"):
        register_permission(CALENDARS_RESOURCE, action)
    for action in ("read", "create", "cancel"):
        register_permission(CALENDAR_EVENTS_RESOURCE, action)
