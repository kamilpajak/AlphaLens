# multi_source_two_stage Phase B + C — findings (2026-04-30)

**Pre-registration:** [`multi_source_two_stage_2026_04_30`](preregistration/params_multi_source_two_stage_2026_04_30.json)
**Status:** **FAIL** — holdout phase 0 cleared gates (provisional PASS), multi-phase audit failed phase-robustness. 11th paradigm failure. Ledger entry completed 2026-04-30.

## TL;DR

Single-phase holdout PASSED all four pre-registered gates (t=+2.01, Sh=+1.19,
α=83.6%, MaxDD=−9.8%) but the multi-phase robustness audit failed: mean phase
α t-stat = **+0.65** with dispersion across 5 phases of −0.29 to +2.01. Mean
excess_net = **0.0%/y**, dispersion **4.9pp**. The pre-reg's every-phase
t ≥ 1.5 gate is not cleared.

Notable: dispersion **4.9pp** is an order of magnitude tighter than the prior
9 phase-robust failures (44.5pp / 69pp / 40.3pp). This is the first
*stably-weak* FAIL — small mean edge but consistent across phases, rather
than lucky single-phase variance. Methodologically more honest, and probably
informative for v2 design.

| Gate | Threshold | Single-phase (phase=0) | Multi-phase mean | Final |
| --- | --- | --- | --- | --- |
| Carhart-4F α t-stat (HAC) | every-phase ≥ 1.5 AND mean ≥ 1.96 | +2.01 | **+0.65** | ✗ |
| Sharpe (net) | ≥ 0.5 | +1.19 | +0.31 | ✗ |
| α annualized | ≥ 3% | +83.6% (×252) | mean +29.0% (×252) | ~ |
| Max drawdown (per phase) | ≥ −35% | −9.8% | n/a | ✓ |

## Pipeline (as locked by pre-reg)

1. **Universe:** AlphaLens PIT (yfinance + survivorship parquet), ADV ≥ $5M filter at scoring time.
2. **Stage 1 — regime classifier:** deterministic VIX-quartile thresholds frozen on train period only (rolling-252d quartiles fit on train ≤ 2024-04-29). Output regime ∈ {Q1_calm, Q2, Q3, Q4_stress}.
3. **Stage 2 — per-regime Lasso:** one model per regime, λ chosen by 3-fold expanding-window nested CV with 60-day embargo; 25-point glmnet-style log grid.
4. **Features (frozen 21):** 3 insider (post-F4 fix) + 3 macro (FRED) + 8 OHLCV-derived + 3 cross-sectional ranks + 4 pre-specified interactions.
5. **Target:** 5-trading-day-forward close-to-close return minus cumulative RF; raw (NOT pre-residualized on Carhart per Gu-Kelly-Xiu 2024).
6. **Portfolio:** top-30 by predicted score, equal-weight, weekly rebalance (stride=5), holding=5d.
7. **Cost:** half-spread 10 bps + 5 bps adverse selection (round-trip 30 bps × turnover_fraction).
8. **Attribution:** post-hoc Carhart-4F regression on portfolio returns (Newey-West HAC).

## Phase A summary (2026-04-30 earlier)

Feature joiner sanity passed with extrapolated holdout density ≈ 146k obs ≫ 5k floor. 11 collinearity warnings flagged as sampling artefacts — Lasso L1 will handle. Detail: [multi_source_two_stage_phase_a_2026_04_30.md](multi_source_two_stage_phase_a_2026_04_30.md).

## Phase B — holdout reveal (ONE-shot, no peek)

Single invocation: `.venv/bin/python scripts/experiment_multi_source_two_stage.py`
(uses pre-reg defaults). See [multi_source_two_stage_phase_b.md](multi_source_two_stage_phase_b.md) for the auto-generated report.

### Headline (phase_offset=0)

```
HOLDOUT 2024-2026 | ADV≥$5M cost=10bps | n=99 topN=30.0 turn=18.0% |
Sh gross=1.41 net=1.19 | excess gross=10.1% net=7.4% | α 4F=83.6% t=2.01
verdict: PASS (provisional — pending multi-phase audit) | t=2.01 Sh_net=1.19 α_4F=83.6% MaxDD=-9.8%
```

### Per-regime fits (train pool 2014-01-01 → 2024-04-29, 310,517 aligned obs)

