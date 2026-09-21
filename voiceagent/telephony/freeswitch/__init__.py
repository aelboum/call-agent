"""FreeSWITCH adapter (Phase 2.2).

ADR-0002 (2026-09-21 amendment): every FreeSWITCH-specific concept lives here
and nowhere else -- the ESL client and its commands (`esl.py`, `provider.py`),
`mod_audio_stream` framing (`media.py`), channel-UUID semantics, and hangup-
cause strings before they are normalized into
`voiceagent.telephony.contracts`. `FreeSwitchTelephonyProvider` and
`FreeSwitchMediaProvider` are the only implementations of the two contracts
in that package; `fakes.py` holds their injected `EslConnection`/
`MediaSocket` doubles, used by every test in `tests/telephony/freeswitch/`
so none of them requires a running FreeSWITCH instance.

No other product module may import this package -- not even to re-export a
name. `pyproject.toml`'s import-linter contract and
`tests/architecture/test_import_boundaries.py` both enforce that; this
`__init__` deliberately holds nothing so there is nothing to accidentally
re-export past the fence.
"""

from __future__ import annotations

__all__: list[str] = []
