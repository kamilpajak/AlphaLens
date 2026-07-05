# AlphaLens monitoring (Prometheus + Grafana + Alertmanager)

PR-2 of the cron-observability epic wires every active AlphaLens
systemd-user unit on the VPS into the Prometheus textfile collector
that node_exporter already exposes. PR-3 (not yet merged) ships alert
rules + Alertmanager Telegram routing + a Grafana dashboard on top.

## What this PR delivers

   (Output dir = `$ALPHALENS_TEXTFILE_DIR`; in prod
   `/var/lib/node_exporter/textfile`, see the wiring section below. The
   `~/.alphalens/metrics` path mentioned below is the dev/test fallback only.)

1. **Bash hook** `deploy/systemd/bin/alphalens-emit-job-metrics` —
   called as `ExecStopPost=` from every active service. Writes
   `$ALPHALENS_TEXTFILE_DIR/alphalens_job_<job>.prom` with cron-health
   gauges (last_run, last_duration, last_exit_code, last_success).
   Atomic via tempfile + `mv`.

2. **Python helper**
   `alphalens_pipeline/observability/textfile.py::emit_domain_metrics` —
   called from each CLI success-path. Writes
   `$ALPHALENS_TEXTFILE_DIR/alphalens_domain_<job>.prom` with
   domain-specific gauges (events detected, briefs written, AV
   quota remaining, etc.). Atomic via `os.replace`.

3. **ExecStopPost hooks on the active units**:
   - `alphalens-edgar-detect.service`
   - `alphalens-literature-scan-weekly.service`
   - `alphalens-literature-scan-monthly.service`
   - `alphalens-thematic-build.service`

   The `alphalens-form4-backfill.service` is excluded — it is a
   long-running daemon that completed its bulk run on 2026-05-08
   and would produce a single end-of-run point.

## node_exporter wiring (LIVE VPS config)

> **The scrape dir is `/var/lib/node_exporter/textfile`, NOT
> `~/.alphalens/metrics`.** The live VPS node_exporter container runs
> `--collector.textfile.directory=/var/lib/node_exporter/textfile` with an
> identity bind mount of that path, and `/etc/alphalens/env` sets
> `ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile` so **every**
> emitter writes there. This section was originally written against
> `~/.alphalens/metrics` (the Python `DEFAULT_DIR` fallback); the live
> wiring moved to a dedicated system dir and the docs are kept in sync here.

Both halves of the metric stream must land in the one scraped dir:

- **Host emitters** (the bash `ExecStopPost` hook + the host-venv CLI
  commands like `feedback backfill-shadow-returns`) read
  `ALPHALENS_TEXTFILE_DIR` from `/etc/alphalens/env` → write to
  `/var/lib/node_exporter/textfile`.
- **Container emitter** (the thematic-build pipeline image, which emits the
  5 stage gauges + the VIX freshness gauge from inside `docker run`) gets an
  explicit `-e ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile` plus an
  identity `-v /var/lib/node_exporter/textfile:/var/lib/node_exporter/textfile`
  mount in `alphalens-thematic-build.service`. Without both, the container
  falls back to `Path.home()/.alphalens/metrics` (the unscraped `~/.alphalens`
  bind mount) and its gauges never reach Prometheus.

The live node_exporter container (recreate to match):

```bash
docker run -d --name node-exporter \
    --restart always \
    --net host \
    --pid host \
    -v /:/rootfs:ro,rslave \
    -v /var/lib/node_exporter/textfile:/var/lib/node_exporter/textfile \
    prom/node-exporter:latest \
    --path.rootfs=/rootfs \
    --collector.textfile.directory=/var/lib/node_exporter/textfile
```

The scrape dir must be writable by the operator UID (the systemd-user units +
the `--user %U:%G` pipeline container both write there as the operator).

Verify after restart:

```bash
sudo mkdir -p /var/lib/node_exporter/textfile && sudo chown "$USER" /var/lib/node_exporter/textfile
systemctl --user start alphalens-edgar-detect.service
ls -la /var/lib/node_exporter/textfile/   # alphalens_{job,domain}_edgar-detect.prom
curl -s localhost:9100/metrics | grep '^alphalens_'
```

## Metric reference

### Cron-health (emitted by every unit's ExecStopPost)

| Metric | Type | Description |
|---|---|---|
| `alphalens_job_last_run_timestamp_seconds{job}` | gauge | Unix time of last invocation (success or failure). |
| `alphalens_job_last_duration_seconds{job}` | gauge | Wall-clock seconds of last invocation. |
| `alphalens_job_last_exit_code{job}` | gauge | Exit status of last invocation (0 = success). |
| `alphalens_job_last_success_timestamp_seconds{job}` | gauge | Unix time of last **successful** invocation. PR-3 alert rules use `time() - this > N` to detect stale jobs. |

### Domain (emitted from CLI success-paths)

| Job | Metrics |
|---|---|
| `edgar-detect` | `alphalens_edgar_events_detected_total`, `alphalens_edgar_events_dispatched_total`, `alphalens_edgar_portfolio_size{class}` |
| `literature-scan-{weekly,monthly}` | `alphalens_literature_last_run_trigger{window}` |
| `thematic-build` | `alphalens_thematic_briefs_total`, `alphalens_thematic_briefs_by_model{model}` |

All metrics are **gauges** — they describe THIS run's outcome, not a
cumulative counter. A run that emits 0 values is meaningful (and
should be visible on the dashboard) rather than a silent gap.

## Alertmanager wiring (PR-3)

