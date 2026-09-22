"""`voiceagent.contacts` -- the Contact aggregate (Phase 2.6).

Exists solely to support the phone-call lifecycle: a `CallSession` may
optionally be associated with a `Contact`, and a call agent may look one up
by the caller's normalized E.164 number. Not a CRM (Phase 2.6 brief §25):
no search, no fuzzy matching, no activity tracking.
"""

from __future__ import annotations
