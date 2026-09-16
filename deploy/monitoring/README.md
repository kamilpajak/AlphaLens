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

The hook writes samples only — no `# HELP` / `# TYPE` lines — so node_exporter
exposes these families untyped with a synthesised help text. Seventeen files share
the five names; the collector drops a family whose help text differs from the first
it met per scrape (#1461), and the registry behind it drops any sample whose help or
type differs from the first family under that name, `scrape_error` untouched — so
a HELP-bearing file must never appear next to the hook's files again (the one-shot
strip at deploy time is in `deploy/systemd/README.md`; the mixed-directory outcome
is pinned against the real exporter by `tests/test_node_exporter_textfile_mixing.py`).
The "Type" column below is the semantic type every rule treats them as.

| Metric | Type | Description |
|---|---|---|
| `alphalens_job_last_run_timestamp_seconds{job}` | gauge | Unix time of last invocation (success or failure). |
| `alphalens_job_last_duration_seconds{job}` | gauge | Wall-clock seconds of last invocation. |
| `alphalens_job_last_exit_code{job}` | gauge | Exit status of last invocation. `0` = success, including a run systemd reports as `success` after a `TERM` (a plain `systemctl stop`). `256` = a failed result with no usable numeric status: a signal kill, one of the Table 6 rows (`protocol`, `start-limit-hit`) where `$EXIT_CODE` and `$EXIT_STATUS` are both unset, or a status of `0` on a failure (`timeout : exited : 0`) — a failure never reports `0`. `256` is deliberately outside the 0-255 a wait status can hold, so it can never be confused with a code a job returned. |
| `alphalens_job_last_signal{job}` | gauge | Signal number that terminated the last invocation, `0` when none. Kept out of `last_exit_code` on purpose: `code=exited, status=128` happens for real, and the four units that shell out to `docker run` report a killed container as `code=exited, status=137`, so a `128+signum` encoding would be indistinguishable from a genuine exit code. |
| `alphalens_job_last_success_timestamp_seconds{job}` | gauge | Unix time of last **successful** invocation. Alert rules use `time() - this > N` to detect stale jobs. A failed run carries the previous value forward verbatim rather than dropping the line: an absent series makes `max()` empty, which disarms the staleness rule exactly while the job is broken (#1369). |

### Domain (emitted from CLI success-paths)

| Job | Metrics |
|---|---|
| `edgar-detect` | `alphalens_edgar_events_detected_total`, `alphalens_edgar_events_dispatched_total`, `alphalens_edgar_portfolio_size{class}` |
| `literature-scan-{weekly,monthly}` | `alphalens_literature_last_run_trigger{window}` |
| `thematic-build` | `alphalens_thematic_briefs_total`, `alphalens_thematic_briefs_by_model{model}` |
| `edge-mirror` (the Django `rebuild_ladder_outcomes_cache` command, #1436) | `alphalens_edge_mirror_watermark_timestamp_seconds` (`completed_at` of the ingest watermark the run read; `0` when none), `alphalens_edge_mirror_unsettled_dates` (dates refused because their parquet is newer than the watermark — non-zero for one hour every morning while the nightly is mid-run), `alphalens_edge_mirror_newest_brief_date_timestamp_seconds` (the newest brief date in the mirror's per-date ledger `edge_daymetaladderoutcome` after the run, midnight UTC; a 0-candidate day counts, `0` before the first ingest). Written by the container through the `/var/lib/node_exporter/textfile` bind mount on the `rebuild-ladder-outcomes` compose service; the Django image cannot import the pipeline writer, so `edge/ingest/textfile.py` is its mirror. |
| any job written by either Python writer (#1462) | `alphalens_textfile_invalid_samples{textfile}`: how many values this write DROPPED because they were not a finite number (a bool, a string, None, NaN, Inf). Written only when that count is above zero, so a healthy file carries no such line. |

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

### /edge freshness: AlphalensEdgeStale, AlphalensEdgeMirrorRefusing, AlphalensEdgeNewestBriefDateStale

Until 2026-09-15 `AlphalensEdgeStale` read `alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}`
and this README said that measured "/edge Postgres freshness directly". It did not: that series
is the mirror unit's exit-0 clock, and the mirror exits 0 on a run that refuses the whole store.
On 2026-09-13 the nightly compute job was timeout-killed before it wrote the ingest watermark,
every hourly mirror run logged `unsettled=117` and exited 0, `/edge` sat a brief day behind, and
the alert stayed silent (#1436). The rules now read the gauges the mirror command publishes about
the data (`alphalens_domain_edge-mirror.prom`; see `deploy/systemd/README.md` "What the mirror
publishes"):

| Rule | Reads | Fires when |
|---|---|---|
| `AlphalensEdgeStale` | `alphalens_edge_mirror_watermark_timestamp_seconds` | the settled watermark the mirror last read is older than 36h (15-min debounce). Nothing newer than that completed compute run is in `/edge`: the nightly stopped completing, or the mirror stopped running (the gauge freezes with the file). |
| `AlphalensEdgeMirrorRefusing` | `alphalens_edge_mirror_unsettled_dates` | the mirror has refused store dates for 3h. One refusal a day (the 07:05 UTC run, while the nightly is mid-run) is normal and clears by 08:05; hours of it is the 2026-09-13 shape (a killed nightly) or a store rewritten outside the nightly. Fires the same morning (~10:05 UTC) instead of a day later. |
| `AlphalensEdgeNewestBriefDateStale` | `alphalens_edge_mirror_newest_brief_date_timestamp_seconds` | the newest brief date in the mirror's per-date ledger (`edge_daymetaladderoutcome`) is older than 96h (healthy peak about 55h; one missed night about 79h). The backstop for a nightly that completes but ingests nothing new, which the two rules above cannot see. |
| `AlphalensEdgeMetricMissing` | `absent(alphalens_edge_mirror_watermark_timestamp_seconds)` | the gauges do not exist (image not yet pulled by the hourly run, compose mount or `ALPHALENS_TEXTFILE_DIR` missing, scrape broken), so the three rules above are disarmed. One guard covers all three: one atomic file. |

Recovery for all of them is a full rerun of the nightly with its default budget:
`systemctl --user start alphalens-feedback-shadow-returns.service` (about 75 min; the
`OnSuccess=` handoff mirrors the result). Do not use `ALPHALENS_FEEDBACK_FETCH_DEADLINE_S=0`
(it disables all fetching and stamps a half-rewritten store as settled, which is what the
2026-09-13 recovery did) and do not use `rebuild_ladder_outcomes_cache --force` (it bypasses the
gate for one run and leaves the watermark file untouched, so the next hourly run refuses again).

The four rules are evaluated in `prometheus/rules/alphalens_test.yaml` (`just test-rules`)
against the 2026-09-13 series shape, a healthy morning and the missing-series case. Nothing
reads the mirror's `last_success` clock any more; `AlphalensJobFailed` still pages a mirror run
that exits non-zero. After the 2026-09-13 timeout kill node_exporter rejected the nightly's own
job textfile, because the pre-#1441 hook had written a non-float exit code, so every series for
that job vanished from the collector (#1437, fixed the same day; #1458 was a duplicate report).

### Textfile integrity: AlphalensTextfileScrapeError, AlphalensJobFamilyMissing, AlphalensTextfileInvalidSample

Three ways a metric goes silent without any job failing. The first two were
demonstrated on the live gauge before their rules existed (#1439, #1461); the
third is prevention (#1462: no invalid value was on the VPS when it shipped):

| Rule | Reads | Fires when |
|---|---|---|
| `AlphalensTextfileScrapeError` | `node_textfile_scrape_error` | node_exporter has been rejecting at least one file in the textfile directory for 30m. A non-float sample drops the WHOLE file (the 2026-09-13 `TERM`, #1437); a `# HELP` text differing between files drops one FAMILY from the later file (#1461). The gauge is global and unlabelled, so the description carries the one-liner that names the file: `docker logs --since 10m node-exporter 2>&1 \| grep -E 'failed to collect textfile data\|inconsistent metric help text' \| tail -3`. |
| `AlphalensJobFamilyMissing` | `alphalens_job_last_run_timestamp_seconds unless alphalens_job_last_exit_code` | a job's `last_run` is scraped but its `last_exit_code` family is not, for 15m — `AlphalensJobFailed` is blind for exactly that job. Labelled by `job`, which the global gauge cannot give. The hook writes no HELP since #1461, so this now means a foreign writer of the `alphalens_job_*` names. |
| `AlphalensTextfileInvalidSample` | `max_over_time(alphalens_textfile_invalid_samples[15m]) > 0` | a Python writer dropped a value in the last 15 minutes, held for 5m. Labelled by `textfile` (the writer's job argument). Every rule that reads the dropped series without an `absent()` partner is blind meanwhile. The ERROR log line naming the expression is written once per defect and latched until the expression writes cleanly. |

Why 30m: a 30-day census of the live gauge (read 2026-09-15, 5-min resolution)
found 17 episodes — 14 nightly rejections of `alphalens_job_thematic-build.prom`
lasting 140-225 min (02:25-05:30 UTC, 2026-08-21..09-06: the 00:30 slot hit
`TimeoutStartSec` and the pre-#1441 hook wrote `TERM`; stopped with #1363), one
230-min rejection of the `bracket-cost` file (2026-09-01), the 80-min #1437
rejection and the 46h #1461 conflict. None paged. Every real episode lasted
>= 80 min, and writer-vs-scrape races (every writer renames a tempfile) never
showed at 5-min resolution, so 30m clears the blips and still lands inside the
hour after the `AlphalensJobMetricMissing` (5m) it disambiguates. Why 15m for
the per-job rule: above the 5-min staleness a vanished series lingers, so a file
rewritten within one run cannot page.

Why the Python writers drop instead of raising (#1462): the broker daemon calls
them bare on every tick and catches only `OSError`, so a `ValueError` for a bad
gauge would stop the protective loop on SIM and LIVE. Dropping is also better
than writing: a `True` or a string used to make node_exporter drop the WHOLE
file, and a `nan` it accepts defeats every `!= 0` / `> N` rule. Why
`max_over_time` over 15m rather than a plain `> 0`: a daemon writes its file
clean again on the next tick (15-45 s), so the line lives for a scrape or two
and would never outlast a `for:`; over 15m a one-tick drop pages 5 minutes later
and clears about 15 minutes after the defect stops.

All three are evaluated with `just test-rules`: in `prometheus/rules/alphalens_test.yaml`,
the sustained and the blip shape for the scrape-error gauge, the dropped-family and
the healthy shape for the per-job rule, and the persistent daily-job shape for the
invalid-sample rule; in `alphalens_broker_test.yaml` (1-minute grid), its one-tick
daemon shape. Recovery is the writer's, not Prometheus's:
fix the writer, rewrite the file with tmp + `mv`, and never run the hook by hand
inside the scraped directory (it stamps `last_run=now`). The residual crash
hazard of a differing HELP text on these names is described in
`deploy/systemd/README.md` "Why the hook writes no `# HELP` / `# TYPE`".

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
