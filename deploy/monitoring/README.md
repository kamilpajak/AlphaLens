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

## What's deferred to PR-3

- Alert rules YAML (`prometheus/rules/alphalens.yaml`)
- Alertmanager → Telegram receiver config
- Grafana dashboard JSON
- Per-job staleness thresholds (edgar 30 min, av/thematic 48h, lit-weekly 14d, lit-monthly 70d)
- Domain alerts: `edgar candidates = 0 in 24h`, `av_quota_remaining < 3`

For now, failures still surface via:
- `journalctl --user -u alphalens-<job>.service`
- Telegram alerts from inside the jobs themselves (edgar dispatcher,
  verify-cache ExecStartPost on thematic)
