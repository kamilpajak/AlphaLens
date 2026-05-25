# systemd-user units (VPS deployment)

User-scoped service definitions for AlphaLens long-running tasks on Linux VPS
hosts where launchd is unavailable.

## Environment file setup (`/etc/alphalens/env`)

`alphalens-thematic-build.service` and `alphalens-av-earnings-backfill.service`
both load secrets via `EnvironmentFile=-/etc/alphalens/env`. systemd reads
each `KEY=VALUE` line into the unit's process env before `ExecStart`; for the
docker-run unit, the explicit `-e KEY` flags then cherry-pick which keys
cross into the container.

**Why `/etc/alphalens/env` and not the repo's `.env` files:**
- repo `.env` files (e.g. `apps/alphalens-django/.env`, `deploy/docker/.env`)
  are for `docker compose` interpolation and per-container runtime — different
  purpose, owned by the operator user, mixed with non-secret config knobs
- `/etc/alphalens/env` is **secrets-only**, root:root chmod 600 — survives
  worktree removals, git clean, repo moves; no risk of accidental commit;
  symmetric across pipeline + backfill units

**Bootstrap (once per VPS):**

```bash
sudo mkdir -p /etc/alphalens
sudo tee /etc/alphalens/env > /dev/null <<'EOF'
GOOGLE_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
POLYGON_API_KEY=...
PERPLEXITY_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SEC_EDGAR_USER_AGENT=AlphaLens/1.0 (kontakt@kamilpajak.pl)
EOF
sudo chmod 600 /etc/alphalens/env
sudo chown root:root /etc/alphalens/env
```

Only secrets needed by the units belong here. Knobs that change behaviour
(log level, feature flags) stay in the repo `.env` files where they can be
checked in via `.env.example`.

**Rotate a key:**

```bash
sudo $EDITOR /etc/alphalens/env     # edit value
sudo chmod 600 /etc/alphalens/env   # belt-and-suspenders if editor changed mode
# next timer fire picks up the new value — no daemon-reload needed
```

**Verify a unit can see a key (without leaking it):**

```bash
systemctl --user show alphalens-thematic-build.service \
    -p Environment 2>/dev/null | tr ' ' '\n' | grep -c '^GOOGLE_API_KEY='
# Expect: 1
```

## alphalens-form4-backfill.service

SEC EDGAR Form-4 bulk backfill (`apps/alphalens-research/scripts/run_form4_backfill.py`). Wall-time on
a small VPS: ~5-10 days for the full 2006-2026 R3000 universe (~8000 CIKs,
limited by SEC's 10 req/s rate cap). Resume-safe via the JSON manifest at
`~/.alphalens/form4_backfill_manifest.json`, so a crash + restart skips
already-processed CIKs and resumes from where it left off.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/alphalens-form4-backfill.service ~/.config/systemd/user/

# Edit Environment= lines in the unit file to match your VPS paths and contact.
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

After a successful pipeline run, `ExecStartPost=` invokes
`docker compose --profile maintenance run --rm rebuild-cache` against the
Django stack so the freshly written parquet files are synced into the
Postgres-backed briefs cache. ExecStartPost fires only on ExecStart
success, so a failed pipeline leaves the API untouched and the
dashboard keeps serving the previous day's snapshot.

### Install

```bash
# Prereq: /etc/alphalens/env must exist with GOOGLE_API_KEY, POLYGON_API_KEY,
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
