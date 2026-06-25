# Design: ATR-soft-tilt to brief ordering + scoring transparency

**Date:** 2026-06-25
**Status:** DRAFT v2 (adversarial review folded — see §12) — awaiting final user review before writing-plans
**Author:** session (primary, main checkout — implementation to run in a worktree)
**Related:** `docs/research/edge_signal_attribution_2026_06_25.md`, memory `project_edge_signal_attribution_workflow_2026_06_25`, `project_selection_edge_uniformly_negative_2026_06_23`

---

## 1. Goal & TL;DR

Start improving candidate-pick quality **now**, conservatively, **without contaminating the EDGE test set**. The signal-attribution sweep (2026-06-25, fresh data car_5 N=267 / car_10 N=176) found exactly one verdict-grade separator: `technical_atr_pct` (entry-time volatility/extension) — high-ATR names fade hardest (car_5 high-tercile −7.3% vs mid +1.4%). The single most reliable lever is **deprioritize high-ATR / already-popped names**.

This design adds a soft **ordering** tilt: a new continuous `selection_score` that pushes high-ATR names down the daily brief, plus scoring transparency (score breakdown + an "extended" band + the poolability `scorer_config_version`).

**Why this preserves the test set (the central constraint, verified in code):** the pipeline has **no daily cap** — `_sort_and_dedup_for_brief()` only de-dupes by ticker (`orchestrator.py:404`), no truncation; every verified candidate enters the brief and is monitored regardless of order. The selection *set* is funnel-determined (LLM-propose → mcap-filter → 3-gate OR → per-theme cap=3). Crucially the EDGE **outcome** for `(brief_date, ticker)` is theme-independent (`build_trade_setup` takes no theme; the population monitor replays the real price path), so the monitored-population test set is byte-identical pre/post → fully comparable. We touch ordering, never the funnel.

**Honest scope note:** the brief page (`/brief/[date]`) shows **all** candidates — scrollable, no truncation (`+page.svelte:183` `{#each filtered}`, no slice). The only truncation is the **homepage "top picks" teaser** (`routes/+page.svelte:47` `slice(0,8)`), a preview with the full brief one click away. So the tilt's human effect is mild and is *attention* steering, not hiding: (a) what sits at the top of the fully-visible brief scroll, and (b) on days with >8 candidates, which names make the homepage 8-preview. Neither touches the monitored test set. (This corrects an overstatement in the v1 review, which implied the brief itself truncates — it does not.)

**Decisions locked with the user:**
- Posture: act now, conservative (soft tilt to ordering, not a hard gate).
- Signal: **ATR only**; ROIC + extension promoted later (forward, after they survive partialling ATR).
- Strength: **full continuous term** in a new `selection_score` (becomes primary sort key) + a coarse on-card "extended" band.
- ATR percentile basis: **fixed reference breakpoints** (from the historical panel), not the noisy 3–15-name daily cohort.
- Surfacing: score breakdown + band on the **brief card** (briefs app); `scorer_config_version` chip on the brief card **and** `/edge`.

---

## 2. Non-goals

- **No change to the selection funnel** (gates, mcap filter, per-theme cap, LLM-propose). The test-set guarantee.
- **No hard ATR gate / filtering / hiding** — names are reordered, never dropped. The `/edge` + briefs `min_score` filter **stays on `layer4_weighted_score`** so the tilt cannot hide a name below a visibility floor.
- **No promotion of ROIC or extension into `selection_score`** yet — promote forward, only if ROIC survives partialling out ATR (open question from the research note; on the panel ROIC currently folds into ATR).
- **No experts (buffett/oneil) in ordering** — forward-only, N=0 on matured car_10.
- **No λ optimization against in-sample separation** — λ fixed and modest, deliberately not fit.

---

## 3. Current state (from code map + review)

