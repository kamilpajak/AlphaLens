# Multi-exchange news lake + per-exchange session-window design

**Status:** DRAFT (design, not yet scheduled)
**Date:** 2026-06-05
**Author:** research session 436cc26d
**Supersedes:** the implicit "UTC-day file" news-window contract in `alphalens_pipeline/thematic/news_ingest.py` + `sources/*`

## 0. TL;DR

The thematic news pipeline bakes a per-source, partly now-relative date window into
ingest and labels the output by a single UTC calendar day. That is (a) internally
inconsistent — the "T-1" brief silently contains some T-0 news via GDELT and RSS —
and (b) not point-in-time (PIT) correct for any historical rebuild. It also does
**not** generalize to the multi-exchange future (XTKS / XWAR / XHKG / XSHG), where
each market has a different session in a different timezone.

The fix, validated against industry practice (Bloomberg B-PIPE, Refinitiv, RavenPack)
via two Perplexity deep-research passes, is the standard pattern:

> **One UTC-stamped, append-only, bitemporal news *lake* + a per-exchange
> session-window *view* parametrized by ISO 10383 MIC.** Ingest makes no session
> decision and partitions nothing by any market's trading day. The view is a
> **gap-free, contiguous tiling** of the timeline (each brief covers
> `[prev_cut, this_cut)` — see §3.0), anchored to the exchange's session via a
> MIC-keyed trading calendar, computed as a query parameter at brief-generation
> time. The brief *output* is snapshotted immutably per `(MIC, session_date)` for
> bit-identical PIT rebuilds.

> **Revised after adversarial review (2026-06-05, §9):** the original draft used
> `[prev_close, next_open)` as the window. That is WRONG — it is an overnight-only
> window that leaves the intraday period `[open, close)` uncovered by any session,
> so a midday catalyst (Fed 14:00, lunchtime M&A) would be dropped from every brief.
> The window is now a **gap-free contiguous tiling**, not an overnight slice.

Adding an exchange becomes "register its MIC + calendar", with **zero changes to the
lake** — the same promise the existing exchange-agnostic calendar helper already makes
for trade-setup geometry (see ADR 0012 follow-on, `calendar.py` MIC param).

## 1. The current behaviour (diagnosis)

The daily pipeline (`deploy/docker/run_thematic_day.sh`) runs every stage with no
`--date`, so each stage defaults to `target = today_UTC - 1 day` (T-1). The brief file
is `thematic_briefs/{T-1}.parquet`. Empirically, on a 2026-06-05 run the "2026-06-04"
news file held 200 rows after the cap, of which **8 were from 2026-06-05** (gdelt 5,
rss 3). So the date label is not a clean content boundary.

Per-source windows differ:

| Source | Window | Date-anchored? | Replay-safe? |
|--------|--------|----------------|--------------|
| Polygon (`polygon_news.py`) | `[D 00:00, D+1 00:00)` UTC | yes (hard `gte/lt`) | yes |
| EDGAR 8-K (`edgar_press_release.py`) | daily form index `form.YYYYMMDD.idx` | yes (immutable idx) | yes |
| RSS (`rss.py`) | `[D-2d, D+3d)` (`DEFAULT_WINDOW_DAYS=2`) | timestamp filter, but undated entries get `fallback_date=D` | no (feeds are rolling — past entries gone) |
| GDELT (`gdelt.py`) | `timespan="1d"` relative to **now** | **no** (`date` used only for the cache filename) | **no** |

Two independent problems fall out:

- **A — daily-live label imprecision (LOW–MED).** GDELT (`timespan` is now-relative) and
  RSS (`±2d`) bleed T-0 news into the T-1 file. The 200-item recency cap
  (`news_ingest.py`, sort `_cluster_rank_ts = max(member.timestamp)` desc → `head(200)`)
  *amplifies* this: forward-bleed rows are newest, so they are preferentially **kept**,
  and the older RSS tail is preferentially **dropped**. Because each of the 6×/day
  `--force` runs overwrites the same `{D}.parquet`, and GDELT's 24h window slides forward
  through the day, the bleed **grows** as the day progresses (the 20:30 UTC run captures
  the most T-0). Polygon + EDGAR (~87 rows) stay date-pure and guaranteed; GDELT + RSS
  fill the remainder with a forward bias. Not a look-ahead leak (prices are still T-1),
  but the label is wrong and the cap silently favours one rolling source over high-signal
  date-anchored ones.

- **B — replay / backfill correctness (HIGH, latent).** Any run with an explicit past
  `--date` produces a corrupted news file: GDELT returns "last 24h from now" regardless of
  the requested date; RSS returns current rolling entries tagged with the requested date.
  Polygon + EDGAR are fine. So historical news reconstruction is fundamentally wrong for 2
  of 4 sources. It does not bite today **only because** the pipeline runs ~live
  (`date ≈ now − 1`).

