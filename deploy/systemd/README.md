# systemd-user units (VPS deployment)

User-scoped service definitions for AlphaLens long-running tasks on Linux VPS
hosts where launchd is unavailable.

## Active units

| Unit | Cadence | Source |
|---|---|---|
| `alphalens-edgar-detect.{service,timer}` | every 15 min | Layer 1 EDGAR poll + Telegram alert (migrated from macOS `com.alphalens.edgar-detect` on 2026-05-30) |
| `alphalens-literature-scan-weekly.{service,timer}` | Sun 18:00 Europe/Warsaw | Perplexity weekly RSS scan + Telegram digest + auto-commit to `main` (migrated 2026-05-30) |
| `alphalens-literature-scan-monthly.{service,timer}` | 1st of month 09:00 Europe/Warsaw | Perplexity deep scan + Telegram digest + auto-commit to `main` (migrated 2026-05-30) |
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

> **Decommissioned 2026-07-05:** `alphalens-av-earnings-backfill.{service,timer}`
> were removed. The only consumer was paradigm #14 (PEAD v2), killed
> 2026-06-24 (doctrine FAIL). The VPS timer was disabled 2026-07-03 and its
> Prometheus staleness alert removed 2026-07-05; the `~/.alphalens/av_cache/`
> snapshot (502 tickers) is archived in Nextcloud `AlphaLens-prod/caches/`
> (`av_cache_2026-07-05.tar.zst`).

## Environment file setup (`/etc/alphalens/env`)

AlphaLens systemd units load secrets via
`EnvironmentFile=/etc/alphalens/env`:
- `alphalens-thematic-build.service` — `OPENROUTER_API_KEY`, `POLYGON_API_KEY`,
  `PERPLEXITY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `ALPHA_VANTAGE_API_KEY`, `SEC_EDGAR_USER_AGENT`, **plus `FRED_API_KEY`**
  (the `cache refresh-vix` step at the end of `run_thematic_day.sh` pulls
  VIXCLS so the feedback POST path can stamp a real market regime; the step
  is best-effort, so a missing key only degrades regime stamps to "unknown")
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

`run_thematic_day.sh` runs the five thematic stages (ingest → extract →
map-themes → score → brief) and then, best-effort, **`alphalens experts enrich
<yesterday> --all --scuttlebutt`** — the eager expert-panel qualitative layer. It
runs every registered QUAL-capable expert (today Buffett: moat / trend / candor /
understandability + rationale per brief survivor, from its 10-K) and stamps the
qual columns INTO the brief parquet *before* the `rebuild-cache` ExecStartPost
ingests it, so the SPA card's `expert.panel` deep-read drawer lights up. O'Neil
(momentum, PR-7) is numeric-only and skipped here at $0 — its numerics + the panel
`expert_spread` disagreement scalar are stamped earlier at the `score` stage. Needs
`OPENROUTER_API_KEY` (DeepSeek) + `SEC_EDGAR_USER_AGENT` + `PERPLEXITY_API_KEY` (all
already passed into the container); results are cached per `(date, ticker,
scuttlebutt)` under `~/.alphalens/buffett_qual/` so the 6×/day reruns re-pay the LLM
only for not-yet-classified names (~$3-4/day steady-state).
It is non-fatal under `set -e`: a DeepSeek / Perplexity / SEC hiccup leaves the
drawer absent for that name until the next run, never failing the build.
`--scuttlebutt` is **ON**: it feeds a web-grounded Perplexity context block
(UNVERIFIED) to the classifier and adds the "scuttlebutt: web-grounded,
unverified" drawer footnote. A missing `PERPLEXITY_API_KEY` degrades scuttlebutt
to "no context" rather than failing — the qual layer still runs.

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

## Edge mirror (decoupled) — alphalens-edge-mirror.service + .timer

The Postgres cache rebuild for the `/edge` dashboard has been decoupled from
the compute job (`alphalens-feedback-shadow-returns.service`) into its own
unit fired on a **successful** compute run (`OnSuccess=`) PLUS an **hourly
self-heal timer** that covers the failure/timeout path.

### Why decoupled

The original ExecStartPost on the compute unit ran only after a successful
ExecStart. When the compute job timed out (90-min backstop kill), the
ExecStartPost never fired, leaving the `/edge` dashboard frozen at the last
completed run's date — potentially 2+ days stale on a backlog. Decoupling the
mirror into its own unit fixes this: a clean run hands off instantly via
`OnSuccess=`, and the **hourly self-heal timer** (`*:05:00 UTC`) refreshes the
cache independently of the compute job — so a timeout or error leaves `/edge`
at most ~1h stale (not 2+ days), and a partial deploy that leaves the target
missing still self-heals on the next timer tick.

**Why no `OnFailure=`:** an earlier revision pointed BOTH `OnSuccess=` and
`OnFailure=` at `alphalens-edge-mirror.service`. That registered two identical
"trigger source" back-references on the mirror, so systemd logged a per-run
warning — `multiple trigger source candidates for exit status propagation
(alphalens-feedback-shadow-returns.service, alphalens-feedback-shadow-returns.service),
skipping` — on every hourly mirror fire (cosmetic; the mirror still ran and
exited 0). Dropping `OnFailure=` removes the duplicate trigger source and the
warning. The failure path loses only the *instant* handoff: the hourly timer
re-syncs whatever parquets exist — including any partial output a timed-out run
wrote before the kill — within ≤1h, so this is a latency-only trade-off that
stays well inside the 36h `AlphalensEdgeStale` budget.

### Install (ATOMIC DEPLOY REQUIREMENT)

The compute-unit edit and both new unit files **must land together** in a
single deploy:

```bash
# Step 1: Add the three modified/new files to systemd-user.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-feedback-shadow-returns.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-edge-mirror.service            ~/.config/systemd/user/
cp deploy/systemd/alphalens-edge-mirror.timer              ~/.config/systemd/user/

