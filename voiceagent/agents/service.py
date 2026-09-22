"""Application service for the Agent aggregate (Agent + AgentVersion).

Every function takes a verified `voiceagent.tenancy.TenantContext` and opens
its own `tenant_scope()` -- no function accepts a bare `tenant_id`, and no
function is ever called with a tenant context this module did not receive
from its caller (which itself only ever holds one SaaS-OS already verified,
Phase 1's own invariant, unchanged). Business invariants (draft-only editing,
publish-time validation, the one legal archive transition) live here, not in
route handlers or in the ORM layer (Phase 2.1 brief §17).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event

from voiceagent.agents.config import AgentConfig, canonical_config_dict, compute_config_hash
from voiceagent.agents.errors import (
    AgentNotFoundError,
    AgentVersionNotDraftError,
    AgentVersionNotFoundError,
    AgentVersionNotPublishedError,
    InvalidAgentConfigError,
)
from voiceagent.agents.models import Agent, AgentVersion
from voiceagent.db import select
from voiceagent.tenancy import TenantContext, tenant_scope

# Side-effect import: populates voiceagent.tools.registry.TOOL_REGISTRY
# before validate_tool_references() below ever runs. Safe here (unlike in
# voiceagent.agents.config): voiceagent.providers.engines.factory imports
# voiceagent.agents.config, but never this module.
from voiceagent.tools import handlers as _tool_handlers  # noqa: F401
from voiceagent.workflows.validation import UnknownWorkflowToolError, validate_tool_references

__all__ = [
    "archive_version",
    "create_agent",
    "create_draft_version",
    "get_agent",
    "get_agent_version",
    "list_agents",
    "list_agent_versions",
    "publish_version",
    "select_agent_version_id",
    "update_agent",
]


def _get_agent_row(session, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        # Belt-and-braces on top of RLS (Phase 1's own established pattern,
        # `voiceagent/telephony` fakes aside -- this mirrors the reference
        # consumer's own `get_widget()`): a foreign or nonexistent row is the
        # identical error either way, so no response can distinguish them.
        raise AgentNotFoundError(agent_id)
    return agent


def _get_version_row(session, tenant_id: uuid.UUID, version_id: uuid.UUID) -> AgentVersion:
    version = session.get(AgentVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise AgentVersionNotFoundError(version_id)
    return version


def create_agent(context: TenantContext, *, name: str, description: str | None = None) -> Agent:
    with tenant_scope(context) as session:
        agent = Agent(tenant_id=context.tenant_id, name=name, description=description)
        session.add(agent)
        session.flush()
        session.refresh(agent)
        session.expunge(agent)
        return agent


def get_agent(context: TenantContext, agent_id: uuid.UUID) -> Agent:
    with tenant_scope(context) as session:
        agent = _get_agent_row(session, context.tenant_id, agent_id)
        session.expunge(agent)
        return agent


def list_agents(context: TenantContext) -> Sequence[Agent]:
    with tenant_scope(context) as session:
        agents = (
            session.execute(select(Agent).where(Agent.tenant_id == context.tenant_id))
            .scalars()
            .all()
        )
        for agent in agents:
            session.expunge(agent)
        return agents


def update_agent(
    context: TenantContext,
    agent_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> Agent:
    """Mutates only `agents` columns. Never touches `agent_versions` --
    changing an Agent's own name/description/status must not alter any
    already-created `AgentVersion` (Phase 2.1 brief §15, "historical version
    stability")."""
    with tenant_scope(context) as session:
        agent = _get_agent_row(session, context.tenant_id, agent_id)
        if name is not None:
            agent.name = name
        if description is not None:
            agent.description = description
        if status is not None:
            agent.status = status
        session.flush()
        session.refresh(agent)
        session.expunge(agent)
        return agent


def create_draft_version(
    context: TenantContext, agent_id: uuid.UUID, *, config: AgentConfig
) -> AgentVersion:
    """Inserts a new `draft` row. Never mutates an existing row -- each call
    creates a fresh version number, even if an earlier draft was never
    published (Phase 2.0 report §9.2's append-only model).

    Phase 2.10: if `config.workflow` is set, every `tool` step's `tool_id`
    must name a known, available Tool Gateway tool -- checked here, not by
    `AgentConfig`/`WorkflowDefinition`'s own pydantic validation (see
    `voiceagent.workflows.config`'s module docstring for why that check
    cannot live there)."""
    if config.workflow is not None:
        try:
            validate_tool_references(config.workflow)
        except UnknownWorkflowToolError as exc:
            raise InvalidAgentConfigError(str(exc)) from exc

    config_dict = canonical_config_dict(config)
    config_hash = compute_config_hash(config_dict)

    with tenant_scope(context) as session:
        agent = _get_agent_row(session, context.tenant_id, agent_id)

        # ADR-0007: no sqlalchemy.func. The next version_number is computed
        # in Python from the (small, per-agent) set of existing numbers,
        # never via func.max().
        existing_numbers = (
            session.execute(
                select(AgentVersion.version_number).where(AgentVersion.agent_id == agent_id)
            )
            .scalars()
            .all()
        )
        next_number = (max(existing_numbers) if existing_numbers else 0) + 1

        version = AgentVersion(
            tenant_id=context.tenant_id,
            agent_id=agent_id,
            version_number=next_number,
            status="draft",
            config=config_dict,
            config_hash=config_hash,
        )
        session.add(version)
        session.flush()
        agent.draft_version_id = version.id
        session.flush()
        session.refresh(version)
        session.expunge(version)
        return version


def get_agent_version(context: TenantContext, version_id: uuid.UUID) -> AgentVersion:
    with tenant_scope(context) as session:
        version = _get_version_row(session, context.tenant_id, version_id)
        session.expunge(version)
        return version


def list_agent_versions(context: TenantContext, agent_id: uuid.UUID) -> Sequence[AgentVersion]:
    with tenant_scope(context) as session:
        _get_agent_row(session, context.tenant_id, agent_id)  # 404 if foreign/missing
        versions = (
            session.execute(
                select(AgentVersion)
                .where(AgentVersion.tenant_id == context.tenant_id)
                .where(AgentVersion.agent_id == agent_id)
            )
            .scalars()
            .all()
        )
        for version in versions:
            session.expunge(version)
        return versions


def publish_version(
    context: TenantContext, agent_id: uuid.UUID, version_id: uuid.UUID
) -> AgentVersion:
    """`draft -> published`, one transaction (ADR-0004 §7.3): validate,
    transition, move `agents.published_version_id`, audit. The database
    trigger (migration `0002_...`) does not restrict this transition -- it
    only restricts mutation *of an already-published row* -- so this UPDATE
    succeeds there and the immutability guarantee begins applying to this row
    from this moment forward."""
    with tenant_scope(context) as session:
        agent = _get_agent_row(session, context.tenant_id, agent_id)
        version = _get_version_row(session, context.tenant_id, version_id)
        if version.agent_id != agent_id:
            raise AgentVersionNotFoundError(version_id)
        if version.status != "draft":
            raise AgentVersionNotDraftError(version_id, version.status)

        version.status = "published"
        version.published_at = datetime.now(UTC)
        version.published_by = context.actor_id
        agent.published_version_id = version.id
        if agent.draft_version_id == version.id:
            agent.draft_version_id = None
        session.flush()
        session.refresh(version)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="agent.published",
            resource_type="agent_version",
            resource_id=str(version.id),
            outcome=AuditOutcome.SUCCESS,
            metadata={
                "agent_id": str(agent_id),
                "version_number": version.version_number,
                "config_hash": version.config_hash,
            },
        )

        session.expunge(version)
        return version