- **Score stage** `alphalens_pipeline/thematic/screening/scorer.py`: `score_candidates()` stamps `technical_atr_pct`, `technical_pct_off_52w_high/low`, `roic_pct` at score time (all available before ordering); computes `layer4_weighted_score` via `compose_weighted_score()` (:138-172) → integer **[1,5]** from fcff(+1) + value-or-reversal(+1) + technicals(+1) + `catalyst_floor` (0–2). **`layer4` is theme-dependent** (catalyst_floor uses per-theme `catalyst_strength`).
- **Brief ordering** `alphalens_pipeline/thematic/argumentation/orchestrator.py`: `_BRIEF_SORT_KEYS` (:332-348) primary `layer4_weighted_score` desc; `_sort_and_dedup_for_brief()` (:351-418) stable mergesort → `drop_duplicates(subset=["ticker"], keep="first")` (:404) → `rank_in_day` assigned from final order (:416). **No daily cap.** Historically `also_in_themes` is empty for all 436 rows (no multi-theme survivor has ever occurred).
- **Sort-lock guards** `apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py`: `_NON_EXPERT_SORT_ALLOWLIST` (:314), `test_sort_chain_is_exactly_the_documented_set` (:414 full ordered pin), `test_every_sort_key_is_in_non_expert_allowlist` (:374), `test_primary_sort_is_layer4_weighted_score_desc` (:49). These break **by design** when `_BRIEF_SORT_KEYS` changes — PR-1 must edit them in lockstep (`selection_score` carries no expert prefix, so it is allowlist-eligible).
- **Poolability keys** to mirror: `insider_signal_version` (scorer.py:368), `mapper_config_version`, `novelty_config_version`.
- **Surfacing reality:** `/edge` ingests `population_ladders` (no brief-signal columns); `LEGACY_CONTRACT_COLUMNS` is a **briefs-app** schema-parity construct. Brief card (`CandidateCard.svelte`) already renders `layer4_weighted_score`. Django `rank_in_day` is the API display-order key (`views.py:88`) and auto-propagates the new order — no `.order_by()` change needed.

---

## 4. `selection_score` — the ATR tilt

Computed in `scorer.py` alongside `layer4_weighted_score`; becomes the **new primary brief sort key**.

```
selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)
```

**`atr_penalty` — targeted at the top tercile (where the damage concentrates):**

Reference breakpoints derived once from the historical signal panel (N=328) and **frozen into `scorer_config_version`** (recalibrated only on an explicit version bump). Labelled **provisional** (single 4-week, 29-brief-date in-sample window):

| breakpoint | value (atr_pct) | source |
|---|---|---|
| `ATR_RAMP_LO` | **5.77** | panel p67 (top-tercile onset) |
| `ATR_RAMP_HI` | **8.37** | panel p90 (deep loser tail, car_5 −7.3%) |

```
raw = technical_atr_pct
if raw is None/NaN:        penalty = 0.0          # never punish unknown ATR
elif raw <= ATR_RAMP_LO:   penalty = 0.0          # low + mid terciles untouched
elif raw >= ATR_RAMP_HI:   penalty = LAMBDA
else:                      penalty = LAMBDA * (raw − ATR_RAMP_LO) / (ATR_RAMP_HI − ATR_RAMP_LO)
LAMBDA = 1.0
```

- **`atr_penalty` is a per-ticker constant** (`technical_atr_pct` depends only on `(ticker, asof)`, no theme). This is the invariant the test-set safety rests on — see §5.
- Penalty zero below p67 → low/mid keep their exact layer4 rank. **Honest caveat:** "mid-tercile is benign" holds at car_5 but is horizon-unstable (at car_10 the p50–p67 band ≈ −3.1%, CI excludes 0). p67 is a *conservative onset* that knowingly forgoes some car_10 decay, not a claim that mid is universally safe.
- `_BRIEF_SORT_KEYS`: prepend `selection_score` desc as the **first** key; keep the existing chain after it. **Because penalty=0 for ~67–78% of names** (everything ≤ p67), `selection_score` ties for most rows and the existing tiebreaker chain (`catalyst_strength` → … → `llm_confidence`) stays load-bearing; it is continuous **only in the top ATR tercile**. Determinism preserved (stable sort + full deterministic chain).

