# django-prod deploy

Production stack for the AlphaLens briefs API. Migration plan B:
- **API (here)** — Django + Postgres on the VPS, image pulled from GHCR
- **SPA** — Cloudflare Pages (see `apps/web/README.md`)
- **Edge** — Cloudflare Tunnel from VPS to `api.<domain>`, mapped to `127.0.0.1:8000`

```
            ┌───────────────────────────────────────────────┐
            │ CF Pages (SPA) ── browser ── CF Access (SSO) │
            └────────────────────┬──────────────────────────┘
                                 │ XHR to api.<domain>
                            ┌────▼──────┐
                            │ cloudflared│ host process, public
                            └────┬──────┘
                                 │ 127.0.0.1:8000
                            ┌────▼──────┐
                            │  django   │ gunicorn + uvicorn
                            │  (DRF)    │ image pulled from GHCR
                            └────┬──────┘
                                 │
                            ┌────▼──────┐
                            │ postgres  │ briefs + days_meta + users
                            └───────────┘
```

The daily parquet output (`~/.alphalens/thematic_briefs/*.parquet`) is
bind-mounted read-only into the django container; cache rebuild is a
one-shot `docker compose run --rm rebuild-cache`.

## Two compose files

| File | Auto-loaded? | Used where |
|------|--------------|------------|
| `docker-compose.yaml` | Always | VPS canonical: pulls `ghcr.io/kamilpajak/alphalens-django:${ALPHALENS_DJANGO_TAG:-latest}`, no nginx, no SPA mount, binds `127.0.0.1:8000` |
| `docker-compose.override.yaml` | Yes, when no `-f` flag | Local dev: builds django from the workspace Dockerfile, brings up nginx with `apps/web/build/` bind-mount on `${NGINX_HTTP_PORT:-8080}` |

Compose merges them automatically. To run the VPS-shaped stack locally,
pass `-f docker-compose.yaml` to skip the override.

## VPS bring-up

```bash
# One-time setup
cd ~/AlphaLens/deploy/docker/django-prod
cp .env.example .env
# Fill in SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS,
# CORS_ALLOWED_ORIGINS, CORS_ALLOWED_ORIGIN_REGEXES,
# CORS_ALLOW_CREDENTIALS, CF_ACCESS_TEAM, CF_ACCESS_AUD
# Uncomment COMPOSE_FILE=docker-compose.yaml so a stray `up -d` cannot
# load the local-dev override.
echo $GHCR_PAT | docker login ghcr.io -u kamilpajak --password-stdin

# Deploy / re-deploy
docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
```

**Rollback** — edit `ALPHALENS_DJANGO_TAG` in `.env` (e.g. `sha-883574d`)
and re-run `docker compose up -d`. Do NOT pin inline
(`ALPHALENS_DJANGO_TAG=... docker compose up -d`) — the next `up -d`
without it would silently roll forward to `:latest`. The `.env` file is
the single source of truth.

Downtime during `up -d`: ~2-5 s of 502 from cloudflared while gunicorn
boots the new container. Compose stops the old container before
starting the new one — this is NOT zero-downtime. Acceptable for the
internal buy-side tool.

## Refresh briefs from parquet

```bash
docker compose --profile maintenance run --rm rebuild-cache
```

Schedule from the host via systemd timer; no in-container scheduler by
design (one-shot containers keep state inspectable from outside). Unit
lives in `deploy/systemd/`.

## Local dev (full UI stack)

```bash
# From repo root:
pnpm --filter web build                                # SPA bundle → apps/web/build/
just up                                                # auto-loads override + builds django locally
curl -fsS http://localhost:8080/healthz
open http://localhost:8080/brief/<date>
```

The `pnpm build` step is mandatory on macOS Docker Desktop — if the
`apps/web/build/` directory is missing at container start, the bind-mount
silently produces an empty dir and nginx serves a 403 + redirection cycle.
CLAUDE.md "workflow conventions" documents the gotcha + restart workaround.

## Cloudflare config

- **Pages project** — `apps/web/README.md` has the dashboard runbook
- **Tunnel** — cloudflared runs as a host process, route `api.<domain>` → `http://localhost:8000`
- **Access app for `api.<domain>`** — Google SSO policy + enable "Bypass Access for HTTP OPTIONS requests" so CORS preflights from the SPA pass through
