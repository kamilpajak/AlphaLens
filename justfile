# AlphaLens monorepo orchestrator.
#
# Run `just` (no args) for the help screen, or `just <recipe>`.
#
# Prereqs: just (https://github.com/casey/just), uv, pnpm.

default:
    @just --list

# -------------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------------

# Install every Python member + dev tools + web deps. Single venv at ./.venv
sync:
    uv sync
    cd apps/web && pnpm install

# Refresh the lockfile after pyproject changes
lock:
    uv lock

# -------------------------------------------------------------------------
# Lint / format
# -------------------------------------------------------------------------

# Lint Python (all members) + check web TS
lint:
    uv run ruff check apps/alphalens-pipeline apps/alphalens-research apps/alphalens-django
    cd apps/web && pnpm run check

# Format Python (all members)
fmt:
    uv run ruff format apps/alphalens-pipeline apps/alphalens-research apps/alphalens-django

# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------

# Pipeline + research unittests. Test files live under apps/alphalens-research/
# tests/ (workspace install lets them import from either package) — see
# PR2 split-pipeline-package commit for the rationale.
test-python:
    uv run python -m unittest discover \
        -s apps/alphalens-research/tests \
        -t apps/alphalens-research -v

# Django app tests (pytest)
test-django:
    cd apps/alphalens-django && uv run pytest

# Web hermetic Playwright tests
test-web:
    cd apps/web && pnpm test

# L3 golden-master replay (test-strategy Phase 3): the VCR cassette infra +
# brief-stage replay (research) + the ingest→DRF golden (Django). Hermetic —
# runs off frozen fixtures, no network. Refresh fixtures with
# apps/alphalens-research/scripts/record_golden_brief.py (needs OPENROUTER_API_KEY).
test-golden:
    uv run python -m unittest discover \
        -s apps/alphalens-research/tests/golden \
        -t apps/alphalens-research -v
    cd apps/alphalens-django && uv run pytest briefs/tests/test_golden_api.py -q

# Everything in series — same order as CI
test: test-python test-django test-web

# -------------------------------------------------------------------------
# Dev servers
# -------------------------------------------------------------------------

# Django dev server (briefs API at 127.0.0.1:8000).
# WARNING: conflicts with `just up` — the local Docker stack also binds
# 127.0.0.1:8000 (canonical compose ports). Stop the stack first or pass
# `runserver 127.0.0.1:8001` if you need both running.
dev-django:
    cd apps/alphalens-django && uv run python manage.py runserver

# Web dev server (SvelteKit at 127.0.0.1:5173)
dev-web:
    cd apps/web && pnpm dev

# -------------------------------------------------------------------------
# Docker builds
# -------------------------------------------------------------------------

# Build the pipeline image (research engine — daily thematic ingest)
docker-pipeline:
    docker build -f deploy/docker/Dockerfile.pipeline -t alphalens-pipeline:latest .

# Build the Django prod image
docker-django:
    docker build -f deploy/docker/django-prod/Dockerfile -t alphalens-django:latest .

# Build both
docker: docker-pipeline docker-django

# Bring up the local dev stack — builds django locally + spins nginx with
# bind-mount of apps/web/build (Compose auto-loads docker-compose.override.
# yaml). For the VPS-shaped stack (pull from GHCR, no nginx), pass an
# explicit -f to skip the override: `docker compose -f docker-compose.yaml
# up -d`.
up:
    cd deploy/docker/django-prod && docker compose up -d

down:
    cd deploy/docker/django-prod && docker compose down

# Rebuild the briefs cache (one-shot: parquets -> Postgres)
rebuild-cache:
    cd deploy/docker/django-prod && docker compose \
        --profile maintenance run --rm rebuild-cache