**Also stamp a standalone `atr_rank` column** (the raw `atr_penalty`, or ATR within-day percentile) so the forward A/B (§8) can compare pure-ATR ordering against the blend — see §4a.

### 4a. Why also log pure-ATR (review B3)

The blend `layer4 − atr_penalty` **dilutes the only Bonferroni-grade signal**: on the panel, within-day rank-IC vs CAR is pure `−atr` **0.37/0.50** (car_5/car_10) but the blend **0.24/0.30** (layer4 alone 0.15/0.21), because `var(layer4)=1.03` swamps `var(penalty)=0.12` (~8.7×) and 67% of penalties are zero. The blend still beats layer4 alone, so it is fine as the **shipped display order** (keeps the existing value/catalyst signal in the sort), but §8 must not spend the whole validation window measuring only the diluted arm. Stamp `atr_penalty`/`atr_rank` as a column and forward-A/B blend-IC vs ATR-IC on the new cohort; promote the winner. We do **not** mandate pure-ATR-primary now (that magnitude is itself in-sample).

**Worked example, λ=1.0 → "high ATR costs one quality-tier":** C = strong catalyst, layer4=4, ATR 9.0 (>p90) → 4 − 1.0 = **3.0**, now below any calm layer4=4 peer (4.0). A top-tier A (layer4=5, ATR 9.0) → 4.0, ties a calm B (layer4=4) at 4.0 with `catalyst_strength` keeping A ahead — so λ=1 reorders adjacent tiers and pulls extended names down one tier, without burying a genuinely strong name. Bump λ (and version) if more bite is wanted.

---

## 5. Poolability: `scorer_config_version`

New constant + stamp in `scorer.py`, mirroring the existing keys. Encodes what makes two outcomes poolable: scorer-formula identity + `LAMBDA` + `ATR_RAMP_LO/HI` + ATR-transform identity.

```
scorer-v1-atrtilt-<formula_sha>-lam1.0-lo5.77-hi8.37   (v1 provisional)
```

- **Exactly one integrity-critical hop:** the scored dict → `enriched.to_parquet` (`thematic.py:503-504`); the brief stage re-reads and re-writes without dropping inherited columns, so the version flows to the brief parquet exactly as `insider_signal_version` does today. The attribution panel rsyncs `thematic_briefs/*.parquet` and joins outcomes ⋈ briefs **directly off the parquet** (research note §10) — it does **not** read Django. So the frozen-cohort partition depends only on this one hop; Django registration (§7) is for `/edge`/card **display** of the version chip, not the partition.
- Any change to λ / breakpoints / formula bumps the version → pre-change briefs frozen cohort, post-change new pool. **A bump resets the cohort clock to zero** (§8).
- `rank_in_day` semantics shift at cutover (becomes `selection_score`-ordered). The research note forward-tracks `rank_in_day` as a selection covariate → **partition `rank_in_day` by `scorer_config_version`** in any forward attribution.

---

## 6. ROIC / extension — promote forward, no new columns now

`roic_pct` (scorer.py:379) and `technical_pct_off_52w_low` (:387) are **already stamped on every row and already in the panel**, so the forward promotion test computes any candidate transform retroactively. We add **no shadow columns now** (v1 dropped the §6 shadow-log as YAGNI — the formula shape was undefined and the signals miss Bonferroni by ~2.4× / may never promote). If poolability ever needs a frozen snapshot, add a single column then.

---

## 7. Surfacing (brief card + version chip on /edge)

