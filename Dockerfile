# Production image for every backend process this product runs: the API
# (`voiceagent.api.asgi:app`), the call-runtime (`scripts/run_call_runtime.py`),
# the two background workers (`scripts/run_followup_worker.py`,
# `scripts/run_call_intelligence_worker.py`), and the migration step
# (`alembic`/`saas-os-migrate`). One image, many commands -- these processes
# share the identical dependency set and source tree; building a separate
# image per process would duplicate the build for no isolation benefit this
# product's process boundaries (Phase 2.18 brief, Workstream B) don't already
# get some other way (a distinct `docker-compose.yml` service + command per
# process, see that file).
#
# Two stages: `builder` has a C toolchain and git (needed once, to build the
# SaaS-OS git dependency and any wheel with a native extension); `runtime`
# copies only the resulting installed packages and this product's source --
# no compiler, no git, no pip cache ships in the final image.
#
# requires-python = ">=3.13" (pyproject.toml) -- pinned to the exact minor
# version verified in Phase 2.17's own validation (Python 3.13.14).

FROM python:3.13-slim AS builder

# git: required by `pip install .` to resolve the pinned
# `saas-os @ git+https://...@<sha>` dependency (pyproject.toml, ADR-0001).
# build-essential: covers any transitive dependency with a native extension;
# not removed here because this whole stage is discarded, not shipped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installed into a venv (not system site-packages) purely so the whole
# directory can be copied into the runtime stage as one unit.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# pyproject.toml + README.md first: `pip install .` needs both (setuptools
# reads `readme = "README.md"` while building package metadata) but neither
# changes as often as the source tree, so this layer caches across ordinary
# source edits.
COPY pyproject.toml README.md ./
COPY voiceagent ./voiceagent
RUN pip install --no-cache-dir .

FROM python:3.13-slim AS runtime

# Non-root execution (Phase 2.18 brief, Workstream C). A fixed, low, non-system
# uid/gid -- not `--system` (which picks an arbitrary free id at build time,
# making the image's own uid non-reproducible across rebuilds).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

# The application source, plus what a migration or a script needs at runtime
# (Alembic reads `migrations/` and `alembic.ini` from the working directory,
# not from anything `pip install` puts in site-packages).
COPY voiceagent ./voiceagent
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN chown -R app:app /app
USER app

EXPOSE 8000

# No default CMD: `docker-compose.yml` sets the actual command per service
# (api / call-runtime / follow-up worker / call-intelligence worker /
# migration step) against this one image -- there is no one "right" default
# process for an image four different service roles share.
