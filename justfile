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

# Lint Python (both apps) + check web TS
lint:
    uv run ruff check apps/alphalens-research apps/alphalens-django
    cd apps/web && pnpm run check

# Format Python (both apps)
fmt:
    uv run ruff format apps/alphalens-research apps/alphalens-django

# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------

# Research engine tests (unittest)
test-research:
    uv run python -m unittest discover \
        -s apps/alphalens-research/tests \
        -t apps/alphalens-research -v

# Django app tests (pytest)
test-django:
    cd apps/alphalens-django && uv run pytest

# Web hermetic Playwright tests
test-web:
    cd apps/web && pnpm test

# Everything in series — same order as CI
test: test-research test-django test-web

# -------------------------------------------------------------------------
# Dev servers
# -------------------------------------------------------------------------

# Django dev server (briefs API at 127.0.0.1:8000)
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

# Bring up the Django prod stack (Postgres + Django + nginx)
up:
    docker compose -f deploy/docker/django-prod/docker-compose.yaml up -d

down:
    docker compose -f deploy/docker/django-prod/docker-compose.yaml down

# Rebuild the briefs cache (one-shot: parquets -> Postgres)
rebuild-cache:
    docker compose -f deploy/docker/django-prod/docker-compose.yaml \
        --profile maintenance run --rm rebuild-cache