# Step 2: Reload and enable the timer (triggers the service on every fire,
# and on the next fire of the compute job).
systemctl --user daemon-reload
systemctl --user enable --now alphalens-edge-mirror.timer

# Step 3: Verify the timer is active.
systemctl --user list-timers alphalens-edge-mirror.timer
```

**CRITICAL:** Do NOT deploy the compute-unit edit alone. The `OnSuccess=`
directive points to `alphalens-edge-mirror.service`, which must exist before
`daemon-reload` runs. If the mirror unit is missing, systemd will fail to load
the compute unit and block all future fires.

### systemd version requirement

The `OnSuccess=` directive requires **systemd ≥ 249**. Check your version:

```bash
systemctl --version   # first line: "systemd X.Y"
```

On older versions, the `OnSuccess=` line is parsed but ignored — so the hourly
timer becomes the **sole** self-heal mechanism (not a loss, since the timer
fires every hour). A systemd upgrade is outside the scope of this deploy; if the
version is older than 249 and you want the instant success handoff, that
requires a VPS OS upgrade.

### Inspect

```bash
systemctl --user status alphalens-edge-mirror.timer
systemctl --user list-timers alphalens-edge-mirror
journalctl --user -u alphalens-edge-mirror.service --since today
systemctl --user start alphalens-edge-mirror.service       # manual fire
```

### How it works

1. The compute job (`alphalens-feedback-shadow-returns.service`) completes
   successfully (ExecStart exit 0).
2. systemd fires `alphalens-edge-mirror.service` immediately via the
   `OnSuccess=` directive (requires systemd ≥ 249).
3. The mirror runs `docker compose --profile maintenance run --rm rebuild-ladder-outcomes`
   to sync the freshly written population-ladder parquets into the
   Postgres-backed briefs cache.
4. Independently, the hourly timer fires `alphalens-edge-mirror.service` at
   `*:05:00 UTC` each hour as a self-heal backstop. This is what covers the
   failure/timeout path: if the compute job fails or is timeout-killed, no
   `OnSuccess=` handoff fires, but the next timer tick re-syncs within ≤1h.

The mirror command is **idempotent and mtime-gated** — redundant runs (e.g. the
`OnSuccess=` handoff and the hourly timer fire within the same hour) are cheap,
re-mirroring unchanged parquets and exiting quickly.

### Alerting

`AlphalensEdgeStale` (in `deploy/monitoring/prometheus/rules/alphalens.yaml`)
fires when `alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}` has
not been refreshed for >36h. This is independent of whether the compute job
(`feedback-shadow-returns`) succeeded — it directly measures /edge Postgres
freshness, closing the blind spot where a timed-out compute job left /edge
frozen with no alert.

### Deployment runbook (ordered steps)

**Step 1: Pull pipeline code into the host venv**

The compute unit `alphalens-feedback-shadow-returns.service` runs
`~/.local/bin/alphalens` directly (NOT a docker image), so pipeline code
lives in the editable host venv. After merging the PR to main, pull on the VPS:

```bash
cd ~/AlphaLens && git pull --ff-only origin main
# The venv is editable (installed via `uv sync --editable`), so
# the code is live immediately — no reinstall needed.
```

**Step 2: Verify systemd version supports handoff directives**

The compute unit uses `OnSuccess=` to trigger the mirror, which requires
**systemd ≥ 249**. Check your version:

```bash
systemctl --version   # first line: "systemd X.Y"
```

If the version is < 249, the `OnSuccess=` line is ignored, and only the hourly
timer (`*:05:00 UTC`) fires the mirror — a viable fallback (one-hour max
staleness) but not ideal. A full systemd upgrade is outside this runbook; if you
need instant success handoff, that requires a VPS OS upgrade.

**Step 3: Copy the three unit files and reload systemd**

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-feedback-shadow-returns.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-edge-mirror.service            ~/.config/systemd/user/
cp deploy/systemd/alphalens-edge-mirror.timer              ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now alphalens-edge-mirror.timer
```

