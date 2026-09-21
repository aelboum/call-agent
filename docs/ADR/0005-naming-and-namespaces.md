# ADR-0005: Product, repository, package and API naming

Status: Accepted (Phase 0.1)
Date: 2026-09-21
Resolves: Phase 0 report §19 OD-1

## Context

Phase 0 used `callagent` as an explicit placeholder for the Python package name
and flagged it as a blocker: renaming is cheap before migrations and a database
schema exist, and expensive afterwards. The placeholder must not become a
permanent architectural dependency by default.

Four distinct names are routinely conflated, and each has a different cost of
change:

| Name | Cost of changing later |
|---|---|
| Commercial/product name | Low for the codebase; marketing and UI strings only |
| Repository name | Low — a remote rename plus local remotes |
| Python package/module name | Moderate — mechanical import churn, but it touches every file |
| Database schema name | **High** — a live migration of every table, index, policy and grant |
| Public API path namespace | **High** — a published contract with clients |

Two constraints are absolute: the name must encode no vendor (ElevenLabs,
FreeSWITCH, Dograh, Fonio, Pipecat), and the architecture must survive a change
of commercial name without a code migration. A third is practical: the installed
`saas-os` package already occupies the top-level import names `core`, `infra`,
`api`, `control_plane` and `contracts`, so the product may not use any of them.

## Decision

The four names are decoupled and fixed independently:

1. **Commercial/product name: deliberately UNDECIDED.** Choosing a brand now
   would be inventing one to fill a field. It is not a blocker, because nothing
   in the codebase depends on it. When chosen, it appears only in user-facing
   strings and configuration (a single `APP_DISPLAY_NAME` setting and the
   frontend), never in a module path, a schema name, or a URL.
2. **Python package / distribution name: `voiceagent`.** One top-level package,
   importable as `voiceagent`, distribution name `voiceagent`. It is
   descriptive of the domain (voice AI agents), not of a brand, not of a vendor,
   short, a valid Python identifier, and does not collide with any name the
   `saas-os` dependency occupies.
3. **Database schema name: `app` — frozen, permanently.** Product tables live in
   schema `app` (`app.agents`, `app.call_sessions`, …), alongside the platform's
   `core.*`. The schema name is deliberately *not* the package name: it carries
   no product or domain identity, so it never needs to change when either the
   package or the commercial name changes, and a schema rename is the single
   most expensive rename in the list. It also reads unambiguously against
   `core.*` at every join: platform tables vs this application's tables.
4. **API path namespace: version-first and resource-shaped**, e.g.
   `/v1/agents`, `/v1/agents/{id}/versions`, `/v1/calls`, `/v1/contacts`. No
   brand, no product name, no package name in any path. The application
   namespace inside the code is `voiceagent.api`, mounted onto the app built by
   `api.platform.build_platform_app()`.
5. **Repository name: `ai-agent`** — the existing directory name, retained. It
   is already neutral and carries no brand; renaming it buys nothing and breaks
   existing local paths.
6. **`callagent` is retired.** Wherever Phase 0 documentation writes
   `callagent.*`, read `voiceagent.*`. Phase 0 documents are not rewritten for
   this (the placeholder is recorded as history); no code has been written under
   the placeholder, so there is nothing to migrate.
7. **Rename policy.** If the package name ever changes, that change is an import
   rewrite and a distribution rename only. It must not require a schema
   migration, an API path change, or a data migration. Any design that would
   couple those is rejected on sight.

## Alternatives considered

- **Keep `callagent`** — rejected: the brief requires the placeholder be
  replaced by a deliberate choice, and "callagent" reads as a product brand
  while being nobody's brand.
- **Invent a commercial brand now (e.g. a coined word) and use it everywhere** —
  rejected: it fills the field without information, and it guarantees a
  four-way rename when marketing eventually chooses something else.
- **Namespace package (`acme.voiceagent`)** — rejected: namespace packages buy
  distribution flexibility this product does not need (one repository, one
  distribution, never published), at the cost of import verbosity and packaging
  subtleties.
- **Schema named after the package (`voiceagent`)** — rejected: it welds the
  most expensive name to the cheapest one for cosmetic consistency.
- **A cryptic acronym (`vap`, `cap`)** — rejected: unreadable, and short
  top-level names carry a real import-shadowing risk.

## Consequences

- Phase 1 creates exactly one top-level package, `voiceagent/`, and one
  schema, `app`.
- Before creating it, Phase 1 verifies in a clean virtual environment with the
  pinned dependencies installed that `import voiceagent` fails — proving no
  transitive dependency already occupies the name.
- A commercial name can be chosen at any time with no code impact. That is the
  property this ADR is buying.
- Documentation written during Phase 0 retains the `callagent` placeholder; the
  mapping in point 6 is the authority.

## What would be difficult to change later

Point 3 (schema `app`) and point 4 (API paths). Both are recorded now precisely
because they are the expensive ones, and both are deliberately chosen to be
independent of every name that might plausibly change.

## Related

`docs/PHASE-0-ARCHITECTURE.md` §19 OD-1, §22.
