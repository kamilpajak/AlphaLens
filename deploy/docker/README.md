# VPS Docker deployment

Two images, one compose file, one systemd timer. Web UI is served by nginx;
the thematic pipeline runs on demand inside its own image and refreshes the
JSON the web container serves.

```
┌──────────────────────────────────────────────────────────────┐
│ jacoren@vps  ~/AlphaLens/  (git checkout of main)            │
│                                                              │
│  ~/.alphalens/             ─── bind-mount ──┐                │
│    form4_parquet/                            ▼               │
│    av_cache/                       ┌─────────────────────┐   │
│    companyfacts_parquet/           │ alphalens-pipeline  │   │
│    thematic_briefs/  ── exporter ► │ python:3.13-bookw.  │   │
│    …                               └─────────────────────┘   │
│                                              │               │
│  ~/AlphaLens/web-data/  ◄────────────────────┘ writes JSON   │
│      days.json                       ▲                       │
│      days/<date>.json                │                       │
│              ▲                       │                       │
│              │ bind-mounted          │ docker compose run    │
│              │ as /usr/share/nginx/  │ --rm pipeline …       │
│              │ html/data:ro         on systemd timer         │
│  ┌──────────────────────────┐                                │
│  │ alphalens-web            │ ── 127.0.0.1:8080 ─►           │
│  │ nginx:1-alpine           │           Cloudflare ► <sub>   │
│  └──────────────────────────┘                                │
└──────────────────────────────────────────────────────────────┘
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

### 3. Build + start the web image

```bash
mkdir -p web-data
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml build
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml up -d web
docker ps --filter name=alphalens-web   # should be Up / healthy
```

At this point `web-data/` is empty so `/data/days.json` will 404. Move on
to the first pipeline run.

### 4. Seed JSON from the briefs you rsync'd in step 1

```bash
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml \
    run --rm --entrypoint /app/.venv/bin/python pipeline \
    /app/scripts/export_briefs_to_json.py --out /web-data
ls web-data/days.json web-data/days/    # should show jacoren-owned files
curl -fsS http://127.0.0.1:8080/data/days.json | jq length
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

Same pattern as your other apps (e.g. `gridfinitylabels.com`): point a
subdomain at the VPS, send traffic to `127.0.0.1:8080`. This file does not
manage Cloudflare.

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
UID="$(id -u)" GID="$(id -g)" docker compose -f deploy/docker/docker-compose.yml up -d web
```

The pipeline image rebuild only matters for the next timer fire — the
running web container keeps serving JSON from the bind-mount.

### Inspect what nginx is serving

```bash
curl -fsS http://127.0.0.1:8080/data/days.json | jq '.[0]'
ls -la web-data/days/ | head
```

### Disable the timer

```bash
systemctl --user disable --now alphalens-thematic-daily.timer
```

## Known issues / behaviour notes

- The pipeline container runs **as `${UID}:${GID}`** from the host so writes
  to `~/.alphalens/` and `web-data/` are jacoren-owned. The compose file
  defaults to `1000:1000` if `UID`/`GID` aren't exported. Systemd passes
  them via `%U`/`%G` in the service unit.
- `HOME=/app/home` in the pipeline container is mandatory — every cache
  path in `alphalens.thematic.*` resolves via `Path.home() / ".alphalens"`.
  Without it the bind-mount target wouldn't match the code.
- nginx serves `/data/*.json` with `Cache-Control: no-cache, must-revalidate`
  and `/_app/<hash>` immutable for 30 days. New briefs are visible within
  the browser's next request (~no cache miss); hashed JS/CSS still cache
  aggressively.
- Pipeline image build pulls `phase-robust-backtesting` from git — needs
  outbound HTTPS to GitHub during build.
- `Type=oneshot` systemd unit blocks overlap by default — if a previous
  run is still going when the next 06:30 UTC fire would trigger, the timer
  skips. No `flock` wrapper required.