**CRITICAL:** The compute-unit edit and both new unit files must land
together. If the mirror unit is missing when `daemon-reload` runs, systemd
will fail to parse the compute unit (unknown target in `OnSuccess=`) and
block future fires.

**Step 4: Verify a clean run**

Trigger the compute job manually (or wait for the next scheduled 06:30 UTC
fire):

```bash
systemctl --user start alphalens-feedback-shadow-returns.service
```

Monitor the compute job and mirror in separate terminals:

```bash
# Terminal 1: watch compute job progress
journalctl --user -u alphalens-feedback-shadow-returns.service -f

# Terminal 2: wait for mirror handoff (exit after 1 min to avoid tail loop)
journalctl --user -u alphalens-edge-mirror.service -f --since "1 min ago" &
sleep 60 && kill %1
```

Verify the mirror fired and `/edge` refreshed:

```bash
# Check mirror ran successfully
systemctl --user status alphalens-edge-mirror.service
# Expected: "Active: inactive (dead)" with exit code 0

# Check Postgres timestamp updated
docker compose -f deploy/docker/django-prod/docker-compose.yaml exec postgres \
  psql -U alphalens -c \
  "SELECT last_rebuild_at FROM edge_rebuild_log ORDER BY last_rebuild_at DESC LIMIT 1;"
# Expected: a timestamp within the last ~2 min

# Verify metrics updated
curl -s localhost:9100/metrics | grep 'alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}'
# Expected: a recent Unix timestamp
```

**Step 5: Simulate the failure path**

Confirm that a failed/timed-out compute run does NOT hand off to the mirror
(no `OnFailure=`), and that the hourly self-heal timer is what keeps `/edge`
fresh. Run one compute cycle with a short deadline to trigger early exit:

```bash
ALPHALENS_FEEDBACK_FETCH_DEADLINE_S=1 \
  systemctl --user start alphalens-feedback-shadow-returns.service
```

Monitor the sequence:

