#!/usr/bin/env python
"""Real OpenAI `LlmProvider` smoke test (Phase 2.20).

Clearly separate from `scripts/smoke_test_staging.py` (Phase 2.19), which
checks the API process/OIDC/tenant-isolation surface over HTTP and never
touches an AI provider. This script validates exactly one thing: that this
product's own `LlmProvider` abstraction (`voiceagent.providers.engines
.contracts.LlmProvider`) works end-to-end against the real OpenAI API,
through the exact adapter (`voiceagent.providers.llm.openai
.create_openai_llm_provider`) production code uses -- never a vendor SDK,
never a hand-rolled HTTP call outside the adapter.

**What this proves**: `OPENAI_API_KEY` is valid (authentication succeeds),
a minimal, bounded (`max_tokens`) chat-completion request succeeds, and the
streamed response is normalized into this product's own internal
representation (plain `str` text deltas and a closing `TurnEnded` -- never
an OpenAI SDK type or raw JSON reaching this script).

**What this deliberately does NOT prove** (Phase 2.20 brief section 12):
it does not require, exercise, or say anything about a phone call,
telephony, STT, or TTS. This is the LLM provider leg only.

**Credential handling**: reads `OPENAI_API_KEY` through the same
`infra.secrets` mechanism the adapter itself uses -- this script never
reads the environment variable directly, and never prints its value, the
model's generated text, or any raw provider response. If the credential is
absent, this script says so and exits without fabricating a result (Phase
2.20 brief section 14: "do NOT fabricate a successful real-provider test").

Usage::

    python scripts/smoke_test_openai_provider.py --model gpt-4o-mini

`--model` is a real, currently-selectable OpenAI chat-completion model of
the operator's choice (Phase 2.20 brief section 15: model is configuration,
never something this script or the adapter hard-codes) -- the default
shown here is an example, not an architectural commitment; verify current
model availability against OpenAI's own documentation before relying on it.

Exit codes: `0` real-provider validation passed; `1` it was attempted and
failed; `2` skipped -- no `OPENAI_API_KEY` configured in this environment,
so no real-provider claim is made either way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from infra.secrets import SecretNotFoundError, get_secrets_provider

from voiceagent.providers.engines.contracts import EngineException, ToolCallRequested, TurnEnded
from voiceagent.providers.llm.registry import create_llm_provider

__all__ = ["main"]

_SECRET_NAME = "OPENAI_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.  # pragma: allowlist secret
_REQUEST_TIMEOUT_SECONDS = 30.0


async def _run(model: str) -> int:
    try:
        get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError:
        print(
            f"SKIPPED: {_SECRET_NAME} is not configured in this environment -- "
            "no real-provider claim is made either way. See "
            "docs/PHASE-2.20-OPENAI-PROVIDER-INTEGRATION.md for what this leaves "
            "unvalidated."
        )
        return 2

    print(f"OK   {_SECRET_NAME} is configured")

    try:
        provider = create_llm_provider(
            "openai", model, {"max_tokens": 8, "timeout_seconds": _REQUEST_TIMEOUT_SECONDS}
        )
    except EngineException as exc:
        print(f"FAIL provider construction: {exc.code.value}: {exc}", file=sys.stderr)
        return 1
    print("OK   provider constructed (voiceagent.providers.llm.openai)")

    messages = [
        {"role": "system", "content": "Reply with exactly one word."},
        {"role": "user", "content": "Say OK."},
    ]

    text_deltas = 0
    tool_calls = 0
    saw_turn_ended = False
    try:

        async def _consume() -> None:
            nonlocal text_deltas, tool_calls, saw_turn_ended
            async for item in provider.stream_turn(messages, []):
                if isinstance(item, str):
                    text_deltas += 1
                elif isinstance(item, ToolCallRequested):
                    tool_calls += 1
                elif isinstance(item, TurnEnded):
                    saw_turn_ended = True

        await asyncio.wait_for(_consume(), timeout=_REQUEST_TIMEOUT_SECONDS)
    except EngineException as exc:
        print(f"FAIL request: {exc.code.value}: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print(f"FAIL request exceeded {_REQUEST_TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1

    print(f"OK   authentication succeeded, request completed (model={model!r})")
    # Never print the generated text itself (Phase 2.20 brief section 19:
    # "do NOT log ... full model responses") -- only its shape, proving the
    # response was normalized into this product's own representation
    # (plain str deltas / ToolCallRequested / TurnEnded, never a raw OpenAI
    # object) rather than the content of the response.
    print(f"OK   normalized response: {text_deltas} text delta(s), {tool_calls} tool call(s)")
    if not saw_turn_ended:
        print("FAIL response stream never yielded TurnEnded", file=sys.stderr)
        return 1
    print("OK   TurnEnded observed -- turn closed cleanly")

    print(
        "\nReal OpenAI LlmProvider validation passed. This does NOT validate telephony, "
        "STT, or TTS -- see docs/PHASE-2.20-OPENAI-PROVIDER-INTEGRATION.md."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="A real OpenAI chat-completion model id (configuration, not architecture -- "
        "verify current availability yourself before relying on this default).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.model))


if __name__ == "__main__":
    raise SystemExit(main())
