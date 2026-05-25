# VPS Docker deployment

Two artifacts live under this directory:

- **`django-prod/`** — API stack (Django + Postgres). Pulls
  `ghcr.io/kamilpajak/alphalens-django` per migration plan B. nginx is
  local-dev only (via `docker-compose.override.yaml`); on the VPS the
  SPA lives on Cloudflare Pages and cloudflared maps `api.<domain>` →
  `127.0.0.1:8000` directly. See [`django-prod/README.md`](django-prod/README.md)
  for the full VPS bring-up + rollback runbook.
- **`Dockerfile.pipeline`** — image that runs the daily thematic ingest
  + brief generation. Invoked by the systemd timer, not by docker
  compose; the pipeline writes parquet to `~/.alphalens/thematic_briefs/`
  and a separate one-shot syncs it into Postgres.

```
┌───────────────────────────────────────────────────────────────┐
│ CF Pages (SPA)                                                │
│   apps/web/build/ → app.<domain>                              │
└─────────────────────────────┬─────────────────────────────────┘
                              │ XHR /api/* → api.<domain>
                              │ (CF Access cookie cross-origin)
                              ▼
┌───────────────────────────────────────────────────────────────┐
│ jacoren@vps  ~/AlphaLens/                                     │
│                                                               │
│ ┌─────────────┐                                               │
│ │ cloudflared │  api.<domain> → http://127.0.0.1:8000         │
│ └──────┬──────┘                                               │
│        │                                                      │
│ ~/.alphalens/thematic_briefs/  ── parquet ──┐                 │
│           ▲                                 ▼                 │
│           │                  ┌─────────────────────────┐      │
│           │                  │ rebuild-cache (one-shot)│      │
│           │                  │  django-prod stack      │      │
│           │                  └────────────┬────────────┘      │
│           │                               │ ON CONFLICT       │
│ ┌─────────┴───────┐                       ▼                   │
│ │ pipeline image  │             ┌─────────────────┐           │
│ │ alphalens cli   │             │ postgres        │           │
│ │ (systemd timer) │             │  briefs +       │           │
│ └─────────────────┘             │  days_meta      │           │
│                                 └────────┬────────┘           │
│                                          │                    │
│                                  ┌───────▼────────┐           │
│                                  │ django (DRF)   │           │
│                                  │  /v1/* API     │           │
│                                  │  GHCR pull     │           │
│                                  └────────────────┘           │
└───────────────────────────────────────────────────────────────┘
```

## Bring-up summary

1. **Daily pipeline image** — `docker build -f deploy/docker/Dockerfile.pipeline -t alphalens-pipeline:latest .` (built on the VPS; not pushed to a registry today).
2. **API stack** — `cd deploy/docker/django-prod && cp .env.example .env`, fill in secrets + uncomment `COMPOSE_FILE=docker-compose.yaml` for VPS, `docker login ghcr.io`, then `docker compose pull && docker compose up -d`. Full runbook + rollback recipe in [`django-prod/README.md`](django-prod/README.md).
3. **systemd timer** — `deploy/systemd/` ([README](../systemd/README.md)) for daily pipeline + rebuild-cache wiring.
4. **Cloudflare** — Pages project for the SPA per [`../../apps/web/README.md`](../../apps/web/README.md); Tunnel routes `api.<domain>` → `http://localhost:8000`; Access app for the API origin needs Google SSO policy + "Bypass Access for HTTP OPTIONS" enabled.

The legacy FastAPI service + SQLite cache (`alphalens/api/`,
`~/.alphalens/api/briefs.db`) is no longer part of this deploy — F8 of
the Django migration removed it. See
`apps/alphalens-django/docs/migration-log.md` for the full migration
history.