```bash
# Terminal 1: compute exits early (deadline exceeded)
journalctl --user -u alphalens-feedback-shadow-returns.service -f --since "now"
# Expected: exit code non-0, "stopped_for_deadline" in output

# Terminal 2: mirror does NOT fire from the compute exit (no OnFailure handoff).
# Instead, force one mirror run to confirm the self-heal path works:
systemctl --user start alphalens-edge-mirror.service
journalctl --user -u alphalens-edge-mirror.service --since "1 min ago" --no-pager
# Expected: mirror runs and exits 0 (carries prior parquet state).
# In production this fire comes from the hourly *:05:00 UTC timer (≤1h latency).
```

Verify `/edge` still serves with carried data:

```bash
# Check Postgres timestamp (should be from the OnFailure mirror)
docker compose -f deploy/docker/django-prod/docker-compose.yaml exec postgres \
  psql -U alphalens -c \
  "SELECT last_rebuild_at FROM edge_rebuild_log ORDER BY last_rebuild_at DESC LIMIT 1;"
# Expected: a recent timestamp (within 1 min, from the carried data)

# View dashboard at https://app.kamilpajak.pl/edge — should show yesterday's
# population rows with NO new refreshes (stale-on-failure), and NO 503 errors.
```

**Done.** The edge mirror is now running decoupled, resilient to compute
timeouts, and monitored for staleness via the Prometheus alert.

## Saxo auto-manager (SIM) — VPS deploy runbook

This section complements the inline install comments already in
`alphalens-broker-manager.service`.

**Target:** the always-on SIM auto-manager (`alphalens broker manage`) + the OAuth keep-alive timer, on the VPS (`jacoren@vault`, host-venv systemd-user — same pattern as `alphalens-edgar-detect`).
**Scope:** SIM only. The `$100` live escape is **out of scope** (needs a separate ADR + `ALPHALENS_BROKER_LIVE=1` + `$100` caps; the structural SIM rail stays untouched).
**Merged:** PR #876 (`9005b5eb`). Design: [`docs/research/saxo_automanager_mvp_design_2026_07_21.md`](../../docs/research/saxo_automanager_mvp_design_2026_07_21.md).
**Golden rule:** deploy INERT first (no `ALLOW_ORDERS`) → smoke → arm ONE SIM test pick → only then go live.

### 0. Prereqs (on the VPS)

```bash
ssh jacoren@vault
cd ~/AlphaLens
git pull --ff-only origin main          # must include 9005b5eb (#876)
uv sync                                  # host venv picks up brokers/automanager + the CLI
.venv/bin/alphalens broker manage --help # sanity: the subcommand exists
loginctl enable-linger "$USER"           # units survive logout (idempotent)
```

Confirm the runtime dirs exist (created on first use, but the metrics dir needs write):
```bash
mkdir -p ~/.alphalens/broker_orders
sudo install -d -o "$USER" -g "$USER" /var/lib/node_exporter/textfile   # heartbeat gauge target
```

### 1. Env file — `/etc/alphalens/env`

Both units read `EnvironmentFile=/etc/alphalens/env` (fail loud if missing). Add the Saxo OAuth + Telegram keys. **Leave `ALPHALENS_BROKER_ALLOW_ORDERS` OUT for now** — the daemon then runs inert (reconcile + read only, places nothing).

```bash
sudo tee -a /etc/alphalens/env >/dev/null <<'EOF'
# --- Saxo SIM auto-manager ---
SAXO_ENV=sim
SAXO_APP_KEY=<sim app key>
SAXO_APP_SECRET=<sim app secret>
SAXO_AUTH_REDIRECT_URL=http://localhost:8765/callback   # MUST byte-match the SIM portal registration
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID likely already present (literature scans) — verify
# ALPHALENS_BROKER_ALLOW_ORDERS=1   <-- add ONLY at go-live (§6)
EOF
sudo chmod 600 /etc/alphalens/env
```

### 2. One-time attended OAuth **on the VPS** (headless → SSH port-forward)

