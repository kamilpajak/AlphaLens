# Options retro pattern sweep — exploratory appendix

**Status:** COMPLETE — exploratory sweep on the BURNT pilot panel; kill-evidence only, zero new signals
**Date:** 2026-07-09 (same day as the pilot, same frozen panel)
**Parent:** [`options_retro_pilot_results_2026_07_09.md`](options_retro_pilot_results_2026_07_09.md)
**Method:** multi-agent workflow — 5 finder lenses (nonlinearity, IV dynamics, subgroup conditioning, incremental-over-ATR/ma50, time/regime) x adversarial verification (independent reproduction + episode dedup + wild cluster bootstrap + ATR partial + drop-influential-days), 26 agents total. 15 candidates found, 10 verified, 4 hold (all null/meta claims), 2 weakened, 4 refuted.
**Look accounting:** ~100-130 specifications examined on the one burnt 148-episode panel — recorded on the `options_retro_pilot_2026_07` ledger entry. NOTHING here is a discovery; the panel cannot be reused for confirmation.

---

# Options retro pilot — pattern-sweep synthesis (2026-07-09 panel, EXPLORATORY)

Sample: frozen pilot panel (319 rows → 149 episodes after `ticker_episode_dedup` → 148 complete-case), 26 brief-day clusters, window 2026-05-27..06-22 only. All inference: episode dedup + brief-day wild cluster bootstrap (n_boot ≥ 999) + ATR partial. **Nothing below is a discovery.**

## 1. Survivors (status HOLDS after adversarial verification)

All four survivors are **null / meta / robustness claims**. Zero positive tradable signals survived.

| Name | Claim (post-verification) | Key numbers | Why it survived |
|---|---|---|---|
| `vol-levels-are-atr-restated` | Options vol LEVELS (ivx30, hv20) are ATR restated; zero incremental forward info. Kill evidence for any level-based options feature. | R²(feat~ATR) 0.74/0.78; corr 0.86/0.89. Forward with ATR+ma50+mcap: ivx30 β/SD −0.0026 (p_wcb 0.928), hv20 +0.0187 (p_wcb 0.485). Sign-flips in both split-halves. | Byte-for-byte independent reproduction; a null claim that survives all five kill attempts. Verifier found no channel overturning it. |
| `half-split-slope-jump-is-noise` | The H1→H2 term-slope β jump (0.32→0.87) is noise-consistent — no time trend, no H2 interaction, no regime shift. | slope×tidx p_wcb 0.443; slope×is_H2 p_wcb 0.395; day-FE version 0.349; rolling betas non-monotone (pass through ~0 mid-window). | Exact reproduction to 3 decimals; the "jump" story fails every interaction test under clustering. |
| `market-regime-confounded-with-time-untestable` | SPY rv10 doubled exactly at the sample midpoint (0.107→0.201, corr with time 0.876) — time vs market-state attribution is structurally impossible in this window; direct vol/dd interactions null anyway. | slope×rv10_c p_wcb 0.334 (seed-robust); slope×spy_dd p_wcb 0.585. | Reproduced from raw SPY closes; negative/meta claim, structurally sound. Treat any future "slope works in high vol" story as UNTESTED. |
| `no-mechanical-artifact-slope-is-within-day` | The pinned term-slope near-miss is cross-sectional *within* brief-day, not a day-level baseline artifact; day-of-week / cohort-size channels clean. | Day-FE (within-day demeaned) slope β +0.560, t_cr2 2.73, p_wcb ~0.018–0.021 (seed-stable); +mcap 0.011–0.013. Pooled 0.0495. Mild car_10 drift (+0.31%/day, p_wcb 0.065) cannot manufacture it. | Reproduced across seeds; explicitly framed as robustness on the ALREADY-PINNED hypothesis, not a new discovery. But see finding 4's artifact note below — the within-day effect still concentrates in 3 brief-days / 2 sector complexes. |

## 2. Weakened / refuted

| Name | Status | One-line kill reason |
|---|---|---|
| `ivx30-concavity-extreme-iv-drag` | REFUTED | The extreme-IV tail is one co-moving speculative complex (space/meme names) on 3 adjacent brief-days with ~80%-overlapping k=10 windows — one market episode wearing a cross-sectional costume; rank/quintile versions die; p_wcb 0.034 doesn't clear the family bar. |
| `runup10-fade-replicates-at-10d-horizon` | REFUTED | Repackaging of a known separator: runup10 corr 0.73 with `technical_ma50_distance_pct`; the run-up fade itself is on the do-not-rediscover list (2026-06-23). |
| `term-slope-effect-homogeneous-no-concentration` | REFUTED | The homogeneity claim is mechanically false — the effect concentrates in 2 sector episodes on 3 brief-days (2026-06-13/05-30/06-16) that median-split interactions could never localize; pooled near-miss itself reproduces fine. |
| `ivx30-null-in-every-subgroup` | WEAKENED | The null stands, but "no cell approaches significance" is power-by-construction: corr(ivx30, ATR)=0.86 means the ATR partial removes ~¾ of ivx30 variance in n=52–74 cells — the test could hardly have found anything. |
| `ivp30-signal-is-slope-restated` | WEAKENED | Core reproduces exactly (joint ivp30 β +0.0009, p_wcb 0.946) and ivp30 is not an independent channel; weakened only because corr(ivp30, slope) = −0.63 collinearity makes the "which of the two carries it" attribution model-dependent. |
| `ivp30-high-percentile-drag` | REFUTED | One early-June 2026 speculative-sector crash episode amplified by pseudo-independent overlapping-window clusters; p=0.052 only at the mined cutoff of exactly 80 (sweep 70–90: p 0.05–0.17). |

