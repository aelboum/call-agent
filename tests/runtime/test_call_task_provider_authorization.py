"""Phase 2.25 regression: both `_engine_provider_name()` implementations
(`voiceagent.runtime.call_task` and `voiceagent.runtime.orchestrator` --
deliberately duplicated, per each one's own docstring, "a private four-line
helper neither module has any other reason to share") must read the real
`AgentConfig` schema (`engine.stt`/`engine.llm`/`engine.tts`, per
`voiceagent.agents.config.EngineConfig`), not a non-existent
`engine.pipelined` key.

Found while wiring a genuine real-SIP-caller spoken E2E call
(`scripts/validate_staging_sip_spoken_e2e.py`): every prior hermetic and
integration test happened to use `provider: "fake"` for every real
`pipelined` agent config, which the old, wrong implementation
(`engine_config.get(kind)` where `kind == "pipelined"`, but no config ever
has a top-level `"pipelined"` key) also produced as its fallback default --
so the two coincidentally agreed on every existing test, real or
otherwise. The first config to use a real, non-"fake" provider
(`deepgram`/`openai`/`deepgram_aura`) surfaced the mismatch immediately:
`authorize_call_data_access()` denied a real call outright -- both call
sites (`CallOrchestrator._activate()`, before media/engine ever start, and
`run_call_task()`'s own re-check) always reported `"fake"` regardless of
the agent's actual configured provider -- a real defect in the
one-time-per-call privacy audit/authorization decision
(`voiceagent.runtime.privacy`'s own module docstring), not merely a test
gap.
"""

from __future__ import annotations

import uuid

from voiceagent.agents.models import AgentVersion
from voiceagent.runtime.call_task import _engine_provider_name
from voiceagent.runtime.orchestrator import _engine_provider_name as _orchestrator_provider_name


def _agent_version(config: dict) -> AgentVersion:
    return AgentVersion(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version_number=1,
        status="published",
        config=config,
        config_hash="0" * 64,
    )


def test_pipelined_engine_reports_the_real_stt_provider_not_fake() -> None:
    version = _agent_version(
        {
            "engine": {
                "kind": "pipelined",
                "stt": {"provider": "deepgram", "config": {}},
                "llm": {"provider": "openai", "model": "gpt-4o-mini", "config": {}},
                "tts": {"provider": "deepgram_aura", "config": {}},
            }
        }
    )
    assert _engine_provider_name(version) == "deepgram"


def test_pipelined_engine_with_fake_provider_still_reports_fake() -> None:
    version = _agent_version(
        {"engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}}}
    )
    assert _engine_provider_name(version) == "fake"


def test_realtime_engine_reports_its_own_provider() -> None:
    version = _agent_version(
        {"engine": {"kind": "realtime", "realtime": {"provider": "openai_realtime", "config": {}}}}
    )
    assert _engine_provider_name(version) == "openai_realtime"


def test_missing_engine_config_falls_back_to_fake() -> None:
    version = _agent_version({})
    assert _engine_provider_name(version) == "fake"


def test_orchestrator_copy_agrees_with_call_task_copy_for_pipelined() -> None:
    version = _agent_version(
        {
            "engine": {
                "kind": "pipelined",
                "stt": {"provider": "deepgram", "config": {}},
                "llm": {"provider": "openai", "model": "gpt-4o-mini", "config": {}},
                "tts": {"provider": "deepgram_aura", "config": {}},
            }
        }
    )
    assert _orchestrator_provider_name(version) == "deepgram"
    assert _orchestrator_provider_name(version) == _engine_provider_name(version)


def test_orchestrator_copy_agrees_with_call_task_copy_for_realtime() -> None:
    version = _agent_version(
        {"engine": {"kind": "realtime", "realtime": {"provider": "openai_realtime", "config": {}}}}
    )
    assert _orchestrator_provider_name(version) == "openai_realtime"
    assert _orchestrator_provider_name(version) == _engine_provider_name(version)
