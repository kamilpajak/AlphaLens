# event_drift v4 PEAD x Earnings-Quality on S&P 1500 — Design Memo

**Status**: pre-registered 2026-05-03, Phase 2 breadth audit pending
**Pre-reg id**: `event_drift_v4_pead_quality_sp1500`
**Class**: `event_drift_search_2026_05_03` (in-class extension; 2/2 attempts after v3 ABANDONED)
**Threshold**: `|t| >= 3.50` (program meta-Bonferroni n=20)

## Context

v3 (`event_drift_v3_pead_quality_clean`) ABANDONED 2026-05-03 at Phase 2 breadth audit on R2000-PIT. Primary attempt mean breadth 3.84/day (gate ≥10), retry (sue_quartile + no Day-1) 9.80/day (still <10, p10=3 vs ≥5 gate). Root cause: 71.9% data attrition (7163 of 9967) computing Sloan accruals on R2000 small-caps — sparsely tagged US-GAAP concepts (`LongTermDebtCurrent`, `IncomeTaxesPayable`, `DepreciationAndAmortization`). No holdout run, no Bonferroni budget burned. Class closed under v3's pre-committed `fail_classification.breadth_collapse` rule.

v3's postmortem (`docs/research/event_drift_v3_breadth_audit_2026_05_03.md` line 121-125) explicitly identified the pivot: *"Future PEAD-class experiments should use a universe with denser fundamentals tagging (e.g., S&P 1500 + Russell Top 1000) before re-attempting accruals conditioning."* v4 executes this pre-committed contingency. Universe-pivot is NOT post-hoc HARKing; it is the ex-ante recovery path written into v3's closing diagnostic.

Foster SUE plumbing (`alphalens.data.fundamentals.sue.FosterSUEStore`) verified defect-free across 9967 v3 invocations; reusable end-to-end. Sole change vs v3: universe.

## Adversarial review history (v4)

**Perplexity Sonar Reasoning Pro (high-effort, 2026-05-03)**: ranked PEAD on dense universe #1 by ex-ante info-value-per-Bonferroni-cost vs alternatives (iVolatility v10 sunk-cost trap; alt-data v2 holdout Lasso-zero collapse predicted). Cited Mclean post-2021 PEAD decay ~40-50% in mid-cap; Engelberg-Sasseville-Williams 2021 baseline +2.0-2.5 αt. Honest expected ceiling for v4 mid-cap dense universe: **+1.8 mean αt** — likely program-bar FAIL but cleanest available diagnostic of PEAD-class viability.

**Zen gemini-3-pro-preview (high thinking, 2026-05-03)**: AGREE v4 pivot grounded; AGREE rejection of GAAP-tag-cascading on R2000 (HARKing-on-data-engineering risk). **DISSENT on universe**: recommended S&P 600 small-cap-only (concentrate where PEAD literature places anomaly; S&P 500 PEAD largely arbitraged away → S&P 1500 dilutes signal). User elected to override on 2026-05-03, kept S&P 1500 for safer breadth (~22/day expected vs ~8/day for S&P 600 alone) accepting lower αt ceiling. Contingency: if v4 produces mid-strength signal (αt 1.0-1.8) with confirmed adequate breadth, v5 should re-test on S&P 600 isolated to validate the dilution hypothesis.

## Thesis (unchanged from v3)

Foster (1977) standardised unexpected earnings predict drift in the [d+2, d+60] post-announcement window. Sloan (1996) accrual quality conditions the magnitude: low-accrual-ratio firms (high earnings quality, cash flow exceeds reported earnings) exhibit MORE drift because the market under-reacts to high-quality positive surprises. Bernard-Thomas (1989) day-1 reaction sign confirms the surprise direction is intact (forward guidance has not destroyed the historical surprise).

Long top-quintile Foster-SUE firms whose accruals are below-median (highest earnings quality) and whose day-1 reaction sign matches the SUE sign. Hold equal-weight for [d+2, d+60]. NO regime conditioning. Universe ex Financials/Utilities (Sloan accruals destabilise on banks/REITs). Single-active-window invariant per ticker.

## Why v4 is in-class extension (NOT fresh class)

- **Same scoring logic**: `alphalens.screeners.event_drift.score_pead_quality` reused verbatim
- **Same hypothesis**: PEAD × Sloan-quality × Day-1 confirmation, same selection rule
- **Same holdout window**: 2024-04-30 → 2026-04-30 (NOT burnt by v3 abandon)
- **Sole change**: universe (R2000-PIT → S&P 1500-PIT)

