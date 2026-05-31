# syntax=docker/dockerfile:1.7

# Multi-stage build for the Django app. Stage 1 (`builder`) uses the uv
# image to resolve + install dependencies into a venv; stage 2 (`runtime`)
# is a slim Python image that only carries the venv and source. This keeps
# the runtime image free of uv, build tools, and the dependency resolver
# cache.

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------------------
# Stage 1 — resolve + install dependencies
# ---------------------------------------------------------------------------
# The Debian-slim variant of the uv image is required: the distroless
# ``:VERSION`` tag has no shell, which breaks RUN steps. ``python3.13-
# bookworm-slim`` carries both uv and a system python out of the box.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy the workspace manifests + lockfile. uv sync --frozen needs every
# member's pyproject.toml to validate the workspace dependency graph,
# even when --package narrows the install to a single member.
COPY pyproject.toml uv.lock ./
COPY apps/alphalens-pipeline/pyproject.toml apps/alphalens-pipeline/pyproject.toml
COPY apps/alphalens-research/pyproject.toml apps/alphalens-research/pyproject.toml
COPY apps/alphalens-django/pyproject.toml apps/alphalens-django/pyproject.toml

# Install the alphalens-django member's transitive PyPI deps from the
# frozen lockfile. --no-install-workspace skips the workspace members
# themselves (source isn't copied yet); --package narrows resolution to
# the django dep subtree so the runtime image doesn't pull pandas+
# lightgbm+statsmodels from the research stack. --no-dev excludes the
# workspace-wide dev tools group (ruff/pytest/pyright/etc.).
RUN uv sync --frozen --no-dev --no-install-workspace --package alphalens-django

# Copy the django package source AFTER the deps layer so source edits
# don't bust the (expensive) PyPI install cache.
COPY apps/alphalens-django/manage.py apps/alphalens-django/manage.py
COPY apps/alphalens-django/config/ apps/alphalens-django/config/
COPY apps/alphalens-django/briefs/ apps/alphalens-django/briefs/
COPY apps/alphalens-django/auth_cf/ apps/alphalens-django/auth_cf/
COPY apps/alphalens-django/core/ apps/alphalens-django/core/
COPY apps/alphalens-django/market/ apps/alphalens-django/market/

# Install the django workspace member itself (no deps re-resolution —
# already locked into .venv above). --no-editable so site-packages owns
# the source rather than a .pth pointer back to /app/apps/...
RUN uv sync --frozen --no-dev --no-editable --package alphalens-django


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# WORKDIR is the django member dir so `manage.py` + `config/` are at cwd
# (gunicorn finds `config.asgi:application` via the default cwd PYTHONPATH).
# The .venv lives one level up at /app/.venv (workspace root).
WORKDIR /app/apps/alphalens-django

# Non-root user — Django reads ``$HOME/.cache`` on some code paths so we
# create the home dir even though the app itself doesn't need it.
RUN groupadd --system --gid 1000 django \
    && useradd --system --uid 1000 --gid django --home /home/django --create-home django

# libpq for psycopg's binary distribution + curl for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=django:django /app /app

USER django

# Collect static at build time so the runtime container is read-only.
# Run as `django` (not root) so the generated staticfiles/ tree is owned
# by the runtime user — any future WhiteNoise compression / startup
# rewrite avoids PermissionError. /tmp is world-writable, so the
# throwaway SQLite DB still creates fine. DEBUG=False is the default in
# prod settings; we feed a throwaway SECRET_KEY purely so the management
# command can boot.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    SECRET_KEY=collectstatic-only \
    ALLOWED_HOSTS=localhost \
    DATABASE_URL=sqlite:///tmp/collectstatic.db \
    /app/.venv/bin/python manage.py collectstatic --noinput

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Gunicorn with the uvicorn ASGI worker — DRF view functions are sync, but
# Django 5.2+ has async middleware/orm support we may grow into. uvicorn
# workers run sync views in a threadpool, so this is a no-cost upgrade
# over plain gunicorn sync workers.
CMD ["gunicorn", \
    "--bind", "0.0.0.0:8000", \
    "--workers", "2", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--access-logfile", "-", \
    "--error-logfile", "-", \
    "config.asgi:application"]
