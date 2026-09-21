"""`AgentConfig` validation boundary (Phase 2.1 brief §9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voiceagent.agents.config import AgentConfig, canonical_config_dict, compute_config_hash


def _minimal_payload() -> dict:
    return {
        "instructions": "Answer the phone politely.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
    }


def test_minimal_valid_config_parses() -> None:
    config = AgentConfig.model_validate(_minimal_payload())
    assert config.instructions.startswith("Answer")
    assert config.workflow is None
    assert config.tools == []


def test_unknown_top_level_key_is_rejected() -> None:
    payload = _minimal_payload()
    payload["api_key"] = "sneaky"
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(payload)


def test_unknown_nested_key_is_rejected() -> None:
    payload = _minimal_payload()
    payload["voice"]["secret"] = "sneaky"  # noqa: S105 -- an invalid key, not a real credential
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(payload)


def test_missing_required_field_is_rejected() -> None:
    payload = _minimal_payload()
    del payload["privacy"]
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(payload)


def test_engine_kind_is_constrained() -> None:
    payload = _minimal_payload()
    payload["engine"]["kind"] = "not-a-real-kind"
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(payload)


def test_no_field_named_like_a_secret_exists_on_the_schema() -> None:
    """Phase 0 report §10.3/Phase 2.1 brief §9: no provider-credential field
    of any kind anywhere in this schema. Checked recursively across every
    nested model this module defines."""
    from pydantic import BaseModel

    import voiceagent.agents.config as config_module

    # "key" alone is deliberately excluded: `ToolBinding.key` is a tool
    # *identifier* (e.g. "calendar.book"), the same naming convention as
    # `tool_key`/`idempotency key` throughout Phase 0/2.0 -- a real false
    # positive if included. "api_key" stays, as the actual credential shape.
    forbidden = {"secret", "token", "password", "credential", "api_key"}
    offenders: list[str] = []
    for name in dir(config_module):
        obj = getattr(config_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            for field_name in obj.model_fields:
                if any(word in field_name.lower() for word in forbidden):
                    offenders.append(f"{obj.__name__}.{field_name}")
    assert offenders == []


def test_config_hash_is_deterministic_regardless_of_key_order() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert compute_config_hash(a) == compute_config_hash(b)


def test_config_hash_is_a_64_char_hex_string() -> None:
    config = AgentConfig.model_validate(_minimal_payload())
    digest = compute_config_hash(canonical_config_dict(config))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_config_hash_changes_when_config_changes() -> None:
    first = AgentConfig.model_validate(_minimal_payload())
    payload = _minimal_payload()
    payload["instructions"] = "A different instruction."
    second = AgentConfig.model_validate(payload)
    assert compute_config_hash(canonical_config_dict(first)) != compute_config_hash(
        canonical_config_dict(second)
    )


def test_canonical_dict_is_json_round_trip_stable() -> None:
    import json

    config = AgentConfig.model_validate(_minimal_payload())
    as_dict = canonical_config_dict(config)
    # Must survive an actual JSON round-trip unchanged -- this is exactly
    # what a JSON database column does to it.
    assert json.loads(json.dumps(as_dict)) == as_dict
