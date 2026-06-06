# P2 — per-exchange trading-session VIEW over the bitemporal news lake

**Status:** DRAFT — adversarial review FOUND A FOUNDATIONAL FLAW (below). P2 build is GATED. The first corrective step — the **lake-raw substrate fix** — shipped separately; the rest of P2 (session window, snapshot-as-you-go, Postgres PK migration, late-arrival watermarking) remains unbuilt pending a fresh design pass.
**Date:** 2026-06-05
**Follows:** [`multi_exchange_news_lake_design_2026_06_05.md`](multi_exchange_news_lake_design_2026_06_05.md) (P1 lake foundation; §3.0 gap-free tiling, the 6 corrective requirements, the P2/P3 sketch)
**Scope:** Phase 2 of that memo — the per-exchange session VIEW. **Assumes P1 (the bitemporal UTC lake) lands first.** Where P1 is not yet in place, this memo marks the dependency.

---

## Adversarial review outcome + resolution (2026-06-05)

A 4-lens adversarial review (no-op/regression, replay-determinism, gap-free-tiling, completeness) found the design below **flawed (high)** on one root cause, plus secondary gaps. The window/tiling math (§1) and the earnings guard (§5) were confirmed **sound**; the body below is kept as the design exploration but is **superseded in part** by this header.

