# AI Call Agent Platform

A multi-tenant AI call-agent platform: businesses deploy AI voice agents that
answer and place phone calls, understand callers, take controlled actions, book
appointments, transfer to humans, and keep a searchable call history.

FreeSWITCH is the telephony core. [SaaS-OS](https://github.com/aelboum/saas-os)
is an external, pinned dependency providing tenancy, identity, authorization,
audit, secrets, idempotency, billing and infrastructure primitives.

```
Product (voiceagent)  ->  SaaS-OS  ->  Infrastructure
```

## Status: Phase 1 — foundation, IN PROGRESS

Phase 0 (architecture) and Phase 0.1 (blocker resolution) are complete and
approved. Phase 1 builds the repository foundation only: project structure,
dependency consumption, configuration, the migration foundation, the tenant
context boundary, the application shell, the architecture fences, tests, CI and
a frontend scaffold.

**There is no domain yet, by design.** No agent, call, contact, calendar, tool,
workflow, recording, STT, LLM, TTS, FreeSWITCH or runtime code exists. See
[`docs/PHASE-1-STATUS.md`](docs/PHASE-1-STATUS.md) for exactly what is built,
what is deliberately absent, and what Phase 2 does next.

## Naming (ADR-0005)

Four names, decoupled so the expensive ones never depend on the cheap ones:

| | Value |
|---|---|
| Commercial product name | **deliberately undecided** — configuration only (`VOICEAGENT_APP_DISPLAY_NAME`), never a literal in code |
| Repository | `ai-agent` |
| Python package / distribution | `voiceagent` |
| Database schema | `app` — frozen permanently; it carries no product identity so a rename never implies a schema migration |
| API namespace | `/v1/<resource>` — no brand, no package name in any path |

## The SaaS-OS pin

```
saas-os @ git+https://github.com/aelboum/saas-os@ff550010e5eafecace7311038aadc99fcecfbe3d
```

Binding rules (ADR-0001): an exact commit SHA, never a branch, tag, editable
path, submodule or vendored copy. SaaS-OS is never forked, copied or modified
from this repository — a needed platform change is made there, released there,
and consumed here by re-pinning. Re-pinning is a deliberate, human-reviewed
act; automated dependency upgrades are prohibited.

`saas-os` is this product's only runtime dependency. FastAPI, SQLAlchemy,
Alembic, psycopg, Redis, ARQ, OpenTelemetry and cryptography all arrive
transitively through it, so the product cannot drift from the platform's stack.

## Architecture summary

```
                    PSTN / SIP trunks
                            |
                      FreeSWITCH (PBX core)      -- Phase 2
                     /                  \
             control (ESL)          media (PCM over WebSocket)
                    |                       |
        TelephonyProvider            MediaProvider          <- product contracts
                    \                       /
                     Call Orchestrator / Runtime            -- Phase 2
                                |
                        ConversationEngine                  <- product contract
                        /                \
            PipelinedEngine            RealtimeEngine       -- Phase 2
                                |
                          Tool Gateway                      -- Phase 2
                                |
       application services -> voiceagent.db -> infra.db -> PostgreSQL (RLS)
```

Package layout:

```
voiceagent/
  api/          Composition root (build_app), /v1 routers, error seam
  config/       Typed product configuration
  db/           The persistence seam over infra.db
  tenancy/      TenantContext: the product's tenant boundary
  telephony/    TelephonyProvider + MediaProvider contracts, fakes
    freeswitch/   The only place FreeSWITCH may be imported (empty in Phase 1)
  providers/    External provider contracts
    engines/      ConversationEngine contract + a deterministic fake
  runtime/      The AI call runtime (empty in Phase 1)
migrations/     The product's own Alembic history (imports SQLAlchemy directly)
frontend/       Independent frontend scaffold (React + Vite)
docs/           Architecture, ADRs, phase status
```

## Development setup

Requires Python 3.13+ and Node 22+.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; `source .venv/bin/activate` elsewhere
pip install -e ".[dev,security]"

cp .env.example .env          # fill in locally; .env is gitignored

cd frontend && npm install && cd ..
```

Run the API and the frontend:

```bash
uvicorn voiceagent.api.asgi:app --reload    # http://localhost:8000
cd frontend && npm run dev                  # proxies /v1 to the API
```

Apply migrations (platform history first, then this product's — SaaS-OS
ADR-0016 fixes that order):

```bash
saas-os-migrate upgrade    # the platform's own history
alembic upgrade head       # this product's history
```

## Test and validation commands

```bash
bash scripts/check-backend.sh    # ruff, ruff format, pyright, pytest, lint-imports
bash scripts/check-security.sh   # detect-secrets, .env not tracked
bash scripts/check-frontend.sh   # tsc, vite build
bash scripts/check-all.sh        # all of the above
```

CI runs these same scripts, so "passing locally" and "passing in CI" cannot
diverge. While iterating, run the tools directly (`pytest -k tenancy`,
`ruff check .`, `lint-imports`).

## Architecture boundaries

Four rules, each enforced twice — by an import-linter contract in
`pyproject.toml` and by an AST scan in `tests/architecture/` that runs with
nothing installed. Both run in CI, and both were verified to fail when
deliberately violated.

1. **Application code never imports SQLAlchemy or psycopg** (ADR-0007).
   `infra.db` withholds `sqlalchemy.text` and `sqlalchemy.func` on the strength
   of two live audits: either one lets application code run
   `set_config('app.tenant_id', ...)` inside a tenant-scoped session and bypass
   Row-Level Security for the rest of the transaction. Persistence goes through
   `voiceagent.db`. **Migrations may import SQLAlchemy** and own physical
   PostgreSQL types — they emit DDL in their own transaction and never run
   inside a tenant-scoped session.
2. **Pipecat stays behind `voiceagent.providers.engines.pipecat`** (ADR-0006).
   It is an optional extra, deliberately not installed in Phase 1, never
   forked, never monkeypatched. The `ConversationEngine` contract is
   product-owned and framework-free, which is what keeps the runtime rewrite
   Phase 0 prohibits structurally impossible.
3. **FreeSWITCH stays behind `voiceagent.telephony.freeswitch`** (ADR-0002).
   Domain code sees `TelephonyProvider`, `MediaProvider` and a normalized
   `HangupCause` — never an ESL command or a carrier string.
4. **No vendor SDK above its adapter.** `boto3` appears nowhere in the product;
   object storage is a contract with no implementation yet.

Architecture decisions live in [`docs/ADR/`](docs/ADR): SaaS-OS consumption and
pin (0001), FreeSWITCH as telephony core (0002, amended with the
provider boundaries), Tool Gateway mediation (0003), immutable agent versions
(0004), naming (0005), ConversationEngine strategy (0006), persistence boundary
(0007).

## Non-goals

Durable, not phase-scoped:

- **Not a CRM.** Contacts stay minimal: no pipelines, deals, custom fields,
  segments or activity feeds.
- **Not a marketing or outbound-campaign platform.** No campaign runner, lead
  lists, dialer or drip sequences.
- **Not a general business-automation or iPaaS platform.** Workflows are *call*
  workflows only.
- **Not an accounting or invoicing product.** Billing is SaaS-OS's
  `core.billing`/`core.usage` plus a payment provider.
- **Not a general-purpose PBX or softswitch.** FreeSWITCH is; the product does
  not reimplement SIP, RTP or call control.
- **Not a CPaaS abstraction over many telephony vendors.** Carrier variation is
  a SIP-trunk concern, not a provider class.
- **Not a fork or vendor** of SaaS-OS, Dograh or Pipecat.
- **Not a chat/omnichannel platform in v1.**
- **No tenant-authored code execution** and **no generic outbound HTTP tool**
  until each passes its own security review.
