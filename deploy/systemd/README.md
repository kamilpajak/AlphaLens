# systemd-user units (VPS deployment)

User-scoped service definitions for AlphaLens long-running tasks on Linux VPS
hosts where launchd is unavailable.

## Active units

| Unit | Cadence | Source |
|---|---|---|
| `alphalens-edgar-detect.{service,timer}` | every 15 min | Layer 1 EDGAR poll + Telegram alert (migrated from macOS `com.alphalens.edgar-detect` on 2026-05-30) |
| `alphalens-literature-scan-weekly.{service,timer}` | Sun 18:00 Europe/Warsaw | Perplexity weekly RSS scan + Telegram digest + auto-commit to `main` (migrated 2026-05-30) |
| `alphalens-literature-scan-monthly.{service,timer}` | 1st of month 09:00 Europe/Warsaw | Perplexity deep scan + Telegram digest + auto-commit to `main` (migrated 2026-05-30) |
| `alphalens-av-earnings-backfill.{service,timer}` | daily 00:05 UTC | AV EARNINGS daily 25-call quota burn into `~/.alphalens/av_cache/` |
| `alphalens-thematic-build.{service,timer}` | 6× daily at HH:30 UTC (00/04/08/12/16/20) | docker-run thematic pipeline + verify-cache + Django rebuild-cache (PR-F, epic #295 #300) |
| `alphalens-feedback-shadow-returns.{service,timer}` | daily 06:30 UTC | host-venv `alphalens feedback backfill-shadow-returns` — runs the broker-free population monitor over its own ~42-session window (price-path replay over Polygon minute bars) and the benchmark-excess + size-field enrichment tail. `Persistent=true` catch-up; idempotent re-stamp. Needs `POLYGON_API_KEY`. NOT trading-day-gated (the per-date maturity guard handles non-trading dates). The unit + command name are retained for the existing timer; the per-decision ladder replay (Track A click ledger) was removed (#465), so the command now drives only the population monitor — a rename is a deferred follow-up. |
| `alphalens-form4-backfill.service` | long-running | SEC EDGAR Form-4 bulk backfill (resume-safe) — the one-time historical seed (DONE 2026-05-08) |
| `alphalens-form4-incremental.{service,timer}` | daily 02:30 UTC | Form-4 daily incremental ingest — keeps `~/.alphalens/form4_parquet/` fresh after the seed froze. Self-sizing lookback (min 3 days, auto-extends to the store's newest filing, capped at `--max-catchup-days`) via the SEC daily form index; overlap dedups on `accession_number`. Needs `SEC_EDGAR_USER_AGENT`. **First run auto-catches-up the seed→today gap — no manual step** (see section below). |

> **Decommissioned 2026-06-03 (ADR 0012):** the Alpaca paper-trading units
> (`alphalens-paper-plan`, `alphalens-paper-submit`, `alphalens-paper-reconcile`,
> `alphalens-paper-trade-stream`) and the Saxo token keep-alive
> (`alphalens-saxo-refresh`) were removed with the broker chain. Feedback
> measurement is now fully broker-free price-path replay. On the VPS, stop +
> disable these units and drop the broker keys from `/etc/alphalens/env` (a
> separate operator runbook step).

## Environment file setup (`/etc/alphalens/env`)

All three AlphaLens systemd units load secrets via
`EnvironmentFile=/etc/alphalens/env`:
- `alphalens-thematic-build.service` — `OPENROUTER_API_KEY`, `POLYGON_API_KEY`,
  `PERPLEXITY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `ALPHA_VANTAGE_API_KEY`, `SEC_EDGAR_USER_AGENT`, **plus `FRED_API_KEY`**
  (the `cache refresh-vix` step at the end of `run_thematic_day.sh` pulls
  VIXCLS so the feedback POST path can stamp a real market regime; the step
  is best-effort, so a missing key only degrades regime stamps to "unknown")
- `alphalens-av-earnings-backfill.service` — `ALPHA_VANTAGE_API_KEY`
- `alphalens-form4-backfill.service` — `SEC_EDGAR_USER_AGENT`
- `alphalens-form4-incremental.service` — `SEC_EDGAR_USER_AGENT` (the
  residential-VPS IP must carry the operator contact UA; the canonical client
  has a built-in default but the `EnvironmentFile=` has no leading dash so a
  missing `/etc/alphalens/env` fails the unit loudly)
- `alphalens-edgar-detect.service` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `alphalens-literature-scan-{weekly,monthly}.service` — `PERPLEXITY_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, **plus `GH_TOKEN`** (HTTPS push
  back to `kamilpajak/AlphaLens`; see "Cutover from launchd" §3 below)
- `alphalens-feedback-shadow-returns.service` — `POLYGON_API_KEY` (the
  minute-bar pricing leg). A missing key does not hard-fail the run — every
  ticker fetch is skipped and the sweep reports "0 priced" (looks like a
  quiet night), so the fail-loud-on-missing-file `EnvironmentFile=` guard is
  the only protection against a silently mis-pointed env.

(The Alpaca `ALPACA_*` and Saxo `SAXO_*` keys are no longer used — the broker
chain was decommissioned per ADR 0012. Remove them from `/etc/alphalens/env`
on the VPS.)

systemd reads each `KEY=VALUE` line into the unit's process env before
`ExecStart`; for the docker-run unit, the explicit `-e KEY` flags then
cherry-pick which keys cross into the container.

**No leading `-` on `EnvironmentFile=`** — a missing/typoed file MUST
fail the unit loud, not silently degrade to "no secrets" (Polygon
skipped, LLM extract fails partway, partial parquet poisons cache).
CI smoke runs install a stub: `sudo mkdir -p /etc/alphalens && sudo touch /etc/alphalens/env`.

**Why `/etc/alphalens/env` and not the repo's `.env` files:**
- repo `.env` files (e.g. `apps/alphalens-django/.env`, `deploy/docker/.env`)
  are for `docker compose` interpolation and per-container runtime — different
  purpose, owned by the operator user, mixed with non-secret config knobs
- `/etc/alphalens/env` is **secrets-only**, `root:<operator-group>` chmod
  640 — survives worktree removals, git clean, repo moves; no risk of
  accidental commit; symmetric across pipeline + backfill units

**Perms gotcha — `root:root 600` does NOT work for user-scope units:**
systemd-user runs as the operator UID (typically 1000), so a `600`
root-owned file is unreadable and the unit fails to start with
"unavailable resources or another system error" before ExecStart fires.
Use `chmod 640` + `chown root:<operator-group>` so root keeps write but
the operator user reads. On Debian/Ubuntu the operator's primary group
typically matches their username (`jacoren:jacoren`); on RHEL-family it's
often `users`. Verify with `id -gn` before running the bootstrap.

**Bootstrap (once per VPS):**

```bash
OPERATOR_GROUP=$(id -gn)   # e.g. `jacoren` on Debian
sudo mkdir -p /etc/alphalens
sudo tee /etc/alphalens/env > /dev/null <<'EOF'
OPENROUTER_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
POLYGON_API_KEY=...
PERPLEXITY_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SEC_EDGAR_USER_AGENT=AlphaLens/1.0 (kontakt@kamilpajak.pl)
EOF
sudo chmod 640 /etc/alphalens/env
sudo chown "root:${OPERATOR_GROUP}" /etc/alphalens/env
```

Only secrets needed by the units belong here. Knobs that change behaviour
(log level, feature flags) stay in the repo `.env` files where they can be
checked in via `.env.example`.

**Rotate a key:**

```bash
sudo $EDITOR /etc/alphalens/env                          # edit value
sudo chmod 640 /etc/alphalens/env                        # restore mode if editor stripped it
sudo chown "root:$(id -gn)" /etc/alphalens/env           # restore owner if editor rewrote inode
# next timer fire picks up the new value — no daemon-reload needed
```

**Verify a unit can see a key (without leaking it):**

```bash
systemctl --user show alphalens-thematic-build.service \
    -p Environment 2>/dev/null | tr ' ' '\n' | grep -c '^OPENROUTER_API_KEY='
# Expect: 1
```

## Cutover from launchd (one-time, 2026-05-30)

The three units `alphalens-edgar-detect`, `alphalens-literature-scan-weekly`,
`alphalens-literature-scan-monthly` replace the macOS `launchd` jobs
`com.alphalens.{edgar-detect,literature-scan-weekly,literature-scan-monthly}`.
The cutover has three steps; do them in order.

### 1. Migrate state from Mac → VPS

The EDGAR detector's `seen_events.db` is the SoT for "filings already
alerted on". Starting fresh on the VPS would re-fire alerts on filings
the user has already seen. So the cutover rsyncs the four state files:

```bash
# On the Mac:
for f in seen_events.db portfolio.yaml company_tickers.json digest.db; do
    rsync -av "$HOME/.alphalens/edgar-detect/$f" \
        vault.kamilpajak.pl:.alphalens/edgar-detect/
done
```

### 2. Add `GH_TOKEN` to `/etc/alphalens/env`

The literature scan units commit + push to `main` via the
`alphalens-literature-scan-publish` wrapper. The push uses HTTPS through
the `gh` credential helper, which picks up `GH_TOKEN` automatically:

```bash
# On the VPS:
sudo $EDITOR /etc/alphalens/env
# Append: GH_TOKEN=<fine-grained PAT, scope: contents:write on kamilpajak/AlphaLens>

# One-time: wire `git push` through gh's credential helper so the token
# applies to plain ``git push origin main`` (not just ``gh`` commands).
gh auth setup-git
```

The PAT should be **fine-grained** (not classic), scoped to the single
repo with `contents:write`. Rotating it later is the standard
`/etc/alphalens/env` edit recipe (see "Rotate a key" above).

### 3. Install + enable the units

```bash
# On the VPS:
mkdir -p ~/.config/systemd/user
cp ~/AlphaLens/deploy/systemd/alphalens-edgar-detect.{service,timer}            ~/.config/systemd/user/
cp ~/AlphaLens/deploy/systemd/alphalens-literature-scan-weekly.{service,timer}  ~/.config/systemd/user/
cp ~/AlphaLens/deploy/systemd/alphalens-literature-scan-monthly.{service,timer} ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now alphalens-edgar-detect.timer
systemctl --user enable --now alphalens-literature-scan-weekly.timer
systemctl --user enable --now alphalens-literature-scan-monthly.timer

# Validate
systemctl --user list-timers --no-pager | grep alphalens
systemctl --user start alphalens-edgar-detect.service   # manual smoke
journalctl --user -u alphalens-edgar-detect.service -n 50 --no-pager
```

### 4. Decommission the Mac launchd jobs (after 7 clean days)

```bash
# On the Mac:
for unit in edgar-detect literature-scan-weekly literature-scan-monthly; do
    launchctl unload ~/Library/LaunchAgents/com.alphalens.${unit}.plist
    rm ~/Library/LaunchAgents/com.alphalens.${unit}.plist
done
launchctl list | grep alphalens   # expect: empty
```

The macOS plist sources were removed from the repo once the systemd
units had run clean; `git log` is the historical record. Do not
re-create the launchd path — systemd on the VPS is the only scheduler.

## alphalens-edgar-detect.service + .timer

Layer 1 SEC EDGAR poller — runs every 15 min, reads
`~/.alphalens/edgar-detect/portfolio.yaml`, classifies new filings on
held + watchlist tickers, dispatches Telegram alerts on AUTO_TRIGGER /
APPROVAL / DIGEST routes. State (`seen_events.db`, `digest.db`,
`company_tickers.json`) lives under `~/.alphalens/edgar-detect/` and
survives unit restarts.

### Install (see "Cutover from launchd" above for the first-time path)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-edgar-detect.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-edgar-detect.timer
```

### Inspect

```bash
systemctl --user status alphalens-edgar-detect.timer
journalctl --user -u alphalens-edgar-detect.service -f
journalctl --user -u alphalens-edgar-detect.service --since today
sqlite3 ~/.alphalens/edgar-detect/seen_events.db 'SELECT COUNT(*) FROM seen_events;'
```

### Adjust the watchlist

```bash
$EDITOR ~/.alphalens/edgar-detect/portfolio.yaml
# Next timer fire (≤ 15 min) picks up the new ticker set automatically —
# no daemon-reload needed, the CLI re-reads the file on every run.
```

## alphalens-literature-scan-{weekly,monthly}.service + .timer

Perplexity literature scans. Weekly fires Sun 18:00 in `Europe/Warsaw`;
monthly fires on the 1st at 09:00 same TZ. Both call the bash wrapper
`deploy/systemd/bin/alphalens-literature-scan-publish` which:

1. Pulls `main` (fast-forward only).
2. Runs `alphalens literature scan --window {weekly|monthly}` — writes
   `docs/research/literature_review/weekly/<period>.md` or
   `docs/research/literature_review/<period>.md`.
3. If the scan produced a tracked-file diff, `git commit` as
   `alphalens-bot <bot@alphalens.kamilpajak.pl>` and `git push origin
   main`. One rebase-retry on push race; second race fails loud.
4. Telegram digest is dispatched inside step 2 by the CLI itself when
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set.

The commit + push lives in bash (not Python) so the unit can be
statically linted by `apps/alphalens-research/tests/test_deploy_systemd_units.py::TestLiteraturePublishWrapper`
without spinning up the CLI.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-literature-scan-weekly.{service,timer}  ~/.config/systemd/user/
cp deploy/systemd/alphalens-literature-scan-monthly.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-literature-scan-weekly.timer
systemctl --user enable --now alphalens-literature-scan-monthly.timer
```

### Inspect

```bash
systemctl --user list-timers --no-pager | grep literature
journalctl --user -u alphalens-literature-scan-weekly.service --since "1 week ago"
journalctl --user -u alphalens-literature-scan-monthly.service --since "1 month ago"

# Force a one-off run (skips clock-wait, picks the wrapper up):
systemctl --user start alphalens-literature-scan-weekly.service
```

### Why a wrapper, not direct ExecStart?

systemd directive substitution is awkward for chained `git` commands
(no shell, no error handling, no rebase-retry). The wrapper keeps the
shell logic in one file that the lint tests can inspect line-for-line.

## alphalens-form4-backfill.service

SEC EDGAR Form-4 bulk backfill (`apps/alphalens-research/scripts/run_form4_backfill.py`). Wall-time on
a small VPS: ~5-10 days for the full 2006-2026 R3000 universe (~8000 CIKs,
limited by SEC's 10 req/s rate cap). Resume-safe via the JSON manifest at
`~/.alphalens/form4_backfill_manifest.json`, so a crash + restart skips
already-processed CIKs and resumes from where it left off.

### Install

```bash
# Prereq: /etc/alphalens/env must exist with SEC_EDGAR_USER_AGENT=...
# see "Environment file setup" section at the top of this README.

mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-form4-backfill.service ~/.config/systemd/user/

# Edit Environment= lines in the unit file ONLY if you want non-default
# config paths or year range. SEC_EDGAR_USER_AGENT is sourced from
# /etc/alphalens/env, not the unit file.
systemctl --user daemon-reload
systemctl --user enable --now alphalens-form4-backfill.service

# One-time: allow the unit to keep running after logout.
sudo loginctl enable-linger "$USER"
```

### Inspect

```bash
systemctl --user status alphalens-form4-backfill.service
journalctl --user -u alphalens-form4-backfill.service -f       # live tail
journalctl --user -u alphalens-form4-backfill.service --since "1 hour ago"
```

### Stop / restart

```bash
systemctl --user stop alphalens-form4-backfill.service
systemctl --user restart alphalens-form4-backfill.service
```

### Parallel backfill across multiple machines

SEC's polite-rate cap (10 req/s) is enforced **per source IP**, not per
User-Agent. With multiple machines on distinct IPs, the backfill can be
sharded so each machine fetches a non-overlapping slice in parallel. A
5-machine fan-out cuts wall-time from ~7 days to ~1.5 days.

**Step 1 — split the CIK universe (run once on any machine):**

```bash
.venv/bin/python apps/alphalens-research/scripts/split_cik_list.py \
    ~/.alphalens/form4_cik_universe.txt \
    --num-shards 5 \
    --output-dir ~/.alphalens/form4_shards/
# Produces ciks_shard_{1..5}_of_5.txt
```

The split is round-robin so each shard contains a representative mix of
small and large filers — no machine ends up stuck on a long tail of
prolific issuers.

**Step 2 — copy the appropriate shard to each machine, then run:**

```bash
# On machine N (with its own IP):
apps/alphalens-research/scripts/run_form4_backfill.py \
    --user-agent "Your Name your@email.com" \
    --cik-list ~/.alphalens/form4_shards/ciks_shard_N_of_5.txt \
    --parquet-root ~/.alphalens/form4_parquet \
    --manifest ~/.alphalens/form4_backfill_manifest.json \
    --start-year 2006 --end-year 2026
```

Each machine has its own manifest covering only its slice; no
cross-machine synchronization is needed.

**Step 3 — merge the parquet outputs into a central tree:**

Once every machine has finished its shard, rsync each machine's
\`~/.alphalens/form4_parquet/\` into one central \`form4_parquet_merged/\`
tree. Parquet filenames carry a timestamp + random hex suffix so there
are no collisions between machines.

```bash
# On the central machine:
mkdir -p ~/.alphalens/form4_parquet_merged

for host in machine1 machine2 machine3 machine4 machine5; do
    rsync -av --info=progress2 \
        "$host:.alphalens/form4_parquet/" \
        ~/.alphalens/form4_parquet_merged/
done
```

**Step 4 — compact the merged tree:**

```bash
.venv/bin/python apps/alphalens-research/scripts/compact_form4_parquet.py \
    --parquet-root ~/.alphalens/form4_parquet_merged
# Produces ~/.alphalens/form4_parquet_merged/transaction_year=YYYY/compacted.parquet
# (one file per year — replaces all part-*.parquet from every machine)
```

The compactor is idempotent and atomic: writes to \`.tmp\` then renames,
deletes originals only on success. Safe to re-run.

### Why this exists

The earlier deployment ran the script inside `screen` with
`bash -c "... ; exec bash"`. That setup has no auto-recovery — a reboot,
OOM kill, or `pkill` aborts a multi-day run with no restart. systemd's
`Restart=on-failure` + `RestartSec=60` automates recovery while
`StartLimitBurst=5` prevents tight crash loops if the underlying problem
is persistent (bad credentials, exhausted disk, SEC ban).

## alphalens-form4-incremental.service + alphalens-form4-incremental.timer

Keeps the hive-partitioned Form-4 parquet store fresh after the one-time
historical bulk backfill above (the seed, DONE 2026-05-08) froze. Each daily
fire fetches a fixed lookback window `[asof - lookback_days, asof]` (UTC) via the
SEC daily form index, intersects each day's accession set with the per-CIK
submissions block, parses the XML, writes to
`~/.alphalens/form4_parquet/transaction_year=YYYY/`, and compacts so overlapping
re-fetches collapse on the unique `accession_number`. **No state file** — the
fixed lookback re-reads recent immutable days every run, so a one-run miss
self-heals on the next run.

Design memo: [`docs/research/form4_daily_incremental_design_2026_06_07.md`](../../docs/research/form4_daily_incremental_design_2026_06_07.md).

### Install

```bash
cp deploy/systemd/alphalens-form4-incremental.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-form4-incremental.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-form4-incremental.timer
```

### First run — automatic catch-up (no manual step)

The window self-sizes: each run reads the store's newest `filed_date` and
extends the window back to it (minus a 2-day overlap), capped at
`--max-catchup-days` (default 400). So the FIRST fire after the seed froze
(~2026-05-08) automatically walks the whole seed→today gap, dedups against the
seed on compaction, and then settles to the 3-day steady state. The same is
true after any missed run. No `--lookback-days N` catch-up to remember, and it
works whether you deploy days or weeks after the seed.

Just enable the timer (above) — or trigger one fire immediately:

```bash
systemctl --user start alphalens-form4-incremental.service
```

Verify the first run reached today:

```bash
curl -s localhost:9100/metrics | grep alphalens_form4_latest_filing_date
# the gauge (a Unix timestamp) should be within ~1 day of `date +%s`
```

### Inspect

```bash
systemctl --user list-timers alphalens-form4-incremental.timer
journalctl --user -u alphalens-form4-incremental.service -f
journalctl --user -u alphalens-form4-incremental.service --since "yesterday"
```

### Why daily-index, not a per-CIK walk

The seed walks the full 8005-CIK universe; the incremental does NOT. The SEC
daily form index lists every Form-4/4-A filed that UTC day, so one index fetch
per date gives complete coverage with no stale-roster risk, at ~200× lower HTTP
than re-walking 8005 submissions every run. A daily-index fetch failure (403
under shared-IP load) is counted and the date is skipped — the next run's
overlapping window + the immutable `.idx` are the recovery. See the design memo
§2 for the full rationale.

### Output

`~/.alphalens/form4_parquet/transaction_year=YYYY/compacted.parquet` — the same
store the seed wrote, consumed in-place by the Cohen-Malloy / opportunistic-Form4
scorers. The incremental adds tens of KB/day.

## alphalens-av-earnings-backfill.service + alphalens-av-earnings-backfill.timer

Alpha Vantage `EARNINGS` daily backfill (`apps/alphalens-research/scripts/av_earnings_daily_backfill.py`).
Unlike the Form-4 daemon, this is a **oneshot** triggered by a daily timer:
each fire consumes up to the AV free-tier 25-call/day quota then exits. Full
S&P 500 union backfill (~503 names) takes ~21 calendar days. Cache lives at
`~/.alphalens/av_cache/earnings_<T>.json` and is general-purpose (any future
paradigm reading AV EARNINGS hits the same store).

### Install

```bash
# Prereq: /etc/alphalens/env must exist with ALPHA_VANTAGE_API_KEY=...
# see "Environment file setup" section at the top of this README.

mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-av-earnings-backfill.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-av-earnings-backfill.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now alphalens-av-earnings-backfill.timer

# Optional: trigger an immediate fire to validate the unit works.
systemctl --user start alphalens-av-earnings-backfill.service
```

### Inspect

```bash
systemctl --user list-timers --all              # see next-fire / last-fire
systemctl --user status alphalens-av-earnings-backfill.timer
journalctl --user -u alphalens-av-earnings-backfill.service -f
journalctl --user -u alphalens-av-earnings-backfill.service --since "yesterday"
```

### Optional rclone sync — systemd PATH caveat

If a future operator extends `ExecStart` with `--rclone-remote nextcloud:alphalens/av_cache`,
note that systemd-user services run with a restricted `$PATH` (typically
`/usr/local/bin:/usr/bin`). If `rclone` is installed elsewhere (e.g.
`/usr/local/bin/rclone` on Debian, `~/.local/bin/rclone` on a pip-installed
copy), pass an absolute path via `--rclone-bin /full/path/to/rclone` in the
`ExecStart` line to avoid `FileNotFoundError` at fire time.

### Why oneshot + timer (not long-running daemon)

The free-tier quota is the binding constraint, not compute. Holding a
process resident 23h+ just to wake up for 30s of API calls wastes
resources and complicates restart semantics. The timer pattern fires
the script daily, picks up only uncached tickers, exits cleanly on
`AVRateLimitError` (return code 0 — expected steady-state), and lets
systemd handle persistence across reboots via `Persistent=true`.

## alphalens-thematic-build.service / .timer

End-to-end thematic pipeline (news → brief → JSON refresh) running inside the
`alphalens-pipeline` docker image. Fires daily at 06:30 UTC via the companion
timer; spaced from the AV backfill (00:05 UTC) so the two don't contend for
the Alpha Vantage key.

Full operator recipe (image build, env file, Cloudflare wiring) lives at
[`deploy/docker/README.md`](../docker/README.md). The systemd unit itself is
a thin wrapper around:

```bash
docker compose -f deploy/docker/docker-compose.yml run --rm pipeline \
    /app/deploy/docker/run_thematic_day.sh
```

The unit passes the operator's UID/GID to compose via `%U`/`%G` so files
written into `~/.alphalens/` and `web-data/` are jacoren-owned, not root.

After a successful pipeline run, two `ExecStartPost=` slots fire in
order:

1. **Gap-detection on the news cache (PR-E, epic #295 Risk A).**
   `alphalens thematic verify-cache --days 7 --alert` (run inside the
   same `alphalens-pipeline` image, bind-mounted on `~/.alphalens`)
   confirms that every parquet for the past 7 days is present and
   readable. Missing days dispatch a Telegram alert via the
   inherited `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` env vars and
   exit 1, which halts the systemd chain.
2. **Django cache rebuild.** `docker compose --profile maintenance
   run --rm rebuild-cache` syncs the freshly written parquet files
   into the Postgres-backed briefs cache.

ExecStartPost runs in declared order and a failure on any one stops
the rest — so a corrupt or missing parquet halts the chain rather
than silently refreshing Django from incomplete data. The dashboard
then keeps serving the previous day's snapshot until the operator
investigates.

### Install

```bash
# Prereq: /etc/alphalens/env must exist with OPENROUTER_API_KEY, POLYGON_API_KEY,
# ALPHA_VANTAGE_API_KEY, PERPLEXITY_API_KEY, TELEGRAM_BOT_TOKEN,
# TELEGRAM_CHAT_ID, SEC_EDGAR_USER_AGENT — see "Environment file setup" at
# the top of this README.

cp deploy/systemd/alphalens-thematic-build.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-thematic-build.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-thematic-build.timer
```

### Inspect

```bash
systemctl --user list-timers alphalens-thematic-build
journalctl --user -u alphalens-thematic-build.service --since today
systemctl --user start alphalens-thematic-build.service     # manual fire
```