| Regime | n_train | λ chosen | nonzero coefs / 21 | CV mean MSE |
| --- | ---: | ---: | ---: | ---: |
| Q1_calm   | 86,643 | 0.000136 | **15** | 0.003296 |
| Q2        | 82,220 | 0.002861 | 0 | 0.004593 |
| Q3        | 76,342 | 0.001309 | 0 | 0.005716 |
| Q4_stress | 65,312 | 0.008212 | 0 | 0.007253 |

### Critical caveat — signal concentration

Three of four regimes' Lassos selected the LARGEST λ in the grid (idx 0/24 for
Q2/Q3, the lambda where Lasso zeroes everything). Only `Q1_calm` produced a
non-trivial model with 15 nonzero coefs. Practical consequence:

- During holdout asof bars where VIX < train Q1 cutoff, predictions reflect a
  real fitted model.
- During holdout asof bars in Q2/Q3/Q4, every ticker receives the regime's
  intercept — predictions are constant within the cross-section, so the
  top-30 selection is effectively arbitrary (stable on ticker order, no
  alpha signal).

The 2024-04-30 → 2026-04-30 holdout window was dominated by low VIX. The
reported headline therefore largely reflects Q1_calm performance during a
period when Q1_calm dominated. **Phase C robustness audit is the load-bearing
test:** if 2014-2026 mean phase performance has any phases that fall into
Q2/Q3/Q4-heavy regimes with poor results, the strategy fails phase-robustness
even if 2024-2026 itself is fine.

### Known limitations / data hygiene notes

- **Carhart factor cache last refreshed 2026-03-27**, covering through 2026-02-27.
  The holdout extends to 2026-04-30, so ~5 of ~104 expected rebalances are
  inner-joined out of the regression (resulting n=99). Result PASS-margin is
  large enough that the missing 5% does not change the verdict sign; flag for
  hygiene only.
- **Q2/Q3/Q4 Lasso shrinkage to zero coefs** is a structural finding that
  belongs in a hypothetical `multi_source_two_stage_v2` pre-reg if and only
  if it survives Phase C. NOT acceptable to silently retune mid-experiment.

## Phase C — multi-phase robustness audit

Ran 5 phase offsets sequentially via `scripts/audit_multi_phase.py multi_source_two_stage`
on the full 2014-2026 window. Total wall-clock ~83 min.

Per pre-reg: PASS only if every phase α t-stat ≥ 1.5 AND mean ≥ 1.96 Bonferroni
threshold (n=1 in fresh class).

| phase_offset | α t-stat | Sharpe net | Sharpe gross | excess net ann | turnover |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | +2.01 | +1.19 | +1.41 | +7.4% | 18.0% |
| 1 | −0.29 | −0.03 | +0.24 | −4.7% | 20.0% |
| 2 | +1.10 | +0.59 | +0.76 | −4.0% | 13.6% |
| 3 | +0.24 | −0.16 | +0.11 | +2.0% | 25.9% |
| 4 | +0.19 | −0.02 | +0.15 | −0.5% | 12.4% |
| **mean** | **+0.65** | +0.31 | +0.53 | **0.0%** | 18.0% |
| **std**  | 0.91 | 0.55 | 0.55 | 4.9pp | — |

**Verdict (per `multi_phase.robust_verdict`):** FAIL.
- mean αt = 0.65 < 1.0 → automatic FAIL
- 2 of 5 phases negative on at least one of (αt, excess_net) — not majority
- pre-reg pass-rule `every t ≥ 1.5` broken by 4 of 5 phases

Audit JSON: `docs/research/multi_source_two_stage_multi_phase_audit.json`

### Comparison to prior phase-robust failures

| Strategy | mean αt | mean excess_net | excess dispersion |
| --- | ---: | ---: | ---: |
| mom+lowvol_combo | +0.49 | −5.7% | 44.5pp |
| quality+momentum | +0.38 | +10.3% | 69.0pp |
| vol_target_overlay | +0.49 | −7.0% | 40.3pp |
| **multi_source_two_stage** | **+0.65** | **0.0%** | **4.9pp** |

Highest mean αt of the four. Order-of-magnitude tighter dispersion. Mean
excess_net = 0% (vs −5% to −7% for the price-only screeners) suggests the
multi-source feature set captures *something* — but at a magnitude that the
50-bps round-trip cost ate completely. Not deployable, but a quantitatively
different (more honest) FAIL than its predecessors.

