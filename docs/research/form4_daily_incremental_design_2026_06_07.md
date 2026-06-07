# Form-4 daily incremental ingest — design memo

**Status: LOCKED**
**Date: 2026-06-07**
**Branch: `feat/form4-daily-incremental`**
**Scope: keep `~/.alphalens/form4_parquet/` (hive-partitioned by `transaction_year=YYYY`) COMPLETE and FRESH automatically, hands-off, VPS as single source of truth.**

> **Scope revision 2026-06-08 — universe-scoped, not market-wide.** The original
> design ingested every Form-4/4-A in each daily index (market-wide). Live, that
> was ~2000-3000 filings/day → ~2640 `.txt` fetches/day at SEC's 10 req/s →
> ~2 h for the 27-day catch-up, which blew the 45-min unit timeout. The runner
> now filters the daily index to the **8005-CIK universe** (the same scope the
> historical seed was built from; the thematic tool only ever reads insider data
> for universe issuers) BEFORE any `.txt` fetch — ~10× fewer requests, catch-up
> in minutes. The engine takes `cik_universe: set[str] | None` (None = the old
> market-wide path, kept for `--market-wide`); the runner loads
> `~/.alphalens/form4_cik_universe.txt` by default and **fails loud** if it is
> missing rather than silently degrading to the slow path. Already-ingested
> market-wide rows (seed + the partial catch-up) stay — harmless extra data the
> consumer never queries.

---

## 1. Problem & non-goals

The historical Form-4 base is SEEDED and compacted (~2.66M rows, complete through ~2026-05-08, per
`alphalens-form4-backfill.service`, DONE). It is now a frozen snapshot that ages a little more every day.
We need a DAILY job that:

- **(a) First run:** catch up the gap from the seed date (~2026-05-08) to today (~30+ days).
- **(b) Every later run:** fetch only the recent window.
- Overlap between runs must be **dedup-safe** (no duplicate rows, no manual state file to corrupt).

**Non-goals (explicitly NOT built here):**
- the one-time historical bulk re-fetch (already done by the seed);
- any Mac↔VPS sync (VPS cache is SoT; `rsync` recipe stays the documented manual escape hatch);
- any change to the thematic pipeline, brief schema, or `FORM4_SCHEMA_COLUMNS`;
- a long-tail "late amendment" deep-refresh job (noted as a future enhancement, see §7).

---

## 2. Strategy decision: daily-index discovery, CIK-scoped submissions for the doc path

**Chosen: SEC daily-index per-date discovery to find WHICH CIKs filed Form-4/4-A on a date, then fetch
the per-CIK `submissions/CIK{cik}.json` ONLY for that small filer set to resolve the raw XML
`primary_document` path, intersected by accession.** This is a hybrid of the two research reports.

### Why daily-index, not a per-CIK walk of the 8005-CIK universe

| Strategy | HTTP / day (steady-state) | Coverage | Notes |
|---|---|---|---|
| Walk 8005-CIK universe submissions every run | ~8005 req | complete but stale-roster-risk | ~200× more SEC budget; collides with edgar-detect + thematic on the shared per-IP 10 req/s bucket every single run |
| **Daily-index per date (CHOSEN)** | 1 index fetch/day + ~1 submissions fetch per *distinct filer that filed a 4 that day* (~tens–low-hundreds) + 1 XML fetch per accession | complete (the `.idx` lists EVERY 4 filed that day) + no roster staleness | immutable past-date `.idx` ⇒ re-runs are cheap; mirrors the proven `edgar_press_release.py` pattern |

The daily form index (`form.{YYYYMMDD}.idx`) lists every filing of every form type submitted that UTC day,
so it can never miss an in-universe filer absent from a stale local roster. This is the same coverage
argument that already justifies `thematic/sources/edgar_press_release.py` for 8-K.

### Why ALSO touch per-CIK submissions (the one wrinkle vs. the 8-K path)

The 8-K press-release path resolves its document name from the filing's `{accession}-index.htm` Document
Format Files table. Form-4 needs the **raw XML** path, and the proven, already-tested resolver for that
is `iter_form4_filings(submissions_json)` (it applies `_strip_xsl_prefix` so the path resolves to raw XML,
not the XSL-rendered HTML that makes every parse fail). Rather than write a second, untested index.htm
document-table walk for Form-4, the engine:

1. parses the daily-index for Form-4/4-A rows → set of distinct `cik` that filed that day;
2. for each such CIK, fetches `submissions/CIK{cik}.json` once and runs the existing
   `iter_form4_filings` over the `recent` block;
3. keeps only the `FilingMetadata` whose `accession_number` ∈ the day's daily-index accession set
   (so a CIK that filed both an old 4 last year and a new 4 today contributes only today's accession);
4. fetches + parses XML via the existing `client.fetch_form4_xml` + `parse_form4_xml`.

Step 2 only ever reads the `recent` block (≤1000 newest filings) — a same-week Form-4 is always in
`recent`, so the `files` overflow walk used by the bulk backfill is **not** needed here. This keeps the
per-CIK cost at exactly one submissions fetch.

**HTTP budget, steady-state 3-day window:** 3 index fetches + (~tens of distinct filers × 1 submissions)
+ (~tens of accessions × 1 XML) ≈ low-hundreds of requests, comfortably inside the 10 req/s bucket in a
few minutes. **First-run auto catch-up (~30-day seed gap):** ~30 index fetches + a few thousand
submissions/XML fetches ≈ tens of minutes to ~2h wall at 10 req/s; bounded by `--max-catchup-days`.

### Fallback when a daily-index fetch fails

A `SecForbiddenError` / transient 5xx / parse-miss on a single date is **logged and the date is skipped**
(degrade gracefully, count it as a transient error, never raise). Because the next run's window overlaps
(see §3) and the `.idx` is immutable, a skipped date is re-attempted on the next run with no permanent
loss. We do **not** fall back to walking 8005 CIKs (expensive, defeats the purpose) — the overlapping
window IS the recovery mechanism.

---

## 3. State / freshness mechanism: STATELESS fixed-lookback window

**No state file.** Each run fetches a fixed date window `[asof_date − lookback_days, asof_date]` (UTC),
walking each date via the daily index. State lives entirely in the parquet store + `accession_number`
uniqueness.

- **Steady-state minimum:** `--lookback-days 3` (the SEC 2-business-day filing deadline means a 3-day
  window over-covers the normal case; a weekend run still re-reads Fri+Sat+Sun).
- **Self-sizing window (no manual catch-up):** each run reads the store's newest `filed_date`
  (`latest_filed_date_in_store`, scanning every partition's `filed_date` column since a late 4/A can land
  a recent filing in an old `transaction_year` partition) and extends the window start back to
  `latest − overlap_days` whenever that is earlier than the 3-day default. So the FIRST run after the seed
  (or after any missed run) closes the gap automatically; the same `--lookback-days 3` invocation serves
  both catch-up and steady state. `_resolve_window_start` = `max(min(asof − (lookback−1), latest − overlap), asof − max_catchup_days)`.
- **Bounded:** `--max-catchup-days` (default 400) caps the reach-back so an empty/misread store can never
  walk years of daily indexes.
- **Overlap is dedup-safe by construction:** consecutive daily runs overlap by `lookback_days − 1` days.
  `accession_number` is the globally-unique SEC filing ID; the same accession re-fetched on overlapping
  days produces byte-identical `Form4Record`s, which collapse under
  `compact_partition`'s `drop_duplicates()` (full-row). The compactor already relies on exactly this
  property for resumed bulk backfills — no new dedup code.

**Why stateless over a last-success-date file:** a state file is a single point of silent corruption
(truncated write, clock skew, manual edit) that can permanently skip a window with no alarm. A fixed
lookback re-reads recent days every run, so a one-run miss self-heals on the next run. The only cost is
re-fetching ~`lookback_days` of immutable index data — negligible.

---

## 4. First-run catch-up (automatic, no operator math)

The window self-sizes, so the first run catches up on its own regardless of deploy date:

1. Operator just enables the timer (or triggers one fire). The first run reads the store's newest
   `filed_date` (~2026-05-07 from the seed), sets the window start to `latest − overlap` (~2026-05-05),
   and walks the whole gap to today in one fire — bounded by `--max-catchup-days` (400). The overlap
   re-reads the seed's last days; those accessions collapse on compaction (no double-count).
2. Every subsequent run finds the store fresh (newest filing ≈ yesterday), so the 3-day default dominates
   and the window settles to steady state.
3. A missed run (VPS down for a week) self-heals identically on the next fire — the window auto-extends
   back to wherever the data ends.

This removes the deploy-date fragility of a fixed catch-up size: deploying days or weeks after the seed
froze makes no difference. Verified by comparing `alphalens_form4_latest_filing_date` against today
post-run (runbook §6).

---

## 5. Exact file list

Engine in `alphalens_pipeline/data/alt_data/` (pipeline tier — data-acquisition infra, ADR 0011); runner
as a **script under `apps/alphalens-research/scripts/`** mirroring `run_form4_backfill.py` +
`av_earnings_daily_backfill.py`. **Why a script, not a CLI subcommand:** the existing Form-4 + AV backfills
are both scripts, the systemd pattern (`%h/AlphaLens/.venv/bin/python apps/.../scripts/<x>.py`) is proven,
and a CLI subcommand would add a lazy-import surface + a typer-default-parity test for zero benefit. The
engine module carries all importable logic; the script is a thin argparse + metrics shell.

| Path | Kind | Purpose |
|---|---|---|
| `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/form4_incremental.py` | engine | `parse_form4_index_rows(idx_text) -> list[Form4IndexRow]` (Form-4/4-A rows from a daily `.idx`); `fetch_form4_records_for_window(client, *, start_date, end_date, parquet_root) -> IncrementalResult` (per-date daily-index → CIK-scoped submissions → XML → parse → `write_records_to_parquet` → `compact_root`). Reuses `iter_form4_filings`, `write_records_to_parquet`, `parse_form4_xml`, `compact_root`. Returns counts (raw fetched, rows written, distinct accessions, transient/other errors, latest filing date) for metrics. Degrades gracefully per-date on `SecForbiddenError`/parse-miss. |
| `apps/alphalens-research/scripts/run_form4_daily_incremental.py` | runner_or_cli | argparse (`--lookback-days` default 3, `--asof-date` default today-UTC, `--parquet-root`, `--user-agent` optional→client default), logging, calls the engine, emits metrics via `emit_domain_metrics` wrapped in try/except. Exits 0 on clean run OR on transient SEC degradation (next run retries). |
| `deploy/systemd/alphalens-form4-incremental.service` | systemd_service | `Type=oneshot`, `WorkingDirectory=%h/AlphaLens`, `EnvironmentFile=/etc/alphalens/env` (no leading dash — fail loud), `ExecStart=%h/AlphaLens/.venv/bin/python apps/alphalens-research/scripts/run_form4_daily_incremental.py --lookback-days 3`, `ExecStopPost=%h/AlphaLens/deploy/systemd/bin/alphalens-emit-job-metrics form4-incremental`, `StandardOutput/Error=journal`, `MemoryMax=1G`, `TasksMax=32`. |
| `deploy/systemd/alphalens-form4-incremental.timer` | systemd_timer | `OnCalendar=*-*-* 02:30:00 UTC` (staggered: 1h+ after AV 00:05, off the thematic HH:30 grid + the every-15-min edgar-detect window), `Persistent=true`, `[Install] WantedBy=timers.target`. |
| `deploy/monitoring/prometheus/rules/alphalens.yaml` | monitoring_rule | add `AlphalensJobStale{job="form4-incremental"} > 172800` (48h = 2× daily cadence) + `AlphalensJobMetricMissing{job="form4-incremental"}`, each with `unit: form4-incremental` + `route: telegram`; add an output-volume dead-man rule `AlphalensForm4IncrementalDark` on the rows-written gauge (`max_over_time(...[5d]) == 0`, warning, distinct alertname, NO `job=` label — same isolation contract as the EDGAR-dark / VIX rules) + its `absent()` companion. |
| `apps/alphalens-research/tests/test_form4_incremental.py` | test | engine TDD: index parse (Form-4/4-A kept, 8-K/other dropped), window iteration date math, accession-set intersection, overlap dedup-safety (two overlapping windows → compacted store has unique accessions), graceful 403 degradation (no raise, counted). |
| `apps/alphalens-research/tests/test_form4_incremental_systemd.py` | test | unit-file lint: oneshot + WorkingDirectory, fail-loud EnvironmentFile, ExecStart script + `--lookback-days`, ExecStopPost emit-hook with job `form4-incremental`, no doubled token; timer `OnCalendar=...02:30:00 UTC` + `Persistent=true` + `[Install]`. |
| `apps/alphalens-research/tests/test_monitoring_alerts.py` | test | extend `ACTIVE_JOBS` with `"form4-incremental"` + its staleness threshold `172800` in the expected dict; add a `TestForm4IncrementalDark` suite mirroring `TestEdgarPressReleaseDark` for the new dead-man rule. |
| `deploy/systemd/README.md` | doc | new operator section (install, first-run catch-up recipe, inspect commands, output path `~/.alphalens/form4_parquet/`); add the unit row to the VPS-backfills catalogue. |

`apps/alphalens-research/tests/test_prometheus_rule_unit_parity.py` and
`test_deploy_systemd_units.py` are **not edited** — they auto-discover the new unit + rule via glob and
satisfy themselves once the unit wires the emit-hook and the YAML carries the paired staleness rule. They
are the parity gates that MUST stay green (§8).

---

## 6. TDD test plan (red conditions)

Engine — `apps/alphalens-research/tests/test_form4_incremental.py`:

- `test_parse_form4_index_rows_keeps_only_form4_and_amendments` — RED: `parse_form4_index_rows` does not
  exist / returns 8-K rows or drops `4/A`.
- `test_fetch_window_intersects_daily_index_accessions_with_submissions` — RED: a CIK that filed an old 4
  last year + a new 4 today writes BOTH (no accession intersection) — over-fetch.
- `test_overlapping_windows_dedup_to_unique_accessions` — RED: running two overlapping windows then
  `compact_root` leaves duplicate `accession_number` rows in `compacted.parquet`.
- `test_window_date_math_is_inclusive_utc` — RED: `[asof − lookback, asof]` off-by-one (misses `asof` or
  the floor day).
- `test_daily_index_403_is_counted_and_does_not_raise` — RED: a `SecForbiddenError` from one date's index
  fetch propagates out / aborts the whole window instead of skip-and-count.
- `test_result_reports_rows_written_and_latest_filing_date` — RED: `IncrementalResult` lacks the metric
  fields the runner needs.

Runner — covered indirectly by the engine tests + the systemd-unit lint (the script is a thin shell;
no separate runner unit test beyond a smoke that `--help` parses, optional).

Systemd — `apps/alphalens-research/tests/test_form4_incremental_systemd.py`:

- `test_service_is_oneshot_with_working_dir` — RED: missing `Type=oneshot` / `WorkingDirectory`.
- `test_service_loads_etc_alphalens_env_fail_loud` — RED: `EnvironmentFile` missing or has a leading `-`.
- `test_service_execstart_runs_incremental_script_with_lookback` — RED: ExecStart wrong script/flag.
- `test_service_wires_emit_hook_with_own_job_name` — RED: ExecStopPost missing / wrong job token.
- `test_timer_fires_daily_at_0230_utc_persistent` — RED: wrong `OnCalendar` / missing `Persistent=true`.
- `test_timer_carries_install_section` — RED: missing `[Install]`/`WantedBy=timers.target`.

Monitoring — extend `test_monitoring_alerts.py`:

- `ACTIVE_JOBS` gains `"form4-incremental"` → `test_every_active_job_has_a_staleness_rule` +
  `test_every_active_job_has_a_metric_missing_rule` go RED until the two job rules are added.
- `expected` threshold dict gains `"form4-incremental": 172800` →
  `test_staleness_thresholds_match_expected_cadence` RED until the rule threshold matches.
- new `TestForm4IncrementalDark` → RED until `AlphalensForm4IncrementalDark` + its `absent()` companion
  exist with the gauge-correct `max_over_time(...[5d]) == 0`, `route: telegram`, `severity: warning`,
  no `job=` label.

Parity gates (no edit, must stay GREEN): `test_prometheus_rule_unit_parity.py` (forward: the new
staleness rule's `job=form4-incremental` is emitted by the unit; backward: the emitting unit has a rule)
and `test_deploy_systemd_units.py::TestJobMetricsHook` (the new service is NOT in `ACTIVE_SERVICES` there,
so add it to that tuple too — the hook-parity test iterates a hardcoded tuple; **edit `ACTIVE_SERVICES`
in `test_deploy_systemd_units.py` to include the new service** so the hook is enforced).

---

## 7. Dedup-safety, late filings, disk

- **Dedup:** `accession_number` global uniqueness + full-row `drop_duplicates` in `compact_partition`.
  Compaction runs after every incremental append (`compact_root` is a no-op on already-single-file
  partitions, cheap otherwise).
- **Late filings beyond the window:** a Form-4 filed >`lookback_days` after its transaction date (rare:
  amendments, typos) is missed by the fixed window. Accepted limitation — it lands in the parquet on its
  actual filing date when filed; a future quarterly deep-refresh (walk universe for the trailing N years)
  is the catch-all, explicitly OUT of scope here. Documented in the PR `## Known issues`.
- **Disk:** seed is ~37MB compacted; incremental adds tens of KB/day. No rolling-export needed for years.

---

## 8. Monitoring summary

- **Staleness rule (~48h):** `AlphalensJobStale{job="form4-incremental"} > 172800` + `for: 5m`, paired
  with `AlphalensJobMetricMissing{job="form4-incremental"}` (absent-guard). 48h = 2× daily cadence; one
  missed day is non-fatal (next run's overlapping window re-covers). The unit exits 0 even on a
  transient-403 night, so `last_success` refreshes nightly and staleness cleanly catches "the job stopped
  running" (same contract as `feedback-shadow-returns` / `av-earnings-backfill`).
- **Output-volume gauge:** the runner emits `alphalens_form4_rows_written` (rows added this run) +
  `alphalens_form4_distinct_accessions` (sanity: filings touched) + `alphalens_form4_latest_filing_date`
  (max filing date in the window, coverage signal). The dead-man rule `AlphalensForm4IncrementalDark`
  (`max_over_time(alphalens_form4_rows_written[5d]) == 0`, warning) catches "ran clean, wrote nothing for
  5 days" — the silent-success-noop class the exit-code check misses. 5d window tolerates the worst
  legitimate all-zero cluster (holiday + weekend with no new filings) by the same reasoning as the
  EDGAR-dark rule.
- **Parity tests that MUST pass:** `test_prometheus_rule_unit_parity.py` (both directions),
  `test_deploy_systemd_units.py::TestJobMetricsHook::test_every_active_service_wires_emit_hook`,
  `test_monitoring_alerts.py::{test_every_active_job_has_a_staleness_rule,
  test_every_active_job_has_a_metric_missing_rule, test_staleness_thresholds_match_expected_cadence,
  test_no_duplicate_alertname_job_combos, test_no_counter_functions_on_gauge_metrics}`.

---

## 9. Operator runbook (goes into deploy/systemd/README.md)

```bash
# Install (VPS, as jacoren)
cp deploy/systemd/alphalens-form4-incremental.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alphalens-form4-incremental.timer

# First run auto-catches-up the seed→today gap (window self-sizes — no manual
# --lookback-days). Trigger one fire now instead of waiting for 02:30:
systemctl --user start alphalens-form4-incremental.service
# then verify coverage reached today:
#   curl -s localhost:9100/metrics | grep alphalens_form4_latest_filing_date

# Inspect
journalctl --user -u alphalens-form4-incremental.service -f
systemctl --user list-timers alphalens-form4-incremental.timer

# Output: ~/.alphalens/form4_parquet/transaction_year=YYYY/compacted.parquet
```

---

## 10. Open risks

1. **Shared per-IP 10 req/s SEC bucket** — form4-incremental (02:30 UTC) + every-15-min edgar-detect +
   6×/day thematic. Mitigation: 02:30 stagger (off both grids); `SecEdgarClient` global throttle + 403
   backoff; per-date graceful degrade. The first-run auto catch-up (bounded by `--max-catchup-days`) is
   the only heavy burst, and 02:30 UTC keeps it off-peak.
2. **`submissions/recent` 1000-cap edge** — a CIK that files >1000 filings in the lookback window would
   push a same-week 4 out of `recent`. Implausible at 3-day (even catch-up) windows; if it ever bites, the
   overlapping next-day window + the future deep-refresh catch it. Not handled inline (no overflow walk).
3. **Late filings beyond the window** (§7) — accepted; future quarterly deep-refresh.
4. **Daily-index format drift** — the engine reuses the proven `.idx` fixed-width parse shape from
   `edgar_press_release.py`; a format change degrades to "0 rows + transient" not a crash. An opt-in live
   smoke (mirroring the L4 `SEC_LIVE_TEST` probes) is a reasonable follow-up, NOT in this PR.
5. **First run / missed runs self-heal** — the window auto-extends to the store's newest filing, so there
   is no manual catch-up to skip and no fixed-size catch-up that breaks if the deploy slips. A gap can only
   persist past `--max-catchup-days` (400); the `alphalens_form4_latest_filing_date` gauge surfaces any
   hole (it would sit far behind `time()`).
