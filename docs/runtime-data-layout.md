# Runtime data under `~/.alphalens/`

Everything here lives OUTSIDE the repository, so it survives every git operation and is never
restored by one. Nothing in this tree is shared between the Mac and the VPS: each host holds its
own copy, and a path present on one can be absent or stale on the other.

Rules before you read a store:

- **Write path and read path are different facts.** A store with no live producer can still have
  live readers (`av_cache/` below), and a store a timer writes can be read by nothing on this host.
- **Check the store, not this file.** Each row names the code that owns the path; that code is the
  authority. Verified 2026-10-14, by grep over `apps/` and `deploy/`.
- **A local copy of a VPS store is a different store.** After a rebuild its fresh mtime says
  nothing about the VPS.
- **Never merge the two grouped-daily caches.** See the row below; they differ in adjustment.

## Live stores

| Path | Written by | Read by |
|---|---|---|
| `candidates.db` | Layer 1 EDGAR detector (historical log) | nothing drains it today |
| `edgar-detect/{seen_events,digest,company_tickers}.{db,json}` + `portfolio.yaml` | `alphalens edgar detect`, every 15 min | the same command (dedup + digest) |
| `form4_parquet/` (hive-partitioned) + `form4_cik_universe.txt` (8005 issuers per the comment at `data/alt_data/form4_incremental.py:340`) | `alphalens-form4-incremental` daily; seeded once by the 2026-05-08 backfill | the live reader `thematic/sources/form4_store.py:31` opens the directory BY PATH; lab scripts and the `opportunistic_form4` scorer |
| `grouped_daily_history/<YYYY-MM-DD>.parquet` | `alphalens-grouped-daily-topup` (ET-anchored since #1774) | the thematic `score` stage, disk-only, via `rs_history.rs_percentile` — the O'Neil **R** term |
| `thematic_briefs/` | the daily thematic pipeline's `brief` stage | Django `rebuild_briefs_cache`, the feedback population monitor, the lab |
| `buffett_qual/<date>/<TICKER>{.sb}.json` | `alphalens experts enrich` (successes only, so a rerun re-pays only for unclassified names) | the same command, as its cache |
| `population_ladders/` + `population_ladders/bars/<TICKER>_<arrival-session>.parquet` (`feedback/population_ladder_monitor.py:516`) | `alphalens feedback backfill-shadow-returns`, nightly | the hourly `alphalens-edge-mirror` rebuild of Postgres `edge_ladderoutcome`; the lab |
| `broker_orders/<env>/{picks,submissions,pick_waits,standalone_stops,entry_trails}.jsonl`, plus `KILL` per env and a legacy global `broker_orders/KILL` | `alphalens broker arm` writes `picks.jsonl`; the `broker manage` daemon writes the rest | both, and `alphalens broker {picks,status,trades,…}`. Path helpers: `brokers/automanager/state_paths.py` |
| `saxo_auth/token_store.json` (0600, flock) | `alphalens broker auth --refresh`, every 20 min | the broker-manager daemons |
| `survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet` | hand-seeded, Mac-only | `delisted_2021_2026` only: `screeners/options_implied/target.py` and ten scripts under `apps/alphalens-research/scripts/` (counted 2026-10-14). `delisted_2007_2018` has no code consumer |
| `replay_bars/` | `scripts/replay_from_pick.py`, as its default `--bar-cache` | the same script |
| `audit/<strategy>/phase_*_returns.parquet` | the per-strategy audit runners (`DEFAULT_ARTIFACT_ROOT`) | attribution and the Layer 4 pre-screen cross-check |

## iVolatility — several roots, none of them `ivolatility_cache/`

There is no `ivolatility_cache/` directory and there never was; only a `CLAUDE.md` line and one
memo ever named it. The roots the pulling scripts really write, each named in the script that owns
it:

- `ivolatility_smd/{TICKER}.parquet` — `scripts/backfill_ivolatility_pre_2018.py`,
  `scripts/check_v7_pull_status.py`, the SMD cache readers.
- `ivolatility_smd_inventory.parquet` — `scripts/build_ivol_inventory.py`.
- `ivolatility_ivs/{TICKER}.parquet` — `scripts/pull_ivolatility_ivs.py`.
- `ivolatility_calendar/` — the calendar endpoint puller.
- `ivolatility_smd_retro_2026_07_deep/` — `scripts/run_options_retro_pilot.py`.
- `options_retro_pilot_2026_07/` — the same pilot's own output root.
- Further roots exist for the one-off probes (`ivolatility_pre_2018`,
  `ivolatility_options_survivorship{,_v2}`, `ivolatility_survivorship_probe*`). Before adding a
  puller, grep `apps/alphalens-research/scripts/` for `ivolatility` and reuse the root that owns
  your endpoint; the list above is the set in use on 2026-10-14, not a closed vocabulary.

A new puller reuses the root for its endpoint rather than inventing a path. Persist the raw API
response BEFORE processing and never re-fetch on retry: the subscription is metered at $399/mo.

## `av_cache/` — archived producer, live readers

- The producer is GONE: `alphalens-av-earnings-backfill.{service,timer}` were decommissioned in
  #752, the VPS timer disabled 2026-07-03, its staleness alert removed 2026-07-05, and the
  502-ticker snapshot archived to Nextcloud `AlphaLens-prod/caches/av_cache_2026-07-05.tar.zst`.
- The readers are LIVE: `av_cache` is still a declared `DataDep` in
  `alphalens_research/preaudit/profiles.py`, so `alphalens preaudit` on a PEAD strategy still
  checks coverage for it, and `scripts/validate_av_report_time_pit.py` defaults its `--cache-dir`
  to it.
- So a missing `av_cache/` is a coverage failure with no job to fix it. Restore from the archive,
  or change the `DataDep`.

## Copying between hosts

- The VPS copy is the source of truth for VPS-side consumers. Nextcloud sync is NOT a repo-wide
  convention: the `--rclone-remote` flag exists on exactly ONE script,
  `apps/alphalens-research/scripts/av_earnings_daily_backfill.py`, and is currently off.
- For Mac-side use, copy explicitly: `rsync -av jacoren@vps:.alphalens/<area>/ ~/.alphalens/<area>/`.