## 3. Dead ends worth recording (from finders, do not re-run)

- **VRP (iv30−hv20)**: null (β/SD −0.007, p_wcb 0.62) — matches pinned prior.
- **volgap (hv20 − annualized ATR)**: mechanically collinear with hv20 once ATR is a control; nothing.
- **IV dynamics (5d/10d changes)**: d5_hv20 p_wcb 0.675; IV-vs-HV divergence trend p_wcb 0.234; ~30 spec combos, none clear under the full doctrine.
- **Term-slope nonlinearity**: illusory — inverted-term-structure dummy (p 0.025 alone) collapses when linear slope added (flips sign, p 0.418); slope quadratic exactly null (p 0.996); slope×ivx30 interaction null.
- **hv20 nonlinearity**: null everywhere (quadratic p 0.121, extreme-vs-middle p 0.187).
- **Low-IV bounce (ivp30<10/<20)**: n=5/9 — untestable.
- **earnings_within_30d subgroup**: 3 episodes after dedup — untestable in this panel.
- **NO_FILL subgroup slope**: 22 episodes < 30 — refused (interaction with filled was testable and null).
- **Calendar/mechanical channels**: day-of-week (min p_wcb 0.26), cohort size, SPY drawdown level — all null.

## 4. Multiplicity ledger

Declared `tests_run_count` across the 10 verified patterns: **87 specs**. Finder dead-end notes additionally cite ~30 dynamics-lens and ~20 regime-lens spec combos partially outside the declared counts, so the honest total is **on the order of 100–130 examined specifications** on ONE 148-episode / 26-cluster / 4-week panel with heavily overlapping outcome windows. At that search width, even the best in-sample p_wcb values (0.014–0.049) are exactly what noise produces. **Nothing in this sweep is a discovery.** Any survivor is a hypothesis-generation note only; before it may influence selection, ordering, display, or any live surface, it requires fresh pre-registration in September-#774 style (frozen spec, forward/fresh data, t > 3, family declared in advance).

Structural caveats that apply to everything above: single 4-week window; one vol-regime break co-located with the sample midpoint; k=10 outcome windows overlap ~80–90% across adjacent brief-days (26 clusters overstate independence); one speculative-sector crash episode contaminates every tail-driven pattern.

## 5. Pre-registration sketches (max two; neither is endorsed)

**Sketch A — term-slope within-day cross-sectional test (the only live thread).**
- Hypothesis: steeper ivx180−ivx30 term structure (contango) at brief time predicts higher car_10, cross-sectionally within brief-day.
- Data: matured episodes with brief_date ≥ 2026-07-09 (strictly post-freeze; the current panel is burnt), minimum N ≥ 150 episodes AND ≥ 45 brief-day clusters AND ≥ 5 distinct GICS-ish sector groups in the top-decile slope cells (guards against the finding-4 concentration failure).
- Spec (frozen): `ticker_episode_dedup` → within-brief-day demean car_10 / slope / ATR / log10_mcap → `cluster_ols(car_10_w, [1, slope_w, atr_w, mcap_w], clusters=brief_date)` → `wild_cluster_bootstrap_p(coef_idx=1, n_boot=4999, seed=0)`.
- Success: β > 0 with t_cr2 > 3. Kill conditions (pre-declared): sign flips when the 3 most influential brief-day clusters are dropped, OR effect vanishes under a sector-episode dedup coarser than ticker-episode.
- Family: this ONE test; Bonferroni against the program-level ledger.

**Sketch B — defensive extreme-IV exclusion (weak; only if A is run anyway).**
- Hypothesis: episodes with ivx30 > 1.0 (>100% annualized) at brief time have negative mean car_10 vs the rest — as an *exclusion filter*, not a signal.
- Same fresh-data window and dedup as A. Spec: `car_10 ~ 1 + 1{ivx30>1.0} + technical_atr_pct + z(atr)² + log10_mcap`, brief-day clusters, WCB n_boot=4999. Fixed cutoff 1.0 — no sweep. Precondition before the test counts: ≥ 25 treated episodes spanning ≥ 4 sectors and ≥ 10 brief-days (the in-sample version was one sector crash; if the diversity gate fails, the test is refused, not run).
- Honest prior: likely dies — the in-sample evidence was refuted as a single-episode artifact and the level channel is ATR-restated. Register only if the exclusion would have operational value regardless (position-risk hygiene).

Bottom line: the sweep's durable output is **kill evidence** (vol levels = ATR restated; ivp30 = slope restated; no subgroup rescue; no regime story testable in this window) plus one already-pinned near-miss (term slope) that gained a within-day robustness note but also a concentration warning. No new signal exists here.
