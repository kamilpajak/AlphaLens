# /edge enrichment completion-stamp + chart freshness signal — design

**Status:** LOCKED (approved 2026-07-28)
**Date:** 2026-07-28
**Author:** session-driven design (brainstorming → writing-plans → subagent-driven-development)
**Related:** PR #847 (benchmark pending-vs-na), PR #904 / #911 / #912 (chart staleness / starvation / reuse-first context band), ADR 0011 (split pipeline/research), migration B (Django mirror topology)

---

## 1. Problem

`/edge` reads Postgres `edge_ladderoutcome`, a **mirror** rebuilt from the per-brief-date
population-ladder parquets (`~/.alphalens/population_ladders/<brief_date>.parquet`) by the
Django management command `rebuild_ladder_outcomes_cache`
(`apps/alphalens-django/edge/ingest/parquet.py`). The mirror is gated **per brief date on raw
parquet `st_mtime`** vs the stored `DayMetaLadderOutcome.parquet_mtime`.

The nightly job `alphalens feedback backfill-shadow-returns`
(`alphalens-feedback-shadow-returns.service`, ~06:30 UTC) rewrites each parquet in **several
independent passes**, each doing its own atomic rewrite:

```
replay (maturation) → benchmark → sector → size → chart   (chart is LAST)
```

Every pass bumps the file `st_mtime`. Two mirrors read the store: the nightly's own
`OnSuccess` mirror (fires after the command exits) **and** the decoupled hourly
`alphalens-edge-mirror.timer` (`:05`). The hourly mirror can sample the store **mid-run**,
after `terminal` / `forward_return` / `market_excess_return` are already fresh but before a
later pass (e.g. `chart`) has rewritten its column. The mtime-only gate has no cross-column
awareness, so Postgres ends up holding a **half-updated row**: a fresh row-level maturation
paired with a stale or absent enriched column.

### 1.1 Two confirmed symptoms

1. **Benchmark `-` (the reported symptom).** A closed `TP_FULL` position showed `-` for
   EXCESS RETURN. The value **was** computed and present in the parquet
   (BIO 2026-07-17 `market_excess_return = +5.06%`), but the 09:05 hourly mirror had ingested
   the post-maturation-but-pre-benchmark state (stored `parquet_mtime` four minutes older than
   the file). It self-heals at nightly completion (the `OnSuccess` mirror re-ingests the final
   state — verified: BIO healed to `+5.06 / +5.72 / +5.72 / +9.70%` at 09:36 CEST). But during
   the ~1h nightly window the `-` is **indistinguishable from a bug or a real zero**.

2. **Chart stale-render (worse, not yet observed live but structurally present).** The chart
   column `chart_payload_json` shares the **identical** mirror/ingest/multi-pass machinery, and
   the race window is **wider** because `chart` is the last pass and still does live Polygon
   fetches for the context band. When the row's `terminal=true` is fresh but the chart payload
   is stale, the frontend derives `lifecycle='CLOSED'` off `payload.terminal`, paints the
   in-trade band to the last bar, and shows a `closed · {realized_r}R` chip — **with no exit
   marker on the tape and no signal that anything is missing**. A stale closed chart is
   visually indistinguishable from a complete one. (This is the FTRE stale-marker family from
   PR #904; the `_is_frozen_terminal_ok` docstring records the 2026-07-13 blackout incident.)

### 1.2 The trust problem (the real driver)

Incomplete data on `/edge` erodes trust in **all** the data. A user cannot tell "bug" from
"real zero" from "still computing", and there is no signal that a gap will self-heal. The
benchmark `-` is at least *honestly ugly* — it reads as incomplete. The chart is worse: it
*lies pretty* — it renders a complete-looking closed trade. The payload **already carries** a
completeness signal (`context: "OK" | "reused" | "in_trade_only"`, plus a `status` field) but
it is discarded three layers deep: the Django chart view never reads it, the serializer has no
field for it, and the SPA has no freshness state at all.

### 1.3 Current live state

Clean post-nightly: 303/303 July rows `status="OK"`, 0 terminal rows without a usable chart,
parquet ↔ Postgres in sync. **This is not an active incident — it is trust-correctness
hardening.** The exposure is the daily ~1h nightly race window (and the chart's more deceptive
failure texture when it does occur).

---

## 2. Goal

Make `/edge` **never serve a half-updated row** (row-level state and every enriched column
always come from the same completed run), and make the **genuine** residual incomplete states
(starved context band, missing minute-bar cache) **unmistakable** to the user. One shared
data-layer fix protects every enriched column at once (benchmark, sector, size, chart); the UI
signal is chart-specific; a table-level banner gives a global trust signal.

---

## 3. Non-goals / out of scope

- **Benchmark newest-first deadline starvation of the old tail** (~110 older-date rows that
  may go nights genuinely un-benchmarked). Same pattern as the chart starvation fixed in
  #904/#912, but a separate follow-up. The completion-stamp does **not** touch it. The existing
  #847 pending-vs-na affordance keeps communicating those honestly.
- **Chart marker-core starvation** — already fixed by #904/#912 (Polygon-free marker core,
  rebuilt every run, oldest-matured-first, anti-downgrade guard). Do not re-solve.