The token store must live **on the VPS** (`~/.alphalens/saxo_auth/token_store.json`), and the redirect goes to `localhost:8765` — so run `broker auth` ON the VPS while forwarding that port from your laptop.

**Laptop terminal A** — open the tunnel (leave it running):
```bash
ssh -L 8765:localhost:8765 jacoren@vault
```
**In that same SSH session (on the VPS)** — start the auth listener, no browser:
```bash
cd ~/AlphaLens && set -a && source /etc/alphalens/env && set +a
.venv/bin/alphalens broker auth --no-browser
# prints: "open this URL to authorize (SIM credentials): https://sim.logonvalidation.net/authorize?..."
# and waits up to 300s on http://localhost:8765/callback
```
**Laptop browser** — open the printed authorize URL, log in with SIM credentials. Saxo redirects to `http://localhost:8765/callback` → the SSH tunnel forwards it to the VPS listener → `broker auth` catches it and writes the token store **on the VPS**. You should see `authorized — OAuth session established`.

Verify (on the VPS):
```bash
.venv/bin/alphalens broker auth --status   # access valid, refresh ALIVE
.venv/bin/alphalens broker account         # SIM EUR account snapshot
.venv/bin/alphalens broker positions       # should be flat before first run
```

> The refresh chain dies after ~40 min without a refresh. Do §3 (install the keep-alive timer) **right after** this so the chain stays alive; otherwise re-run §2.

### 3. Install the systemd units

```bash
cd ~/AlphaLens
cp deploy/systemd/alphalens-broker-manager.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-saxo-refresh.service   ~/.config/systemd/user/
cp deploy/systemd/alphalens-saxo-refresh.timer     ~/.config/systemd/user/
systemctl --user daemon-reload

# Keep-alive FIRST (holds the OAuth chain during idle stretches, ~20min < 40min window):
systemctl --user enable --now alphalens-saxo-refresh.timer
systemctl --user list-timers | grep saxo-refresh      # next fire scheduled
journalctl --user -u alphalens-saxo-refresh.service -n 20   # first --refresh ran clean
```

**Do NOT enable `alphalens-broker-manager.service` yet** — smoke it manually first (§5).

**Single-refresher invariant:** only THIS VPS may refresh the token. Do not run a concurrent `alphalens broker` CLI that refreshes on another host sharing the token store — they burn each other's rotation chains.

### 4. Prometheus + metrics wiring

- The per-tick **heartbeat gauge** writes to `/var/lib/node_exporter/textfile` (via `ALPHALENS_TEXTFILE_DIR`, set in the unit) — node_exporter's textfile collector scrapes it.
- The alert rules (`AlphalensJobStale{job="broker-manager"}` / `{job="saxo-refresh"}` + the heartbeat rule) are in `deploy/monitoring/prometheus/rules/alphalens.yaml` in the repo, **but the live Prometheus rules are NOT repo-mounted** — hand-sync the new rule blocks into the live rules file and reload:
```bash
# copy the new broker-manager + saxo-refresh rule blocks into the live rules file, then:
sudo promtool check rules /path/to/live/alphalens.rules.yml
sudo kill -HUP "$(pgrep -x prometheus)"     # or systemctl reload prometheus
```
- Verify after go-live: the heartbeat metric appears in node_exporter's `/metrics`, and `AlphalensJobStale` is not firing.

### 5. Smoke — INERT (no placement), then a single tick

With `ALLOW_ORDERS` still unset, a manual tick reconciles + reads only (places nothing):
```bash
cd ~/AlphaLens && set -a && source /etc/alphalens/env && set +a
.venv/bin/alphalens broker manage --once
# expect: kill-gate ok, session alive, orphan-sweep (start), 0 armed picks, reconcile runs, no crash, exits 0
touch ~/.alphalens/broker_orders/KILL && .venv/bin/alphalens broker manage --once   # confirm kill path skips placement
rm ~/.alphalens/broker_orders/KILL
```

