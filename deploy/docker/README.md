# VPS Docker deployment

Two artifacts live under this directory:

- **`django-prod/`** — production stack (Django API + Postgres + nginx +
  static SPA). Replaces the legacy FastAPI deploy. See
  [`django-prod/README.md`](django-prod/README.md) for bring-up.
- **`Dockerfile.pipeline`** — image that runs the daily thematic ingest +
  brief generation. Invoked by the systemd timer, not by docker compose;
  the pipeline writes parquet to `~/.alphalens/thematic_briefs/` and a
  separate one-shot syncs it into Postgres.

```
┌─────────────────────────────────────────────────────────────┐
│ jacoren@vps  ~/AlphaLens/                                   │
│                                                             │
│  ~/.alphalens/thematic_briefs/  ── parquet ──┐              │
│           ▲                                  ▼              │
│           │                  ┌─────────────────────────┐    │
│           │                  │ rebuild-cache (one-shot)│    │
│           │                  │  django-prod stack      │    │
│           │                  └────────────┬────────────┘    │
│           │                               │ ON CONFLICT     │
│ ┌─────────┴───────┐                       ▼                 │
│ │ pipeline image  │             ┌─────────────────┐         │
│ │ alphalens cli   │             │ postgres        │         │
│ │ (systemd timer) │             │  briefs +       │         │
│ └─────────────────┘             │  days_meta      │         │
│                                 └────────┬────────┘         │
│                                          │                  │
│                                  ┌───────▼────────┐         │
│                                  │ django (DRF)   │         │
│                                  │  /v1/* API     │         │
│                                  └────────┬───────┘         │
│                                           │                 │
│                                  ┌────────▼───────┐         │
│                                  │ nginx          │ ────►   │
│                                  │  static SPA +  │  Cloud- │
│                                  │  /api/ proxy   │  flare  │
│                                  └────────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

## Bring-up summary

1. Daily ingest image: `docker build -f deploy/docker/Dockerfile.pipeline -t alphalens-pipeline:latest .`
2. Production stack: `cd deploy/docker/django-prod && cp .env.example .env && docker compose up -d --build`
3. systemd timer for the daily run: `deploy/systemd/` ([README](../systemd/README.md))

The legacy FastAPI service + SQLite cache (`alphalens/api/`,
`~/.alphalens/api/briefs.db`) is no longer part of this deploy — F8 of
the Django migration removed it. See
`apps/alphalens-django/docs/migration-log.md` for the full migration
history.