- **Brief card (briefs app)** — `CandidateCard.svelte` already renders `layer4`. Add:
  - **Score breakdown** in the deep-read drawer: layer4 components (fcff / value-or-reversal / technicals / catalyst_floor) **+ the `− ATR penalty −X.X → selection_score` line**. The precise number + `scorer_config_version` live **in the drawer**, behind a `suggestive / not yet validated` label.
  - **Coarse on-card band** for top-tercile-ATR names (raw ≥ `ATR_RAMP_LO`): a tone-neutral `extended — deprioritized` chip. **No precise number on the card** — a precise `−X.X` line in the primary sort would read as validated math, contradicting the expert-panel precedent (bands are display-only-and-out-of-sort; here ATR *is* in the sort, so the discipline lever is keeping the card coarse + the drawer honest).
  - Register new brief columns in `LEGACY_CONTRACT_COLUMNS` (briefs app schema-parity) — recurring gotcha if missed.
- **Homepage teaser** (`routes/+page.svelte`, top-8 preview of the latest brief) — auto-follows `rank_in_day`/`selection_score`, so on a >8-candidate day a high-ATR name may drop out of the 8-preview (full brief one click away, all names visible there). Already shows "open {date} brief"; optionally label the panel **"top 8 of N"** so the preview cutoff is explicit. The brief page itself needs no truncation change (shows all).
- **/edge** — add only the `scorer_config_version` chip (small; carry the single column through the monitor/ladder store or a `Brief ⋈ LadderOutcome` lookup on `(brief_date, ticker)`). Full score breakdown on `/edge` is **out of scope** (would need the Brief⋈LadderOutcome join — a separate, larger PR). The chip is the poolability boundary where outcomes are read.

---

## 8. Validation (pre-registered)

Pure ordering does not touch the monitored population, so we validate the **predictiveness of the ordering signal**, not realized human picks. **Pre-registered to avoid a moving target:**

- **Unit of N = distinct matured brief-DAYS, not rows** (a multi-ticker theme over 3 days ≈ 1 observation; car_10 = 176 rows but only 14 distinct brief-dates). Gate: **≥30 distinct matured brief-dates** on the new `scorer_config_version` cohort (~5–6 calendar weeks from a reset, not weeks-of-rows).
- **Forward A/B:** rank-IC of `selection_score` (blend) **and** `atr_rank` (pure) vs fixed-horizon CAR, both **against a `layer4`-only baseline** (layer4 carries its own positive IC, so report incremental IC), partitioned by `scorer_config_version`. Promote the stronger arm.
- **SUCCESS line:** the high-ATR tercile separates (less-bad vs the rest) with bootstrap CI excluding 0 on the post-deploy cohort, target read ~early-Aug.
- **KILL line:** no separation or sign-flip on the post-deploy cohort → bump `scorer_config_version`, revert `_BRIEF_SORT_KEYS` to `layer4`-primary (one-line edit at orchestrator.py:332), keep ATR as a display-only band. Cheap, supported revert.
- λ + breakpoints are **frozen for the horizon** (a tweak resets the cohort) — no re-tuning mid-window.

---

## 9. PR decomposition (TDD each; zen pre-merge per CLAUDE.md)

1. **Pipeline** — `atr_penalty` + `selection_score` + `atr_rank` + `scorer_config_version` + `_BRIEF_SORT_KEYS` reorder. Tests: penalty monotonic on the ramp, 0 below p67 / full above p90, NaN→0, `selection_score = layer4 − penalty`; sort places high-ATR below an equal-`layer4` low-ATR name; **version survives the WRITTEN brief parquet** (existing `test_scorer.py:289` only checks the in-memory frame); **drop_duplicates survivor identity unchanged** on a synthetic multi-theme ticker after prepending the key (guards the per-ticker-constant invariant for future maintainers). **Sort-lock lockstep edits (review B2):** add `selection_score` to `_NON_EXPERT_SORT_ALLOWLIST`, prepend it in the ordered-tuple pin, rewrite `test_primary_sort_*` with a high-ATR row (else it passes vacuously), update the docstring/comment.
2. **Django (briefs app)** — ingest + serializer for breakdown / band / version; `LEGACY_CONTRACT_COLUMNS` registration; serializer tests. **No `.order_by()` change** (rank_in_day auto-propagates). Decide: `min_score` filter **stays** `layer4_weighted_score__gte` (§2 — tilt reorders, never hides). Plus the `/edge` version-chip carry (one column).
3. **SPA** — brief-card score-breakdown drawer + coarse `extended` band + version chip; front-page "8 of N"; `/edge` version chip; whitespace-nowrap on atomic tokens; component tests / story.

