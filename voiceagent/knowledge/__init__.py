"""`voiceagent.knowledge` -- the controlled agent knowledge/context foundation
(Phase 2.11).

A tenant-owned, text-first knowledge boundary: `KnowledgeSource` groups
`KnowledgeItem` rows (short business documents -- hours, pricing, policies,
FAQs), an `AgentVersion` approves a bounded set of items by id
(`voiceagent.knowledge.config.KnowledgeConfig`, living inside
`AgentVersion.config["knowledge"]` the same way `voiceagent.workflows.config
.WorkflowDefinition` lives inside `config["workflow"]`), and
`voiceagent.knowledge.retrieval.search_items()` answers a bounded, tenant-
and-association-scoped lexical query for the Tool Gateway's `knowledge.search`
tool (`voiceagent.tools.handlers`) and for `voiceagent.knowledge.context
.assemble_call_context()`.

This is not a vector database, a RAG platform, or a document-ingestion
pipeline (see `docs/PHASE-2.11-STATUS.md`, "Non-goals"). It is never reached
from the audio/media pump directly -- always through the Tool Gateway (an
AI-issued query) or through the call runtime's own application-service calls
(context assembly ahead of an engine turn), both of which cross
`voiceagent.runtime.db.DatabaseBoundary`, never the event loop.

**Determinism (ADR-0004 extended).** A `KnowledgeItem`'s `title`/`content` are
immutable from the moment it leaves `status='draft'`
(`app.forbid_knowledge_item_content_update`, migration `0009`) -- the same
discipline `agent_versions_immutable` already applies to a published
`AgentVersion`. A published `AgentVersion` therefore references knowledge
items by id only; because an active or archived item's own text can never
change again, that reference stays deterministic for the version's entire
lifetime without needing to duplicate the item's content into
`AgentVersion.config` itself (the smaller of the two architectures this
phase's brief offers -- see the module docstring of
`voiceagent.knowledge.config`).
"""

from __future__ import annotations