- Changing *what* the benchmark or chart computes. This design only changes *when the mirror
  trusts a parquet* and *how the UI communicates completeness*.

---

## 4. Architecture — three components

### Component 1 — completion-stamp (settled-watermark) · shared race fix

**Invariant to establish:** the mirror only ingests a brief-date parquet whose contents are
**settled** — i.e. written by a *completed* nightly run, never mid-run.

**Sentinel.** The `backfill-shadow-returns` command, as the **final step after all passes
succeed**, writes a JSON sentinel:

```
~/.alphalens/population_ladders/.ingest_watermark.json
{"completed_at": <float epoch seconds>}
```

- `completed_at` is captured with `time.time()` **after** the last pass (chart) returns, so it
  is strictly greater than every parquet's `st_mtime` written during the run.
- Written to the same directory the ingest already reads, which is bind-mounted **read-only**
  into the Django container — Django reads it with no `alphalens_pipeline` import (ADR 0011
  dependency direction preserved).
- Explicit epoch in the **content** (not the file's own mtime) so a backup/rsync/Nextcloud
  pass that rewrites file mtimes cannot corrupt the watermark.
- File name is **not** `*.parquet`, so `_scan_parquets` (globs `*.parquet`) ignores it
  automatically. Add an explicit guard/comment anyway.

**Ingest gate change** (`edge/ingest/parquet.py`, `rebuild_from_parquet`):

```
read watermark = completed_at from sentinel   (None if sentinel missing/unparseable)

for each date d with a parquet:
    mtime = parquet_path.stat().st_mtime
    if watermark is not None and mtime > watermark:
        # written by an in-progress (or failed) run — not settled yet
        skip (record as "unsettled", distinct from the mtime-equal "skipped")
        continue
    if not force and abs(stored_mtime[d] - mtime) < _MTIME_EPS:
        skip                      # unchanged since last ingest (existing behaviour)
        continue
    rebuild d
```

- **Bootstrap / graceful fallback:** sentinel missing or unparseable → `watermark = None` →
  behaves exactly as today (mtime-gated, no settled filter). Prevents a fresh deploy — or a
  window where the pipeline has not yet run the new code — from freezing `/edge`. Once a
  new-code nightly writes the first sentinel, the protection engages. This is a bootstrap
  affordance, not a backward-compat shim (there is no old on-disk format to support).
- **Deletion path unchanged:** dates whose parquet vanished are still dropped.

**Why this eliminates the race (per state):**
- *Mid-run:* passes rewrite parquets → their `st_mtime` > previous `completed_at` → **skipped
  as unsettled** → mirror keeps serving the last complete state. No half-state can be ingested.
- *Post-run:* command writes new `completed_at` > all parquet mtimes → every changed date
  becomes eligible → next mirror (hourly or `OnSuccess`) ingests the **complete** state.
- *Run fails mid-way:* sentinel is **not** advanced (it is the on-success final step) →
  half-rewritten parquets stay unsettled → `/edge` holds the last complete state. Correct.

**Deliberate behaviour change (→ §7):** a run that is **process-killed** (SIGTERM at
`TimeoutStartSec`, OOM) part-way will **not** surface its partial work — the half-rewritten
parquets stay `> watermark` and are skipped until a later completed run — because a matured row
paired with a half-written chart is exactly the deceptive half-state being removed. A run that
*completes* (even with an internally-failed pass) still advances the watermark; its columns are
honestly degraded per the guards above. Kills are monitored (`AlphalensJobStale`@48h).

**Write point:** end of `_refresh_population_ladders`
(`apps/alphalens-pipeline/alphalens_cli/commands/feedback.py`, after the chart pass at line
136). Written **atomically** (tmp file + `os.replace`) so a mirror can never read a torn
sentinel. A dedicated small helper `_write_ingest_watermark(store_dir)` keeps it testable.

**What "the run reached the end" actually means (corrected after adversarial review).** Every
pass in `_refresh_population_ladders` is swallow-all — the `replay` call is inside a
try/except that logs and continues, and each `_enrich_*` helper "never raises". So **nothing
propagates**: the watermark advances on **every run that is not process-killed**. The only
case that skips the write is a hard kill — SIGTERM at `TimeoutStartSec=90min`, OOM, power loss
— which is exactly the case where the parquets are half-rewritten and must not be trusted
(their `st_mtime > previous watermark` → skipped). This is **process-kill / timeout
protection**, not "only stamps on a fully-successful run".

**Why gating only on process-completion is still correct.** The completion-stamp solves the
*mirror-timing* race (the mirror reading a parquet mid-rewrite). It is deliberately **not** the
mechanism for a pass that runs but fails to compute a value — that is handled per column by the
producers: the benchmark pass's `_carry_forward_prev_pair` degrades a fetch miss to `NULL` →
the frontend shows the honest #847 *pending* state; the chart pass has an anti-downgrade guard
(`ladder_chart.py`) that keeps the last-good `status="OK"` payload rather than blanking it on a
transient failure. So when a run completes with an internally-failed pass, the watermark
advances and the mirror ingests a row whose that-column value is already *honestly degraded*.
The two mechanisms are orthogonal: the watermark guarantees **no mid-rewrite / no killed-run
partial is ever read**; the per-column guards guarantee **each column is either current or
honestly marked incomplete**. (The one residual where a *stale non-null* value can survive —
cheap NO_FILL maturation under benchmark starvation — is documented in §7; it shares its
mechanism with the out-of-scope old-tail starvation.)

### Component 2 — chart freshness signal · UI (chart-specific)

With the race fixed, the mirror never serves a stale-vs-fresh mismatch. What remains are
**genuine** partial states the payload already describes:

- `context = "reused"` / `"in_trade_only"` — the context band was deadline-starved; the trade
  itself is fully rendered, only the surrounding lead-in/trailing daily band is partial.
- `status = "NO_DATA"` — a fresh terminal whose minute-bar cache was never populated → no
  chart yet (today renders as an empty/broken box).
- 4 legacy pre-#904 rows missing the `context` key entirely → default to `OK` (harmless).

**Wire the existing signal through the three layers it is currently dropped at:**

1. `apps/alphalens-django/edge/api/chart.py` — read `payload.get("context")` and include it in
   the chart response. (`status` **already flows** end-to-end — serializer, view body, and the
   SPA `ChartPayload.status` all carry it today; only `context` is missing.)
2. `apps/alphalens-django/edge/api/serializers.py` (`ChartResponseSerializer`) — add the
   nullable `context` field only.
3. `apps/web/src/lib/types.ts` — add `context?: …` to the chart payload type (`status` is
   already typed).

**SPA affordance** (in the chart component under `apps/web/src/routes/edge/` /
`LadderChart.svelte`):

- `context = "OK"` (or absent legacy) → **nothing** (complete, trustworthy — the default).
- `context ∈ {"reused","in_trade_only"}` → a **subtle** marker: "context band incomplete —
  recomputes nightly". The trade is fully shown; this only qualifies the surrounding band. Not
  alarming.
- `status = "NO_DATA"` (or payload absent for a terminal row) → an honest **"chart computing —
  not available yet"** state instead of an empty/broken box.

The discriminator is `context`/`status`, **not** marker-counting: `NO_FILL` and `TIME_STOP`
terminals legitimately have no exit arrow, so "no exit marker" is not a reliable
incompleteness tell. This mirrors the #847 benchmark pending-vs-na pattern for the chart.

**Storybook:** per repo doctrine, every changed `apps/web` component ships/updates a
`.stories.svelte` covering the new states (`OK` / `reused` / `in_trade_only` / `NO_DATA`),
bound to real fixtures.

### Component 3 — /edge completeness banner (table-level trust signal)

A single header line on `/edge` giving a global trust signal, e.g.:

> "enriched data: N / M complete · last computed <relative time>"

- **Counted over TERMINAL rows only** (LOCKED 2026-07-28): open positions have no realized
  result yet, so including them would understate N/M and mislead.
- **Sourced from the SUMMARY payload, not the outcomes list** (corrected after adversarial
  review): `M = summary.n_terminal`, `N = summary.n_matured` (terminal rows with a finite
  `market_excess_return` — benchmark coverage, the metric behind the visible `-`). Both are
  already computed server-side over the whole window (`plannable AND terminal`) and already
  fetched by the banner's sibling summary call. **Do NOT derive N/M from the `/edge` outcomes
  rows** — that list is paginated (`_OUTCOMES_LIMIT=500`, newest-first = least-enriched),
  filtered by the terminal/ongoing tab, and narrowed by toolbar filters, so a count off it
  would swing with an unrelated tab toggle (ongoing tab → `M=0`) and read *worse* than truth
  once terminals exceed the cap — a trust banner that lies is worse than the `-` it replaces.
- Chart completeness is surfaced **per row** by Component 2, not folded into this count
  (`chart_payload_json` is a heavy per-row blob; the banner tracks the visible excess dashes).
- "last computed" = the `completed_at` watermark surfaced through a small API field (the
  watermark already exists from Component 1 — cheap to expose).
- Purpose: a single dash in one row no longer reads as "the system is broken" — the user sees
  "data is N/M complete, last refreshed X ago", which frames any residual gap as a known,
  bounded, self-healing state.

**API:** expose `completed_at` (and optionally the N/M counts, or compute N/M client-side from
the already-fetched rows) via the edge summary/outcomes endpoint. Prefer computing N/M
client-side from rows already in hand to avoid a new server aggregation; the server only needs
to surface `completed_at`.

---

## 5. Consistency invariant (the property tests must assert)

> No `/edge` row ever reflects a **mid-rewrite** parquet or a **process-killed** run's partial
> output. Every row Postgres serves comes from a parquet that a completed run left settled
> (`st_mtime ≤ completed_at`). Each enriched column on that row is then either the value that
> run's pass computed, or a value that pass's own guard honestly degraded (benchmark → `NULL` /
> #847 pending; chart → last-good `status`).

This is the accurate, achievable claim (corrected after adversarial review — the earlier "all
columns from one run" wording overclaimed). What the watermark does **not** promise: that a
deadline-starved pass ran on every date this run (see §7 — the old-tail residual). The property
tests assert **this** invariant (no mid-rewrite / no killed-run partial read), not the stronger
one, so they cannot be quietly written to dodge the starvation path.

---

## 6. Test strategy

**Component 1 — ingest gate (`apps/alphalens-django/edge/tests/test_ingest.py`):**
- watermark present, `parquet_mtime ≤ watermark`, changed vs stored → **ingested**.
- watermark present, `parquet_mtime > watermark` (simulated mid-run write) → **skipped
  (unsettled)**, DB retains prior row values (assert the old benchmark/chart survive).
- watermark advances (second run) → previously-unsettled date now ingests the complete row.
- sentinel missing → fallback to pure mtime gate (existing behaviour unchanged — regression).
- sentinel present but unparseable/malformed → same safe fallback, logged.
- `RebuildResult` distinguishes `unsettled` from mtime-`skipped` (new field or counter) so the
  command output and monitoring can tell "waiting for run to finish" from "nothing changed".

**Component 1 — pipeline sentinel (`apps/alphalens-research/tests/…` per test layout for the
CLI/feedback seam):**
- `_write_ingest_watermark` writes valid JSON with `completed_at` > all parquet mtimes in the
  store.
- a successful `_refresh_population_ladders` leaves a sentinel; a degraded run (replay or an
  enrich pass raising) still stamps the watermark, because every pass swallows its own errors
  internally (§7) — nothing propagates out of `_refresh_population_ladders` for a test to catch.
  The implemented test therefore pins the achievable invariant: a completed-but-degraded run
  still advances the watermark. Process-kill protection (the previous sentinel staying intact) is
  a property of construction — the write happens only after the last pass returns, so a killed
  process never reaches it — not something a unit test can simulate.

**Component 2 — Django chart API (`edge/tests/test_api.py` / chart test):**
- `context` + `status` from the payload appear in the serialized chart response; absent keys →
  null, not a KeyError.

**Component 2 — SPA:** `.stories.svelte` states + a `pnpm run check` / `build-storybook` gate;
Playwright/unit coverage for the render branch selecting the right affordance per
`context`/`status`.

**Component 3:** banner renders N/M + relative "last computed" from a fixture; the `completed_at`
field is present in the API fixture.

**Regression:** full research suite (`unittest discover`), Django tests, `apps/web`
`pnpm run check` + `build-storybook`.

---

## 7. Behaviour notes / known issues (for the PR body)

- **Killed-run visibility trade-off (intended):** a nightly that is **process-killed**
  (SIGTERM at `TimeoutStartSec`, OOM) part-way will not surface its partial work until a later
  completed run. This is the price of the no-half-state guarantee and is desired. Monitored via
  `AlphalensJobStale`. (A run that *completes* with an internally-failed pass still advances the
  watermark — its columns are honestly degraded, not withheld.)
- **Cheap NO_FILL maturation can carry a stale excess under benchmark starvation (residual,
  same mechanism as old-tail starvation):** the monitor's `_cheap_update_row` matures a
  `NO_FILL` row by copying the prior dict without recomputing `market_excess_return`. In a
  healthy run the benchmark pass recomputes it later the same run (the `_carry_forward_prev_pair`
  consistency check forces it on maturation); it only survives if the benchmark pass is
  deadline-starved and never reaches that (old) date. So a starved old date can show a
  now-terminal NO_FILL row with a stale non-null excess. This is a facet of the out-of-scope
  old-tail benchmark starvation, not a new hole the watermark introduces — folded into the same
  follow-up. The watermark deliberately does not claim to fix it (§5).
- **Clock / filesystem corners (low risk):** the strict `mtime > watermark` comparator is
  correct on nanosecond-mtime filesystems (ext4/apfs) under NTP slew. A coarse-second-mtime FS
  or a backward NTP *step* during a run could false-flag a settled parquet as unsettled → `/edge`
  lags one mirror cycle (fails safe, self-heals next run). Assumption stated here rather than
  engineered around.
- **`~/.alphalens` cache restore needs a nightly (or a watermark bump) before `/edge`
  re-ingests:** an `rsync` without `-t` / untar restore stamps parquets "now" (`> watermark`)
  → all marked unsettled → `/edge` holds the last-ingested state until a fresh completed run
  advances the watermark. Fails safe; note for operators doing a cache restore.
- **Bootstrap window:** until the pipeline side ships the sentinel writer, the ingest falls
  back to today's mtime gate (no regression, but also no protection). Deploy order therefore
  matters: pipeline (writer) then Django (reader) is safest, but either order is safe because
  the reader degrades gracefully when the sentinel is absent.
- **Manual `feedback backfill-shadow-returns` runs** also write the sentinel on success (the
  writer lives in the command, not in systemd), so ad-hoc remediation runs keep `/edge`
  consistent.
- **Old-tail benchmark starvation** remains (§3) — surfaced honestly by #847 pending-vs-na,
  fixed later.

## 8. Deploy

Standard split: Django image via CI (`ghcr.io/kamilpajak/alphalens-django`, pulled on VPS);
pipeline change picked up by the host venv `git pull` **and** the VPS-local
`alphalens-pipeline:latest` docker rebuild (the nightly runs the docker image). Deploy order:
pipeline first (starts writing the sentinel), then Django (starts honouring it). Zen
pre-merge codereview mandatory (mixed pipeline + Django + web → one combined pass with
`deepseek/deepseek-v4-pro`, `thinking_mode="high"`).

## 9. File-touch map

| Layer | File | Change |
|---|---|---|
| pipeline | `apps/alphalens-pipeline/alphalens_cli/commands/feedback.py` | write sentinel at end of `_refresh_population_ladders`; `_write_ingest_watermark` helper |
| Django ingest | `apps/alphalens-django/edge/ingest/parquet.py` | read watermark; settled-gate (bypassed by `force`); `RebuildResult.unsettled_dates` |
| Django command | `apps/alphalens-django/edge/management/commands/rebuild_ladder_outcomes_cache.py` | add `unsettled=` to the stdout summary (operator/journal visibility) |
| Django API | `apps/alphalens-django/edge/api/chart.py` | pass `context` + `status` through |
| Django API | `apps/alphalens-django/edge/api/serializers.py` | nullable `context` + `status` on `ChartResponseSerializer`; surface `completed_at` on summary/outcomes |
| Django API | `apps/alphalens-django/edge/api/summary.py` (or view) | expose `completed_at` watermark |
| web types | `apps/web/src/lib/types.ts` | `context?` + `status?` on chart type; `completed_at?` on summary type |
| web UI | `apps/web/src/routes/edge/**` + `LadderChart.svelte` | chart freshness affordance; completeness banner |
| web stories | `*.stories.svelte` next to changed components | new states |
| tests | `edge/tests/test_ingest.py`, `edge/tests/test_api.py`, research CLI test, web stories/tests | per §6 |
| docs | this memo | status LOCKED |