### 6. Arm ONE SIM test pick + go live

Pick a ticker from a recent local brief (needs `~/.alphalens/thematic_briefs/<date>.parquet` on the VPS; a cheap ticker like `S` sizes to whole shares). US market should be open for a marketable tier to fill.

```bash
# 6.1 arm it (attended CLI — this is the human "pick"):
.venv/bin/alphalens broker arm S --date <YYYY-MM-DD>
cat ~/.alphalens/broker_orders/picks.jsonl        # one armed line

# 6.2 turn on placement (the arm) + restart-scoped go-live:
sudo sed -i 's/^# ALPHALENS_BROKER_ALLOW_ORDERS=1/ALPHALENS_BROKER_ALLOW_ORDERS=1/' /etc/alphalens/env
#   (or add the line if not present)

# 6.3 one supervised tick to place the in-band subset + standalone stop:
set -a && source /etc/alphalens/env && set +a
.venv/bin/alphalens broker manage --once
.venv/bin/alphalens broker orders      # entry bracket + (after fill) standalone StopIfTraded
.venv/bin/alphalens broker reconcile --json   # FILLED once filled; realized_r when closed
```

Watch it on **saxotrader.com/sim** (same SIM login). Confirm the entry + standalone disaster stop appear and match the brief geometry.

**Go live (daemon):**
```bash
systemctl --user enable --now alphalens-broker-manager.service
journalctl --user -u alphalens-broker-manager.service -f      # per-tick loop
```

### 7. Day-2 operations

| Action | Command |
|---|---|
| **Emergency stop (instant)** | `touch ~/.alphalens/broker_orders/KILL` — the loop stops placing, still reconciles + cancels |
| Resume after kill | `rm ~/.alphalens/broker_orders/KILL` |
| **Disarm placement** (softer than kill) | comment `ALPHALENS_BROKER_ALLOW_ORDERS` in `/etc/alphalens/env` → `systemctl --user restart alphalens-broker-manager.service` (runs inert) |
| Arm a new pick | `.venv/bin/alphalens broker arm TICKER --date YYYY-MM-DD` (daemon picks it up next tick, joined to `submissions.jsonl` so it places once) |
| Inspect | `journalctl --user -u alphalens-broker-manager.service -f` |
| State files | picks: `~/.alphalens/broker_orders/picks.jsonl`; placements: `~/.alphalens/broker_orders/submissions.jsonl` (both append-only) |
| Stop the daemon | `systemctl --user disable --now alphalens-broker-manager.service` |
| Full flat check | `.venv/bin/alphalens broker positions` + `... orders` |

**OAuth outage caveat:** if the VPS is down (or the keep-alive stops) for **>40 min**, the refresh chain dies → a `_chain_lost` Telegram alert fires and the daemon stops placing. Recovery = re-do §2 (attended browser login via SSH-forward). This is the one un-automatable step.

### 8. Safety recap

- **SIM-only is structural** — `SaxoClient` refuses any non-SIM base URL; live is unreachable in code.
- Layers before any real POST each tick: kill-file → chain alive → `ALLOW_ORDERS=1` → `MAX_OPEN` / portfolio-gross / daily-loss caps.
- The disaster stop is ALWAYS a standalone `StopIfTraded` placed after the entry fills, sized to realized qty (a ~30–60 s unprotected window per tick — acceptable on SIM).
- **Deferred (known issues, see the PR):** far-TP tranches are reported operator-managed (NOT placed); no ratchet / resize-on-partial / 42-session time-stop / streaming; alert debounce absent (persistent alerts repeat each tick).

### 9. `$100` live escape — NOT in this runbook

Requires: a new ADR lifting the structural rail, a separate `ALPHALENS_BROKER_LIVE=1` env (never a runtime flag), sizing equity `$1000` with `~$100` max per-pick loss, `MAX_OPEN=1`, and shrinking the unprotected window (wire the `StreamingFillSource` first). Do NOT reuse the SIM env/units for live.