No test pins that the unified news file's rows fall within the target window — the
cross-source date semantics are unenforced.

### 1.1 Downstream trusts the label, not timestamps

`extract_daily(date=D)` reads `{D}.parquet` and processes **all** rows (bleed included) —
no timestamp re-filter. `score` uses `asof=D` for PIT price slicing, so a T-0 news row
gets scored against T-1 prices (a price/news asof mismatch, not a leak). `brief` labels
everything D. The `next_earnings_date` PIT guard (`fetch_next_earnings`,
`if asof < today: return None`) suppresses the field on every production run because
`asof = T-1 < today` always — the field is structurally always `None` (verified across
12 recent briefs). The earnings field resolves naturally once the window is explicit and
freshness-defined rather than `asof < today`.

## 2. Why "anchor to US 4pm ET" does not generalize

The first instinct (Perplexity pass 1) was to session-align to the US close (16:00 ET).
That is right in spirit — a morning brief should answer "what happened since the last
close": after-hours earnings, overnight Europe, pre-market — and the UTC day cuts the
overnight session awkwardly. But it is US-specific. The future adds XTKS (no DST), XWAR
(CET/CEST), XHKG, XSHG — each with its own close/open and timezone, plus intraday lunch
breaks (Tokyo, HK). Baking any single session window into the pipeline is a dead end.

## 3. The design (Perplexity pass 2, validated against Bloomberg/Refinitiv/RavenPack)

### Layer 1 — News lake (global, UTC, timezone-neutral, bitemporal, append-only)

