# Buffett lens on the candidate card — surfacing + sorting design

**Status: LOCKED** (design agreed; implementation not yet started)
**Date:** 2026-06-12
**Author:** research session (multi-agent workflow `buffett-card-design` + user design review)
**Related:** [`buffett_cascade_alphalens_feasibility_2026_06_10.md`](buffett_cascade_alphalens_feasibility_2026_06_10.md), epic #500 (Buffett filter-cascade), `project_buffett_edge_correlation_deferred_2026_06_11` (deferred validation)

## 0. Purpose

Decide **what Buffett-lens output appears on the daily-brief candidate card, how it
renders without bloating an already-dense card, and whether candidate sorting should
account for it.** The Buffett lens (`buffett lens <date> --qualitative [--scuttlebutt]`,
epic #500) is today an opt-in ad-hoc CLI path, NOT wired into the daily thematic brief.
This memo locks the target architecture for wiring it onto the card.

User requirements (verbatim intent):
1. Buffett qualitative + scuttlebutt results should appear on the candidate card.
2. Sorting **may** take them into account.
3. The card must **not** bloat excessively.

## 1. The cost asymmetry (the fact the design pivots on)

The lens has two layers with radically different per-candidate cost:

| Layer | Cost / candidate | Source |
|---|---|---|
| **Cheap** — owner-earnings yield, ROIC (latest + 3y avg), margin-of-safety, coverage | ~zero | the L4 scorer **already** fetches EDGAR companyfacts + yfinance per candidate (`screening/scorer.py:560-595`); the Buffett quant panel (`buffett/comparison.py:135-165`) is marginal arithmetic over facts already in hand |
| **Expensive** — moat type/trend, candor, understandable, rationale prose, scuttlebutt | ~$0.10-0.15 all-in + ~25 s | per-candidate 10-K fetch + DeepSeek Pro classification + Perplexity scuttlebutt |

The original workflow synthesis proposed a **two-clock** split: cheap numerics on every
brief, expensive qual lazily fetched on-demand and cached. **The user overruled this** — see §2.

## 2. Decision: eager-compute the full panel (single clock)

**The "expensive" LLM option is paradoxically the cheaper one.** Reasoning, agreed with the user:

- **Magnitude.** ~$0.15/candidate × ~14 survivors = ~$2/brief. Dedup by `(date, ticker)`
  across the 6×/day runs → realistically **~$2-3/day ≈ $60-90/mo worst case.** A single
  capital decision on a poorly-researched company costs a multiple of that. Doctrine:
  people act on AlphaLens with real money (`feedback_dont_oversimplify_money_entrusted_2026_06_05`).
- **The stronger argument is friction, not price.** An on-demand "fetch (~25 s)" model
  creates exactly the friction that discourages the analysis the tool exists to provide —
  a user may decide on the cheap chip alone. That **is** the "insufficiently-researched
  company" failure mode. Eager-compute puts the qualitative verdict **always on the card**;
  it cannot be skipped.
- **Bonus.** Eager qual on every brief accelerates the deferred **Buffett×EDGE** dataset
  (every candidate gets a verdict that later matures into an EDGE outcome) → N≥30 sooner.

### What this simplifies vs. the original synthesis

- **PR-4 (interactive write/compute endpoint) is removed.** No browser-triggered compute —
  it would have been the project's first interactive write path (auth, abuse, per-day fetch
  budget, idempotent enqueue). That entire risk surface disappears.
- Drawer **absent/fetch/failed** states collapse to **present** or honest `—` (no fetchable 10-K).
- The deep-read drawer becomes **pure progressive-disclosure of already-present data** (like
  the existing narrative band), not a compute trigger.

### Single clock — where it runs

Qual is computed **eager, once per `(date, ticker)`, as a post-brief enrichment pass** — the
same operational pattern as the existing `rebuild-ladder-outcomes` ExecStartPost (host venv,
full pipeline image, writes a sidecar that the cache rebuild reads). NOT inside the slim
Django container (slim-image boundary unchanged). The `(date, ticker)` panel is **PIT
as-of `brief_date` → immutable once written** (compute paid at most once per name ever).

## 3. Scope: survivors in the brief only

Qual+scuttlebutt runs for **the candidates that reach the brief card** (~14), NOT names cut
earlier in the proposal funnel. Those funnel names are never shown to a human, so qual-ing
them is pure waste with no decision value. Scope = brief survivors. **Agreed.**

