# ADR-0001: SaaS-OS consumption model and dependency pin

Status: Accepted (Phase 0)
Date: 2026-09-21

## Context

This product is a consumer of SaaS-OS, which is developed in its own repository
(`github.com/aelboum/saas-os`). SaaS-OS's own ADR-0015 ("SaaS OS distribution
and independent-consumer boundary", Accepted) already fixes the terms: SaaS-OS
is a business-domain-agnostic foundation distributed as a Python package;
consuming projects live in their own repositories and own their own database,
deployment, secrets and domain code; a consumer depends on SaaS-OS and SaaS-OS
must never depend on a consumer; consumption is a pinned Git/VCS dependency with
an exact commit SHA preferred over a mutable tag.

That ADR evaluated and rejected the alternatives (monorepo, template-with-copied
source, Git submodule, and a centrally hosted SaaS-OS network runtime). This
product has no reason to revisit any of them, and revisiting one unilaterally
would violate a decision binding on the dependency itself.

At the time of this decision, inspection of the dependency found: `HEAD` =
`ff550010e5eafecace7311038aadc99fcecfbe3d`, on branch `main`, equal to the
tracked `origin/main` (`git rev-list --left-right --count origin/main...HEAD`
= `0 0`), with a clean working tree, and **no release tags published at all**
(`git tag` returns nothing).

## Decision

1. This product consumes SaaS-OS as an external Python package dependency,
   pinned to the **exact commit SHA**:

   ```
   saas-os @ git+https://github.com/aelboum/saas-os@ff550010e5eafecace7311038aadc99fcecfbe3d
   ```

2. SaaS-OS source is never copied, vendored, forked, or submoduled into this
   repository.
3. SaaS-OS is never modified from this repository. A required change to the
   platform is proposed in the platform's repository, released there, and
   consumed here by re-pinning.
4. This product imports only SaaS-OS's public surfaces: `core.*`, `infra.*`,
   `api.*` (the reusable library surface classified in SaaS-OS ADR-0017),
   `control_plane.*`, and `contracts.*`. It never imports `api.main` or
   `api.server` as its own application, and it never reaches into a private
   module of any of them.
5. This product builds its own FastAPI application with
   `api.platform.build_platform_app()` and owns its own composition root,
   entrypoint, configuration and deployment.
6. This product owns its own PostgreSQL database and its own Alembic migration
   history, applying SaaS-OS's migrations through the installed package
   (`infra.db.migration_runner.run_core_migrations()` or the `saas-os-migrate`
   console script), never by pointing its own Alembic at the dependency's
   filesystem path (SaaS-OS ADR-0016).
7. Re-pinning is a deliberate, human-reviewed act. Automatic upgrades
   (`pip install -U`, dependency bots, floating refs) are prohibited. Each
   re-pin records the reviewed diff and the reason.

## Rejected alternatives

- **Floating `main` or a branch ref** — reproducibility loss and a supply-chain
  exposure; also contrary to SaaS-OS ADR-0015 rule 8.
- **Tag-based pin** — no tags exist, and SaaS-OS's own ADR prefers a SHA over a
  mutable tag regardless.
- **Vendoring / forking** — rejected by SaaS-OS ADR-0015, and demonstrated
  costly in practice by Dograh's vendored Pipecat fork (four separate
  provenance/rebase/compatibility documents to maintain).
- **Copying the reference consumer wholesale** — `examples/reference-consumer/`
  is a CI validation fixture, not a product scaffold. It is read as a pattern.

## What would be difficult to change later

Rule 3 (never modify the dependency from here). The first local patch to
SaaS-OS — even a temporary one — makes every subsequent re-pin a merge, and the
boundary that makes this product's architecture legible stops being real.

## What is deliberately not decided here

Whether SaaS-OS eventually publishes to a private package index (its own open
question); the re-pin cadence and reviewer (Phase 0 report §19 OD-12); and
whether this product ever contributes changes upstream.

## Related

SaaS-OS ADR-0015, ADR-0016, ADR-0017, ADR-0018; `docs/PHASE-0-ARCHITECTURE.md`
§2.
