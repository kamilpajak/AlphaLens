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
   gauges (last_run, last_duration, last_exit_code, last_signal,
   last_success). Atomic via tempfile + `mv`.

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
| `alphalens_job_last_exit_code{job}` | gauge | Exit status of last invocation. `0` = success, including a run systemd reports as `success` after a `TERM` (a plain `systemctl stop`). `256` = systemd gave the hook no numeric status — a signal kill, or one of the Table 6 rows (`protocol`, `start-limit-hit`) where `$EXIT_CODE` and `$EXIT_STATUS` are both unset. `256` is deliberately outside the 0-255 a wait status can hold, so it can never be confused with a code a job returned. |
| `alphalens_job_last_signal{job}` | gauge | Signal number that terminated the last invocation, `0` when none. Kept out of `last_exit_code` on purpose: `code=exited, status=128` happens for real, and the four units that shell out to `docker run` report a killed container as `code=exited, status=137`, so a `128+signum` encoding would be indistinguishable from a genuine exit code. |
| `alphalens_job_last_success_timestamp_seconds{job}` | gauge | Unix time of last **successful** invocation. Alert rules use `time() - this > N` to detect stale jobs. A failed run carries the previous value forward verbatim rather than dropping the line: an absent series makes `max()` empty, which disarms the staleness rule exactly while the job is broken (#1369). |

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

# 2. The committed config carries a placeholder chat_id; the deploy
#    recipe below substitutes the real value (from /etc/alphalens/env)
#    into the LIVE copy. NOT a secret; leaking a chat_id without the
#    bot token does nothing. Never sed the repo copy in place — it
#    shows up as a permanent local diff.
```

### Deploy the Alertmanager config (live path is OUTSIDE the repo)

The container mounts the DIRECTORY `~/monitoring/alertmanager` at
`/etc/alertmanager` (verify with `docker inspect alertmanager`), so the
live files are `~/monitoring/alertmanager/alertmanager.yml` +
`telegram.tmpl` + `telegram_bot_token` — hand-copied from the repo, not
bind-mounted from it. Every change to `config.yaml` or `telegram.tmpl`
is deployed like this (both files travel together: the config names the
template by its in-container path `/etc/alertmanager/telegram.tmpl`):

```bash
D=~/monitoring/alertmanager; R=~/AlphaLens/deploy/monitoring/alertmanager
set -a; . /etc/alphalens/env; set +a          # TELEGRAM_CHAT_ID
cd ~/AlphaLens && git pull --ff-only
cp "$D/alertmanager.yml" "$D/alertmanager.yml.bak-$(date +%F)"
cp "$R/telegram.tmpl" "$D/telegram.tmpl"
sed "s/-1001234567890/$TELEGRAM_CHAT_ID/" "$R/config.yaml" > "$D/alertmanager.yml.new"
mv "$D/alertmanager.yml.new" "$D/alertmanager.yml"   # same directory: atomic rename
docker exec alertmanager amtool check-config /etc/alertmanager/alertmanager.yml   # expect "1 templates"
docker exec alertmanager kill -HUP 1
docker logs --since 2m alertmanager | grep -i -E "Completed loading|error"
```

A failed reload keeps the OLD config running (Alertmanager logs the
error and carries on), so always read the log line after the HUP.

### Annotations are free text — the template does the escaping (#1345)

The receiver sends with `parse_mode: HTML`. In that mode Alertmanager
renders `telegram.tmpl` through Go `html/template`, which escapes
`<`, `>` and `&` on insertion, so a rule description may say
"stayed > 0" or name `node_exporter` as-is. Rules:

- Write annotations as plain prose. Backtick pairs and `*` show up
  literally (no code spans, no bold) — that is deliberate.
- Never add `safeHtml`, `reReplaceAll` or hand-built tags to the
  template; the only HTML in it is the static `<b>` around the status
  and alertname. `safeHtml` switches the escaping off and re-opens the
  failure this replaced: with the old `parse_mode: Markdown`, every
  bare `_` in a description started an unterminated italic and Telegram
  rejected the whole message ("can't parse entities") — 7191 failed
  notify attempts for `AlphalensJobMetricMissing` alone, no page.
- Telegram truncates at 4096 runes. A group of many alerts under one
  alertname can be cut mid-entity; keep descriptions short.
- Gate: `just lint-alertmanager` (or the CI `prom-rules` job) runs
  `amtool check-config` plus `amtool template render --template.type=html`
  on `render_fixture.json` and diffs against `render_expected.txt`. After
  an intended template change, regenerate the expectation with the
  render command minus `| diff` and commit it.

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

# Alertmanager — bind mount the config DIRECTORY (alertmanager.yml +
# telegram.tmpl + telegram_bot_token live there; see "Deploy the
# Alertmanager config" above for how the repo files get into it).
docker run -d --name alertmanager \
    --restart unless-stopped --net host \
    -v ~/monitoring/alertmanager:/etc/alertmanager:ro \
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

The datasource and the dashboard provider config are versioned beside
it, under `grafana/provisioning/`:

| Repo file | Live path | What it is |
|---|---|---|
| `grafana/provisioning/datasources/prometheus.yml` | `~/monitoring/grafana/provisioning/datasources/prometheus.yml` | the Prometheus datasource, pinned to `uid: prometheus` |
| `grafana/provisioning/dashboards/dashboards.yml` | `~/monitoring/grafana/provisioning/dashboards/dashboards.yml` | the file provider (`updateIntervalSeconds: 10`) |
| `grafana/dashboards/alphalens-cron-health.json` | `~/monitoring/grafana/provisioning/dashboards/alphalens-cron-health.json` | the dashboard itself |

Every dashboard target in the JSON addresses `uid: prometheus`. That is
why the datasource yml pins that uid, and the sync refuses any set
where a dashboard references a uid no synced datasource declares. Do
NOT "fix" a uid mismatch by editing the dashboard JSON — that is the
wrong direction, and it is how the 2026-08-24 incident happened.

### Deploy = merge to main (#1110)

The live tree is NOT bind-mounted from the repo. Grafana runs from the
compose stack at `~/monitoring/docker-compose.yml`, which bind-mounts
`./grafana/provisioning` into `/etc/grafana/provisioning`. The hourly
`alphalens-grafana-provisioning-sync` timer converges that live tree to
the `origin/main` blobs of the three files above, so **merging a
dashboard or datasource change deploys it within ~1h**. Converge
immediately with `systemctl --user start
alphalens-grafana-provisioning-sync.service`.

Two consequences worth stating plainly:

- **A hand-edit under `~/monitoring/grafana/provisioning/` does not
  survive.** The next fire overwrites it from `origin/main` and keeps
  the overwritten bytes as one `.bak-autosync-<UTC>` file.
- **Adding a dashboard means adding its datasource first.** The
  cross-file gate refuses the whole set — nothing syncs — if a
  dashboard references a datasource uid the repo does not declare.

Reload semantics, measured on the VPS 2026-08-24: a dashboard JSON
change is picked up by the provisioning watcher with NO container
restart (and with no log line either — the watcher is silent on the
happy path). The datasource and provider ymls are read only during the
startup provisioning pass, so those changes cost one
`docker restart grafana`, which the sync spends only when one of them
actually changed.

To confirm what Grafana ingested, read `grafana.db` with sqlite
(`dashboard_provisioning.check_sum` is the md5 of the source file's
bytes). The admin API cannot be used: its password is a dead
placeholder and it answers 401. Full recipe + the runbook:
`deploy/systemd/README.md` § "Grafana provisioning sync".

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