## 4. What appears on the card, and at what disclosure level

The card is already very dense (4 headline metrics + 8 SignalBars + 24 `dl` rows + 3 narrative
columns). The Buffett surface is **additive and small**.

| Level | Content | Where |
|---|---|---|
| **Always-visible (1 token)** | `buffett NN/100` score chip from the **cheap** numerics only; tone green ≥70 / amber 40-69 / muted <40; dim + dashed-underline when coverage <0.5; `{#if score != null}` guard so a no-Buffett day is pixel-identical to today | 5th right-side span in the meta bar, `CandidateCard.svelte:98-120`; `whitespace-nowrap` on `NN/100` (atomic-token rule, PR #261) |
| **Hover (ChipTip)** | the 4 cheap numerics spelled out: OE-yield % / ROIC 3y % / MoS % / coverage N/6 + one-line meaning | on the score chip |
| **Expand (drawer, collapsed by default → zero resting vertical cost)** | 4 tri-state pillar badges MOAT / TREND / CANDOR / UNDERSTOOD (`BuffettPillar.svelte` cloned from `GatePill.svelte`) + full `qualitative_rationale` in a violet-left-border blockquote matching the thesis block + `scuttlebutt: web-grounded, unverified` amber footnote when used | `buffett.deep-read >` toggle at the bottom of the LEFT col-7, before the narrative band |

No new full-width band, no 5th narrative column, no extra SignalBar, no new `dl` rows, no new
visual primitive (reuses ChipTip + a GatePill clone). **Net resting growth: +1 chip.**

Low-coverage honesty: a thin-fundamentals name shows "verdicts withheld — insufficient
coverage (N/6)" (mirroring the `peer_cohort_level` THIN treatment), NOT "failed the test".

## 5. Sorting decision

**Qual (LLM verdict) NEVER enters sorting.** Even though it is now always present, it has no
validated link to forward returns until **Buffett×EDGE** (deferred, N≥30, ~2026-09+,
`project_buffett_edge_correlation_deferred_2026_06_11`). Eager-compute makes it
*visible-always*, not *predictive*. Qual stays **display-only**.

Sorting may use **only the cheap numeric `buffett_quality_score`**, as a **secondary
tie-break placed AFTER `layer4_weighted_score`** — Buffett refines, never overrides, the
validated primary ranking.

- Formula (hand-chosen, unvalidated — ship as additive/telemetry):
  `raw = 100·(0.45·norm(OE-yield, clip 0-10%) + 0.35·norm(ROIC_3y, clip 0-30%) + 0.20·mos_term)`,
  then COV-shrink `score = raw·(0.5 + 0.5·coverage)` so thin-data names are neither buried to
  zero nor over-boosted past a fully-covered high-quality name.
- Neutral default when coverage <0.5 or column absent → sort-inert; old parquets order byte-identically.

### OPEN — resolve before PR-1

Whether to wire the tie-break at all in v1 is **not yet decided**. `layer4_weighted_score` is
an int; if ties are rare the sort effect is cosmetic and all the value is in the chip + drawer.
Honest alternative (Approach-1 stance): **v1 = display-only, no sort change**; add the
tie-break only once Buffett×EDGE validates it. The user's requirement said sorting *may* —
this is the condition. Decide at PR-1 time after measuring tie frequency.

## 6. Other open questions (carried from design review, not yet closed)

1. **Composite vs. raw.** `NN/100` is convenient for one chip + one sort key but implies false
   precision over 3 hand-weighted numerics. Alternative: drop the composite, show the single
   hardest number (e.g. ROIC_3y) on the chip and sort on that. Decide with §5.
2. **Coverage threshold for the chip.** Brief 06-11 had ~half the names at COV 0.33. At
   `0.5+0.5·COV` a 2/6-field name still shows ~0.67 weight. Should the chip **not render at
   all** below a hard COV floor, instead of rendering dim? Lean: hard floor for the chip,
   keep the dim state only for the 0.5–0.7 band.
3. **Chip placement width.** The meta bar gains a 5th metric at the edge of comfortable width —
   verify clean wrap at the narrowest supported width.

## 7. Data model (fields carried in the brief parquet → Django → API)

Cheap (flat nullable columns on the Brief row, every brief, drives chip + sort):
`buffett_quality_score` (0-100), `buffett_data_coverage` (0-1),
`buffett_owner_earnings_yield_pct`, `buffett_roic_latest`, `buffett_roic_3y_avg`,
`buffett_margin_of_safety_pct`.

Qual (eager-computed, immutable per `(date, ticker)`; carried for the drawer):
`buffett_moat_type`, `buffett_moat_trend`, `buffett_management_candor`,
`buffett_understandable` (enums), `buffett_qualitative_rationale` (prose),
`buffett_used_scuttlebutt` (bool), `buffett_qual_computed_at`, `buffett_source_accns_json`.

Storage choice (cheap-cols-on-Brief vs. separate `BuffettQual` table) to be fixed at PR-2/PR-3;
with eager single-clock compute a flat block on the Brief row is now viable since qual is always
present — the separate-table rationale (lazy fetch) is gone. Lean: flat block on Brief row +
qual prose/enums either inline or in a 1:1 sidecar table for row-width hygiene.

## 8. Phasing (revised — PR-4 removed)

1. **PR-1** (pipeline, cheap numerics + optional sort): compute the 4 numerics + `buffett_data_coverage`
   + `buffett_quality_score` in `score_candidates` (~`scorer.py:575`) from already-fetched facts;
   decide §5 (tie-break vs. display-only) after measuring tie frequency; write columns into the
   `{asof}.parquet`. TDD golden sort test.
2. **PR-2** (slim Django + chip): nullable cheap columns on `briefs/models.py` (byte-stable
   migration, migration_guard), ingest auto-maps, serializer emits; SPA `types.ts` + meta-bar
   chip + ChipTip hover. **After PR-2 the card shows + sorts on cheap Buffett, zero LLM spend.**
3. **PR-3** (eager qual enrichment pass): `alphalens buffett qual-enrich <date>` (or fold into a
   post-brief ExecStartPost) wrapping `build_comparison` + `_run_qualitative` + `_assessment_record`
   for every brief survivor; **10-K immutable cache** (fetch-once-per-filing) to avoid worsening the
   #379 SEC per-IP 403 contention; parallelize per candidate; write qual fields into the sidecar the
   cache rebuild reads. Slim-safe Django surface for the qual fields.
4. **PR-4** (SPA deep-read drawer): `BuffettPillar.svelte` cloned from `GatePill`; collapsed
   `buffett.deep-read >` toggle rendering 4 pillars + rationale violet blockquote + scuttlebutt
   footnote + low-coverage withheld state. Pure progressive disclosure of present data — no fetch.

Each PR: TDD red→green→refactor; zen `deepseek/deepseek-v4-pro` thinking=high pre-merge on the
mixed-stack PRs (PR-2/PR-4 touch serializer + renderer); CI green on latest commit before merge.

## 9. Risks

1. **Sort heuristic unvalidated** — 0.45/0.35/0.20 + COV-shrink are hand-chosen; Buffett×EDGE that
   would calibrate them is deferred. Ship additive/telemetry; never fold into the int
   `layer4_weighted_score`; PR Known-issues must state it is a display heuristic, not an optimal blend.
2. **Composite over-trust** — `NN/100` from 3 cheap numerics is a weaker proxy than the full lens;
   mitigate via COV-gating + dim low-coverage + hover disclosure + a one-line tooltip caveat.
3. **SEC 10-K fetch load** — eager qual fetches a 10-K per survivor; without an immutable 10-K cache
   this worsens the epic-#379 403-starvation. The cache is a PR-3 hard requirement, not an optimization.
4. **Pipeline latency** — ~25 s × ~14 added if run inline; mitigate via the post-brief enrichment pass
   + per-candidate parallelism (do not block the 6×/day brief build).
5. **Slim-image + migration-skew** — the qual enrichment must run in the full pipeline image, never the
   slim Django container; migrations byte-stable and shipped in the same image (#331/#340).
6. **Coverage asymmetry copy** — drawer must make "withheld" mean *insufficient data*, not *failed the
   Buffett test*.

## 10. What changed from the workflow synthesis

The multi-agent synthesis recommended a **two-clock** architecture (cheap-eager, qual-on-demand
+ interactive fetch endpoint). The user's cost reframe in §2 replaced it with a **single eager
clock**: pay the LLM upfront for every brief survivor, because the LLM cost is trivial next to the
capital at risk and on-demand friction defeats the tool's purpose. This **removes** the interactive
write path (old PR-4) and its entire auth/abuse/budget risk surface, and turns the drawer into pure
progressive disclosure. The cheap/expensive split survives **only for sorting** (sort on validated-or-
cheap; qual stays display-only until Buffett×EDGE).