Ingest stores **all** news with **absolute UTC timestamps**, using **explicit
start/end datetime bounds per source** (fixing GDELT's now-relative window via its
`DATESTART`/`DATEEND` params, and RSS's rolling window). It makes **no** session-window
decision and partitions **nothing** by any exchange's trading day. It is a continuous,
append-only, immutable log with monotonic ids.

**Bitemporal** — store two times, not one:
- *valid time* — when the news was true / publicly available (publish/acceptance time);
- *transaction time* — when our pipeline recorded it.

This is the cornerstone of a true PIT rebuild ("what was knowable at moment T"), and the
reason an append-only, never-mutated log is required.

### 3.0 Windowing model — gap-free contiguous tiling (corrected)

The window must **tile the timeline with no gaps and no overlaps**, so no news item is
ever orphaned. An overnight-only window `[prev_close, next_open)` does NOT tile — it
leaves the intraday period `[open, close)` uncovered, dropping midday catalysts (Fed
14:00, lunchtime M&A) from every brief. That is the single most damaging failure mode for
an event-driven tool and was the original draft's error (caught in adversarial review, §9).

The correct primitive is **contiguous cut-to-cut**: each brief covers `[prev_cut, this_cut)`
where `this_cut` is the brief's generation time and `prev_cut` is the previous brief's cut.
Consecutive briefs tile the full timeline; nothing falls between them. This is exactly what
the existing **6×/day cadence** is for — the midday runs cover the intraday window, so
intraday catalysts are captured rather than dropped. The exchange **session** (close / open
from the MIC calendar) is the **anchor/label** — it names which run is the flagship morning
brief for a market and how the window is labelled — **not** a lossy filter on which items
are admitted. (Equivalent framing: close-to-close `[prev_close, this_close)` also tiles; the
cut-to-cut form generalizes it to an arbitrary generation cadence.)

### Layer 2 — Per-exchange session view (parameter = ISO 10383 MIC)

A brief for exchange X generated at cut time `T` selects from the lake the items whose valid
time falls in `[prev_cut(X), T)` (the gap-free window of §3.0), labelled against X's current
session from a **MIC-keyed trading-calendar service** (timezone + historical DST + half-days
+ intraday breaks per market). The window is a **query parameter at brief-generation time**,
never baked into ingest. Same lake, different slice per exchange. Adding an exchange =
register its MIC + calendar, zero changes to the lake.

News **relevance** is per-ticker (a US-themed story can drive a Warsaw-listed
beneficiary), so the lake is shared; the session window decides **timing** (what is
"fresh" for that market's open), not relevance partitioning. Relevance is the existing
downstream theme→beneficiary mapping (a ticker's home exchange).

### Six corrective requirements (beyond the bare two-layer split)

1. **Bitemporal storage** (valid + transaction time). Not one timestamp.
2. **Gap-free contiguous window** (cut-to-cut `[prev_cut, this_cut)`, §3.0) — NOT an
   overnight `[prev_close, next_open)` slice (which orphans intraday news) and NOT a fixed
   24h UTC period. The MIC session is the anchor/label, not the admission filter; it must
   still model holidays, early closes / half-days, and **intraday breaks** (Tokyo / HK
   lunch recess) for correct labelling.
3. **MIC-keyed calendar with historical DST.** Strong candidate: the `exchange_calendars`
   PyPI library (MIC-keyed global calendars with breaks/holidays/half-days) — prefer it
   over hand-rolling. Decide whether to adopt it or extend the existing `calendar.py`.
4. **Cross-listing dedup is the #1 insidious failure.** The same event (a Fed decision,
   an NVDA print) must not inflate importance by appearing N times because it touches N
   listings. Pattern: **global event clustering first** (one global event id), **then
   per-exchange attribution with that market's local session timing** — the event appears
   once per exchange brief, in each market's session. Dedup high-signal sources (SEC)
   rigorously; tolerate higher dup on low-signal RSS.
5. **Source timestamp semantics.** SEC filing-date ≠ public-availability time → use the
   acceptance/`pubDate` time (we already do this — EX-99.1 acceptance-datetime, #391).
   GDELT `seendate`. Add watermarking / bounded allowed-lateness for out-of-order arrivals.
6. **PIT rebuild via "snapshot-as-you-go".** Do **not** rely on reconstructing a brief
   from the raw lake for bit-identical replay. Materialize the brief **output** as an
   immutable snapshot keyed `(MIC, session_date)` at generation time. We have this
   partially (Postgres `briefs`), but it is keyed by date (not MIC) and *rebuilt* from
   parquet rather than immutable — to be tightened.

## 4. Recommended data model (retail-scale)

- **Event lake** — append-only, monotonic id, never mutated, bitemporal (valid +
  transaction UTC). Storage partitioning is by UTC time only (e.g. hourly/daily UTC) —
  that is physical layout, never window semantics.
- **Calendar service** — `exchange_calendars` keyed by MIC (tz / DST / half-days / breaks).
- **Brief snapshot store** — table keyed `(MIC, session_date)`, immutable materialization
  of the output; the SPA/API read this.
- **Recency cap** — per-source quota **within** each per-exchange window (so GDELT can't
  crowd out SEC / Polygon), allocated across time segments.
- **Dedup** — global event cluster → per-exchange attribution; earliest-published item is
  the cluster representative (it sets the market narrative), domain authority only as a
  <15-min tie-break. (Today's cap uses newest-first — the opposite.) Implementation reuses
  the **already-extracted** theme/entity from the existing LLM pipeline — NOT a from-scratch
  cross-lingual NLP clustering system (see §9, rejected simplification #2 caveat).
- **Label** — `(MIC, session_date, [start_utc=prev_cut, cut_utc=this_cut], generated_at)`.
  The date is no longer a bare UTC day; it is a per-exchange session with explicit
  contiguous bounds and a cut time.

## 5. Phasing (validate US first, then split-session markets)

- **Phase 1 — lake foundation (correctness + replay).** GDELT / RSS → explicit UTC
  datetime bounds; ingest becomes a bitemporal UTC lake (absolute timestamps, no
  now-relative windows). Add a test pinning that every unified-news row falls within the
  declared bounds. Add per-source cap quota so SEC/Polygon are not crowded out. Small,
  local to `sources/` + `news_ingest.py`. Closes problem **B** and most of **A**.
- **Phase 2 — session view, XNYS first.** `session_window(MIC, cut)` on a MIC-keyed
  calendar producing the gap-free `[prev_cut, this_cut)` window (§3.0); snapshot-as-you-go
  keyed `(MIC, session_date)`; global-event dedup (earliest-wins, reusing existing
  theme/entity). Validate on a single market (XNYS). Resolve the `next_earnings_date` guard
  here (freshness window replaces `asof < today`).
- **Phase 3 — split-session + scale-out.** Tokyo (lunch break → multi-window), Warsaw
  (CET/CEST), HK, Shanghai = register MIC + calendar, zero lake changes.

## 6. Investor benefit (why this is worth it, not just cleaner)

- A morning brief that is **session-aligned** matches how the investor actually decides:
  "what changed since the last close" → gap risk on the open. The UTC day mislabels and
  mis-times that.
- **Freshest catalysts are the most actionable** (last hours before open). Explicit
  datetime bounds let the window run right up to the cut while staying replay-correct — we
  get freshness *and* PIT, not a trade-off.
- **Per-source quota** guarantees SEC 8-Ks (the hardest catalysts) are never swamped by
  GDELT volume.
- **Global event dedup** stops a single macro event (Fed, mega-cap print) from looking
  like N signals just because it touches N names / markets.
- A **per-exchange** design is the prerequisite for the planned XTKS/XWAR/XHKG/XSHG
  expansion — without it, every new market is a refactor instead of a registration.

## 7. Open decisions (for the epic)

1. Adopt `exchange_calendars` vs extend the in-house `calendar.py`? (lean: adopt — breaks
   / half-days / historical DST are exactly its job.)
2. Lake storage substrate — stay on partitioned parquet under `~/.alphalens/` vs move to a
   table? (lean: bitemporal parquet partitions for Phase 1; revisit if query patterns
   demand a DB.)
3. Window labelling edge: how the gap-free `[prev_cut, this_cut)` window is *named* against
   a session (which cut is the "morning brief" for a market; how late-day intraday items are
   labelled vs the next morning's flagship); boundary tolerance (~10 min) at the cut.
4. Snapshot store: extend the Postgres `briefs` table to key `(MIC, session_date)` and
   make it immutable, vs a separate snapshot table.

## 8. Sources

Two Perplexity Sonar deep-research passes (2026-06-05) grounded in Bloomberg B-PIPE,
Refinitiv Workspace, RavenPack, S&P Capital IQ PIT, ISO 10383 MIC, GDELT DOC API
(`DATESTART`/`DATEEND`), the `exchange_calendars` library, and bitemporal / append-only
data-modelling literature. Note: the specific quantitative claims in those passes (market
shares, terminal prices, "X% of volatility", named clustering algorithms) are treated as
flavour, not load-bearing facts; the **architecture** is the validated signal.

## 9. Adversarial review (2026-06-05) — accepted / rejected

A third Perplexity pass adversarially reviewed this memo. Verdict was "RETHINK". One
catch is accepted as a real bug; the rest are **rejected** as a context mismatch — the
reviewer repeatedly argued "simplify, you are a solo retail builder", but this is a mature
tool that real people in the investing group act on with their own money, so the rigour is
warranted, not over-engineering. The reviewer conflated "hard for a solo builder" (a weak
argument — the work gets done) with "unnecessary" (a different claim it never established).

**ACCEPTED (real bug — fixed above):**

- **Windowing.** `[prev_close, next_open)` is an overnight-only window that orphans the
  intraday `[open, close)` period, dropping midday catalysts from every brief. Replaced with
  the gap-free contiguous `[prev_cut, this_cut)` tiling of §3.0, which the 6×/day cadence
  already supports. This was the single genuine flaw and the most damaging one for an
  event-driven tool. (The reviewer's interval arithmetic was muddled, but the conclusion —
  intraday news is orphaned — is correct.)

**REJECTED (kept; reframed as sequencing, not scope-cut):**

1. *"Bitemporality is over-engineered; use single-timeline event-time, 90/10."* — Rejected.
   With money at stake, the audit trail ("what the brief knew at moment T, from which data")
   is a feature, not a luxury; storage cost on news volume (hundreds of items/day) is modest,
   not the "explosion" the reviewer claimed (a news-scale, not web-scale, lake; the
   NoSQL-required claim is false on parquet). Bitemporal stays. It is sequenced so it does
   not block the windowing fix, but it is NOT cut.
2. *"Global event clustering is an NLP tarpit for a solo builder."* — Partly fair on the
   from-scratch cross-lingual version, but rejected as a scope-cut: duplicate-inflated event
   importance can mislead a money decision, so cross-exchange dedup is a real requirement.
   It is (a) only relevant once multi-exchange is live (P3, not P1), and (b) built by reusing
   the **already-extracted theme/entity** from the existing LLM pipeline + the existing
   URL/lexical dedup — NOT a new research-grade NLP system. Kept, scoped, deferred to P3.
3. *"Compliance / regulatory controls are missing — would be rejected by enterprise review."*
   — Out of scope. This is a personal augmentation tool for one investing group, not a
   redistributed or institutional product (GDELT open, SEC public, Polygon/RSS personal-use
   ToS). Revisit only if it ever serves external/commercial clients.
4. *"No data-quality / freshness monitoring."* — Largely already exists and the reviewer
   could not see it: per-stage volume gauges (#373), the EDGAR dead-man-switch (#384/#390),
   VIX-cache staleness, `StageZeroOutput` / `BriefVolumeAnomaly` alerts. P1 adds one more: a
   window-correctness test (every admitted item inside the declared `[prev_cut, this_cut)`).

**Also noted (minor, accepted):** calendars are dynamic, not static — rules change over time
(EU ending DST 2026, exchange-hour changes), so for PIT-correct rebuild the calendar must be
the one in force at date D; `exchange_calendars` carries historical schedules, which is an
extra reason to adopt it (open decision #1).

**Net:** the core (UTC lake + per-exchange MIC view) stands. One change is a true fix
(windowing → gap-free tiling); bitemporality and cross-exchange dedup are retained as
rigour the money-stakes justify, sequenced later rather than removed.
