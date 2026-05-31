# AlphaLens monitoring (Prometheus + Grafana + Alertmanager)

PR-2 of the cron-observability epic wires every active AlphaLens
systemd-user unit on the VPS into the Prometheus textfile collector
that node_exporter already exposes. PR-3 (not yet merged) ships alert
rules + Alertmanager Telegram routing + a Grafana dashboard on top.

## What this PR delivers

1. **Bash hook** `deploy/systemd/bin/alphalens-emit-job-metrics` —
   called as `ExecStopPost=` from every active service. Writes
   `~/.alphalens/metrics/alphalens_job_<job>.prom` with cron-health
   gauges (last_run, last_duration, last_exit_code, last_success).
   Atomic via tempfile + `mv`.

2. **Python helper**
   `alphalens_pipeline/observability/textfile.py::emit_domain_metrics` —
   called from each CLI success-path. Writes
   `~/.alphalens/metrics/alphalens_domain_<job>.prom` with
   domain-specific gauges (events detected, briefs written, AV
   quota remaining, etc.). Atomic via `os.replace`.

3. **ExecStopPost hooks on all 5 active units**:
   - `alphalens-edgar-detect.service`
   - `alphalens-literature-scan-weekly.service`
   - `alphalens-literature-scan-monthly.service`
   - `alphalens-av-earnings-backfill.service`
   - `alphalens-thematic-build.service`

   The `alphalens-form4-backfill.service` is excluded — it is a
   long-running daemon that completed its bulk run on 2026-05-08
   and would produce a single end-of-run point.

## node_exporter wiring (one-time operator step on the VPS)

The existing node_exporter docker container needs the textfile
collector enabled + the metrics dir bind-mounted. The metrics dir
ownership is the operator UID (the systemd-user units run as that
UID) and the read mode is `:ro` for node_exporter.

```bash
# Stop the existing node-exporter container.
docker stop node-exporter

# Recreate with the textfile collector flag + bind mount.
# (Real change is the two new args at the bottom of `docker run`.)
docker run -d --name node-exporter \
    --restart always \
    --net host \
    --pid host \
    -v /:/host:ro,rslave \
    -v /home/jacoren/.alphalens/metrics:/host/textfile:ro \
    prom/node-exporter:latest \
    --path.rootfs=/host \
    --collector.textfile.directory=/host/textfile
```

If the existing node_exporter command lives in a docker-compose
stack outside the repo, add:

```yaml
services:
  node-exporter:
    volumes:
      - /home/jacoren/.alphalens/metrics:/host/textfile:ro
    command:
      - "--collector.textfile.directory=/host/textfile"
```

Verify after restart:

```bash
mkdir -p ~/.alphalens/metrics
systemctl --user start alphalens-edgar-detect.service
ls -la ~/.alphalens/metrics/         # alphalens_{job,domain}_edgar-detect.prom
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
| `av-earnings-backfill` | `alphalens_av_tickers_total{status}`, `alphalens_av_quota_remaining`, `alphalens_av_quota_blocked` |

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
    > ~/.alphalens/metrics/alphalens_job_edgar-detect.prom

# Within ~5 minutes the `AlphalensJobStale` alert fires and lands in
# Telegram. Restore by running the unit:
systemctl --user start alphalens-edgar-detect.service
```

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