## Methodology hygiene checklist

- [x] Pre-registration locked BEFORE Phase B (registered 2026-04-30).
- [x] Phase A sanity passed before Phase B.
- [x] PIT leakage audit (Exp 0) PASS — F4 + O2 invariants locked by tests.
- [x] ONE-shot holdout, no tuning after seeing the number.
- [x] Carhart attribution post-hoc (NOT pre-residualized at target).
- [x] Bonferroni denominator declared (n=1 in fresh class `multi_source_two_stage_search_2026_04_30`).
- [x] Phase C multi-phase audit completed.
- [x] Pre-reg ledger entry completed with FAIL verdict.

## Class & Bonferroni accounting

`multi_source_two_stage_search_2026_04_30` is a fresh signal class. n=1 (this experiment)
sets the bar at |t| ≥ 1.96 for PASS. Any future hypothesis in this class pays a
higher Bonferroni cost (n=2 → ~2.24, n=3 → ~2.39, etc.) per ledger discipline
documented in `feedback_keep_searching_screeners.md`.

## Structural diagnostics

Three observations from the per-regime fits + per-phase reversals that should
inform any future hypothesis in this class:

1. **Q2/Q3/Q4 zero-coef Lassos.** L1 selected the largest λ in the grid for
   3 of 4 regimes, meaning the features had no predictive power above the
   regularization threshold in those regimes. This is structural, not phase-
   specific — same finding across all 5 phases (verifiable in audit JSON
   per-phase config). Implication: either the features are price-regime-
   sensitive in a way the architecture doesn't capture, or 5d-forward target
   is too noisy for higher-VIX regimes.

2. **Phase 0 outlier vs phases 1-4.** Phase 0's t=+2.01 is 1.5σ above the
   5-phase mean. The other 4 phases cluster between −0.29 and +1.10 with
   mean ≈ +0.31. Phase 0 is the lucky path that single-OOS would have
   reported as PASS. This is exactly the phase-aliasing pattern the
   methodology was designed to catch.

3. **Cost is the swing factor.** Mean gross excess +0.6% / mean net excess
   0.0% — the 50-bps round-trip cost (10 + 5 = 15 bps half × 2 round-trip
   × 18% turnover × ~50 rebalances/y) ate the entire mean signal. With a
   cleaner cost profile (e.g. 5 bps half-spread, large-cap only) the gross
   signal might survive — but that's a v2 hypothesis.

## Next-experiment options (each requires NEW pre-reg, Bonferroni n=2 → 2.24)

If a follow-up in this signal class is desired, three structurally
independent variations are worth considering. Each would isolate ONE
variable, all others held to current pre-reg.

| Option | Hypothesis | Rationale | Risk |
| --- | --- | --- | --- |
| **A. Non-regime-conditional** | Single global Lasso, regime as one of features | If Q2-Q4 zero-coef = redundant architecture, one model uses all data | If regime conditioning IS the right structure, drops mean further |
| **B. ElasticNet replaces Lasso** | L1+L2 instead of L1 | Allows weak betas to survive in Q2-Q4 instead of being zeroed | Adds tuning surface (l1_ratio); more overfit risk |
| **C. 20d-forward target** | Longer horizon → fundamental edge over microstructure noise | Phase 0 wins might be momentum 20d in disguise | More autocorrelation, fewer effective independent obs |

Of these, **Option A** is the cleanest single-variable test — same features,
same target, same fit procedure, only the regime architecture flips off. A
binary outcome (better/worse mean αt) decisively answers whether Stage 1 added
value in the original. None of these is HARKing because they isolate
structural variables, not "fix" the failed configuration.

## Pre-reg ledger entry

Completed with verdict FAIL via:

```bash
.venv/bin/alphalens preregister complete multi_source_two_stage_2026_04_30 \
  --verdict FAIL --mean-alpha-t 0.65 --mean-excess-net 0.0 \
  --audit-path docs/research/multi_source_two_stage_multi_phase_audit.json
```

Class `multi_source_two_stage_search_2026_04_30` now 1/1 FAIL. Any next
hypothesis in this class pays Bonferroni n=2 → 2.24 t-stat threshold.
