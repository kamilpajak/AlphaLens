# VPS Docker deployment

Three services, one compose file, one systemd timer. Web UI is served by
nginx; a long-running FastAPI process (`api`) exposes briefs over REST and
nginx reverse-proxies `/api/` to it; the thematic pipeline runs on demand
inside its own image and refreshes the SQLite cache the api reads.

```
┌──────────────────────────────────────────────────────────────────┐
│ jacoren@vps  ~/AlphaLens/  (git checkout of main)                │
│                                                                  │
│  ~/.alphalens/             ─── bind-mount ──┐                    │
│    thematic_briefs/                          ▼                   │
│    api/briefs.db   ◄── rebuild-cache ── ┌─────────────────────┐  │
│    …                                    │ alphalens-pipeline  │  │
│           ▲                             │  python:3.13-bookw. │  │
│           │ ro mount via                └─────────────────────┘  │
│           │ ~/.alphalens                  ▲   on systemd timer   │
│           │                               │                      │
│  ┌────────┴──────────┐                    │                      │
│  │ alphalens-api     │ ── 127.0.0.1:8086 ─┘                      │
│  │ uvicorn /v1/*     │                                           │
│  └────────┬──────────┘                                           │
│           │ docker DNS `api:8000`                                │
│           ▼                                                      │
│  ┌──────────────────────────┐                                    │
│  │ alphalens-web            │ ── 127.0.0.1:8085 ─►               │
│  │ nginx:1-alpine           │           Cloudflare ► <sub>       │
│  │   /api/ → http://api/    │                                    │
│  └──────────────────────────┘                                    │
└──────────────────────────────────────────────────────────────────┘
```

## One-time bootstrap

### 1. Seed caches that aren't already on the VPS

Form-4 and AV cache are already provisioned by their own backfills. The
thematic pipeline additionally needs `companyfacts_parquet/`, `factors/`,
`prices/`, and (for the initial brief history) `thematic_briefs/`:

```bash
# from Mac
rsync -av ~/.alphalens/companyfacts_parquet/ jacoren@vps:.alphalens/companyfacts_parquet/
rsync -av ~/.alphalens/factors/              jacoren@vps:.alphalens/factors/
rsync -av ~/.alphalens/prices/               jacoren@vps:.alphalens/prices/
rsync -av ~/.alphalens/thematic_briefs/      jacoren@vps:.alphalens/thematic_briefs/
# Optional but speeds the first run: other thematic_*/ caches.
# They self-regenerate from API calls if missing.
rsync -av ~/.alphalens/thematic_news/        jacoren@vps:.alphalens/thematic_news/
rsync -av ~/.alphalens/thematic_press/       jacoren@vps:.alphalens/thematic_press/
rsync -av ~/.alphalens/thematic_tenk/        jacoren@vps:.alphalens/thematic_tenk/
rsync -av ~/.alphalens/thematic_etf_holdings/ jacoren@vps:.alphalens/thematic_etf_holdings/
```

### 2. Drop API keys

```bash
# on VPS
cd ~/AlphaLens
cp deploy/docker/.env.example deploy/docker/.env
chmod 600 deploy/docker/.env
$EDITOR deploy/docker/.env
# fill GOOGLE_API_KEY, POLYGON_API_KEY, ALPHA_VANTAGE_API_KEY
```

### 3. Build + start the web and api images

```bash
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml build
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml up -d web api
docker ps --filter name=alphalens   # both Up / healthy
```

At this point the api's SQLite cache doesn't exist yet, so `/api/v1/days`
returns 503 and the web UI shows the empty-state placeholder. Move on to
the first cache build.

### 4. Seed the api cache from the briefs you rsync'd in step 1

```bash
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml \
    run --rm pipeline api rebuild-cache
curl -fsS http://127.0.0.1:8085/api/v1/days | jq '.meta'
```

