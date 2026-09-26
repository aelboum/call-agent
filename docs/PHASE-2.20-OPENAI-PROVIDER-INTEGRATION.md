# Phase 2.20: Real AI Provider Staging Integration (OpenAI)

Checkpoint: `c3cde99 feat: establish staging integration foundation`
(Phase 2.19) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified at
`ff550010e5eafecace7311038aadc99fcecfbe3d`. No commit exists yet for this
phase's own work. `docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.

This phase integrates OpenAI as the first real, staging-configurable
`LlmProvider` -- proving the existing provider abstraction against a real
external vendor, not deciding OpenAI is permanent or exclusive.

## 1. Repository baseline (inspected before any change)

- **`LlmProvider`** (`voiceagent/providers/engines/contracts.py:323-332`) is
  a `Protocol` with one method, `stream_turn(messages, tools) ->
  AsyncIterator[str | ToolCallRequested | TurnEnded]`. No vendor type
  anywhere in its signature.
- **`voiceagent/providers/llm/_openai_compatible.py`** already implements
  the *exact* wire mechanics OpenAI's own Chat Completions API uses
  (`POST /chat/completions`, Bearer auth, SSE `data: {...}` chunks
  terminated by `data: [DONE]`, `choices[0].delta.content`/`.tool_calls`) --
  it is not a coincidence: this is literally the format Groq and Mistral
  cloned from OpenAI (their own module docstrings say so), which is why its
  own docstring already promised "a third OpenAI-compatible vendor ... is a
  five-line new module reusing this class, never a change to this file."
  **OpenAI support did not exist as a registered provider before this
  phase**, but the machinery it needed already did.