Forward-only (new columns populate from the next thematic-build image rebuild on VPS; older briefs stay pre-tilt, in the old cohort by design). Deploy operator-owned (VPS image rebuild + Django pull). No backfill of old briefs.

---

## 10. Risks & mitigations

- **Overfitting the magnitude** → λ fixed + modest, breakpoints frozen + labelled provisional + versioned; honest "suggestive" labelling; pre-registered kill line (§8).
- **Manufactured authority** → coarse band on card, precise number + version in drawer behind not-validated label; min_score never moves to `selection_score`.
- **Blend dilutes ATR** → acknowledged (§4a); pure-`atr_rank` logged + forward A/B; revert is one line.
- **Homepage-teaser steer** (mild — only the top-8 preview of the latest brief; the brief page shows all, scrollable) → optionally label "top 8 of N" + the `extended` band; test set unaffected (monitoring is order-independent).
- **Improvement may be unmeasurable from realized picks** → we validate the *ordering signal's* forward IC on the monitored population instead (measurable), with a pre-registered success/kill line.
- **Future maintainer folds a theme-dependent term into `selection_score`** (would break the drop_duplicates invariant) → §9.1 regression test + §5 documented invariant.
- **ATR↔ROIC ~0.63 collinear + layer4 carries a mild anti-high-ATR tilt** → net tilt compounds slightly more than one tier; an argument *for* keeping λ=1 modest.

---

## 11. Resolved open items

- **Breakdown location:** brief-card deep-read drawer (precise) + coarse on-card band; `/edge` gets only the version chip. (§7)
- **Front page:** accept the steer, show "8 of N". (§7)
- **min_score filter:** stays on `layer4_weighted_score`. (§2, §9.2)
- **Shadow columns:** dropped (§6).
- Remaining for the plan: exact column names/dtypes + `LEGACY_CONTRACT_COLUMNS` entries; the precise `atr_rank` definition (raw penalty vs within-day percentile — pick one, stamp it in the version).

---

## 12. Adversarial review (2026-06-25) — what changed from v1

Multi-lens workflow review (5 lenses × code-grounded verification). Verdict: **sound-with-required-changes**; the central test-set claim **verified end-to-end**. Folded: B1 surfacing re-scoped to the **briefs app** (was wrongly pointed at `/edge`); B2 sort-lock lockstep test edits added to PR-1; B3 blend-dilution acknowledged + pure-`atr_rank` A/B added; S1 validation gate moved to **brief-days** + pre-registered success/kill; S3 corrected (the brief page shows ALL candidates, scrollable — only the homepage top-8 *teaser* truncates, so the steer is a mild attention/preview effect, not hiding); S4 precise number moved to drawer, card band coarse; S5 shadow-log columns dropped. The `drop_duplicates(keep="first")` survivor question resolved in the design's favor — survivor is **invariant** because `atr_penalty` is a per-ticker constant (not because layer4 is theme-independent — it is not); justification corrected + regression test added. **Refuted (not chased):** breakpoint leakage (stamp is forward-only → discovery rows excluded by construction), ATR↔ROIC steering-wrong-axis (resolves in ATR's favor), survivor-content-swap (per-ticker-constant).
