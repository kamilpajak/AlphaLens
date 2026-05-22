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

# Copy only the dependency manifest first so the layer cache survives
# source edits.
COPY apps/alphalens-django/pyproject.toml ./pyproject.toml

RUN uv venv .venv --python python3.13 \
    && uv pip install --python .venv/bin/python -e . "gunicorn>=26.0"

# Now copy the source modules explicitly. A blanket
# ``COPY apps/alphalens-django/ ./`` would clobber the .venv directory
# created above, because Docker's .dockerignore only applies when it
# lives at the build context root — our scoped one next to the
# Dockerfile is read by buildkit as a per-Dockerfile ignore but the
# semantics are non-portable, so we just list directories explicitly.
COPY apps/alphalens-django/manage.py ./manage.py
COPY apps/alphalens-django/config/ ./config/
COPY apps/alphalens-django/briefs/ ./briefs/
COPY apps/alphalens-django/auth_cf/ ./auth_cf/
COPY apps/alphalens-django/core/ ./core/
# Final install is non-editable so site-packages owns the source rather than
# a .pth pointer back to /app. Deps already resolved in the previous layer.
RUN uv pip install --python .venv/bin/python --no-deps --force-reinstall .


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# Non-root user — Django reads ``$HOME/.cache`` on some code paths so we
# create the home dir even though the app itself doesn't need it.
RUN groupadd --system --gid 1000 django \
    && useradd --system --uid 1000 --gid django --home /home/django --create-home django

# libpq for psycopg's binary distribution + curl for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=django:django /app /app

# Collect static at build time so the runtime container is read-only.
# DEBUG=False is the default in prod settings; we feed a throwaway
# SECRET_KEY purely so the management command can boot.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    SECRET_KEY=collectstatic-only \
    ALLOWED_HOSTS=localhost \
    DATABASE_URL=sqlite:///tmp/collectstatic.db \
    /app/.venv/bin/python manage.py collectstatic --noinput

USER django

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