- **`voiceagent/providers/llm/registry.py`**
  (`LLM_PROVIDERS: ProviderRegistry[LlmProvider]`) already supports
  provider selection by name with zero `if provider == ...` branching
  (`voiceagent/providers/registry.py`'s own docstring: "Adding a new
  provider should require only: 1. provider adapter, 2. provider-specific
  configuration, 3. provider adapter tests, 4. registration in the provider
  resolver"). Confirmed adequate as-is -- **not rewritten**.
- **Provider selection is per-`AgentVersion`, not a global env var**
  (`voiceagent/providers/engines/factory.py:build_conversation_engine()`
  calls `create_llm_provider(engine.llm.provider, engine.llm.model,
  engine.llm.config)` from a database row's own `EngineSelection`).
  `AiProviderSettings.eligible_providers`
  (`voiceagent/config/settings.py`) is the one genuinely deployment-wide
  surface -- a policy allowlist (`voiceagent/runtime/privacy.py`), not a
  provider selector. This phase preserves that split exactly (section 6/17).
- **`ConversationEngine`/`PipelinedEngine`/`RealtimeEngine`**: untouched.
  `RealtimeEngine` still only has `"fake"` registered
  (`voiceagent/providers/engines/factory.py:REALTIME_PROVIDERS`) --
  ADR-0009 names OpenAI Realtime as that engine's own eventual first
  candidate, still explicitly deferred; this phase is `PipelinedEngine`'s
  `LlmProvider` leg only, unrelated to that deferred decision.
- **A genuine, pre-existing gap found and fixed**: neither
  `OpenAiCompatibleLlmProvider` nor any of `GroqLlmConfig`/
  `MistralLlmConfig` had *any* generation-bounding field (no
  `temperature`, no `max_tokens`) -- the wire body sent only
  `model`/`messages`/`stream`/`tools`. Phase 2.20 brief section 5 asks for
  "bounded generation settings required by the existing adapter"; there
  were none to require. See section 8.
- **Existing tests**: `tests/providers/llm/test_openai_compatible_adapter.py`
  already covers the shared wire mechanics (success, tool-call assembly
  across chunks, malformed-chunk dropping, the full HTTP status ->
  `EngineErrorCode` mapping, transport failure) via `httpx.MockTransport`.
  It did **not** cover request timeout or mid-request cancellation --
  closed in this phase (section 13), benefiting Groq/Mistral too, not just
  OpenAI. `test_groq_adapter.py`/`test_mistral_adapter.py` are thin,
  factory-only tests (secret resolution, default endpoint) -- mirrored for
  `test_openai_adapter.py`.
- **Fakes**: `voiceagent/providers/engines/component_fakes.FakeLlmProvider`,
  registered as `"fake"` in `LLM_PROVIDERS`, untouched -- still the default
  for every hermetic test.
- **Phase 2.19's `voiceagent/config/validation.py`**: `validate_deployment_
  readiness()` already rejects the unconfigured `eligible_providers ==
  ("fake",)` default under staging/production and checks vendor-secret
  presence for every name in that policy list via a
  `_PROVIDER_SECRET_NAMES` map -- `"openai"` was simply missing from that
  map (added, section 6/9).

## 2. Architecture preserved

```text
ConversationEngine (unchanged)
       |
       v
  LlmProvider (Protocol, unchanged)
       |
       +-- fake (tests)
       +-- gemini
       +-- mistral  --\
       +-- groq       +-- share voiceagent.providers.llm._openai_compatible
       +-- openai   --/    .OpenAiCompatibleLlmProvider (wire mechanics)
       +-- (future Anthropic/other providers: register(), nothing above changes)
```

No OpenAI-specific type, SDK object, HTTP response object, or error type
leaks past `voiceagent/providers/llm/openai.py` and the shared
`_openai_compatible.py` module: both are confined behind the exact same
import-linter contract that already fences Groq/Mistral/Gemini ("LLM vendor
adapters stay behind voiceagent.providers.llm.registry",
`pyproject.toml`) -- verified unmodified and passing (section 16).

**What must change to add a fourth real provider (e.g. Anthropic)**: one
new adapter module (`voiceagent/providers/llm/anthropic.py`) implementing
`LlmProvider`, its own typed config model, one `LLM_PROVIDERS.register(...)`
call. Nothing in `ConversationEngine`, `PipelinedEngine`, the Call Runtime,
the Tool Gateway, or any API contract needs to change -- exactly the
invariant section 6 names: "adding OpenAI must not make switching providers
an architectural rewrite."

## 3. OpenAI adapter (new)

`voiceagent/providers/llm/openai.py` -- a five-line-shaped module (matching
`groq.py`/`mistral.py` exactly):

- `OpenAiLlmConfig` (pydantic, `extra="forbid"`): `model` (required, no
  default -- see section 15), `endpoint` (default
  `https://api.openai.com/v1`), `timeout_seconds` (default `30.0`),
  `temperature`/`max_tokens` (both optional, `None` by default -- section 8).
- `create_openai_llm_provider(config)`: resolves `OPENAI_API_KEY` through
  `infra.secrets.get_secrets_provider().get_required(...)` -- identical
  pattern to every other vendor, never a plain env-var read, never a
  deployment-global credential field on any `Settings` dataclass (Phase 0
  report section 2.4 G-3, unchanged).
- Registered: `LLM_PROVIDERS.register("openai", create_openai_llm_provider)`
  in `voiceagent/providers/llm/registry.py` -- the only registry change.

## 4. Provider configuration

| Level | Field | Notes |
|---|---|---|
| `AgentVersion.engine.llm` (DB, per-agent) | `provider: "openai"`, `model`, `config: {endpoint?, timeout_seconds?, temperature?, max_tokens?}` | The real selection mechanism (section 17) -- unchanged shape, `openai` is just a new valid `provider` string. |
| `VOICEAGENT_AI_ELIGIBLE_PROVIDERS` (deployment-wide policy) | must include `"openai"` for any agent to be allowed to select it | `voiceagent/runtime/privacy.py`'s existing allowlist; unchanged mechanism. |
| `OPENAI_API_KEY` (secret) | real credential | Read via `infra.secrets`, never through `Settings`. |

No `OPENAI_ONLY` flag, no "every agent must use OpenAI" assumption, and no
new provider-management subsystem were introduced (section 6/16): OpenAI is
one more name in an already-open registry.

## 5. Model selection

`model` remains a required field with **no default** on `OpenAiLlmConfig`,
identical to `GroqLlmConfig`/`MistralLlmConfig` -- model choice was already
fully external to the adapter and stays that way. `scripts/
smoke_test_openai_provider.py --model` defaults to `gpt-4o-mini` purely as
an illustrative example (documented in its own `--help` text as "verify
current availability yourself before relying on this default") -- a
`WebFetch` against OpenAI's current model-listing page during this phase
returned an inconsistent/unverifiable result (unfamiliar model names that
could not be corroborated against any second source), so this document
makes no claim about which specific OpenAI model is current as of this
phase's write date; it documents that the field is configuration an
operator sets at deploy/agent-publish time, never something this codebase
hard-codes an assumption about.

## 6. Bounded generation settings (the one shared-module change)

`voiceagent/providers/llm/_openai_compatible.py`'s `OpenAiCompatibleLlmProvider`
gained `temperature: float | None` and `max_tokens: int | None` constructor
parameters, included in the wire request body only when not `None`. Before
this phase, **no LLM adapter in this codebase could bound a generation's
length at all** -- a real gap for a call platform, where an unbounded
completion is both a latency and a cost risk. `GroqLlmConfig`/
`MistralLlmConfig`/`OpenAiLlmConfig` all gained the identical two optional
fields (`max_tokens` validated `> 0`); an agent that sets neither gets
exactly the wire body this class always sent -- purely additive, verified by
a dedicated test (`test_temperature_and_max_tokens_are_included_only_when_set`).

## 7. Real provider request path (traced, unchanged shape)

```text
ConversationEngine (PipelinedEngine)
    -> LlmProvider.stream_turn(messages, tools)
    -> OpenAiCompatibleLlmProvider.stream_turn()   [voiceagent/providers/llm/_openai_compatible.py]
    -> httpx POST https://api.openai.com/v1/chat/completions  [real OpenAI API]
    -> SSE chunks parsed into str | ToolCallRequested | TurnEnded
    -> yielded back to PipelinedEngine, exactly as any other vendor's stream
```

No OpenAI response schema (a raw `choices[]`/`delta` dict, an SDK object)
ever leaves `_openai_compatible.py`'s `stream_turn()` -- confirmed by
reading every line of that method (unchanged apart from the two new,
optional request fields) and by `test_openai_compatible_adapter.py`'s
existing assertions on the *normalized* output shape only.

## 8. HTTP, timeout, and error-taxonomy behavior (reused, not reimplemented)

All already true of the shared class before this phase, verified to still
hold and now exercised for OpenAI specifically:

- Explicit request timeout: `httpx.AsyncClient(timeout=self._timeout)`,
  operator-configured via `timeout_seconds` (default `30.0`).
- No unbounded response buffering: the response body is consumed line by
  line via `response.aiter_lines()` inside the streaming `async with`, never
  read fully into memory.
- HTTP status classification (`_status_error`, unchanged):
  `401`/`403` -> `AUTH`; `429` -> `RATE_LIMIT`; `400`/`422` ->
  `INVALID_REQUEST`; `>=500` -> `PROVIDER_DOWN`; anything else ->
  `TRANSIENT` (truncated body, never the full text, in the message).
- `httpx.TimeoutException` -> `TRANSIENT`; `httpx.TransportError` ->
  `TRANSIENT`.
- Malformed responses fail safe, not loud: an unparseable SSE chunk or a
  chunk missing `choices[0]` is logged at `WARNING` and skipped, never
  raised -- a single bad chunk cannot abort an otherwise-good stream.
- Cancellation: nothing in this module catches `asyncio.CancelledError`
  (confirmed by reading the file) -- a cancelled consumer task propagates
  the cancellation normally, unwinding the `async with
  httpx.AsyncClient()/client.stream()` context managers on the way out
  (their own `__aexit__` closes the connection). Verified by a new test
  (section 13) mirroring the STT adapter's own existing cancellation test.
- Retries: **none exist, and none were added.** An interactive voice call
  cannot afford an LLM adapter silently retrying and stalling the
  conversation -- a failure surfaces immediately as an `EngineException`
  for `PipelinedEngine`'s own, pre-existing turn-failure handling to decide
  what happens next (unchanged by this phase).

## 9. Error taxonomy

Reused, not extended: `EngineErrorCode` still has exactly five values
(`AUTH`, `RATE_LIMIT`, `TRANSIENT`, `INVALID_REQUEST`, `PROVIDER_DOWN`,
`voiceagent/providers/engines/contracts.py`) -- OpenAI's failures map onto
them via the identical `_status_error()` every OpenAI-compatible vendor
already shares. No raw OpenAI error payload is ever raised to a caller
(only a truncated, non-secret status/body-excerpt string inside the
message); no API key can appear in an exception string (it is never
interpolated into one anywhere in this file).

## 10. Timeout and cancellation -- explicitly tested

New tests in `tests/providers/llm/test_openai_compatible_adapter.py`
(exercising the exact class OpenAI uses):

- `test_request_timeout_maps_to_transient` -- `httpx.ReadTimeout` ->
  `EngineException(TRANSIENT)`.
- `test_cancellation_during_the_request_propagates_and_is_not_swallowed` --
  a task awaiting `stream_turn()` against a handler that never responds is
  cancelled mid-request; `await task` raises `asyncio.CancelledError`, not
  an `EngineException` and not a hang.

"Caller hangup during provider request" and "runtime shutdown during
provider request" are the identical mechanism at the `PipelinedEngine`
layer (`_turn_task.cancel()` on `interrupt()`/`close()`,
`voiceagent/providers/engines/pipelined.py`) -- already covered by that
module's own existing test suite (`tests/providers/test_pipelined_engine_
hardening.py`), unchanged by this phase, and exercised transitively through
any `LlmProvider` including the new OpenAI one (the engine has no
vendor-specific cancellation path to begin with).

## 11. Fake-provider behavior in staging

Unchanged and reconfirmed: `LLM_PROVIDERS` still registers `"fake"` for
hermetic tests. `voiceagent.config.validation.validate_deployment_readiness`
(Phase 2.19, extended this phase only by adding `"openai"` to its secret-name
map) already rejects `VOICEAGENT_AI_ELIGIBLE_PROVIDERS` left at its
unconfigured `("fake",)` default under `deployment_stage in ("staging",
"production")` -- fail-closed, not merely discouraged. Selecting `openai`
without `OPENAI_API_KEY` configured is caught by the same function's
existing per-provider secret-presence check, now covering `openai` too
(`tests/config/test_deployment_validation.py::test_missing_openai_secret_
is_rejected`).

## 12. Real-provider smoke test (new)

`scripts/smoke_test_openai_provider.py` -- standalone, clearly separate from
`scripts/smoke_test_staging.py`. Checks, only when `OPENAI_API_KEY` is
actually configured: the secret is present; the adapter constructs; a
bounded (`max_tokens=8`) request completes within 30s; the response is
normalized (counts `str` deltas / `ToolCallRequested` / a closing
`TurnEnded` -- never prints the generated text itself); no secret is
printed anywhere. Exit codes distinguish **pass (0)**, **attempted and
failed (1)**, and **skipped -- no credential (2)**, so a CI/operator script
can never read a skip as a pass.

**Executed for real in this environment** (no legitimate OPENAI_API_KEY
available here, so full success could not be claimed -- section 14):

- With no `OPENAI_API_KEY` set: printed `SKIPPED: OPENAI_API_KEY is not
  configured...`, exit code `2`. Confirms the honest no-credential path.
- With a deliberately invalid key, against the **real** `https://api.openai.
  com/v1/chat/completions` endpoint (a single, bounded, harmless request --
  no account, no spend, no rate-limit risk): OpenAI's real server returned
  `401`, correctly classified by the adapter as `EngineException(AUTH,
  "openai: auth rejected (401)")`, and the script reported `FAIL request:
  auth: ...`, exit code `1`. **This is real evidence** the adapter reaches
  the correct real endpoint with the correct request/auth shape (a
  malformed request would have produced a different status), even though it
  does not exercise the success path.
- **Not validated**: a successful real completion (needs a real,
  billable `OPENAI_API_KEY`, unavailable in this environment). This is the
  one genuine gap between "CI/provider-contract validation" (fully done,
  section 13) and "real external-provider validation" (partially done: the
  auth/error path only, for real; the success path only via mocks).

## 13. Automated tests (deterministic, mocked transport)

- `tests/providers/llm/test_openai_compatible_adapter.py` (extended, +3
  tests; used by Groq/Mistral/OpenAI alike): success/tool-call/malformed
  cases were already present; added timeout, cancellation, and
  temperature/max_tokens wire-inclusion tests.
- `tests/providers/llm/test_openai_adapter.py` (new, mirrors
  `test_groq_adapter.py`): secret resolution succeeds; missing secret raises
  `EngineException(AUTH)`; default endpoint is `https://api.openai.com/v1`;
  unknown config fields are rejected (`extra="forbid"`); non-positive
  `max_tokens` is rejected; unset generation settings default to `None`
  (omit the wire field).
- `tests/config/test_deployment_validation.py` (extended, +2 tests):
  missing `OPENAI_API_KEY` is rejected when `openai` is an eligible
  provider; present, it is accepted.

Security-specific, confirmed by reading (not merely asserted): the API key
is interpolated only into the `Authorization` header dict passed to
`httpx`, never into a log line, an f-string exception message, or any
telemetry attribute anywhere in `openai.py`/`_openai_compatible.py`; no test
prints or asserts against a real secret value (the one fake key literal used
in tests carries a `# pragma: allowlist secret` annotation, verified against
`detect-secrets`, section 16).

## 14. Observability

No new metrics/logging were added -- Phase 2.14's own pattern (bounded
labels, no prompt/response/tenant/call-id cardinality) already applies
unchanged to a request through this adapter, since nothing about OpenAI
required a new metric. The one `_logger.warning(...)` call in
`_openai_compatible.py` (a dropped malformed chunk) logs the vendor label
only, never chunk content -- unchanged, reviewed for this phase, not
modified.

## 15. AgentVersion compatibility

Unchanged and preserved deliberately: `LlmComponentConfig`
(`voiceagent/agents/config.py`) still carries `provider`, `model`, and a
free-form `config: dict`, all already flowing through
`build_conversation_engine()` untouched. Choosing `openai` is an
`AgentVersion` publish-time decision like any other provider; staging
environment configuration only supplies the credential and the
deployment-wide eligibility policy, never the per-agent selection --
exactly the split section 17 asks to preserve, not hide.

**Known, pre-existing, un-closed gap** (not introduced or worsened by this
phase): `AiProviderSettings.default_engine` still has no real consumer
(confirmed by grep, unchanged since Phase 2.19's own report) -- the true
per-conversation selector remains the database row. Documented here again
because it is directly adjacent to what this phase touches, not because
this phase changed it.

## 16. Registry/factory

`voiceagent/providers/llm/registry.py`: one `register()` call added, one
import added, one docstring line updated to name `OpenAiLlmConfig`. No
other line changed. `voiceagent/providers/engines/factory.py`: **not
touched** -- it already depended only on `create_llm_provider()`, never on
a provider name.

## 17. Security

Preserved, none weakened: tenant isolation, RLS/RBAC, Tool Gateway boundary,
audit logging, and runtime ownership are all upstream of
`PipelinedEngine`/`LlmProvider` and untouched by this phase. The OpenAI
adapter receives exactly what `PipelinedEngine` already assembles for any
provider (turn messages, `ToolSpec`s the Tool Gateway itself already
scoped) -- no new database access, no new tool-execution path, and no
change to how or whether the LLM's own tool-call requests are authorized
(`voiceagent.tools`, untouched). The LLM remains untrusted exactly as
before: `ToolCallRequested` is a value the engine hands to the existing
gateway, never something the adapter itself executes.

## 18. Telephony boundary

Not touched. No ESL/media transport work was done; no claim is made that a
real phone call works. This phase is the LLM provider leg only, as scoped.

## 19. Known limitations

- A real, successful OpenAI completion was not exercised end-to-end in this
  environment -- no billable `OPENAI_API_KEY` was available. The
  auth/error-classification path *was* verified against the real live
  endpoint (section 12); the success path is verified only by mocked
  transport tests (section 13).
- The current-model-availability `WebFetch` returned an unverifiable
  result; this document deliberately does not assert which specific model
  is "current" as of the write date (section 5).
- `AiProviderSettings.default_engine` remains dead configuration
  (pre-existing, Phase 2.19's own documented limitation, unchanged).
- Gemini's own (non-OpenAI-compatible) adapter was not extended with
  `temperature`/`max_tokens` -- out of scope for an OpenAI-focused phase;
  noted as a natural, small follow-up.
- Real telephony end-to-end remains unimplemented, as explicitly scoped out
  (section 18).

## 20. Deviations from the brief

- Section 8's "bounded generation settings" required a small change to the
  *shared* `_openai_compatible.py` module (not just a new OpenAI-only file)
  because no adapter using it had any generation bound before this phase --
  the brief's own section 16 ("if adding OpenAI exposes that the registry
  cannot safely distinguish providers... make the smallest necessary
  correction") is read here as covering this adjacent, concrete,
  pre-existing gap in the shared wire-mechanics module the registry
  dispatches to, not only the registry file itself.
- The two Groq/Mistral config models (`GroqLlmConfig`/`MistralLlmConfig`)
  also gained the two new optional fields, for consistency and because they
  share the exact class being fixed -- not a scope expansion of what OpenAI
  itself needed, but the smallest change that avoids two now-inconsistent
  sibling config models.

## 21. Quality gates

- Backend tests: **all passed** (Phase 2.19's 851 plus this phase's new
  tests; exact count in the final report).
- Ruff: clean. Ruff format: clean.
- Pyright: 0 errors, 0 warnings.
- import-linter: 8/8 contracts kept (the LLM vendor-confinement contract
  covers `openai.py` automatically -- it is named nowhere outside
  `voiceagent.providers.llm`).
- detect-secrets: clean; the one fake test API-key literal carries a
  documented `pragma: allowlist secret` annotation.
- pip-audit: no known vulnerabilities.
- Real, live-endpoint verification: see section 12.

## 22. Recommendation

Implementation complete; ready for review. This phase does not commit --
per its own instructions, the implementation stops here for separate review.
