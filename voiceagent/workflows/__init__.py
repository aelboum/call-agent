"""`voiceagent.workflows` -- the controlled call-workflow foundation (Phase
2.10).

A small, bounded primitive for a published agent configuration to describe a
deterministic sequence of call actions -- never a generic workflow engine,
DAG platform, or scheduler (see `docs/PHASE-2.10-STATUS.md`, "Non-goals").

The workflow *definition* (`voiceagent.workflows.config.WorkflowDefinition`)
lives inside `AgentVersion.config["workflow"]` -- there is no independent
workflow-versioning lifecycle; it inherits `AgentVersion`'s own immutability
(ADR-0004) for free. The workflow *executor*
(`voiceagent.workflows.executor.run_workflow`) is reached only through the
Tool Gateway, as one more allowlisted, RBAC-checked tool
(`workflow.advance`) -- it is never invoked from the audio/media pump, and
it never lets an agent/LLM specify arbitrary code, SQL, HTTP, or a
dynamically constructed step graph.
"""

from __future__ import annotations