**Root flaw — the lake stored the wrong thing.** P1c (as first shipped, #460) wrote the **deduped + strict-single-UTC-day-filtered + 200-capped** `merged` frame to the lake — a faithful audit copy of *what the brief served*, but a **lossy substrate**. P2's flagship session window `[20:30Z(D−1), 20:30Z(D))` straddles UTC midnight and needs exactly the rows the per-UTC-day cap/filter already dropped (the prior evening's after-hours + overnight catalysts). So the as-of query would collapse over a hole. Consequences the review proved: P2a was **not** the claimed no-op (union over 6 capped runs ≠ one 200-cap run, and the read path had no cap step → could blow the 200-item LLM budget); the §2.3 "bit-identical replay" claim is false for any pre-P2 brief.

**Resolution (this is what aligns the lake with the P1-memo Layer-1 principle "ingest makes no session/cap decision").** The lake now stores the **RAW per-source union** — pre-dedup, pre-day-filter, pre-cap — stamped with `ingested_at`. The current-view `{D}.parquet` stays exactly the deduped+capped frame the brief consumes (byte-identical; golden fixtures unchanged). Shipped as the **lake-raw correction** (this branch). After it, the lake is a true substrate the P2 session VIEW can window + cap at read time.

**Still unresolved before any P2 build (next design pass must address):**
1. **Late-arrival watermarking** (P1 corrective req #5) — a news item valid-in-window but ingested *after* the cut is dropped at the valid-time/txn-time seam. Needs an allowed-lateness / re-admission rule.
2. **Postgres PK migration is NOT additive** — `Brief`/`DayMeta` `(date,ticker)` → `(exchange,session_date,ticker)` is a drop-and-recreate on populated Postgres (the #331/#340/#372 skew class), touching ~15 date-keyed read sites incl. `/v1/brief/<date>`, the feedback POST handler, and `edge/ingest`.
3. **No historical lake** — as-of replay only works P2-era-forward; the lake began 2026-06-05.
4. **Per-window raw-news dedup** — the same story ingested across two runs/partitions can survive with distinct `id`s; the read VIEW must re-dedup.

Only XNYS is live, so the multi-exchange payoff is genuinely future. The lake-raw fix is the standalone, low-risk win banked now; the rest of P2 is sequenced behind a fresh design pass.

---

## 0. TL;DR

Today the brief's news set is the single calendar-UTC-day file `~/.alphalens/thematic_news/{D}.parquet` (a current-view overwrite). P2 makes it the news whose **valid-time** falls in **exchange X's trading-session window** for `session_date D`, read from the **immutable lake as-of the brief-generation time**, deduped to the latest transaction-time per `id`.

The keystone already exists: `alphalens_pipeline/paper/calendar.py` is MIC-keyed (`exchange_calendars`, default `"XNYS"`). P2 adds **one new pure module** (`session_window.py`) that turns `(MIC, cut_time)` into a gap-free `[prev_cut, this_cut)` window + `session_date` label, **one new lake-read module** (`lake_view.py`) that does the bitemporal as-of query, and rewires the five thematic stages to consume that frame instead of `{D}.parquet`. The frame **shape** (`NEWS_COLUMNS`) is unchanged — only the window semantics shift, so downstream extract/map/score/brief need no code change to *read* it.

**Recommended first slice (P2a): land the MIC structure + the lake-read path as a near-no-op for XNYS, NOT the content-changing window yet.** Quantification in §6 shows the XNYS trading session window and the calendar-UTC-day window overlap heavily but **not** identically (the overnight + pre-market block is the difference), so the content change is a real correctness fix that reviewers must sign off — sequence it as P2b after the plumbing is proven inert.

---

## 1. SESSION WINDOW / gap-free tiling (LOCKED)

### 1.1 The tiling rule

A **cut** is a brief-generation instant in absolute UTC. Production fires the build 6×/day at `HH:30 UTC` for `HH ∈ {00,04,08,12,16,20}` (CLAUDE.md VPS-backfills `alphalens-thematic-build`; `RandomizedDelaySec=5min`). Each run is one cut.

For a given `(MIC, cut_time)`:

- **`this_cut`** = the run's generation instant, in UTC. To make it replay-stable and jitter-immune, P2 **quantizes** `this_cut` to its scheduled slot `HH:30:00Z` (the nominal cut), NOT the wall-clock `systemd` fire time (which carries up to 5 min jitter). Stored as `cut_utc`.
- **`prev_cut`** = the immediately preceding scheduled slot, computed **from the cadence**, not read back from the previous run's wall clock. For `00:30Z` the previous slot is `20:30Z` of the prior UTC day. This is deterministic and needs no cross-run state. (Resolves discovery open-question "how should prev_cut be stored/tracked" → **computed from the fixed cadence**, not persisted; see §1.4.)

The window admitted to a brief is the half-open interval on **valid-time**:

> **`[prev_cut, this_cut)` — half-open, contiguous, gap-free, non-overlapping.**

Consecutive cuts tile the whole timeline: `... [12:30, 16:30) [16:30, 20:30) [20:30, 00:30+1) ...`. Every news item with a valid-time anywhere on the clock lands in exactly **one** brief's window. No orphaned intraday news (the P1 fix to the rejected `[prev_close, next_open)` overnight slice), no double-count.

### 1.2 The session is the LABEL, not the filter

The MIC calendar does **not** filter which items are admitted — it **names** the window. From `calendar.py`:

- `previous_trading_day(d, exchange)` (line 195) → the prior session date (anchors `prev_session_close`).
- `session_open_utc(d, exchange)` (line 257) → the session's opening auction in UTC.
- `session_close` is read off the calendar directly (`exchange_calendars` `cal.session_close(ts)`; `calendar.py:139` already calls it inside `is_half_day`). **P2 adds a thin `session_close_utc(d, exchange)` helper** mirroring `session_open_utc` (the discovery notes "No explicit `prev_session_close` or `this_session_close` helpers in public API; P2 must compute if needed" — `calInfra` finding "Session boundary semantics").

For each `(MIC, session_date)` we record, alongside the gap-free admission window, the **session anchor pair** `(session_open_utc, session_close_utc)` for that session date. The anchor pair is what makes the brief "session-aligned" (it tells the SPA "this is the brief for XNYS session D, whose close was 20:00Z / 21:00Z"); the `[prev_cut, this_cut)` interval is what actually admits news.

**Which cut is the flagship "morning brief"** for a market = the first cut at-or-after that market's session close. For XNYS the close is `20:00Z` (EDT) / `21:00Z` (EST), so the flagship is the **`20:30Z`** run (summer) — the one that answers "what happened since the last close, ahead of tomorrow's open". The other 5 cuts are intraday refreshes that still tile the window so nothing is orphaned.

### 1.3 DST / holiday / half-day / intraday-break handling

- **DST** — `exchange_calendars` carries historical tz rules, so `session_open_utc` / `session_close_utc` return the correct UTC instant per date (EDT `13:30/20:00Z` vs EST `14:30/21:00Z`). The `[prev_cut, this_cut)` admission window is in **absolute UTC** and is DST-agnostic by construction; only the *anchor labels* shift with DST. (P1 adversarial-review note: the calendar must be the one in force at date D — `exchange_calendars` historical schedules cover this.)
- **Holiday / weekend** — `session_date` is resolved with `session_on_or_after` / `previous_trading_day`, so a Saturday cut labels against the prior Friday session (the flagship morning brief for Monday's open is the Friday-close → Monday cut). News on a non-session day is still admitted by some cut's `[prev_cut, this_cut)` window — **it is never dropped**, it just rolls into the next session's brief. This is the whole point of tiling: the calendar gaps (weekends) do not create news gaps.
- **Half-day** — `is_half_day(d, exchange)` (line 118) already detects early closes; `session_close_utc` returns the actual (early) close (`18:00Z`/`19:00Z` for XNYS 13:00 ET). The flagship-cut selection (§1.2) automatically picks the first cut after the early close. Worked example below.
- **Intraday break** (Tokyo/HK lunch) — **irrelevant to admission** because the window is cut-to-cut on absolute UTC, not session-segment-bounded. It matters only for *labelling* split-session markets in P3; `exchange_calendars` exposes breaks per venue. P2 (XNYS, no lunch break) does not touch this — the seam is the per-MIC anchor record.

### 1.4 Worked XNYS examples (actual UTC timestamps)

**Ordinary summer (EDT) session — Wed 2026-06-03, regular close 16:00 ET = 20:00Z.**
Cadence cuts (nominal `HH:30Z`): `00:30, 04:30, 08:30, 12:30, 16:30, 20:30`.
Admission windows for that UTC day (each `[prev_cut, this_cut)`):

| Cut (UTC) | Admission window (valid-time) | Role |
|---|---|---|
| `2026-06-03 00:30Z` | `[2026-06-02 20:30Z, 2026-06-03 00:30Z)` | overnight tail of prior session |
| `2026-06-03 04:30Z` | `[00:30Z, 04:30Z)` | overnight |
| `2026-06-03 08:30Z` | `[04:30Z, 08:30Z)` | Europe morning |
| `2026-06-03 12:30Z` | `[08:30Z, 12:30Z)` | pre-market US |
| `2026-06-03 16:30Z` | `[12:30Z, 16:30Z)` | US intraday (covers 13:30Z open) |
| `2026-06-03 20:30Z` | `[16:30Z, 20:30Z)` | **flagship** — covers 20:00Z close + after-hours start |

Session anchor for `(XNYS, 2026-06-03)`: `session_open_utc = 13:30Z`, `session_close_utc = 20:00Z`.

**Half-day (Black Friday) — Fri 2026-11-27, early close 13:00 ET = 18:00Z (EST).**
`is_half_day(2026-11-27, "XNYS")` → True; `session_close_utc = 18:00Z`. The flagship is the **first cut at-or-after 18:00Z = `20:30Z`** (the `16:30Z` cut window `[12:30Z,16:30Z)` ends before the close; the `20:30Z` cut window `[16:30Z, 20:30Z)` straddles the 18:00Z early close + after-hours). The next session is Monday 2026-11-30 (winter EST `14:30Z` open). Saturday/Sunday cuts admit weekend news into windows labelled against the Friday session until the Monday-open flagship — nothing orphaned across the long weekend.

---

## 2. LAKE AS-OF VIEW QUERY (LOCKED)

### 2.1 The bitemporal read

Inputs: `(MIC, session_date, as_of_txn_time)`. Window `[prev_cut, this_cut)` from §1.

```
read_session_view(MIC, session_date, as_of) -> NEWS_COLUMNS frame:
  1. resolve [prev_cut, this_cut) for (MIC, this_cut == cut implied by session_date+role)
  2. glob lake run files for the UTC dates the window spans:
       ~/.alphalens/thematic_news_lake/session_date=<D>/run=*.parquet
       (the window can straddle two UTC dates, e.g. [20:30Z(D-1), 00:30Z(D)) →
        read partitions D-1 AND D; physical partition is by UTC ingest date,
        NOT by the logical session — see §2.4)
  3. concat all runs; filter:
       ingested_at <= as_of          # transaction-time: "as known at as_of"
       prev_cut <= timestamp < this_cut   # valid-time in the gap-free window
  4. collapse duplicates: for each `id`, keep the row with the MAX ingested_at
       (the current view of that item as known at as_of)
  5. return NEWS_COLUMNS frame (shape identical to today's {D}.parquet)
```

### 2.2 Dedup key = `id`, NOT `url_canon`

The lake row identity for the as-of collapse is **`id`** (the source-stable unique id, `sources/schema.py:8`). This is the same key the extractor already dedups on (`event_extractor.py:394`, `~news['id'].isin(already)`), so the view and the extractor agree.

`url_canon` is **not** the as-of key: URL canonicalization happens once at ingest (`news_ingest.py` `_canonical_url`) and feeds the **Tier-1 lexical/URL clustering** that produces the dedup'd `id` set *inside* a run. By the time a row is in the lake it already carries its post-cluster `id`. The as-of collapse is purely "same `id`, newer transaction-time wins" — it reconstructs the current view, it does not re-cluster. (Cross-listing/global-event dedup is a *different* layer — §4 — and is P3.)

### 2.3 `as_of` default and PIT replay

- **Default `as_of` = brief-generation time** (`this_cut`, quantized per §1.1). A live run reads "everything ingested up to now".
- **PIT replay** — to reconstruct exactly what brief `(MIC, session_date)` knew, pass `as_of = ` that session's historical `cut_utc`. Because the lake is append-only and never mutated (`news_ingest.py:_write_lake_run`, atomic + never-clobber), `ingested_at <= as_of` deterministically excludes every row recorded after the cut, and the max-`ingested_at`-per-`id` collapse reproduces the exact current-view the live run saw. **This is the bit-identical-replay guarantee** and the reason the snapshot-as-you-go store (§3) is belt-and-braces, not the only path.

### 2.4 Physical partition vs logical session

The lake partitions physically by `session_date=<UTC-ingest-date>` (`news_ingest.py:297`). **This partition is a UTC-ingest-date placeholder, NOT the logical exchange session** — P2 does not re-partition the lake. The session view computes its own logical window and reads whatever UTC partitions the window spans (often two). Renaming the partition to `txn_date=` would be clearer but is a cosmetic P1-side change, **not required for P2** and explicitly out of scope here (no-backward-compat rules let it be renamed later without aliases).

---

## 3. SNAPSHOT-AS-YOU-GO (LOCKED)

### 3.1 Decision: extend the existing tables, add columns — do NOT fork a new table yet

The current store is rebuild-not-immutable: `rebuild_from_parquet` does atomic delete-then-bulk_create per date (`briefs/ingest/parquet.py:195-221`), `Brief` PK = `(date, ticker)`, `DayMeta` PK = `date` (`briefs/models.py:28-164`).

P2 adds, additively:

- **`Brief`**: new columns `exchange` (MIC str, default `"XNYS"`), `session_date` (date), `cut_utc` (datetime), `window_start_utc` / `window_end_utc` (datetime). PK migrates `(date, ticker)` → **`(exchange, session_date, ticker)`**. `date` (the old UTC-day stem) is **retained** as a plain column for the existing date-keyed API/SPA reads during the transition.
- **`DayMeta`**: PK migrates `date` → **`(exchange, session_date)`** (resolves discovery open-question "DayMeta per-exchange cardinality" → **becomes `(exchange, session_date)`-keyed**; one row per exchange-session, so the incremental rebuild gate `parquet_mtime` is now per-exchange-session).

**Why extend, not a separate `BriefSnapshot` table with `snapshot_ttl`:** a parallel immutable snapshot table doubles the read surface (SPA + `/v1/edge/*` ingest both already read `Brief`/`DayMeta`) and forces every consumer to learn "old vs latest". The bit-identical-replay guarantee already lives in the **lake** (§2.3); the Postgres store's job is "serve the latest materialized brief fast", which is exactly what the current rebuild does. So P2 keeps the store as the **serving** layer keyed `(exchange, session_date, ticker)` and leaves true immutable PIT replay to the lake. `snapshot_ttl` is rejected — append-only lake + deterministic as-of query needs no TTL. (Resolves the discovery open-question on `BriefSnapshot` table.)

### 3.2 Coexistence with `rebuild_briefs_cache`

`rebuild_from_parquet` still scans parquet, still uses `parquet_mtime` as the incremental gate, still does atomic delete-then-create — but **scoped to `(exchange, session_date)`** instead of `date`. The pipeline Phase E output parquet must therefore carry `exchange` + `session_date` columns (§ open-decision and the "session-date encoding" discovery question). Two sub-changes:

1. **Pipeline (Phase E orchestrator, `thematic.py:494-562`)** writes `exchange` + `session_date` + `cut_utc` + `window_start/end_utc` columns into `thematic_briefs/{stem}.parquet`. **Recovery path** for files lacking these columns (pre-P2 parquets): the ingest infers `exchange="XNYS"` + `session_date = date` (the file stem) — i.e. legacy files are treated as XNYS calendar-day briefs. This makes the migration a pure superset (no data loss, old files still ingest).
2. **Ingest (`parquet.py`)** reads those columns, falls back to the XNYS/stem recovery when absent, keys delete-then-create on `(exchange, session_date)`.

Distinguishing old snapshots from latest: there is no "old vs latest" — `rebuild` is still last-writer-wins per `(exchange, session_date)`, exactly as today per `date`. Immutable history is the lake's job, not Postgres's.

---

## 4. CROSS-LISTING / GLOBAL-EVENT DEDUP (SEAM NOW, BUILD IN P3)

### 4.1 Not observable with only XNYS live → P3

Per P1 §9 rejection #2 and the `dedupEarnings` discovery finding: the "same event inflates importance by appearing N times across N listings" failure is **latent, not observable, until ≥2 exchanges are live**. XNYS listings are US-primary; a Fed decision or NVDA print appears once per XNYS session today. The failure only manifests when XTKS/XHKG/XSHG/XWAR briefs render the *same* macro event in *their* sessions. **So P2 builds the seam; P3 fills it.**

### 4.2 The seam (designed now, reuses already-extracted structure)

Reuse, do not re-NLP. `event_extractor.py` already emits typed events with `(template_id, primary_entities, event_type, themes)` (`dedupEarnings` finding "Post-extraction typed structures"), and `dedup.py:238-313` (`dedup_template_events`) already clusters on **`(template_id, frozenset(primary_entities))`** with a 24h sliding window, picking the survivor by `template_fields_json` richness. **That is the global-event cluster primitive already in the tree.**

P2's seam: add a **`global_event_id`** column to the events parquet, computed as a **stable hash of `(event_type, frozenset(primary_entities), valid_date)`** for template/typed rows (Flash free-text rows get a per-row `global_event_id = id`, i.e. no clustering — they pass through, matching today's behaviour). This is a deterministic content hash (not a UUID — UUID breaks replay; resolves the discovery open-question "how should the global event cluster id be generated"). The column is **populated but unused** in P2 (single exchange → one attribution per cluster trivially). In P3, per-exchange attribution reads `global_event_id` and emits the cluster once per exchange session in that market's local session timing — no new NLP, just a group-by on the seam column.

**Strict provenance boundary** (the #394 line, already established): `global_event_id` groups events; it never writes one ticker's attributes onto another's card. Per-exchange attribution is "this global event, surfaced in this market's session", not "merge the cards".

---

## 5. EARNINGS-GUARD resolution under a session-defined window (LOCKED)

`fetch_next_earnings` (`earnings_calendar.py:57-100`) currently returns the next earnings date only if `asof` is within `_FRESHNESS_WINDOW` (7 days) of today, else `None`; **and** the legacy guard `if asof < today: return None` suppresses the field on **every** production run because `asof = T-1 < today` always (P1 §1.1; "structurally always None", verified across 12 briefs).

P2 resolution: the session window makes `asof` **explicit and freshness-defined**. Replace the `asof < today` guard with a **freshness check against the session window**: the field is admissible when `session_date` is within `_FRESHNESS_WINDOW` of "now" (the cut time), which is **true for every live run** (the flagship cut's `session_date` is yesterday/today, well within 7 days) and **correctly false for historical replay** (a 2020 replay's `session_date` is >7d from its cut → suppressed, no forward leak). This is exactly the P1 §5-Phase-2 promise ("freshness window replaces `asof < today`"). The `_FRESHNESS_WINDOW` value (7d) is unchanged; only the comparison anchor moves from `today` to the session/cut. **Resolves the discovery `asof` open-question for earnings → the freshness anchor is the cut time.**

For the **price-slicing `asof`** (PIT pricing in `score`/`brief`): P2 sets downstream `asof = session_close_utc` of `(MIC, session_date)` — the "as of this market's close" instant — NOT the bare UTC date and NOT the cut. Rationale: prices should be sliced at the session the brief is anchored to (the close the investor reasons "gap risk on the open" against), and it is replay-stable. (Resolves the discovery open-question "session END vs cut vs separate" → **session END (close) for pricing, cut time for the lake as-of and earnings freshness**. They are different knobs and must not be conflated.)

---

## 6. SLICE PLAN

### Critical analysis: is XNYS-session ≈ calendar-UTC-day?

**No — they overlap heavily but the difference is exactly the high-signal block.** The XNYS session is `13:30Z–20:00Z` (EDT) and the flagship brief window `[16:30Z, 20:30Z)` covers the close + after-hours. The **calendar-UTC-day** window `[00:00Z, 24:00Z)` and the **flagship session** window `[prev_close_cut, this_close_cut)` ≈ `[20:30Z(D-1), 20:30Z(D))` differ by a ~20.5h shift: the session window pulls in **the prior evening's after-hours + the overnight + the pre-market** (when after-hours earnings and overnight M&A actually print — the most actionable catalysts) and pushes **today's late after-hours** into tomorrow's brief.

Concrete overlap estimate: of the ~200 capped items/day, the date-anchored sources (Polygon + EDGAR ≈ 87 rows, P1 §1) are `[D 00:00Z, D+1 00:00Z)`-bounded, so they overlap the flagship session window only in the `[00:00Z, 20:30Z)` portion ≈ **85%** of a UTC day; the `[20:30Z, 24:00Z)` tail (~14%) shifts to the next brief, and the **prior** day's `[20:30Z, 24:00Z)` tail shifts **in**. Net: roughly **10–15% of items move between adjacent briefs** vs today's UTC-day labelling. That is small enough to be safe but **large enough to be a visible content change a reviewer must sign off** — it is a correctness fix (the after-hours print now lands in the brief that reasons about it), not a cosmetic relabel.

This is why the first slice should be the **plumbing as a no-op**, not the window flip.

### P2a — MIC structure + lake-read path, XNYS, **near-no-op** (RECOMMEND FIRST)

- **Scope:** add `session_window.py` (`session_window(MIC, cut) -> (prev_cut, this_cut, session_date, anchors)`) + `lake_view.py` (`read_session_view`, §2). Add `session_close_utc` helper to `calendar.py`. Rewire the five stages to read from `read_session_view(...)` **but with the window deliberately set to the calendar-UTC-day** (`[D 00:00Z, D+1 00:00Z)`) so the admitted frame is byte-for-byte the current `{D}.parquet` content (modulo the as-of collapse, which is a no-op on a single-overwrite day). Add `exchange`/`session_date`/`cut_utc` columns to Phase E parquet + ingest, populated as `XNYS`/`date`/`null`. Migrate `Brief`/`DayMeta` PKs (§3).
- **Files:** new `thematic/session_window.py`, `thematic/lake_view.py`; `paper/calendar.py` (+1 helper); `alphalens_cli/commands/thematic.py` (ingest→extract→map→score→brief read path); `thematic/extraction/event_extractor.py` (read frame in, not `{D}.parquet`); `briefs/models.py` + migration; `briefs/ingest/parquet.py`.
- **Additive vs read-path:** read-path change (the source of the news frame moves from `{D}.parquet` to the lake view) **but content-preserving**.
- **Risk:** LOW. Same items, same order, same cap; the lake already exists and is written by P1. The only behavioural surface is "does the lake view reproduce `{D}.parquet`" — pinned by a test (§ test helper).
- **Changes observable brief content?** **No** (by design — windows set to UTC day).

### P2b — flip to the real session window (content change)

- **Scope:** change the P2a windows from calendar-UTC-day to the real `[prev_cut, this_cut)` flagship session window; set downstream price `asof = session_close_utc`; resolve the earnings guard (§5). Add the extract-cache epoch reset (§ below).
- **Files:** `session_window.py` (flip the window), `thematic.py` (asof wiring), `earnings_calendar.py` (freshness anchor).
- **Additive vs read-path:** read-path semantics change.
- **Risk:** MED. ~10–15% of items move between adjacent briefs (§6 analysis); after-hours earnings re-home. Needs reviewer sign-off + a before/after diff on a sample day.
- **Changes observable brief content?** **YES** — this is the correctness fix.

### P2c — global-event seam + tests + snapshot hardening

- **Scope:** add the `global_event_id` content-hash column to the events parquet (populated, unused; §4); the lake as-of-query test helper; window-correctness regression test (every admitted item inside `[prev_cut, this_cut)`).
- **Files:** `event_extractor.py` (seam column), new `tests/thematic/test_lake_view.py`, `tests/thematic/test_session_window.py`.
- **Additive vs read-path:** additive.
- **Risk:** LOW.
- **Changes observable brief content?** **No.**

### Extract-cache epoch reset (folded into P2b)

The extractor caches by `(news_id, date)` (`event_extractor.py:355-437`). When P2b flips the window, the news set for a `(MIC, session_date)` differs from the old `{D}` set, so the per-day cache is stale (discovery finding #5, the "latent footgun"). **Decision: accept cache invalidation on day 1 of P2b** — re-key the events cache by `(MIC, session_date)` and start each `(MIC, session_date)` from an empty extraction epoch (discovery mitigation (a), "treat the session-window news source as a fresh extraction epoch"). No migration script (extraction is cheap and idempotent; a one-time re-extract is cleaner than reconciling two cache keyings). Resolves the discovery open-question on extract caching → **re-key cache to `(news_id, MIC, session_date)`, accept day-1 invalidation, no migration script.**

---

## 7. OPEN DECISIONS for the user

1. **Ship the content-changing session window now (P2b) vs land the MIC structure as a no-op first (P2a)?**
   **Recommend: P2a first.** Smallest correctness win that is provably inert (windows = UTC day), de-risks the migration + the lake-read path, lets the `Brief`/`DayMeta` PK change land without any content diff to review. P2b (the real window) follows as its own reviewable PR with a before/after sample-day diff. Quantified: ~10–15% item re-homing (§6).

2. **Adopt `exchange_calendars` directly vs extend `calendar.py`?**
   **Recommend: extend `calendar.py`.** It is already MIC-keyed, already wraps `exchange_calendars>=4.5`, already cached/locked, already tested on XWAR. P2 needs only one new helper (`session_close_utc`) mirroring the existing `session_open_utc`. Adopting the library *directly* in new modules would fragment the "one calendar wrapper" surface the CLAUDE.md exchange-parametrized promise relies on. (P1 open-decision #1 leaned "adopt the library"; this memo refines that to "adopt it **through** the existing wrapper, which already does".)

3. **Snapshot store: extend `Brief`/`DayMeta` (add columns + repk) vs new immutable `BriefSnapshot` table?**
   **Recommend: extend.** The lake already gives bit-identical PIT replay (§2.3); Postgres stays the fast serving layer. A second table doubles the read surface for no replay benefit. (§3.1.)

4. **Brief generation entry point: `--date + --mic` explicit, vs infer MIC from candidate home exchange?**
   **Recommend: `--date + --mic` explicit for P2 (default `XNYS`).** Inferring MIC per-candidate is a P3 concern (it only matters once candidates span exchanges); for P2 a brief is one `(MIC, session_date)` artifact and the orchestrator takes `mic` as a parameter (default `XNYS` keeps the current single-exchange behaviour). The Phase E orchestrator is effectively hardcoded XNYS today (no exchange param); adding the `mic` parameter belongs in **P2a** (it is part of the structure), defaulting to XNYS so it is inert.

5. **Lake partition rename (`session_date=` → `txn_date=`) — now or later?**
   **Recommend: later (cosmetic).** The partition is a UTC-ingest-date placeholder (§2.4); the session view computes its own logical window regardless. Rename when convenient under no-backward-compat; not a P2 blocker.

6. **Bitemporal schema (P1 dependency) — confirm P1 lands before P2b?**
   **Recommend: yes, hard gate.** P2a can be built against the lake as it exists today (it already has `ingested_at` + `session_date=` partitions), but P2b's as-of correctness depends on P1's explicit-UTC-bounds ingest (GDELT `DATESTART/DATEEND`, RSS bounds) — otherwise the lake still contains now-relative bleed and the as-of replay is not clean. **Sequence: P1 → P2a → P2b → P2c.**

---

## 8. Sequencing summary

```
P1 (lake foundation, separate memo)  ──hard gate for P2b──┐
P2a  MIC structure + lake-read no-op (LOW risk, no content change)   ◀ RECOMMENDED FIRST
P2b  flip to real session window (MED risk, content change, reviewer sign-off)
P2c  global-event seam + tests + snapshot hardening (LOW risk)
P3   multi-exchange (XTKS/XHKG/XSHG/XWAR) — register MIC + calendar, fill the §4 seam
```

The 6×/day cadence → exchange mapping (discovery open-question) is **P3**, not P2: today all 6 cuts produce XNYS briefs; the CLAUDE.md "global exchange rotation" is documented intent with no CLI/systemd mapping yet. P2 leaves the cadence as-is (6 XNYS cuts, flagship = `20:30Z`); P3 introduces a per-cut `mic` so the rotation becomes real.

---

## 9. FEEDBACK / OUTPUT-SIDE scheduling — per-MIC replay + ingest (multi-exchange)

The session-window problem has a mirror on the **output / feedback side**: the EDGE market-behavior dashboard is fed by a daily **replay** (compute) + **ingest** (publish) chain whose timing today assumes a single exchange. The same MIC-keyed session logic this memo locks for the *input* news window applies to the *output* replay schedule. Captured here so it is not re-discovered when multi-exchange (P3) lands.

### 9.1 Current chain (XNYS-only) and its one inefficiency

- **Replay** — `alphalens-feedback-shadow-returns` systemd timer, **`06:30 UTC` once/day**. Broker-free price-path replay of every candidate's 3E/3TP/1SL ladder over **Polygon minute bars**, plus the population monitor over its ~42-session lookback; enriches `market_excess_return` (vs SPY). Writes `~/.alphalens/population_ladders/*.parquet` + `feedback.db`. Time chosen because XNYS closes ~`20:00–21:00 UTC` so bars are long settled by `06:30 UTC`.
- **Ingest** — `rebuild-ladder-outcomes` compose maintenance one-shot, fired as `ExecStartPost` of the **6×/day** `alphalens-thematic-build` (`HH:30 UTC`). Per-date `parquet_mtime`-gated upsert into Postgres `edge.LadderOutcome` (`edge/ingest/parquet.py:206`). Cheap; 5 of 6 daily runs are no-op skips because the data only changes once/day.
- **Inefficiency (XNYS, minor):** replay finishes ~`07:00 UTC` but the next ingest is the `08:30 UTC` cut (the `04:30 UTC` build ran *before* replay) → ~1.5 h where a fresh parquet exists but is unpublished. A cheap fix is an ingest trigger on the replay unit itself (`ExecStartPost` on `shadow-returns`), but the value is low (once-daily telemetry, not a live trading signal). **Not worth a standalone change before multi-exchange.**

### 9.2 Why a single fixed-time daily replay breaks under multi-exchange

Different MICs close at different UTC times, so one `06:30 UTC` replay cannot serve them:

| MIC | close (approx UTC) | a `06:30 UTC` replay would… |
|---|---|---|
| XTKS Tokyo | ~`06:00` | run only ~30 min post-close — bars possibly unsettled |
| XSHG Shanghai | ~`07:00` | run *before* today's close — misses today |
| XHKG Hong Kong | ~`08:00` | run before close — misses today |
| XWAR Warsaw | ~`15:30` | run ~9 h before close — today's session replayed only **next day** (full-day-stale) |
| XNYS New York | ~`20:00–21:00` (prev day) | fine (9 h after close) |

The fix is the output-side analog of §1: **replay (and its ingest) must be triggered per-MIC after `session_close(MIC) + settle_buffer`**, not at a single wall-clock time. This reuses the exact primitives this memo already needs — `paper/calendar.py` MIC helpers + the new `session_close_utc(MIC, date)` helper (open-decision #2) — so the schedule is *session-driven*, not fixed-UTC. N active exchanges → N daily replay triggers, each followed immediately by its ingest. Sequence this **with P2/P3**, reusing the same calendar layer; do not design a separate scheduler.

### 9.3 The deeper gate — per-exchange price bars (NOT just scheduling)

Scheduling is the easy half. The replay computes the ladder price-path over **Polygon minute bars, and Polygon is US-only**. The calendar is already MIC-parametrized (CLAUDE.md "exchange-parametrized calendar helper"), but **the price-data source is not** — XWAR/XTKS/XHKG/XSHG need their own minute-bar vendors. That is the real cost/decision, and any new bar vendor is subject to the **mandatory data-vendor PIT-validation gate** (CLAUDE.md research methodology: ≥5 sector-diverse anchor events × 2-source triangulation, HALT on fail). **Multi-exchange feedback is blocked on price-vendor coverage before the schedule even matters.**

### 9.4 Recommendation

- **Now:** no change. The XNYS chain is correct; the `06:30→08:30 UTC` lag is cosmetic.
- **At multi-exchange (P3):** fold the replay/ingest schedule into the same MIC-keyed session infrastructure as the news window — per-MIC trigger at `session_close_utc(MIC) + buffer`, ingest immediately after. First resolve the **per-exchange minute-bar vendor** (gated by PIT validation); the schedule is meaningless without it.
- This output-side thread is the feedback-side twin of §1/§4 and should ship in the same P3 epoch, not as a separate scheduler design.