Per `feedback_burnt_holdout_multiplicity.md` (2026-05-01): "Pure model-class swap on identical data inputs does NOT cleanse multiplicity." Universe swap on identical hypothesis/scoring is mechanistically analogous and inherits in-class Bonferroni accounting. Class count: 2 attempts (v3 ABANDONED + v4). Program n: 20 (19 prior completed + v4 attempt; v3 abandon does not increment program n per ledger convention but conceptually contributes to selection bias).

## Pre-reg locked parameters

```
sue_quintile = 5 (top 20%)             # unchanged from v3
accrual_quantile_bottom_pct = 50       # unchanged (below median = high quality per Sloan 1996)
quantile_cohort_window_days = 90       # unchanged (trailing rolling cohort)
holding_window_days = [2, 60]          # unchanged
day1_sign_confirmation = True          # unchanged
t0_after_hours_rule = "filed_time >= 16:00 ET -> entry = next regular open + 1"  # unchanged
overlapping_window_policy = "single_active_window_per_ticker"  # unchanged
sector_exclusions_gics = [40, 55]      # unchanged (Financials, Utilities)
adv_min_usd = 5_000_000                # unchanged (60d trailing)
universe = "sp1500_pit ex Fin/Util"    # CHANGED from edgar_companyfacts_covered (R2000-PIT)
benchmark = MDY                        # unchanged (mid-cap proxy)
cost_profile = long_only_30bps         # unchanged
```

## Threshold rationale

Program n=20 (19 prior completed + v4) → naive Bonferroni one-sided α=0.025/20 = critical |t| ≈ 3.34. v3 escalated naive 3.30 to 3.50 (+0.20) for meta-multiplicity (20-th reuse of holdout + cross-class selection bias). v4 retains **|t| ≥ 3.50** because:

- Universe pivot was pre-committed in v3's `breadth_audit` postmortem (lines 121-125), NOT a post-hoc cherry-pick → no additional cross-universe selection multiplicity
- v3 did NOT burn holdout (no Bonferroni cost incurred) → naive escalation already absorbs v4's increment
- Retaining 3.50 keeps cross-test threshold consistency with v3's framing

Phase-dispersion gate `alpha_t_range across 5 phases <= 0.5` (per amended v9 gate, more informative than 50pp `excess_net_ann` legacy gate).

## Cardinality budget (v4 ex-ante)

| Step | Count (v3 R2000) | Count (v4 S&P 1500) | Notes |
|------|------:|------:|-------|
| Universe (PIT yaml union) | 1612 | ~1500 | R2000 vs S&P 1500 |
| Ex Financials/Utilities | ~1090 | ~1260 | S&P 500/400/600 fin+util ~16% |
| With valid Foster-SUE & accruals | ~760 (47%) | ~1130 (90%)* | *Denser GAAP tagging via S&P committee inclusion |
| Active in 60d PEAD window | ~500 | ~745 | quarterly cycle 60/91 |
| Top-quintile SUE (trailing-90d cohort) | ~100 | ~149 | universe / 5 |
| ∩ below-median accruals | ~50 | ~75 | / 2 |
| ∩ Day-1 sign confirmed | ~30 | **~45** | ~60% pass rate per Bartov-Radhakrishnan-Krinsky 2000 |

Pre-flight breadth gate: mean daily portfolio ≥ 10, p10 ≥ 5. v4 ex-ante mean ~45 → expect comfortable PASS. (v3 actual primary 3.84 vs ex-ante 30: 87% miss attributed entirely to coverage attrition.)

## Pre-committed retry policy

If primary fails breadth gate, ONE retry permitted with single-axis relaxation (per v3 `fail_classification.breadth_collapse`):

- **Retry option A**: sue_top_pct: 25 (vs 20) — same axis as v3 retry
- **Retry option B**: day1_sign_confirmation: DISABLED — same axis as v3 retry

