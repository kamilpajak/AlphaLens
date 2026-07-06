# Selection-score scorer-v2 (ext tilt) — design decision

**Date:** 2026-07-06
**Status:** REJECTED (scorer-v2 ext axis) / NO-CHANGE (V1R breakpoint refresh)
**Decision class:** ordering-only tilt calibration (brief sort key); no gate, cap, or trading-rule change
**Judged by:** three independent adversarial lenses (overfit, doctrine, production) — 1× approve, 2× approve-with-changes; all required changes incorporated below
**Calibration data:** `~/.alphalens/diagnostics/signal_panel.parquet` (523 rows, 50 brief_dates 2026-04-14..07-05, 415 plannable; episode-deduped to 224 ticker-episodes)

---

## 1. Decision

**NO CHANGE. Do not ship scorer-v2. Do not ship the V1R breakpoint refresh. Do not touch `SCORER_CONFIG_VERSION`.**

The live scorer stays exactly as-is:

```
selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)
SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"
```

(`apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py`, unchanged.)

Three sub-decisions:

1. **scorer-v2 ext axis (ma50-distance penalty) — REJECTED for now, DEFERRED as one pre-registered candidate.** The ext axis fails the time-split at the ordering level and no bootstrap CI vs V1 excludes zero (point estimates are on the wrong side). The single deferred candidate is **V2A**: `selection_score = layer4 − atr_penalty − ext_penalty(technical_ma50_distance_pct)`, `LAMBDA_ext = 1.0`, positive-tail-only ramp, missing→0. **No grid at the revisit.** Its future version string follows the v1 pattern: `scorer-v2-atrtilt+ext-lam1.0-lo5.77-hi8.37-elam1.0-elo<X>-ehi<Y>` — where the ATR breakpoints stay **frozen at the v1 values 5.77/8.37** (bundling an ATR recalibration would conflate two changes in one cohort key), and `elo`/`ehi` are p67/p90 of `technical_ma50_distance_pct` **frozen from data strictly before the future evaluation window** (see §2 leakage disclosure — do NOT reuse this calibration's full-panel 5.662/21.189).
2. **V1R breakpoint refresh — REJECTED.** Fresh p67/p90 on N=415 are 5.710/8.249 vs frozen 5.77/8.37: max |score delta| 0.044, **zero** top-3 churn over 38 episode-days, identical top-3 on every day. Bumping `SCORER_CONFIG_VERSION` for zero ordering change would fragment the EDGE poolability cohort and restart the ≥30-matured-brief-days clock (~5-6 weeks) for literally nothing. Pure cost.
3. **One doc-only change ships now (no version bump, no behavior change):** update the PROVISIONAL breakpoint paragraph in `selection_score.py`'s docstring to record the 2026-07-06 re-validation — fresh p67/p90 = 5.71/8.25, max score delta 0.044, zero top-3 churn, no bump warranted. This honestly closes the v1 spec's "single 4-week in-sample window" caveat. Constants and version string unchanged, so no cohort fragmentation. (Doctrine-judge required change #4.)

**The calibration panel through 2026-07-05 is hereby declared BURNT** for the ext axis and for the ATR-tilt endorsement: ~10 configs (V1, V1R, V2A-D, C_l4only, C_atrrank, C_extrank, X_press) × 3 metrics were adjudicated on the late time-split half. Any future look must use only outcomes from brief_dates strictly after 2026-07-05 and must carry this calibration's looks in program-level Bonferroni accounting, per the burnt-holdout doctrine.

## 2. Evidence

All numbers are **in-sample, episode-deduped** (415 plannable rows → 224 ticker-episodes via chained rolling 5-session dedup keeping the first row; the 231/523 same-ticker-within-3d repeats were removed before any significance claim). Outcome coverage: car_5 = 178 episodes, car_10 = 154 episodes / 23 brief-days, car_20 = 72 (context only, unused). Time split at brief_date ≤ 2026-06-06 → early 11 days / late 12 days.

### Calibration table (verbatim)

rho10 = Spearman(score, car_10); T3/R = mean car_10 of top-3-per-day vs rest; LA = fraction of bottom-quintile car_10 episodes placed in top-3 (lower better).

| config | rho10 full/early/late | rho5 full/early/late | top3_car10 full/early/late | rest_car10 full | LA full/early/late |
|---|---|---|---|---|---|
| V1 (ctrl) | .235 / .321 / .116 | .172 / .264 / .079 | −0.0003 / −0.0214 / +0.0183 | −0.0303 | .29 / .24 / .40 |
| V1R | .237 / .320 / .118 | .172 / .264 / .076 | −0.0003 / −0.0214 / +0.0183 | −0.0303 | .29 / .24 / .40 |
| V2A | .254 / .382 / .087 | .176 / .265 / .081 | −0.0005 / −0.0179 / +0.0149 | −0.0302 | .26 / .18 / .40 |
| V2B | .247 / .362 / .102 | .161 / .260 / .062 | same top-3 as V2A | −0.0302 | .26 / .18 / .40 |
| V2C | .229 / .335 / .084 | .128 / .220 / .022 | same top-3 as V2A | −0.0302 | .26 / .18 / .40 |
| V2D | .263 / .390 / .102 | .193 / .280 / .105 | same top-3 as V2A | −0.0302 | .26 / .18 / .40 |
| C_l4only | .153 / .216 / .058 | .082 / .176 / −.023 | −0.0074 / −0.0204 / +0.0042 | −0.0248 | .35 / .24 / .53 |
| C_atrrank | .288 / .524 / .042 | .286 / .431 / .194 | −0.0001 / −0.0018 / +0.0014 | −0.0305 | .13 / .06 / .40 |
| C_extrank | .257 / .433 / .092 | .075 / .149 / .057 | −0.0004 / −0.0153 / +0.0130 | −0.0303 | .32 / .24 / .40 |
| X_press (descriptive) | .258 / .388 / .088 | .175 / .272 / .079 | −0.0015 / −0.0218 / +0.0165 | −0.0294 | .26 / .18 / .40 |

The independent overfit-lens recomputation reproduced these numbers (V1 .235/.321/.117; V2A .250/.382/.081) — they are real, not transcription artifacts.

### Headline results

- **Best challenger vs V1 (V2A), top-3 mean car_10, day-block bootstrap B=2000 over the 23 outcome days:** point **−0.0002**, 95% CI **[−0.0068, +0.0045]** — includes 0, point estimate on the wrong side. V2B/C/D share V2A's top-3 among outcome-bearing episodes → same CI. V1R vs V1: identical top-3 → delta exactly 0.0000.
- **At the 2026-06-06 cut**, every ext-axis config wins the early half (rho10 .34–.39 vs .32; LA .18 vs .24) and loses the late half (rho10 .08–.10 vs .12; top-3 car_10 +0.0149 vs +0.0183) simultaneously on all three metrics.
- **However — the honest framing (overfit-judge required change #3) is not "wins early, loses late" but "no config ordering is stable under ±1-week cut placement":** at cut 2026-05-30 V2A BEATS V1 on the late half (rho10 +0.142 vs +0.110, and that with leaked breakpoints — see below); at cut 2026-06-13 both go negative (V2A −0.242, V1 −0.156) and layer4-only becomes least-bad. The clean sign-flip story is cut-dependent; **the instability itself is the anti-ship evidence.** The ship/no-ship conclusion is invariant to cut placement even though the narrative is not: no cut placement produces a stable V2 win.

### Honesty disclosures (overfit-judge required changes #1, #2, #4, #5)

1. **Challenger-side leakage, pro-null:** the EXT breakpoints tested here (elo=5.662 / ehi=21.189 = p67/p90) were computed on the **full** plannable panel N=415, **including the late evaluation half**; V1's frozen 5.77/8.37 are genuinely pre-period. This biases the comparison **toward V2** on the late half — and V2 still lost. The pre-registered ~2026-09 V2A re-test MUST freeze ext breakpoints from data strictly before its evaluation window; do not re-derive percentiles on the window being scored.
2. **The "ATR tilt earns its keep" claim is descriptive-only, not a supported claim.** C_l4only −0.0070 [−0.0185, +0.0005] is one unadjusted comparison out of a ~10-config × 3-metric grid on the same 23 days, and it **flips** when the cut moves one week later (late-half rho10: C_l4only −0.109 vs V1 −0.156 — layer4-only is better there). Keeping the v1 ATR tilt is justified by **incumbency plus the cohort-continuity cost of removal**, not by this CI. Relatedly, pure ATR rank collapses late (C_atrrank rho10 .52→.04, and −.22 at the +1wk cut) — the July memo's ρ −0.35 is heavily early-weighted and the whole tilt family may be regime-dependent.
3. **Single-regime window:** all outcome data sits in ONE contiguous window — plannable rows begin 2026-05-27 (April brief_dates carry no plannable rows) and car_10 outcome days span 2026-05-27..2026-06-18. The "early/late split" is an intra-3.5-week split inside a single market regime, **not** a regime-crossing validation.
4. **Cross-half ticker-repetition robustness:** 21 tickers span the 06-06 cut and 22/73 late episodes (30%) repeat an early-half ticker, so the halves are not fully independent. This is evaluation-side dependence, not fit-side leakage. Restricting the late half to fresh tickers only (51 episodes / 12 days) preserves the ordering (V1 +0.101 vs V2A +0.086) — repetition does not manufacture the verdict.
5. **Power and mechanism caveats:** 154 car_10 episodes / 23 days, late half = 12 days — a real ext-axis effect of this size is not detectable here; this is "not proven + unstable under cut placement", not "proven absent". The top-3 metric is blunt (8/38 days have ≤3 episodes so top-3 = whole field; V2 variants share V1's top-3 on 19/23 outcome days, so the bootstrap mostly compares identical selections). The ext signal is **not dormant** — ext_pen_ma50 active on 76/224 episodes (34%, mean 0.60 when active; corr(ATR, ma50dist)=+0.18, near-orthogonal) — it just rarely flips top-3 because layer4 is a coarse integer 1..5 (a LAMBDA=1.0 penalty = exactly one layer4 grade; sub-1.0 differences only reorder within layer4 tie groups) and per-day fields are small (median 5.5 episodes).

## 3. What was excluded and why

- **`pass_press` / press-gate axis — excluded from the score by hard invariant, and the invariant cost nothing.** `pass_press` is per-(ticker,theme); including it breaks the per-ticker-constant requirement that keeps `_sort_and_dedup_for_brief`'s `drop_duplicates(keep='first')` survivor stable for monitored outcomes (guarded by `test_sort_and_dedup.py::test_atr_tilt_does_not_change_multi_theme_dedup_survivor`). Descriptively (X_press, not deployable): press-passers = 70/224 episodes (31%), 28/112 top-3 slots under V2A; a flat 0.5 press penalty swaps only 4 top-3 episode-rows and moves top-3 mean car_10 from −0.0005 to **−0.0015** — i.e. **wrong-signed**, CI vs V1 [−0.0084, +0.0046]. The press-fade finding (−0.22/−0.26, survives ATR partial) remains real cross-sectionally but does not convert into top-3 improvement here. **Follow-up:** it routes to a **separate press-gate-respecification work item** (a SELECTION change — gate territory, own Bonferroni entry, must go through the gate-change process, never smuggled into the ordering key), explicitly carrying **no urgency signal** from this calibration.
- **`technical_rsi` — excluded by twin collinearity.** ρ 0.91 with ma50_distance; including both double-counts one factor. ma50_distance was chosen for the (deferred) V2A: continuous, unbounded upside-extension semantics vs RSI's saturation at 100. RSI stays a descriptive covariate. No silent fallback rsi-on-missing-ma50 inside one config version.
- **V2B/V2C/V2D — rejected as grid variants.** Identical top-3 to V2A among outcome-bearing episodes; keeping them alive would be pure multiplicity. The deferred candidate is V2A alone.
- **V1R — rejected** (§1: zero ordering change, cohort-fragmentation cost only).
- **C_l4only (removing the ATR tilt) — not actioned now**, but promoted to a pre-registered kill line on v1 itself (§5), since the late-half ATR collapse means the correct future change may be **removal, not addition**.

## 4. Implementation plan

### 4a. Ships now (this decision)

1. **This memo** → commit as `docs/research/selection_score_v2_ext_tilt_decision_2026_07_06.md` (or `docs/superpowers/specs/2026-07-06-selection-score-v2-ext-tilt-design.md`), Status REJECTED/NO-CHANGE, quoting the episode-deduped table verbatim (done above) — otherwise a future session re-derives the same grid against burnt data.
2. **Doc-only PR** touching `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py` docstring: PROVISIONAL breakpoint paragraph gains one dated line — "re-validated 2026-07-06: fresh p67/p90 = 5.71/8.25 (N=415 plannable), max |score delta| 0.044, zero top-3 churn over 38 episode-days; constants and version string deliberately unchanged." **No constant edits, no version bump — CI must show `SCORER_CONFIG_VERSION` untouched** (confirm in the PR diff). `test_version_string_is_pinned` stays green by construction. Skippable for zen review as doc-only.
3. **Press-gate-respecification issue** filed separately, quoting the §3 descriptive result.

### 4b. Preserved for the pre-registered ~2026-09 V2A revisit (do NOT re-derive)

Verified implementation map (every site checked in-repo by the production lens), in PR order **pipeline → Django → SPA**, TDD red-first + zen pre-merge each:

**PR-1 — pipeline.** Tests first:
- `apps/alphalens-research/tests/thematic/screening/test_selection_score.py::TestExtPenalty` — zero at/below `EXT_RAMP_LO`; full `LAMBDA_EXT` at/above `EXT_RAMP_HI`; linear midpoint; monotonic; None/NaN/non-float→0; **negative ma50_distance→0** (positive-tail-only ramp — deep-below-MA50 is the deep-drawdown-reversal population layer4 deliberately rewards; an |value| ramp would fight `compose_weighted_score`).
- `TestSelectionScore::test_version_string_is_pinned` updated to the new exact v2 literal (lockstep edit, it pins the literal).
- New test: score subtracts BOTH terms; calm+non-extended name keeps layer4 exactly.
- `test_scorer.py`: add `ext_penalty` to the expected-columns pin (:318-323); `test_selection_score_equals_layer4_minus_atr_penalty` becomes minus-both; ext analogue of the ramp-lo-zero test; add `ext_penalty` to the parquet-roundtrip assertion.
- `test_sort_and_dedup.py`: extend `test_atr_tilt_does_not_change_multi_theme_dedup_survivor` — same-ticker theme-rows carry identical `ext_penalty`, survivor identity unchanged. `_row` helper (:43-46) mirrors the scorer fallback — keep in sync.

Then implement: `selection_score.py` — `EXT_RAMP_LO/HI` + `LAMBDA_EXT` + `ext_penalty()` (same ramp shape as `atr_penalty`, None/NaN→0), `selection_score()` subtracts both, bump `SCORER_CONFIG_VERSION`, docstring invariant paragraph covers both penalty inputs (`technical_ma50_distance_pct` is (ticker,asof)-only, same as ATR). `scorer.py:609-614` — stamp `enrichment['ext_penalty'] = enrichment['technical_ma50_distance_pct'].map(ext_penalty)` and subtract both (input already stamped at :390 — **zero new data acquisition**; version stamp at :375 auto-picks-up). Orchestrator: NO functional change (`_BRIEF_SORT_KEYS` stays selection_score-primary; do NOT add to `_EMPTY_OUT_COLUMNS`). Golden: re-record `tests/golden/fixtures/score_day/golden/projection.json` via `scripts/record_golden_score.py` (pins `sorted(scored.columns)`) — do not weaken the projection.

**PR-2 — Django (near-zero work, model-driven ingest).** `briefs/models.py` add `ext_penalty = models.FloatField(null=True, blank=True)` + migration `0015` (verify latest at revisit time) + register `'ext_penalty'` in `LEGACY_CONTRACT_COLUMNS` (`test_schema_parity.py`) **in the same PR** or `test_no_orphan_brief_fields` reddens. Tests first: `test_models` field-exists; `test_ingest` three-case contract (float ingests / absent column→None / NaN→None, template at :591/:613/:630). No `parquet.py`/`coerce.py`/serializer/views edits — ingest iterates model fields, serializers `exclude=("pk",)` auto-serve, `min_score` stays on layer4 per doctrine. Do NOT route through `coerce_finite_float`. **/edge needs nothing** — only `scorer_config_version` crosses (via `_PROVENANCE_COLUMNS`); the bumped string partitions automatically.

**PR-3 — SPA.** `types.ts` add `ext_penalty: number | null`; regenerate `api-types.gen.ts` via `pnpm run gen:api-types` (refresh committed schema first; never hand-edit); `contract.test.ts:93-95` add the field; `CandidateCard.svelte` broaden extended-chip guard to `(atr_penalty ?? 0) > 0 || (ext_penalty ?? 0) > 0` **and update the hand-written mirror `showsExtendedChip` in `candidateCard.test.ts` IN LOCKSTEP** (it duplicates the template guard by hand — else it passes vacuously); `ExpertPanel.svelte` add `extPenalty` prop, OR it into `hasScoreBreakdown` (:47-48), and **refactor the hardcoded dt/dd breakdown to a penalties array `[{label, value}]`** so a v3 term is data-only. `edge.test.ts` unchanged.

**Deploy (forward-only, operator-owned):** score stage runs inside the VPS-**local** `alphalens-pipeline:latest` Docker image (thematic-build unit) — **image rebuild required, NOT host-venv git pull**; rebuild **outside** the 6×/day HH:30 slots (timer-races-half-rolled-image gotcha from the O'Neil-R deploy). Django via GHCR `compose pull && up -d` (migration auto-applies). SPA via CF Pages auto-deploy. Old parquets lack `ext_penalty` → ingest None, sort falls back gracefully; no backfill. Worktree gotcha: pipeline edits need their own `uv sync` or tests import the main-checkout copy (verify via `module.__file__`). Do NOT rename `atr_penalty`/`selection_score` — /edge chip, SPA props, contract lists, and the v1 EDGE cohort key on them; v2 is additive column + formula change + version bump.

## 5. Validation plan

**Pre-registration (write BEFORE looking at any new data):**
- **One candidate only: V2A** (`ext_penalty` on `technical_ma50_distance_pct`, `LAMBDA_ext = 1.0`, positive-tail ramp, no grid). Ext breakpoints = p67/p90 frozen from episode-deduped data **strictly before** the evaluation window; ATR breakpoints stay frozen at 5.77/8.37.
- **Holdout: fresh data only** — outcomes from brief_dates strictly after 2026-07-05. The late half used here is burnt and must NOT be re-used. This calibration's ~10-config × 3-metric look count enters program-level Bonferroni accounting for the revisit.
- **Trigger gates (both required):** (a) ≥40 car_10 brief-days of post-2026-07-05 outcomes (~2026-09 at current cadence); (b) **regime diversity** — the new window must span a genuinely different calendar regime than the burnt 2026-05-27..06-18 block (per the overfit lens: an N-only gate would repeat the single-regime weakness). If the market has not moved regimes by 2026-09, wait.
- **Success line (V2A ships):** day-block-bootstrap top-3 mean car_10 delta vs V1 with 95% CI excluding 0 on the positive side, on episode-deduped fresh data, robust to ±1-week cut placement.
- **Pre-registered kill/re-check line for the EXISTING v1 ATR tilt at the same look:** re-run the ATR-only control (C_atrrank) and C_l4only on fresh data FIRST, before any v2 evaluation. If fresh-data ATR-rank rho10 ≤ 0, or C_l4only is no longer worse than V1, the correct change is **retiring the tilt** (revert to layer4-only) — itself a `SCORER_CONFIG_VERSION` bump with its own cohort reset. The v1 tilt does not get grandfather status; its current support is descriptive-only (§2).

**Forward EDGE comparison mechanics:** `scorer_config_version` is the poolability key — v1 outcomes stay a frozen cohort; any shipped v2 accrues its own from zero. Partition `rank_in_day` by `scorer_config_version` in all forward attribution (rank semantics shift at cutover); the cohort partition rides only the parquet hop (`thematic.py` enriched.to_parquet), not Django. **Sample-size gate before ANY further tuning:** ≥30 distinct matured brief-days in the v2 cohort (~5-6 weeks post-cutover) before the first v2-vs-v1 EDGE look; no parameter touching between cutover and that look.

## 6. Risks & rollback

**Risks of the NO-CHANGE decision:**
- **Foregone ext-axis alpha.** If the ext signal is real, we delay ~2 months. Mitigated: the effect was not detectable at current power, the point estimate vs V1 was negative, and the deferred pre-registration preserves the hypothesis cleanly.
- **The v1 ATR tilt may itself be decaying** (C_atrrank .52→.04→−.22 across cuts; single-regime evidence base). Mitigated by the pre-registered retirement criterion in §5 — this is the largest real risk and it now has a dated kill line instead of grandfather status.
- **Memory decay / re-derivation.** A future session could re-grid against the burnt panel. Mitigated by this memo (burnt declaration + preserved implementation map + verbatim table).
- **Press-fade finding goes stale.** Routed to its own issue with the descriptive numbers; explicitly no urgency, so low cost if it waits.

**Risks avoided by not shipping:**
- Winner's-curse deployment of a config whose best bootstrap CI includes 0 with a wrong-signed point estimate.
- EDGE cohort fragmentation: a version bump resets the ≥30-matured-brief-days clock while the v1 cohort (live only since 2026-06-25) has not yet reached its own first verdict — paying that for V1R's exactly-zero ordering change or V2's negative point estimate is strictly negative expected value.

**Rollback:**
- Of this decision: nothing to roll back — no behavioral code ships. The doc-only docstring PR reverts trivially and cannot change ordering (guarded by the pinned version-literal test and zero constant edits).
- Of a future shipped V2A: forward-only revert — restore the v1 formula and **bump `SCORER_CONFIG_VERSION` again** (e.g. `scorer-v3-atrtilt-...`; never reuse a version string — cohorts must stay partitionable). The `ext_penalty` column stays in the parquet/Django/SPA contract as a nullable dead column (columns are additive; removal would trip `test_every_contract_column_is_modeled_or_dropped` and the SPA contract pins — register a drop instead if ever cleaned up). Old v2-cohort briefs stay a frozen EDGE cohort; no backfill, no data migration.