### 5. Install the systemd timer for autonomous daily runs

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-thematic-daily.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-thematic-daily.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-thematic-daily.timer
systemctl --user list-timers alphalens-thematic-daily  # next fire ≈ 06:30 UTC
```

### 6. Wire Cloudflare

Web (anonymous read): same pattern as the other apps (e.g.
`gridfinitylabels.com`) — point a subdomain at the VPS and send traffic
to `127.0.0.1:8085`.

API (auth-gated): point `api.<your-subdomain>` at `127.0.0.1:8086` via
Cloudflare Tunnel and front it with Cloudflare Access (Google SSO for the
browser, Service Tokens for bots). Full operator manual:
`deploy/cloudflare/access_setup.md`.

> **Port note:** Host-side bind is 8085, not 8080, because CrowdSec's
> local API already holds 127.0.0.1:8080 on this VPS. 8085 is the next
> free slot in the reserved 8085-8089 production range per
> `/home/jacoren/CLAUDE.md`. Container-internal ports are unchanged
> (nginx on 80, api on 8000); only the host-side mapping moves.

## Day-to-day operations

### Trigger the pipeline manually

```bash
systemctl --user start alphalens-thematic-daily.service
journalctl --user -u alphalens-thematic-daily.service -f
```

### Rebuild after a code change

```bash
cd ~/AlphaLens && git pull
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml build
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml up -d web api
```

The web container keeps serving the SPA shell during the build. The api
container shares the alphalens-pipeline image, so the `up -d api` step
is mandatory — otherwise it keeps running the old code. The pipeline
image picks up the new build at the next timer fire.

### Inspect what nginx is reverse-proxying

```bash
curl -fsS http://127.0.0.1:8085/api/v1/days | jq '.meta'
curl -fsS http://127.0.0.1:8085/api/v1/days/2026-05-18 | jq '.n_candidates'
```

### Inspect what the api is serving

```bash
curl -fsS http://127.0.0.1:8086/readyz | jq
curl -fsS http://127.0.0.1:8086/v1/days | jq '.data[0]'
curl -fsS http://127.0.0.1:8086/v1/stats | jq
# OpenAPI / Swagger
open http://127.0.0.1:8086/docs   # browser (via Cloudflare Access)
```

### Manually rebuild the api SQLite cache

The pipeline image's ENTRYPOINT is `/app/.venv/bin/alphalens`, so the
arguments start with the typer subcommand — no leading `alphalens`.

```bash
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml \
    run --rm pipeline api rebuild-cache
# --force ignores the parquet mtime gate
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml \
    run --rm pipeline api rebuild-cache --force
# After a manual rebuild, restart the api container so it re-opens the
# refreshed cache. The systemd timer does this automatically via
# ExecStartPost; manual rebuilds must do it explicitly.
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml \
    restart api
```

### Disable the timer

```bash
systemctl --user disable --now alphalens-thematic-daily.timer
```

## Known issues / behaviour notes

- The pipeline container runs **as `${UID}:${GID}`** from the host so writes
  to `~/.alphalens/` are jacoren-owned. The compose file defaults to
  `1000:1000` if `UID`/`GID` aren't exported. Systemd passes them via
  `%U`/`%G` in the service unit.
- `HOME=/app/home` in the pipeline container is mandatory — every cache
  path in `alphalens.thematic.*` resolves via `Path.home() / ".alphalens"`.
  Without it the bind-mount target wouldn't match the code.
- nginx serves `/api/*` with `Cache-Control: no-cache` and `/_app/<hash>`
  immutable for 30 days. New briefs are visible within the browser's
  next request (~no cache miss); hashed JS/CSS still cache aggressively.
- Pipeline image build pulls `phase-robust-backtesting` from git — needs
  outbound HTTPS to GitHub during build.
- `Type=oneshot` systemd unit blocks overlap by default — if a previous
  run is still going when the next 06:30 UTC fire would trigger, the timer
  skips. No `flock` wrapper required.
- The `api` service reuses the `alphalens-pipeline` image (`fastapi` +
  `uvicorn[standard]` are in the project's main dependencies). Rebuilding
  the pipeline image with `docker compose build pipeline` and then
  `docker compose up -d api` picks up the new image; the daily timer
  refresh of the SQLite cache is unaffected.
- The api cache (`~/.alphalens/api/briefs.db`) is a derived artifact — safe
  to delete; the next `alphalens api rebuild-cache` reconstructs it from
  the parquet briefs.
- The api opens the cache with `?mode=ro&immutable=1` so it can serve from
  a `:ro` bind-mount. `immutable=1` disables SQLite's change detection,
  which is racy against a concurrent writer — a request opening mid-write
  could read inconsistent rows. The daily systemd unit closes this window
  with `ExecStartPost=docker compose restart api` after the pipeline
  succeeds; manual `api rebuild-cache` invocations must restart the api
  container explicitly (see "Manually rebuild the api SQLite cache" above).
