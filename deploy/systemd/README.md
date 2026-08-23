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
| `alphalens-feedback-shadow-returns.{service,timer}` | daily 06:30 UTC | host-venv `alphalens feedback backfill-shadow-returns` — runs the broker-free population monitor over its own ~42-session window (price-path replay over Polygon minute bars) and the benchmark-excess + size-field enrichment tail. `Persistent=true` catch-up; idempotent re-stamp. Needs `POLYGON_API_KEY`. NOT trading-day-gated (the per-date maturity guard handles non-trading dates). The unit + command name are retained for the existing timer; the per-decision ladder replay (Track A click ledger) was removed (#465), so the command now drives only the population monitor — a rename is a deferred follow-up. Replay mechanics: `apps/alphalens-pipeline/alphalens_pipeline/feedback/README.md`. |
| `alphalens-form4-backfill.service` | long-running | SEC EDGAR Form-4 bulk backfill (resume-safe) — the one-time historical seed (DONE 2026-05-08) |
| `alphalens-form4-incremental.{service,timer}` | daily 02:30 UTC | Form-4 daily incremental ingest — keeps `~/.alphalens/form4_parquet/` fresh after the seed froze. Self-sizing lookback (min 3 days, auto-extends to the store's newest filing, capped at `--max-catchup-days`) via the SEC daily form index; overlap dedups on `accession_number`. Needs `SEC_EDGAR_USER_AGENT`. **First run auto-catches-up the seed→today gap — no manual step** (see section below). |
| `alphalens-grouped-daily-topup.{service,timer}` | daily 01:30 UTC | Appends the latest missing session(s) to the split-adjusted (`adjusted=true`) whole-market grouped-daily store that feeds the O'Neil **R** (relative-strength) term at the thematic `score` stage. Self-sizing catch-up; the free-tier entitlement cliff (`NOT_AUTHORIZED` past ~21–24 mo) stops cleanly. Distinct from the population-monitor's `adjusted=false` cache. Needs `POLYGON_API_KEY`. |
| `alphalens-broker-manager.service` | long-running daemon (poll 45 s) | SIM Saxo auto-manager (ADR 0014) — drains armed picks → places brackets + standalone disaster stops → reconciles live broker state → manages ladder exits / protective stops to terminal. `Type=simple` + `Restart=on-failure`. **Trades SIM only** — this is the SIM instance of the two-instance model (ADR 0016); the real-money twin is `alphalens-broker-manager-live.service` (§9, ADR 0017). Placement still needs `ALPHALENS_BROKER_ALLOW_ORDERS=1`. See the "Saxo auto-manager (SIM)" runbook below. |
| `alphalens-broker-manager-live.service` | long-running daemon (poll 45 s) | LIVE twin of the auto-manager — places **REAL-MONEY** orders on Saxo LIVE under the ADR 0017 standing account-bound grant; `ALPHALENS_BROKER_ENVIRONMENT=live` pinned in-unit; ships INERT (`ALLOW_ORDERS=0`). Runbook: §9. |
| `alphalens-saxo-refresh.{service,timer}` | ~every 20 min | Saxo OAuth idle keep-alive (`broker auth --refresh`) — refreshes the SIM token inside its 40 min window so the broker-manager daemon never loses auth. **Re-added 2026-07 under ADR 0014** (the identically-named paper-chain unit was decommissioned 2026-06-03 — see the note below). Keeps only the SIM chain alive — the LIVE daemon's chain is kept by `alphalens-saxo-marketdata-refresh.timer` (§9.0). |
| `alphalens-saxo-marketdata-refresh.{service,timer}` | ~every 20 min | Keep-alive for the LIVE `saxo_auth_live` OAuth chain (app `bracket-keeper`) — feeds the LIVE price stream AND, since ADR 0017, the LIVE order rail's tokens. See "Saxo LIVE market data" §3. |
| `alphalens-edge-mirror.{service,timer}` | hourly at `:05` UTC | Self-heal for the `/edge` dashboard — rebuilds the ladder-outcome Postgres cache from the population-ladder parquets, independent of the nightly `feedback-shadow-returns` compute (so `/edge` never lags a failed/late compute run). See "Edge mirror (decoupled)" below. |
| `alphalens-bracket-cost.{service,timer}` | daily 03:30 UTC | Keeps the market-cap bracket-cost measurement (PR #1087) advancing: `--prepare` (new funnel days only — days already written are FROZEN, because the split-adjusted grouped store retro-adjusts history and re-deriving a setup would move the levels of a ladder already under replay), then `--replay`, then `--benchmark`. **Daily AND multi-pass, both forced rather than chosen**: brand-new rows draw from a hardcoded 50-per-run budget (`_FORCED_RESOLVE_BUDGET`, NOT tunable by `ALPHALENS_FEEDBACK_MAX_FETCHES`), and the measured arrival rate is 51.5 proposals/day since the 2026-08-18 prompt change (17.2/day before it), peaking at 68 — i.e. ABOVE the per-run budget. `--replay` therefore makes up to 4 passes and stops early once a pass fetches nothing. A single pass would fall behind daily and accumulate a backlog that never drains, silently, because the job still exits 0. All three stages run even if an earlier one fails (the unit still reports failure), so a bad `--prepare` never stops `--replay` from advancing existing rows. Writes only to `~/.alphalens/bracket_cost{,_ladders}/` — both write paths refuse the production ladder store by name. Needs `POLYGON_API_KEY`. Monitoring: `AlphalensJobStale`@48h + `MetricMissing`. Contract + reads: `docs/research/mcap_bracket_cost_*.md`. |
| `alphalens-issue-wake.{service,timer}` | daily 05:30 UTC | Wakes GitHub issues parked on external data. Finds open `waiting:data` issues whose body carries a `Wake: YYYY-MM-DD` line dated today or earlier, removes the label from each, and sends ONE Telegram message listing them. **Nothing due sends nothing** — which is exactly why it carries the staleness pair: a dead timer and a quiet day produce identical Telegram traffic, so `AlphalensJobStale`@48h + `MetricMissing` are the only things that can tell them apart, and the failure is cumulative (every issue whose date passes during an outage stays parked). A `Wake:` line the parser cannot read is REPORTED in the same message and the label is left alone — a bad date is never guessed at. A `Wake:` line inside a fenced code block is ignored (issue templates paste the format as a sample). Sends BEFORE unlabelling, so a failed send leaves the labels on and the next run repeats the set. Needs `GH_TOKEN` (via `gh`) + `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Script: `apps/alphalens-research/scripts/wake_due_issues.py` (`--dry-run` prints the message it would send and mutates nothing). |

> **Decommissioned 2026-06-03 (ADR 0012):** the Alpaca paper-trading units
> (`alphalens-paper-plan`, `alphalens-paper-submit`, `alphalens-paper-reconcile`,
> `alphalens-paper-trade-stream`) were removed with the broker chain, and
> feedback measurement is now fully broker-free price-path replay. The Saxo
> token keep-alive (`alphalens-saxo-refresh`) was removed at the same time but
> was **re-added 2026-07 under ADR 0014** for the SIM auto-manager — it now
> runs `broker auth --refresh` (not the old paper-chain refresh) and is listed
> in the Active units table above. On the VPS, stop + disable the paper-trading
> units and drop the *paper-broker* keys from `/etc/alphalens/env` (a separate
> operator runbook step).

> **Decommissioned 2026-07-05:** `alphalens-av-earnings-backfill.{service,timer}`
> were removed. The only consumer was paradigm #14 (PEAD v2), killed
> 2026-06-24 (doctrine FAIL). The VPS timer was disabled 2026-07-03 and its
> Prometheus staleness alert removed 2026-07-05; the `~/.alphalens/av_cache/`
> snapshot (502 tickers) is archived in Nextcloud `AlphaLens-prod/caches/`
> (`av_cache_2026-07-05.tar.zst`).

## Running a unit by hand

`Type=oneshot` units block. `systemctl --user start alphalens-thematic-build.service`
does not return until `ExecStart` exits — for that unit, the full run length.
Run it in the foreground and read the exit code. Do not background it, and do
not pass `--no-block`.

If you are attaching to a run already in flight (a timer fired it), do **not**
wait on `systemctl is-active`. While a oneshot runs, `is-active` prints
`activating` and exits **3**, so `until ! systemctl --user is-active --quiet
<unit>` exits after zero iterations and falsely reports the run as finished.
The inverse form `until systemctl --user is-active --quiet <unit>` never
returns, because a finished oneshot settles at `inactive` and never exits 0.
Exit code 3 means "not active"; exit code **4** means "no such unit", which is
also what a typo in the unit name gives you — so the exit code alone cannot
tell "still running" from "I misspelled it".

Wait on the state string instead, and treat both `activating` and `active` as
still-going. Testing only against `activating` has the mirror-image race: a
unit reports `inactive` between job enqueue and job start, so a loop entered
too early exits immediately for the same reason as the broken form.

```bash
UNIT=alphalens-thematic-build.service
while state=$(systemctl --user show "$UNIT" -p ActiveState --value); \
      [ "$state" = activating ] || [ "$state" = active ]; do
  sleep 5
done
systemctl --user show "$UNIT" -p Result -p ExecMainStatus --value
```

Read `Result` and `ExecMainStatus` only after that loop ends. Read while the
unit is still `activating` and you get the **previous** run's values — that
mistake produced a false "mtime gate skipped" alarm on 2026-06-11.

That loop is for `Type=oneshot` units only. `alphalens-broker-manager.service`
and `alphalens-form4-backfill.service` are `Type=simple` daemons: they sit at
`active` for their whole lifetime by design, so the loop would never return.
Check those with `systemctl --user status` and their own liveness signals
(`AlphalensBrokerManagerHeartbeatStale` for the broker manager) instead.

This matters most for the two long units: `alphalens-thematic-build.service`
(~12-20 min, `TimeoutStartSec=75min`) and
`alphalens-feedback-shadow-returns.service` (~5-30 min, `TimeoutStartSec=90min`).
Both feed user-facing surfaces, so a false "verified" hides a broken deploy
until `AlphalensJobStale` (48h) or `AlphalensEdgeStale` (36h) fires.

## Environment file setup (`/etc/alphalens/env`)

AlphaLens systemd units load secrets via
`EnvironmentFile=/etc/alphalens/env`:
- `alphalens-thematic-build.service` — `OPENROUTER_API_KEY`, `POLYGON_API_KEY`,
  `PERPLEXITY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `ALPHA_VANTAGE_API_KEY`, `SEC_EDGAR_USER_AGENT`, **plus `FRED_API_KEY`**
  (the `cache refresh-vix` step at the end of `run_thematic_day.sh` pulls
  VIXCLS so the feedback POST path can stamp a real market regime; the step
  is best-effort, so a missing key only degrades regime stamps to "unknown"),
  **plus the optional `ALPHALENS_OPENROUTER_*` provider pin** — see
  "OpenRouter provider pin" below
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
checked in via `.env.example` — with one documented exception below.

### OpenRouter provider pin (the one non-secret exception)

`ALPHALENS_OPENROUTER_PROVIDER_ORDER`, `_ALLOW_FALLBACKS`, `_QUANTIZATIONS`
and `_REQUIRE_PARAMETERS` are behaviour knobs, not secrets, but they still go
in `/etc/alphalens/env`. The pipeline container gets its environment ONLY from
the unit's process env (no `--env-file`, no compose), so this file is the only
channel that reaches it. The alternative — an inline `-e KEY=value` in the
unit — would hardcode the production pin in git and need a unit reinstall plus
`daemon-reload` to change it.

They are all OPTIONAL, and **nothing in the repo sets any of them** — the
pin is not on until you put it in this file. Absent, docker forwards nothing
and the pipeline sends no `provider` block, which is byte-for-byte today's
behaviour. The intended live shape:

```bash
# Remove the fp4/fp8 serving mixture without making one provider a single
# point of failure — fallbacks stay ENABLED (the var is simply absent).
ALPHALENS_OPENROUTER_QUANTIZATIONS=fp8
```

`_REQUIRE_PARAMETERS` defaults to ON as soon as any other knob is set, so the
line above also drops providers that do not declare `response_format` support
(they would otherwise ignore it and answer in prose). Set it to `0` to opt out.
A replay / measurement run pins `ALPHALENS_OPENROUTER_PROVIDER_ORDER=<slug>`
with `ALPHALENS_OPENROUTER_ALLOW_FALLBACKS=0` instead — there, a run served by
a different backend is worthless, so failing closed is the point. Note an fp8
pin excludes DeepSeek's own first-party endpoint, which reports its
quantization as `unknown`.

Only the four names above are accepted values. A typo in the value (`yes` and
`no` are accepted, `y` and `n` are not) makes `alphalens` refuse to start, by
design: the alternative was a whole day of prose-less briefs at exit 0.

**Which calls the pin covers:** every OpenRouter call the pipeline makes. All
of them build their client through `get_default_openrouter_client()`, which is
the one constructor that reads these vars. Passing an API key explicitly
bypasses the pin, so no pipeline stage does — see
`apps/alphalens-research/tests/test_openrouter_provider_pin_reach.py`, which
fails if a stage starts doing so again.

**Confirming a pin took effect.** There is no runtime assertion, and the
journal cannot fully answer it either. The client logs the serving provider on
the first call per model and whenever it changes:

```bash
journalctl --user -u alphalens-thematic-build.service --since today \
    | grep -E 'served by provider|provider CHANGED'
```

Read that as "who served us", NOT as "the pin is on" — those lines are emitted
whether or not a `provider` block was sent, so a run with a forgotten
`/etc/alphalens/env` edit looks identical. What it does tell you: with a pin
that names one provider, any `provider CHANGED` line, or a provider you did
not name, means the pin is not reaching the container. To check the config
itself rather than its effect, read it back where the container sees it:

```bash
systemctl --user show alphalens-thematic-build.service \
    --property=Environment | tr ' ' '\n' | grep ALPHALENS_OPENROUTER
```

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

## alphalens-grouped-daily-topup.service + .timer

Appends the latest missing session(s) to the persistent split-adjusted
(`adjusted=true`) whole-market grouped-daily store that feeds the O'Neil **R**
(relative-strength) term — read disk-only at the thematic `score` stage via
`rs_history.rs_percentile`. Daily oneshot at 01:30 UTC (`Persistent=true` so a
missed day self-catches-up), one whole-market Polygon grouped-daily call per
missing session, self-sizing from the newest snapshot on disk (capped
`--max-catchup-days` 400). The free-tier entitlement cliff (`NOT_AUTHORIZED`
past the ~21–24 mo window) stops cleanly. **Distinct from** the
population-monitor's `adjusted=false` grouped cache — the two must NOT be merged
(RS needs split-adjusted closes; the monitor needs raw closes for
minute-bar/ladder matching). Needs `POLYGON_API_KEY`.

Design memo: [`docs/research/oneil_r_reactivation_design_2026_06_14.md`](../../docs/research/oneil_r_reactivation_design_2026_06_14.md).

### Install

```bash
cp deploy/systemd/alphalens-grouped-daily-topup.service ~/.config/systemd/user/
cp deploy/systemd/alphalens-grouped-daily-topup.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-grouped-daily-topup.timer
```

One-time seed (before the first top-up) via the same script WITHOUT `--topup`
— `~88 min` for ~440 sessions at the Polygon free 5 req/min:

```bash
.venv/bin/python apps/alphalens-research/scripts/backfill_grouped_daily_history.py --sessions 440
```

### Inspect

```bash
systemctl --user list-timers alphalens-grouped-daily-topup.timer
journalctl --user -u alphalens-grouped-daily-topup.service --since today
```

Output: `~/.alphalens/grouped_daily_history/<YYYY-MM-DD>.parquet` (one
whole-market snapshot per session). Monitoring: `AlphalensJobStale`@48h +
`MetricMissing`.

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
**Scope:** this section covers the SIM daemon only. The LIVE daemon
(REAL-MONEY orders, ADR 0017 standing-LIVE authorization) has its own
runbook — see "LIVE instance runbook (ADR 0017 standing-LIVE
authorization)" below (§9).
**Merged:** PR #876 (`9005b5eb`). Design: [`docs/research/saxo_automanager_mvp_design_2026_07_21.md`](../../docs/research/saxo_automanager_mvp_design_2026_07_21.md).
**Golden rule:** deploy INERT first (no `ALLOW_ORDERS`) → smoke → arm ONE SIM test pick → only then go live.
**KILL layering applies across BOTH units (ADR 0016 D3):**
`~/.alphalens/broker_orders/KILL` is the GLOBAL kill — once the LIVE unit
(§9) exists, it halts placement in EVERY instance on this host, SIM and
LIVE together, not just this one. `~/.alphalens/broker_orders/sim/KILL`
halts only this SIM instance; the LIVE twin is
`~/.alphalens/broker_orders/live/KILL`. Both scopes keep reconciling and
managing exits — KILL stops new placement, it never abandons an open
position.

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
mkdir -p ~/.alphalens/broker_orders/sim
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
# Add ONLY at go-live — see §6 to turn it on:
# ALPHALENS_BROKER_ALLOW_ORDERS=1
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
cat ~/.alphalens/broker_orders/sim/picks.jsonl        # one armed line

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
| **Emergency stop (instant)** | `touch ~/.alphalens/broker_orders/sim/KILL` (this SIM instance only) or `touch ~/.alphalens/broker_orders/KILL` (GLOBAL — halts SIM and LIVE, ADR 0016 D3) — the loop stops placing, still reconciles + cancels |
| Resume after kill | `rm` the KILL file you created |
| **Disarm placement** (softer than kill) | comment `ALPHALENS_BROKER_ALLOW_ORDERS` in `/etc/alphalens/env` → `systemctl --user restart alphalens-broker-manager.service` (runs inert) |
| Arm a new pick | `.venv/bin/alphalens broker arm TICKER --date YYYY-MM-DD` (daemon picks it up next tick, joined to `submissions.jsonl` so it places once) (`--env sim\|live` selects the instance inbox; default sim — LIVE twin: §9.4) |
| Inspect | `journalctl --user -u alphalens-broker-manager.service -f` |
| State files | picks: `~/.alphalens/broker_orders/sim/picks.jsonl`; placements: `~/.alphalens/broker_orders/sim/submissions.jsonl` (both append-only; LIVE twin under `broker_orders/live/`) |
| Stop the daemon | `systemctl --user disable --now alphalens-broker-manager.service` |
| Full flat check | `.venv/bin/alphalens broker positions` + `... orders` |

**OAuth outage caveat:** if the VPS is down (or the keep-alive stops) for **>40 min**, the refresh chain dies → a `_chain_lost` Telegram alert fires and the daemon stops placing. Recovery = re-do §2 (attended browser login via SSH-forward). This is the one un-automatable step.

### 8. Safety recap

- **SIM-default is structural** — `SaxoClient` refuses any non-SIM base URL on every default/factory path; LIVE opens only under the ADR 0015 attended keyed unlock or the ADR 0017 standing account-bound grant used by the separate LIVE unit (§9).
- Layers before any real POST each tick: kill-file → chain alive → `ALLOW_ORDERS=1` → `MAX_OPEN` / portfolio-gross / daily-loss caps.
- The disaster stop is ALWAYS a standalone `StopIfTraded` placed after the entry fills, sized to realized qty (a ~30–60 s unprotected window per tick — acceptable on SIM).
- **Deferred (known issues, see the PR):** far-TP tranches are reported operator-managed (NOT placed); no ratchet / resize-on-partial / 42-session time-stop / streaming; alert debounce absent (persistent alerts repeat each tick).

### 8.5 Streaming early-wake (dark, SIM-only)

The daemon can early-wake on a Saxo WebSocket fill push instead of only on the ~45 s REST poll, shrinking the typical unprotected window from ~45 s to sub-second. It ships **DARK** — set `ALPHALENS_BROKER_STREAMING_ENABLED=1` in `/etc/alphalens/env` and `systemctl --user restart alphalens-broker-manager.service` to turn it on; unset -> the loop is byte-identical to today's poll-only sleep. Design memo: `docs/research/saxo_streaming_design_2026_07_24.md`.

- **Pure latency win, never-worse-than-poll.** The stream thread's ONLY cross-thread action is waking the existing loop early; it never places, never reconciles, never touches the journals. A total streaming failure (never started / disconnected / silently dead / thread-crashed / circuit-broken) degrades to EXACTLY the poll-only behaviour — the ~45 s absolute-deadline backstop still runs every cycle regardless of stream health. Protection can never be worse than with streaming off.
- **SIM-only + OAuth-only.** The streaming host is a structural SIM rail (live unreachable in code). It requires the OAuth provider — under a static `SAXO_SIM_TOKEN` it cannot re-authorize in place, so it refuses to start, logs once, and runs poll-only. Placement stays gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1` (unchanged — streaming never places).
- **Env knobs** (all optional, in `/etc/alphalens/env`): `ALPHALENS_BROKER_STREAMING_ENABLED=1` (master gate), `ALPHALENS_BROKER_STREAM_STALE_S` (default 45, must be `<=` poll and `>=` the ~20-30 s heartbeat cadence), `ALPHALENS_BROKER_STREAM_DEBOUNCE_S` (default 1.0). See `.env.example`.
- **Observability.** Every tick — including while the breaker is open — the main thread writes SIX stream gauges in ONE atomic emit to the stream's OWN textfile, per-instance since ADR 0016 D5 — `alphalens_domain_broker-manager-<env>-stream.prom` (SIM: `alphalens_domain_broker-manager-sim-stream.prom`) — NOT the `alphalens_domain_broker-manager-<env>.prom` heartbeat file (SIM: `alphalens_domain_broker-manager-sim.prom`) — because `emit_domain_metrics` overwrites a job's file atomically and the two families would otherwise clobber each other every tick (node_exporter merges all `*.prom`, so every series scrapes normally). The gauges (rearm design memo `saxo_stream_breaker_rearm_design_2026_08_22.md` §4.6), all labeled `{job="broker-manager-<env>"}`:
  - `alphalens_broker_manager_stream_reader_up` — 0/1, the reader thread is alive and streaming (claims to be working);
  - `alphalens_broker_manager_stream_breaker_open` — 0/1, EPISODE-scoped: 1 from the down edge until the delivery-confirmed close (it never flickers per re-arm trial, so `for:` windows evaluate against the episode);
  - `alphalens_broker_manager_stream_last_message_age_seconds` — seconds since the last streamed frame; written on every tick and never omitted (before any frame it reports seconds since the reader closure was built);
  - `alphalens_broker_manager_stream_consecutive_failures` — a LEVEL for eyeballing streak composition; `rate()`/`increase()` on it are nonsense;
  - `alphalens_broker_manager_stream_trips_total` — monotonic breaker-trip counter; feeds the flapping rule;
  - `alphalens_broker_manager_stream_in_session` — 0/1 XNYS trading-window gauge; emitted but referenced by NO shipped rule, so making a rule session-aware later is a one-line YAML change.

  A dark-but-connected stream (no frame for `STREAM_STALE_S` while no episode is open) still raises the throttled `stream silent — on poll backstop` Telegram alert; a tripped breaker pages exactly twice per episode (one OPEN, one delivery-confirmed CLOSE) — a stale stream is a latency regression back to the 45 s poll, NOT a protection outage.
- **Prometheus rules (levels).** Three rules ship in `deploy/monitoring/prometheus/rules/alphalens.yaml`, each with the daemon-freshness guard `time() - last_tick < 300` (a stopped daemon freezes the textfile; the guard hands off to `AlphalensBrokerManagerHeartbeatStale`):
  - `AlphalensBrokerStreamBreakerOpen` — `stream_breaker_open == 1` for 20 m (episode-scoped: at least four failed re-arm trials plus a dwell);
  - `AlphalensBrokerStreamStale` — `stream_last_message_age_seconds > 300` while `stream_reader_up == 1`, `unless` a breaker episode is open, for 5 m (dark-but-connected);
  - `AlphalensBrokerStreamFlapping` — `increase(stream_trips_total[1h]) > 3` for 10 m.

  The repo copy is NOT "documentation only": CI runs `promtool check rules` AND `promtool test rules` against it (`.github/workflows/ci.yml` prom-rules job; fixtures `alphalens_test.yaml` + `alphalens_broker_test.yaml`, locally `just lint-rules` / `just test-rules`). It IS the source of truth — but the live VPS Prometheus loads a separate, manually deployed copy. **Live-rules sync checklist (a deploy GATE for any stream-rule change, rearm memo §7.16 — until the rules are live, deleting the Telegram metronome strictly reduces observability):**
  1. copy the repo `alphalens.yaml` over the live rules file on the monitoring host;
  2. `docker exec prometheus kill -HUP 1`;
  3. confirm each rule is present in `curl -s localhost:9090/api/v1/rules | jq '.data.groups[].rules[].name'` before considering the change deployed.
- **30-second triage.** `alphalens broker stream-status` (reads the gauges from the textfile, no broker call, safe while the daemon runs): `breaker_open=1` → an episode is open, check the OPEN page timestamp and `trips_total`; `reader_up=1` with a large `last_message_age_seconds` → dark-but-connected (recv timeout/resubscribe not self-healing); `consecutive_failures` shows how close the streak is to the trip threshold (6). Then `journalctl --user -u alphalens-broker-manager.service --since -1h | grep -i "saxo stream"` for the trial-by-trial story. Protection is never at stake — the ~45 s poll backstop runs regardless.
- **Attended shape probe (before flipping the gate live):** `SAXO_STREAM_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_saxo_stream_live -v` (needs the OAuth env sourced) validates connect + snapshot + heartbeat + PUT-reauthorize 202 + DELETE cleanup against the live SIM host — SHAPE only, places nothing.

### 9. LIVE instance runbook (ADR 0017 standing-LIVE authorization)

`alphalens-broker-manager-live.service` is a SECOND, ADDITIVE daemon next
to the SIM unit above — same manager loop, same `alphalens broker manage`
command, but it places **REAL-MONEY** orders on Saxo LIVE. Locked design:
[`docs/research/broker_live_daemon_arm_design_2026_08_10.md`](../../docs/research/broker_live_daemon_arm_design_2026_08_10.md).
ADR: [`docs/adr/0017-standing-live-authorization.md`](../../docs/adr/0017-standing-live-authorization.md).

**Golden rule — same shape as SIM, higher stakes:** deploy INERT
(`ALLOW_ORDERS=0`, the unit's shipped default) → verify while inert → run
every fire drill → arm ONE pick, attended → only then consider leaving it
running unattended, and only after the §9.4 go/no-go bar is met.

#### 9.0 Prerequisites

- **LIVE OAuth chain bootstrapped.** The LIVE order client reuses the
  SEPARATE `saxo_auth_live` market-data chain (app `bracket-keeper`)
  documented in the "Saxo LIVE market data (INC-2 price feed)" section
  below — bootstrap it there first (§2a), there is no separate OAuth app
  for order placement. Confirm the chain is alive before going further:
  ```bash
  .venv/bin/alphalens broker marketdata-auth --status
  # expect: refresh ALIVE
  ```
- **`alphalens-saxo-marketdata-refresh.timer` enabled** — the keep-alive
  floor for that same chain; install it per "Saxo LIVE market data" §3
  below.
- **Prometheus rules hand-synced.** The LIVE rule blocks
  (`AlphalensBrokerManagerLiveHeartbeatStale`,
  `AlphalensLivePriceStreamReaderDown` / `AlphalensLivePriceStreamStale`
  for `job="live-price-stream-live"`,
  `AlphalensJobStale{job="saxo-marketdata-refresh"}`) ship in
  `deploy/monitoring/prometheus/rules/alphalens.yaml`, but — same as every
  other alert in this repo — the live Prometheus rules are NOT
  repo-mounted:
  ```bash
  # copy the new blocks into the live rules file, then:
  sudo promtool check rules /path/to/live/alphalens.rules.yml
  sudo kill -HUP "$(pgrep -x prometheus)"     # or systemctl reload prometheus
  ```
  Deploy this BEFORE enabling the unit. The LIVE heartbeat rule is
  value-based only for now — deliberately NO `absent()`-based Missing rule
  yet (design memo §5): an absent-rule for a not-yet-running instance would
  page the instant the rules reload (ADR 0016 D5 precedent). Add it in the
  SAME change that installs+enables the unit below, not ahead of it.

#### 9.1 Install

```bash
cd ~/AlphaLens
cp deploy/systemd/alphalens-broker-manager-live.service ~/.config/systemd/user/
```

Fill in the TWO account-bound lines on the **VPS copy only** — the repo
copy ships them commented out on purpose (ADR 0017 §1.3: together they are
the one grant that lets the LIVE factory construct a non-SIM `SaxoClient`
at all; the constructor itself checks
`ALPHALENS_SAXO_LIVE_STANDING == SAXO_LIVE_ACCOUNT_KEY`):

```bash
$EDITOR ~/.config/systemd/user/alphalens-broker-manager-live.service
# uncomment BOTH lines, fill BOTH with the SAME real live account key:
#   Environment=ALPHALENS_SAXO_LIVE_STANDING=<live account key>
#   Environment=SAXO_LIVE_ACCOUNT_KEY=<live account key>
```

Never commit the filled-in unit back to the repo — the account key is not
a secret value in the credential sense, but it is still account-identifying
and must never leave this one VPS file.

```bash
systemctl --user daemon-reload
systemctl --user enable --now alphalens-broker-manager-live.service
```

**Deploy coupling — entry-trailing 8th pin (PR-T0).** A build containing
the entry-trailing scaffolding extends the LIVE boot-assert from seven to
EIGHT pins. Before restarting the daemon onto such a build, add the new
pin to the VPS copy of the LIVE unit (beside the other rails):

```ini
Environment=ALPHALENS_BROKER_ENTRY_TRAIL_BPS=0
```

`0` = feature off (today's limit-at-touch entries; bound `[0, 150]` per
the entry-trailing design memo §6). A LIVE unit missing the pin fails at
`assert_live_rails()` on boot — that failure is the DESIGNED behavior
(fail loud, never inherit a default), not a bug; add the pin,
`daemon-reload`, restart.

#### 9.2 Verify INERT (`ALLOW_ORDERS=0`, the shipped default)

The unit ships with `ALPHALENS_BROKER_ALLOW_ORDERS=0` — it constructs the
LIVE client, keeps the OAuth chain alive, reads the account, reconciles,
and journals under `~/.alphalens/broker_orders/live/`, but places NOTHING
(design memo §7 step 1). Verify:

```bash
journalctl --user -u alphalens-broker-manager-live.service -f
# expect at boot: "SAXO LIVE ORDER RAIL UNLOCKED for ... standing (ADR
# 0017) account-bound grant verified" (loud on purpose, no secret values),
# then a clean steady-state loop — no tracebacks, no crash-restart loop

curl -s localhost:9100/metrics | grep 'job="broker-manager-live"'
# heartbeat gauge present and advancing every ~45s

ls ~/.alphalens/broker_orders/live/
# journals exist, empty of placements
```

**`DelayedByMinutes==0`** confirms the elevated (`FullTradingAndChat`)
session is genuinely real-time, not silently demoted (§5 "Single-holder
rule" of "Saxo LIVE market data" below). Confirm the app/chain CAN reach
it before handing the session to the daemon — run the attended shape
probe (§7 of that section) during a window when nothing else holds the
session, e.g. before this unit is enabled:
```bash
SAXO_MARKETDATA_LIVE_TEST=1 \
    ../../.venv/bin/python -m unittest tests.live.test_saxo_marketdata_live -v
```
Once the LIVE unit is running it becomes the sole elevated holder (§9.6
below) — do **not** re-run that probe against a live daemon without
stopping it first, or the two demote each other. **Known gap:** there is
currently no per-tick gauge reporting the daemon's own live
`DelayedByMinutes` (design memo §6 names this as a follow-up); the
`live-exits-feed-build-fail` throttled alert catches a total feed
construction failure, not a silent 15-minute delay on an otherwise-healthy
session — see "Saxo LIVE market data" §6 "Known issues" below.

**Dry pick evaluation.** Arm one test pick (`arm --env live`, §9.4 below)
while `ALLOW_ORDERS=0` and confirm the tick reaches and refuses it —
proof the daemon reads the picks queue, resolves the instrument, and
evaluates the master-arm gate cleanly before any money math runs:
```bash
journalctl --user -u alphalens-broker-manager-live.service -f | grep place_pick
# expect: "place_pick TICKER: refused — ALPHALENS_BROKER_ALLOW_ORDERS != '1'
# — master arm not set, placement inert"
```
The master-arm refusal fires BEFORE sizing (`safety.check` short-circuits
first), so no sizing-plan log line appears at this stage — that is
expected, not a sign of a missing wire-up.

#### 9.3 Fire drills — BEFORE arming, not after

Rehearse every one of these with the daemon still inert (design memo §7
step 2). An unrehearsed drill during a real incident is the wrong time to
discover a gap.

- **Chain-loss drill.** Invalidate the LIVE token store (or let it idle
  past its window with the refresh timer stopped) and confirm a
  `[live] chain lost` Telegram alert fires and the daemon stops placing
  without breaking reconcile or exit management.
- **Both KILL layers:**
  ```bash
  touch ~/.alphalens/broker_orders/live/KILL   # per-instance: LIVE only
  # confirm: LIVE stops placing; SIM (if running) keeps trading; both
  # instances keep reconciling
  rm ~/.alphalens/broker_orders/live/KILL

  touch ~/.alphalens/broker_orders/KILL        # GLOBAL: halts every instance
  # confirm: LIVE and SIM both stop placing; both keep reconciling
  rm ~/.alphalens/broker_orders/KILL
  ```
- **Manual-flatten rehearsal.** Practice the §9.5 rollback recipe below
  against a SIM position first, so the CANCEL-stops-FIRST ordering is
  muscle memory before it is ever needed with real money.
- **Fee-floor rejection visibility.** Arm a pick sized so the round-trip
  fee floor rejects it (design memo §4) and confirm the
  SKIPPED-AND-ALERTED path shows up in the journal / Telegram — never a
  silent drop.

#### 9.4 Attended arm

Operator present, one liquid US name, `MAX_OPEN=1`:

```bash
$EDITOR ~/.config/systemd/user/alphalens-broker-manager-live.service
# flip: Environment=ALPHALENS_BROKER_ALLOW_ORDERS=1
systemctl --user daemon-reload
systemctl --user restart alphalens-broker-manager-live.service

.venv/bin/alphalens broker arm TICKER --date YYYY-MM-DD --env live
journalctl --user -u alphalens-broker-manager-live.service -f
```

Watch it on saxotrader.com (LIVE, not SIM). Confirm the entry + standalone
disaster stop appear and match the brief geometry before walking away.

**Go/no-go for the first UNATTENDED night** (design memo §7 step 4, do not
skip): ≥3 clean attended round-trips spanning entry→OCO exit, ≥1 trail
event, native `TrailingStopIfTraded` exercised inside the manager loop
(not just probed standalone), every §9.0 telemetry item green, the
daily-loss breaker verified against LIVE P&L (not SIM's), and no
fee-rejection deadlock across the armed picks so far.

#### 9.5 Rollback ladder — least to most drastic, ORDER IS LOAD-BEARING

Go no further down this list than the situation requires; never skip
upward past where you already are.

1. **`touch ~/.alphalens/broker_orders/live/KILL`** — instance placement
   stop. Reconcile and protection (disaster-stop management) keep running.
2. **`touch ~/.alphalens/broker_orders/KILL`** — global kill, halts every
   instance on the host (SIM too). Same continue-reconciling guarantee.
3. **Disarm placement, keep protection:** flip
   `ALPHALENS_BROKER_ALLOW_ORDERS=0` in the unit + restart. The daemon
   returns to the INERT shape from §9.2 — reads, reconciles, manages
   exits, places nothing.
   ```bash
   $EDITOR ~/.config/systemd/user/alphalens-broker-manager-live.service
   # Environment=ALPHALENS_BROKER_ALLOW_ORDERS=0
   systemctl --user daemon-reload
   systemctl --user restart alphalens-broker-manager-live.service
   ```
4. **Manual flatten — CANCEL the resting stop/OCO orders FIRST, then
   market-sell, then cancel entry buys.** Selling with a live stop still
   resting risks the stop firing AFTER the flatten and double-selling into
   an unintended SHORT. Do the three steps in this exact order:
   ```bash
   # 1. cancel every resting StopIfTraded / OCO leg FIRST — check
   #    saxotrader.com or the daemon's own journal for the resting order
   #    IDs; there is no LIVE-targeted `broker orders` CLI yet, so this
   #    step is done on the Saxo web UI or via the daemon's own logs.
   # 2. market SELL the position, summed per-lot owned.
   # 3. cancel any still-resting entry buys.
   ```
5. **`systemctl --user stop alphalens-broker-manager-live.service` LAST —
   never the first move.** Stopping the daemon while positions are open
   removes exit management: the resting disaster stop remains (Saxo holds
   it independently of the daemon process), but planned exits (trail
   updates, OCO upgrades) stop happening without the daemon running. Only
   stop the unit once the account is flat (step 4) or once a human has
   deliberately taken over exit management.

#### 9.6 Sole elevated holder — coordinate with SIM before enabling

The LIVE daemon is the SOLE elevated (`FullTradingAndChat`) holder for the
duration of the soak (design memo §6) — Saxo allows exactly one elevated
session per login, and a second elevated consumer silently demotes both
sides to 15-minute-delayed prices. **Before enabling
`alphalens-broker-manager-live.service`, turn LIVE prices OFF on the SIM
instance:**

```bash
sudo sed -i 's/^ALPHALENS_SAXO_LIVE_PRICES=1/ALPHALENS_SAXO_LIVE_PRICES=0/' /etc/alphalens/env
systemctl --user restart alphalens-broker-manager.service
```

SIM's cost is stale prices on virtual money — zero. Revisit when SIM needs
its LIVE prices back (a cross-process shared price reader is a deferred
standing need, design memo §6, not YAGNI-never).

#### 9.7 Standing-grant decommission

The account-bound grant (`ALPHALENS_SAXO_LIVE_STANDING` /
`SAXO_LIVE_ACCOUNT_KEY`) does not self-expire — a disabled-but-not-cleaned
unit retains authority until the grant is actually removed. No extra
mechanism beyond this note (design memo §8.10 — the operator decision was
"note in runbook; no extra mechanism"); KILL and `systemctl disable` are
the mitigation in the meantime:

```bash
systemctl --user disable --now alphalens-broker-manager-live.service
$EDITOR ~/.config/systemd/user/alphalens-broker-manager-live.service
# delete both account-bound Environment= lines
systemctl --user daemon-reload
```

### 10. Per-environment state separation (ADR 0016) — one-time VPS migration

ADR 0016 (design memo `docs/research/broker_env_state_separation_design_2026_08_10.md`) moves every mutable broker-state path from one flat `~/.alphalens/broker_orders/` tree into a per-instance layout, `~/.alphalens/broker_orders/<env>/`, so a future LIVE instance cannot corrupt the SIM instance's journals. When this migration shipped it was a preparatory move only (ADR 0016 D7 then hard-blocked `ALPHALENS_BROKER_ENVIRONMENT=live`); ADR 0017 since replaced that block with the LIVE factory route — `broker manage` with env=live now boots through `create_saxo_broker_live_from_env` + the live_rails boot-assert, and the LIVE instance exists (§9).

**`ALPHALENS_BROKER_ENVIRONMENT` doctrine — pin it in the unit, never in `/etc/alphalens/env`.** The SIM daemon needs `ALPHALENS_BROKER_ENVIRONMENT=sim` set somewhere. It is pinned as an `Environment=` line inside `alphalens-broker-manager.service` itself (see the `[Service]` section), NOT in `/etc/alphalens/env`. CORRECTED 2026-08-11 (front-4 stage-1 finding): `EnvironmentFile=` overrides ALL `Environment=` lines — in-unit AND drop-in, regardless of order — so an in-unit pin does NOT survive a conflicting entry in the shared file. The pins hold ONLY because every `ALPHALENS_BROKER_*` rail is banned from `/etc/alphalens/env` (the strip is load-bearing), and the LIVE boot-assert is the tripwire: on the LIVE unit's first boot it caught leaked `MAX_OPEN=10` / `ALLOW_ORDERS=1` from the shared file overriding the in-unit pins. **Never set `ALPHALENS_BROKER_ENVIRONMENT` in `/etc/alphalens/env`** — that file is shared across future instances and setting the var there defeats the whole point of the in-unit pin.

**KILL is layered — global vs per-instance (ADR 0016 D3).** `touch ~/.alphalens/broker_orders/KILL` (the parent-level path, unchanged from before this migration) is now the **GLOBAL** kill: it halts placement in every instance on this host, not just SIM. The operator's existing muscle-memory command keeps working and gains scope rather than losing it. A NEW per-instance kill also exists: `touch ~/.alphalens/broker_orders/sim/KILL` stops only the SIM instance. Both are checked on every tick; reconcile and protective actions continue under either.

One-time migration on the VPS, run right after the code deploy that carries ADR 0016 (stop the daemon first — the daemon fail-loud refuses to start against the old flat layout, ADR 0016 D4):

```bash
systemctl --user stop alphalens-broker-manager
mkdir -p ~/.alphalens/broker_orders/sim ~/.alphalens/exec_quality/sim
mv ~/.alphalens/broker_orders/*.jsonl ~/.alphalens/broker_orders/sim/
[ -f ~/.alphalens/exec_quality/tranche_fills.parquet ] && \
  mv ~/.alphalens/exec_quality/tranche_fills.parquet ~/.alphalens/exec_quality/sim/
sudo rm -f /var/lib/node_exporter/textfile/alphalens_domain_broker-manager.prom \
           /var/lib/node_exporter/textfile/alphalens_domain_broker-manager-stream.prom \
           /var/lib/node_exporter/textfile/alphalens_domain_live-price-stream.prom
cp deploy/monitoring/prometheus/rules/alphalens.yaml ~/monitoring/prometheus/alphalens.rules
docker exec prometheus promtool check rules /etc/prometheus/alphalens.rules
docker exec prometheus kill -HUP 1
git -C ~/AlphaLens pull && ~/.local/bin/uv sync
systemctl --user daemon-reload && systemctl --user start alphalens-broker-manager
# verify: new .prom files carry job="broker-manager-sim"; heartbeat fresh
```

A leftover `broker_orders/KILL` at the parent level is now the GLOBAL kill — do not move it; its absence is the normal state.

## Saxo LIVE market data (INC-2 price feed)

The SIM broker-manager daemon (`alphalens-broker-manager.service`, above)
trades on **SIM only** (ADR 0014 — that stays unchanged). The LIVE daemon
(`alphalens-broker-manager-live.service`, §9 above) is the one exception —
it places real orders on Saxo LIVE under the ADR 0017 standing grant — but
its order client is separate CODE (`brokers/saxo/`, still SIM-structural
except for the ADR 0017 widening) that AUTHENTICATES on this same
`saxo_auth_live` chain (`live_tokens.LiveOrderTokenProvider` adapts it,
§9.0) — so since ADR 0017 this app's trading permission IS exercised by the
LIVE order rail, and keeping the chain alive is a LIVE-daemon precondition,
not just a price-feed nicety. Any daemon that reads exit prices from Saxo
reads them from a SECOND app registered on the **LIVE** environment (app
`bracket-keeper`) — LIVE because SIM quotes are 15-minute-delayed.
`SaxoMarketDataClient` itself
(`alphalens_pipeline/data/alt_data/saxo_marketdata_client.py`) still only
reads session capabilities, resolves tickers to uics, and manages one price
subscription — it never places. Design memo: `docs/research/live_market_execution_inc2_design_2026_08_07.md`.

**This is a DIFFERENT app, a DIFFERENT OAuth client, and a DIFFERENT token
store from the SIM auto-manager above.** Do not reuse `SAXO_APP_KEY` /
`SAXO_APP_SECRET` / the SIM token store for any of this.

### 1. Env file — `/etc/alphalens/env`

```bash
sudo tee -a /etc/alphalens/env >/dev/null <<'EOF'
# --- Saxo LIVE market data (INC-2, read-only) ---
SAXO_LIVE_APP_KEY=<live app key>
SAXO_LIVE_APP_SECRET=<live app secret>
SAXO_LIVE_AUTH_REDIRECT_URL=http://localhost:8765/callback   # MUST byte-match the LIVE portal registration
# Add ONLY after §2a (VPS bootstrap) is done — see §4 to turn it on:
# ALPHALENS_SAXO_LIVE_PRICES=1
EOF
sudo chmod 600 /etc/alphalens/env
```

`/etc/alphalens/env` is loaded ONLY by the systemd units on the VPS (see
"Environment file setup" at the top of this README) — it does not exist on a
developer machine. A developer machine instead keeps the same four
`SAXO_LIVE_*` keys in the repo-root `.env` (see the top-level `## Environment`
section of `CLAUDE.md`), used by §2b below.

The redirect reuses the same `localhost:8765` port as the SIM `broker auth`
flow — that is fine, they are never open at the same time — but it is a
SEPARATE app registration on Saxo's LIVE portal, not the SIM one.

### 2. One-time attended OAuth bootstrap — one procedure per machine, never shared

This app can be bootstrapped from more than one machine: the VPS (for the
production daemon, §2a) and, separately, a developer machine (for ad-hoc
attended checks such as §7's probe, §2b). Each bootstrap writes to **that
machine's own** default token-store path
(`~/.alphalens/saxo_auth_live/token_store.json`, overridable via
`SAXO_LIVE_TOKEN_STORE_PATH`) using **that machine's own** env file — never
the other machine's.

**These are two SEPARATE token stores for the same app — NOT
interchangeable.** The refresh token is single-use and rotates on every
refresh (`saxo_marketdata_auth.py` module docstring), so each machine must
bootstrap and refresh its own. Copying either store to the other machine (or
running a bootstrap against a synced copy) invalidates whichever side
refreshes second the moment the other one also tries.

**Separately from that — and this is the part that matters even when both
stores are individually healthy — Saxo permits only ONE elevated session at a
time, full stop, regardless of which token store holds it (§5).** Bootstrapping
or using the store on a developer machine takes real-time data away from the
VPS daemon (if it is running with the flag on) and from the operator's own
SaxoTraderGO, for as long as that machine's session stays elevated. **That is
the actual reason to prefer the VPS for routine checks and to keep any
developer-machine run deliberate and short** — it is not a technical
restriction on where bootstrap is allowed to happen.

**There is no `alphalens broker auth`-equivalent CLI for this app yet** — both
procedures below run the SAME short attended script over the public
primitives in `saxo_marketdata_auth.py` (`LiveAuthConfig.from_env`,
`build_authorize_url`, `exchange_code`). It never prints a token.

#### 2a. Bootstrap on the VPS (for the production daemon)

The token store must be written **on the VPS**, so the OAuth redirect must
land on the VPS too — run the bootstrap ON the VPS while forwarding port 8765
from your laptop, exactly like the SIM `broker auth` bootstrap in the
previous section.

**Laptop terminal A** — open the tunnel (leave it running):
```bash
ssh -L 8765:localhost:8765 jacoren@vault
```
**In that same SSH session (on the VPS)** — start the one-shot listener:
```bash
cd ~/AlphaLens && set -a && source /etc/alphalens/env && set +a
.venv/bin/python - <<'PY'
import http.server
import urllib.parse

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
    LiveAuthConfig,
    build_authorize_url,
    exchange_code,
)

cfg = LiveAuthConfig.from_env()
authorize_url = build_authorize_url(cfg, state="bootstrap")
print("open this URL to authorize (LIVE credentials):")
print(authorize_url)

code_holder: dict[str, str | None] = {"code": None}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        query = urllib.parse.urlparse(self.path).query
        code_holder["code"] = urllib.parse.parse_qs(query).get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"authorized - you can close this tab")

    def log_message(self, *args: object) -> None:  # silence stdlib access log
        pass


print("waiting for the redirect on http://localhost:8765/callback ...")
http.server.HTTPServer(("localhost", 8765), _Handler).handle_request()  # blocks for ONE request
if not code_holder["code"]:
    raise SystemExit("no ?code= on the redirect - check the LIVE portal redirect URL registration")

exchange_code(cfg, code=code_holder["code"])
print("authorized - LIVE token store written (tokens are never printed)")
PY
```
**Laptop browser** — open the printed authorize URL, log in with **LIVE**
credentials (not SIM). Saxo redirects to `http://localhost:8765/callback` →
the SSH tunnel forwards it to the VPS listener → the script exchanges the
code and writes the token store **on the VPS**.

#### 2b. Bootstrap (or re-bootstrap) on a developer machine

No tunnel is needed here: the browser and the one-shot listener are the SAME
machine, so the redirect goes straight to `localhost:8765` without leaving it.
Credentials come from the repo-root `.env`, NOT `/etc/alphalens/env` — that
file does not exist on a developer machine. The token store lands at this
machine's own `~/.alphalens/saxo_auth_live/token_store.json` (same default
path as the VPS — a different physical file, since `$HOME` differs per
machine).

```bash
cd ~/Developer/Personal/AlphaLens/apps/alphalens-research && \
set -a && . ../../.env && set +a && \
../../.venv/bin/python - <<'PY'
import http.server
import urllib.parse

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
    LiveAuthConfig,
    build_authorize_url,
    exchange_code,
)

cfg = LiveAuthConfig.from_env()
authorize_url = build_authorize_url(cfg, state="bootstrap")
print("open this URL to authorize (LIVE credentials):")
print(authorize_url)

code_holder: dict[str, str | None] = {"code": None}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        query = urllib.parse.urlparse(self.path).query
        code_holder["code"] = urllib.parse.parse_qs(query).get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"authorized - you can close this tab")

    def log_message(self, *args: object) -> None:  # silence stdlib access log
        pass


print("waiting for the redirect on http://localhost:8765/callback ...")
http.server.HTTPServer(("localhost", 8765), _Handler).handle_request()  # blocks for ONE request
if not code_holder["code"]:
    raise SystemExit("no ?code= on the redirect - check the LIVE portal redirect URL registration")

exchange_code(cfg, code=code_holder["code"])
print("authorized - LIVE token store written (tokens are never printed)")
PY
```
Open the printed URL directly in this machine's own browser (no SSH tunnel —
you are already on `localhost`), log in with **LIVE** credentials. The script
exchanges the code and writes the token store on THIS machine.

### 3. Refresh cadence — versioned keep-alive timer + per-call adoption

The LIVE market-data token refreshes two ways, and on the VPS both matter:

- **Per-call adoption.** `LiveTokenProvider.access_token()` (same 120 s-
  before-expiry margin as the SIM provider) is called on every REST call
  made through `SaxoMarketDataClient` (session-capability reads, uic
  resolution, subscription create/delete) AND on every WebSocket
  (re)connect the price stream makes — so as long as
  `ALPHALENS_SAXO_LIVE_PRICES=1` and a consumer is actually using the feed
  (the LIVE broker-manager daemon, §9 above), the token
  stays fresh as a side effect of the feed being used.
- **`alphalens-saxo-marketdata-refresh.{service,timer}`** (~every 20 min)
  is the keep-alive FLOOR for idle stretches where nothing else touches
  the chain — the exact role `alphalens-saxo-refresh.timer` plays for the
  SIM order chain. Install it once, on the VPS, alongside the LIVE daemon
  unit (§9 install step):
  ```bash
  cp deploy/systemd/alphalens-saxo-marketdata-refresh.{service,timer} ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now alphalens-saxo-marketdata-refresh.timer
  systemctl --user list-timers | grep saxo-marketdata-refresh
  ```
  **Single-refresher invariant** — same rule as the SIM timer: run this
  unit ONLY on the VPS that also runs the LIVE broker-manager daemon. Two
  hosts refreshing the same token store burn each other's rotation chains
  (the flock is per-host, not per-store).

If `ALPHALENS_SAXO_LIVE_PRICES` stays OFF, or both the timer and the daemon
are down for long enough that the refresh chain goes idle anyway, the fix
is the same as the SIM "OAuth outage" case: re-run §2a's attended
bootstrap.

A developer machine's store has no continuous process refreshing it at
all — the timer above is a VPS-only unit — only an attended run (§7)
touches a developer-machine store, and only while that run is in progress.
If it has been long enough since the last attended run that the refresh
chain has gone idle, the fix is the same: re-run §2b.

### 4. Turning it on

```bash
sudo sed -i 's/^# ALPHALENS_SAXO_LIVE_PRICES=1/ALPHALENS_SAXO_LIVE_PRICES=1/' /etc/alphalens/env
#   (or add the line if not present)
systemctl --user restart alphalens-broker-manager.service
journalctl --user -u alphalens-broker-manager.service -f
```

### 5. Single-holder rule — exactly ONE elevated session

Saxo permits exactly one elevated (`FullTradingAndChat`) session per LIVE
login at a time (verified empirically 2026-08-07 — see
`session_reclaim.py` module docstring). A default OAuth session is
`OrdersOnly`, which silently serves 15-minute-delayed prices; elevating one
holder demotes whichever session held it before, including:

- the operator's own SaxoTraderGO session, and
- the production daemon, if it is currently running with the flag on.

**Before running the §7 live probe from EITHER machine (a developer machine's
own attended run, or a VPS run made outside the daemon's own process) against
this app, stop the production daemon first** (or coordinate the timing with
whoever is running it) — running both at once just makes them demote each
other back and forth, regardless of which machine's token store either side
is using (§2). The daemon's own reclaim logic (`ReclaimLimiter`,
`session_reclaim.py`) automatically re-elevates itself up to 4 times/hour once
it observes a delayed quote, so a human who keeps pressing "resume" in
SaxoTraderGO eventually wins the ping-pong by persistence — by design, not a
bug.

### 6. Known issues

- **No `stop()` on daemon shutdown.** `get_shared_price_stream()`
  (`saxo_price_stream.py`) is a process-lifetime singleton; nothing in the
  daemon's shutdown path calls `SaxoPriceStream.stop()`. The WebSocket thread
  (and its price subscription) runs until the process exits, not until the
  flag is turned off or the daemon is asked to stop gracefully. Not currently
  a resource leak in practice (the process typically exits via
  `systemctl stop`/restart, which tears down the whole process), but do not
  expect toggling `ALPHALENS_SAXO_LIVE_PRICES` off at runtime to close the
  subscription without a restart.
- **An unbootstrapped LIVE token store looks like "nothing happens", not a
  crash.** If `ALPHALENS_SAXO_LIVE_PRICES=1` is set before §2a's VPS bootstrap
  has run (or the store is stale/corrupt), `LiveAuthConfig.from_env()` /
  `LiveTokenProvider` raise inside the feed-factory construction. That
  construction failure is caught deliberately broadly by
  `_build_live_exits_feed` (`control_loop.py`) — "every doubt becomes a
  veto" — and degrades to `_NullPriceFeed`, which vetoes every uic. The
  daemon keeps running, keeps reconciling, keeps managing the standalone
  disaster stop — it just never fires a live-market exit. The only trace is
  ONE throttled alert (`live-exits-feed-build-fail`) the first time it
  happens, then silence. If exits stop firing after enabling this flag, check
  `journalctl --user -u alphalens-broker-manager.service | grep live-exits`
  for that alert before assuming anything else is wrong.

### 7. Attended shape probe (before flipping the gate live)

Run it from whichever machine has a bootstrapped token store (§2) — the
credentials source and the token store used differ by machine; the test code
and its assertions are identical either way.

**On the VPS** (credentials from `/etc/alphalens/env`, store from §2a):
```bash
cd ~/AlphaLens/apps/alphalens-research && set -a && . /etc/alphalens/env && set +a && \
SAXO_MARKETDATA_LIVE_TEST=1 \
    ../../.venv/bin/python -m unittest tests.live.test_saxo_marketdata_live -v
```

**On a developer machine** (credentials from the repo-root `.env`, store from
§2b):
```bash
cd ~/Developer/Personal/AlphaLens/apps/alphalens-research && \
set -a && . ../../.env && set +a && \
SAXO_MARKETDATA_LIVE_TEST=1 \
    ../../.venv/bin/python -m unittest tests.live.test_saxo_marketdata_live -v
```

SHAPE only, never values: elevates the session, resolves AAPL to a uic,
opens and tears down one price subscription, asserts the quote row reports
`DelayedByMinutes == 0`. **Elevates the single-holder session (§5)** — never
run this from either machine while the production daemon holds it without
coordinating first; a closed market (or an already-demoted session) reports
as an inconclusive TRANSIENT result, not a shape failure.
