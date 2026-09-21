"""FreeSWITCH adapter -- Phase 2, deliberately empty in Phase 1.

ADR-0002 (2026-09-21 amendment): every FreeSWITCH-specific concept lives here
and nowhere else -- the ESL client and its commands, channel-UUID semantics,
`mod_audio_stream` framing, dialplan assumptions, and hangup-cause strings
before they are normalized. Two implementations will live here,
`FreeSwitchTelephonyProvider` and `FreeSwitchMediaProvider`, behind the
contracts in `voiceagent.telephony.contracts`.

No other product module may import this package. `pyproject.toml`'s
import-linter contract and `tests/architecture/test_import_boundaries.py`
both enforce that today, while the package is still empty -- which is the
point: the fence exists before there is anything to leak.
"""

from __future__ import annotations

__all__: list[str] = []