Pick whichever single relaxation is closer to the gate. If still <10/day mean OR p10 <5, **class closes** (consistent with v3 closure mechanism applied to v4's universe pivot).

## Auto-pivot triggers (during execution)

Inherited from v3 with one v4-specific addition:

- mean daily breadth < 10 in Phase 2 audit → ABORT pre-holdout-run (retry once, then close)
- holdout single-phase αt < 1.5 → ABORT pre-multi-phase
- Day-1 sign confirmation gate retains <30% candidates → diagnostic regime-shift flag
- any phase αt < 0.5 → FAIL (regime fragility)
- αt range > 0.5 across 5 phases → FAIL (phase aliasing)
- **NEW v4-specific**: if breadth PASS but holdout αt in [1.0, 1.8] range → register diagnostic finding "PEAD lives, S&P 500 dilutes per zen prediction"; v5 candidate = repeat on S&P 600-only

## Capital deploy clause

OFF-TABLE on this burnt holdout regardless of verdict. PASS at |t|≥3.50 triggers prospective walk-forward replication on data accruing post-2026-04-30 at unadjusted p<0.05 single-test before any escalation.

## Phase plan

- **Phase 0** (this memo + ledger lock): in progress
- **Phase 1** — universe wiring (`alphalens/data/universes/sp1500_pit.py` + tests): pending
- **Phase 2** — breadth audit (pre-committed early-stop): pending
- **Phase 3** — holdout single-shot (if breadth PASS): gated
- **Phase 4** — 5-phase robustness (if holdout in-class PASS): gated
- **Phase 5** — ledger update + verdict + memory write: gated

## Reusable modules (verified Explore 2026-05-03)

All under `/Users/jacoren/Developer/Personal/AlphaLens/alphalens/`:

- `screeners/event_drift/score_pead_quality.py` — locked v3 scorer, no changes
- `screeners/event_drift/accruals.py` — `SloanAccrualsStore` (will perform better on dense S&P 1500 GAAP tagging)
- `screeners/event_drift/announcement_dates.py` — `AnnouncementDateProvider` (8-K + 10-Q/K parsing)
- `screeners/event_drift/event_window.py` — `EventWindow` (cohort + single-active-window invariant)
- `screeners/event_drift/t0_timing.py` — after-hours 8-K T0 disambiguation
- `screeners/event_drift/day1_filter.py` — Bernard-Thomas Day-1 gate
- `screeners/event_drift/sector_filter.py` — GICS exclusion
- `data/fundamentals/sue.py` — `FosterSUEStore` (9967/9967 v3 invocations defect-free)
- `backtest/multi_phase.py` — phase-robust audit driver
- `attribution/cost_model.py`, `attribution/factor_analysis.py` — verdict pipeline

## Out-of-scope rejections (consulted perplexity + zen, 2026-05-03)

- **iVolatility v10 (options-implied smd-primary)** — REJECT. Trial deadline 2026-05-08 creates sunk-cost trap; v7+v8+v9D triangulation to +2.2-2.3 αt = selection-bias-as-discovered-ceiling; IV-skew NOT validated as return predictor in 2023-2025 literature. iVolatility decision DEFERRED to 2026-05-07 per user; default action absent new v4 evidence is cancel.
- **alt-data v2 holdout** — DEFER. Prior class 3/3 zero-coef Lasso predicts repeat collapse; Polygon short-interest unvetted in academic literature (Diether-Lee-Werner 2009 covers short *constraints* not generic feeds). Cheap to run; revisit only after v4 produces clean diagnostic.

## Honest expectations (anchor for self-review)

- Breadth audit PASS probability: **~80%** (S&P 1500 GAAP coverage materially denser than R2000)
- Holdout in-class PASS probability (αt ≥ 1.96 at FWER α=0.05 in-class n=2): **~25-35%**
- Program-bar PASS probability (αt ≥ 3.50): **<10%** (PEAD post-2021 decay literature ceiling at +1.8 mean per perplexity)
- Methodological value if FAIL: **HIGH** — closes event-drift class with clean diagnostic of PEAD viability on dense universe; validates project's "search remains open but Bonferroni honest" stance; informs whether v5 pivot to S&P 600 isolated is warranted

## Diagnostic output files (will be created)

- `/tmp/event_drift_breadth_v4_full.json` — primary attempt summary
- `/tmp/event_drift_breadth_v4_retry.json` — retry summary (if needed)
- `docs/backtest/event_drift_v4_holdout_2026_05_DD.txt` — holdout artifact (if breadth PASS)
- `docs/backtest/event_drift_v4_multiphase_2026_05_DD.txt` — multi-phase audit (if holdout in-class PASS)
- `docs/research/event_drift_v4_postmortem_2026_05_DD.md` — verdict memo