Prometheus rules at `prometheus/rules/alphalens.yaml` declare
staleness + failure alerts per job; Alertmanager config at
`alertmanager/config.yaml` routes them all through the
`telegram` receiver via the same bot used by the EDGAR detector.

### Prerequisites on the VPS

```bash
# 1. Telegram bot_token — file form, not env (Alertmanager has no
#    bot_token_file env var; the receiver expects a path).
sudo mkdir -p /etc/alphalens
echo "$TELEGRAM_BOT_TOKEN" | sudo tee /etc/alphalens/telegram_bot_token >/dev/null
sudo chmod 640 /etc/alphalens/telegram_bot_token
sudo chown root:"$(id -gn)" /etc/alphalens/telegram_bot_token

# 2. Replace the placeholder chat_id in the committed config with the
#    real value. NOT a secret; leaking a chat_id without the bot
#    token does nothing.
sed -i "s/-1001234567890/$TELEGRAM_CHAT_ID/" \
    ~/AlphaLens/deploy/monitoring/alertmanager/config.yaml
```

### Wire the configs into the existing containers

Both edits are bind-mount additions to the Alertmanager + Prometheus
containers; no image rebuild needed.

```bash
# Prometheus rules — bind mount the rules dir then HUP to reload.
docker run -d --name prometheus \
    --restart always --net host \
    -v ~/AlphaLens/deploy/monitoring/prometheus/rules:/etc/prometheus/rules:ro \
    prom/prometheus:latest \
    --config.file=/etc/prometheus/prometheus.yml \
    --web.listen-address=:9090

# Ensure the existing prometheus.yml has:
#   rule_files: [/etc/prometheus/rules/*.yaml]
# If not, the rules under the bind mount load but nothing scrapes
# them. Verify via:
#   curl -s localhost:9090/api/v1/rules | jq '.data.groups[].name'

# Reload after editing the YAML in place:
docker exec prometheus kill -HUP 1

# Alertmanager — bind mount both the config + the bot_token file.
docker run -d --name alertmanager \
    --restart always --net host \
    -v ~/AlphaLens/deploy/monitoring/alertmanager/config.yaml:/etc/alertmanager/alertmanager.yml:ro \
    -v /etc/alphalens/telegram_bot_token:/etc/alertmanager/telegram_bot_token:ro \
    prom/alertmanager:latest \
    --config.file=/etc/alertmanager/alertmanager.yml \
    --web.listen-address=:9093

# Wire Prometheus to fan alerts out to Alertmanager (one-time):
# ensure prometheus.yml has:
#   alerting:
#     alertmanagers:
#       - static_configs:
#           - targets: ['localhost:9093']

docker exec alertmanager kill -HUP 1
```

### Smoke test the Telegram pipe

```bash
# Force-fire an alert by stopping the edgar-detect timer for >30 min,
# or by editing the textfile to backdate last_success:
echo "alphalens_job_last_success_timestamp_seconds{job=\"edgar-detect\"} 0" \
    > /var/lib/node_exporter/textfile/alphalens_job_edgar-detect.prom

# Within ~5 minutes the `AlphalensJobStale` alert fires and lands in
# Telegram. Restore by running the unit:
systemctl --user start alphalens-edgar-detect.service
```

### AlphalensEdgeStale

`AlphalensEdgeStale` fires when `alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}` has
not been refreshed for >36h (15-min debounce, severity warning). It measures /edge Postgres
freshness directly — independent of whether `alphalens-feedback-shadow-returns.service` itself
succeeded, closing the blind spot where a timed-out compute job left /edge frozen with no alert.

## Grafana dashboard

`grafana/dashboards/alphalens-cron-health.json` is the cron-health
dashboard (one stat row "time since last success", one per-job
duration time-series, one exit-code state timeline, plus domain
panels for EDGAR / thematic / AV quota).

### Provision via filesystem (no UI clicks)

Drop the JSON into Grafana's provisioning dir and restart:

```bash
docker run -d --name grafana \
    --restart always --net host \
    -v ~/AlphaLens/deploy/monitoring/grafana/dashboards:/var/lib/grafana/dashboards/alphalens:ro \
    -v ~/AlphaLens/deploy/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro \
    grafana/grafana:latest

# After dashboard JSON changes:
docker restart grafana
# (Grafana re-imports on startup if the JSON's `version` field changed
# OR the file mtime is newer than the in-memory copy.)
```

The dashboard expects a Prometheus datasource with `uid: prometheus`.
If the existing datasource has a different uid, edit
`grafana/dashboards/alphalens-cron-health.json` once and `docker
restart grafana`.

Browse to `http://<vps>:3000/d/alphalens-cron-health` (or whichever
hostname the CF Tunnel exposes Grafana on).

## What's deferred (not in this epic)

- Form-4 backfill instrumentation — long-running daemon, would emit
  one point at end-of-run.
- Literature paper count — needs `ReviewResult` schema change.
- EDGAR per-severity dispatch counts — needs `DispatchRouter` counter.
- Multi-host Prometheus federation — single VPS is fine for now.

For now, the workflow is:
1. Job runs (success or failure) → cron-health metrics emitted.
2. Domain success → domain metrics emitted (guarded; failure logged).
3. Prometheus scrapes node_exporter on its own schedule.
4. Alert rules evaluate every 15s; staleness alerts fire after `for: 5m`.
5. Alertmanager groups by (alertname, job), waits 30s, sends Telegram.
6. Failures also surface via `journalctl --user -u alphalens-<job>.service`.
