# django-prod deploy

Greenfield production stack for the AlphaLens briefs UI + API.

## Topology

```
            ┌───────────────────────────────────────────────┐
            │ Cloudflare tunnel + Access (SSO + JWT)        │
            └────────────────────┬──────────────────────────┘
                                 │
                            ┌────▼─────┐
                            │  nginx   │  static SPA + /api proxy
                            └────┬─────┘
                                 │
                            ┌────▼──────┐
                            │  django   │  gunicorn + uvicorn workers
                            │  (DRF)    │
                            └────┬──────┘
                                 │
                            ┌────▼──────┐
                            │ postgres  │  briefs + days_meta + users
                            └───────────┘
```

The daily parquet output (`~/.alphalens/thematic_briefs/*.parquet`) is
bind-mounted read-only into the django container; cache rebuild is a
one-shot `docker compose run --rm rebuild-cache`.

## Bring it up

```bash
cd deploy/docker/django-prod
cp .env.example .env
# fill in SECRET_KEY, POSTGRES_PASSWORD, CF_ACCESS_*, ALLOWED_HOSTS
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:8080/healthz
```

## Refresh briefs from parquet

```bash
docker compose --profile maintenance run --rm rebuild-cache
```

Schedule this from the host via systemd timer or cron — there is no
in-container scheduler by design (one-shot containers keep state
inspectable from outside).

## Bring up the SPA

```bash
cd ../../../web
pnpm install --frozen-lockfile
pnpm build
# nginx mounts apps/web/build/ via the SPA_DIST env var in .env
```

## Greenfield notes

There is **no parallel deploy** in this stack — the original migration
plan called for canary 5% routing through nginx, but a greenfield project
has no production traffic to canary against. Cut over the Cloudflare
tunnel to this stack in one move.

The legacy FastAPI service (`alphalens/api/`) is dropped in F8 along
with the `briefs-api` docker artifact and the old systemd unit.
