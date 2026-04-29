# Diagnostic flag retrospective — high IS α × low Carhart R² as overfit predictor

**Date:** 2026-04-28
**Context:** Layer 2d variant exploration (`docs/research/layer2d_variants.md`) and the
random-subset null test (`docs/research/layer2d_null_distribution.md`) jointly demonstrated
that an IS Carhart α near 100%/y combined with R² near zero is a signature of
set-distributional artifact rather than rank-driven signal — the "alpha" lives in candidate-set
membership and ranking adds zero information. This retrospective tests the diagnostic
universally against the four other CLOSED layers in the AlphaLens project.

## Diagnostic candidate

> If `Carhart-4F IS α (annualized) > 50%/y` AND `Carhart-4F R² < 0.01`,
> treat as a strong red flag for set-distributional artifact.
> Run the random-subset null test before proceeding to OOS validation.

## Evidence from CLOSED layers

| Layer | IS Carhart α | IS Carhart R² | IS t-stat | OOS Carhart t | Diagnostic fires? | Failure mode |
|---|---:|---:|---:|---:|:---:|---|
| **2b themed momentum** (closed 2026-04-22) | **+97.95%** | **0.007** | +2.66 | +0.82 | **YES** | Distributional + cost-eaten |
| **2c lean small-cap** (closed 2026-04-19) | +31.98% | 0.002 | +1.22 | -0.66 | NO (α below threshold) | Weak signal, never significant |
| **2d insider cluster** (closed 2026-04-24) | **+103.53%** | **0.005** | +2.14 | +0.68 | **YES** | Distributional artifact (validated) |
| **2e tactical rotation** (closed 2026-04-22) | n/a | n/a | – | OOS t=0.33 | – | Different — R²=0.999 vs benchmark (Pattern 2 from postmortem) |
| **2g guru-LLM** (closed 2026-04-?) | n/a | n/a | – | – | – | Different — R²=0.97 vs SPY (Pattern 2 from postmortem) |

## Findings

1. **Both layers killed by ranking-invariant overfit (2b, 2d) trip the diagnostic.** Pattern is
   consistent: large gross α with R² near zero, IS t marginally significant, OOS t collapses
   below threshold. Layer 2b was discovered via cost-stress + Phase-1B Bonferroni + survivorship
   audit (4 phases of investigation). Layer 2d went through 3 phases of validation. The
   diagnostic flag, run as a pre-OOS check, would have flagged both at IS-completion time —
   weeks earlier in both cases.

2. **Layer 2c (weak signal) does NOT trigger the flag.** Its α (32%) was below the 50% threshold,
   and its IS t-stat (1.22) never even passed nominal significance. The flag correctly
   distinguishes "moderate signal too weak to detect" from "huge signal too good to be real".

3. **Layer 2e and 2g failed via a different mechanism (high benchmark R², not low factor R²).**
   These are already covered by the existing Pattern 2 in `5_paradigm_failures_postmortem.md`
   ("R² approaching 1.0 vs benchmark = signal dead"). The diagnostic flag complements rather
   than replaces this — they describe orthogonal failure modes:

   - **Pattern 1 (this flag, new):** Carhart R² ≈ 0 + high IS α → distributional artifact
   - **Pattern 2 (existing):** Benchmark R² ≈ 1 + low absolute α → tilt drowned by passive

## Operational rule (for future Layer-2 candidates)

When running an IS backtest, before scheduling OOS:

1. Read off `Carhart-4F α annualized` and `R²` from the factor-attribution output.
2. If `α > 50%/y` AND `R² < 0.01`:
   a. Run `scripts/experiment_layer2d_random_null.py` (parameterised for the candidate's
      universe + signal cache) with K=100 trials.
   b. If V0's Carhart t-stat sits below the 95th percentile of the null t-distribution,
      **the screener provides no rank-quality value**. Do not proceed to OOS validation as
      the primary decision evidence. Report the result and either:
      - kill the candidate immediately (cheaper), or
      - keep IS as exploratory only, with explicit acknowledgement that the apparent α
        is set-membership-driven.
3. If `α > 50%/y` AND `R² ≥ 0.01`, proceed normally — the factor model is at least
   partially explaining returns; the residual α is more likely meaningful.
4. If `α ≤ 50%/y` (any R²), proceed normally — diagnostic does not apply.

## Caveats

- **Threshold values are tuned to the AlphaLens evidence set.** 50%/y and R²<0.01 were
  chosen post-hoc to fit the 2b+2d evidence; they are not pre-registered. If applied to a
  layer with sample size << 600 weekly observations, the noise floor shifts and the
  threshold should be re-tuned. Pre-registration of these thresholds in
  `5_paradigm_failures_postmortem.md` would strengthen the rule's authority.

- **"Diagnostic flag" is necessary-not-sufficient** for the artifact pattern. A layer can
  fire the flag and still genuinely have alpha (passing the random-null test). The flag's
  job is to *trigger* the null test, not replace it.

- **Layer 2c is informative for a different reason:** even with α=32% IS, R²=0.002 → factor
  model still explains nothing. The reason this didn't trip the flag is the α-magnitude
  threshold, not the R². If the rule is later relaxed to "any α with R²<0.01 should run the
  null test", Layer 2c would be a check on whether moderate-α-low-R² also overfits — likely
  yes, but untested here.

## Action items

- Add this diagnostic to `docs/research/5_paradigm_failures_postmortem.md` §Methodological-lessons.
- Reference from `docs/research/kill_verdict_checklist.md` if that file documents future-layer
  validation gates.