def archive_version(
    context: TenantContext, agent_id: uuid.UUID, version_id: uuid.UUID
) -> AgentVersion:
    """`published -> archived` -- the one transition the database trigger
    permits on an already-published row (Phase 2.0 report §23.6)."""
    with tenant_scope(context) as session:
        _get_agent_row(session, context.tenant_id, agent_id)
        version = _get_version_row(session, context.tenant_id, version_id)
        if version.agent_id != agent_id:
            raise AgentVersionNotFoundError(version_id)
        if version.status != "published":
            raise AgentVersionNotPublishedError(version_id, version.status)

        version.status = "archived"
        session.flush()
        session.refresh(version)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=context.actor_id,
            action="agent.version_archived",
            resource_type="agent_version",
            resource_id=str(version.id),
            outcome=AuditOutcome.SUCCESS,
        )

        session.expunge(version)
        return version


def select_agent_version_id(
    agent: Agent, *, pin_mode: str, pinned_version_id: uuid.UUID | None
) -> uuid.UUID:
    """Resolve which `AgentVersion` a call against `agent` should execute
    against (Phase 0 report §7.4 / Phase 2.0 report §5.3): `pinned` uses the
    phone number's own `pinned_version_id`; `follow_published` (the default)
    uses the agent's current `published_version_id`. A pure function -- the
    caller (the future Call Orchestrator, or a Phase 2.1 test standing in for
    it) already holds both rows; this makes no additional query and touches
    no database itself.

    Raises `AgentVersionNotFoundError` if resolution yields nothing
    resolvable (no published version yet, or `pinned` with no
    `pinned_version_id`) -- the caller decides what that means for a call
    (Phase 2.0 report §5.3/§14.2: no-capacity/fallback handling), which is
    Phase 2.2+ behavior, not this function's.
    """
    if pin_mode == "pinned":
        if pinned_version_id is None:
            raise AgentVersionNotFoundError("pinned_version_id is not set")
        return pinned_version_id
    if agent.published_version_id is None:
        raise AgentVersionNotFoundError("agent has no published version")
    return agent.published_version_id
