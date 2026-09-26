"""Authoritative inbound-call routing (Phase 2.22).

**The problem this module exists to solve**: resolving "which tenant owns
this phone number" is the one lookup a `TenantContext` cannot yet exist for
-- it is precisely how a `TenantContext` gets built in the first place, for
a call the platform's own HTTP ingress chain never touches. Every other
tenant-owned table in this schema is `FORCE ROW LEVEL SECURITY`'d
(`voiceagent.phone_numbers.models.PhoneNumber` included) with a policy of
the shape `tenant_id = NULLIF(current_setting('app.tenant_id', true),
'')::uuid` -- correct and safe, but with no session variable set (the exact
situation an inbound call starts in), that comparison is `NULL`-valued and
therefore matches zero rows for every tenant, including the one that
actually owns the number. There was, and remains, no other existing
mechanism in this codebase for this lookup (confirmed by inspection:
`voiceagent.phone_numbers.service` has no tenant-less query of any kind).

**The primitive this module adds** (Phase 2.22 brief section 5: "if no
sufficient mechanism exists, introduce the smallest explicit routing
primitive needed"): `app.inbound_call_routes`
(`migrations/versions/0012_inbound_call_routing.py`) -- a small,
deliberately **not** row-level-secured table holding only the four columns
an inbound-call router needs (`tenant_id`, `phone_number_id`, `e164`,
`agent_id`, `inbound_enabled`), kept in sync with `app.phone_numbers` by a
database trigger, never by application code. Nothing else about that
table's row is sensitive in a way `app.phone_numbers` itself is not already
also un-secret about to the telephony network it serves (a real PSTN switch
already has to know which numbers exist and roughly where they route) --
call content, transcripts, agent prompts, and every other tenant
configuration value remain exactly as row-level-secured as before this
module existed, and this module never reads any of them.

Verified against a real PostgreSQL instance (not merely reasoned about):
with `app.tenant_id` unset, this module's own query against
`app.inbound_call_routes` returns the expected row, while an equivalent
`SELECT` against `app.phone_numbers` itself returns zero rows regardless
of which number is searched for -- proving the narrow table is reachable
and the tenant-owned table remains fully protected.

**A caller-supplied tenant identifier is never sufficient on its own**: this
module's only input is the external, telephony-supplied `e164` string
(never a tenant id, never any other caller-controlled value), and its
output is the single row this schema's own globally-unique `e164` column
(`uq_phone_numbers_e164_global`, mirrored here as `uq_inbound_call_routes_
e164`) can ever resolve to -- there is no way to pass in an arbitrary
tenant id and get it echoed back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from voiceagent.db import (
    Base,
    Boolean,
    DateTime,
    Mapped,
    String,
    mapped_column,
    select,
    session_scope,
)

__all__ = ["InboundCallRoute", "ResolvedRoute", "resolve_inbound_route"]


class InboundCallRoute(Base):
    """ORM mapping for `app.inbound_call_routes`. Deliberately does not use
    `voiceagent.db.tenant_table_args()`: this table carries no Row-Level
    Security policy at all (see module docstring) -- using the tenant-table
    helper here would misstate that fact to a future reader."""

    __tablename__ = "inbound_call_routes"
    __table_args__ = {"schema": "app"}

    phone_number_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    e164: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """The exact, minimal set of facts an inbound call needs before any
    authorization or `CallSession` work can begin. Carries no telephony
    concept (Phase 2.22 brief section 18) -- a plain value the orchestrator
    passes into the existing, unmodified `voiceagent.calls.service`/
    `voiceagent.agents.service`/`voiceagent.runtime.privacy` functions."""

    tenant_id: uuid.UUID
    phone_number_id: uuid.UUID
    agent_id: uuid.UUID | None
    inbound_enabled: bool


def resolve_inbound_route(e164: str) -> ResolvedRoute | None:
    """`None` if `e164` is not a known number at all -- the caller (`voiceagent
    .runtime.orchestrator.CallOrchestrator`) treats that identically to
    `inbound_enabled=False`: reject/fail closed, per Phase 2.22 brief
    section 14 ("Unknown inbound call: reject/fail closed").

    Synchronous, like every other `voiceagent.calls.service`/`voiceagent
    .agents.service` function -- the caller crosses `DatabaseBoundary.run()`,
    never calls this directly from the audio/event-pump path.
    """
    with session_scope() as session:
        row = session.execute(
            select(InboundCallRoute).where(InboundCallRoute.e164 == e164)
        ).scalar_one_or_none()
        if row is None:
            return None
        return ResolvedRoute(
            tenant_id=row.tenant_id,
            phone_number_id=row.phone_number_id,
            agent_id=row.agent_id,
            inbound_enabled=row.inbound_enabled,
        )
