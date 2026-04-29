# Tri-factor — phase-robust FAIL verdict (final)

**Date:** 2026-04-29 (final synthesis of the day)
**Verdict:** **FAIL.** Confirmed under phase-robust methodology. Earlier today's "FAIL" verdict was correct in outcome but the reasoning was incomplete — driven by phase-aliasing rather than fundamental no-edge. This synthesis closes that question with phase-distributed evidence.

## Setup

`scripts/audit_multi_phase.py tri_factor` runs `experiment_tri_factor_edgar.py` at every `phase_offset = 0..4` (stride=5) for the same configuration, parses headline stats, and aggregates via `alphalens/backtest/multi_phase.summarise_phase_results` + `robust_verdict`.

Run: tri-factor (rw=1.0, ADV $5M, vw=1.0), period 2019-2022 IS + first half 2023 OOS, locked 2015-2022 PIT universe.

## Results — IS 2019-2022 across 5 phases

| Phase | Sharpe gross | Sharpe net | excess gross /y | excess net /y | α 4F t |
|---:|---:|---:|---:|---:|---:|
| 0 | -0.33 | -0.48 | -37.9% | -40.5% | **-0.82** |
| 1 | -0.32 | -0.51 | -64.9% | -67.5% | **-0.95** |
| 2 | +0.15 | -0.01 | +32.2% | +29.7% | +0.55 |
| 3 | +0.29 | +0.12 | +0.6% | -1.9% | +0.66 |
| 4 | +0.83 | +0.65 | +40.1% | +37.5% | **+2.24** |

**Aggregate:**
- mean Sharpe gross = +0.12, std = **0.48**
- mean t-stat = +0.34, std = **1.30**, range [-0.95, +2.24]
- mean excess net = **-8.5%/y**, std = **45.1pp**
- 2/5 phases t < 0 ; 1/5 phases t > 1.5 (the outlier we observed earlier)

**Robust verdict: FAIL** (mean t < 1.0; mean excess net < 0).

## Results — OOS 2023-Q1+Q2 across 5 phases (n=25 rebalances per phase)

| Phase | Sharpe gross | Sharpe net | excess net /y | α 4F t |
|---:|---:|---:|---:|---:|
| 0 | -0.29 | -0.51 | -107.4% | -0.58 |
| 1 | +0.21 | +0.01 | -14.7% | +0.14 |
| 2 | +0.39 | +0.19 | +51.4% | +0.58 |
| 3 | -0.16 | -0.40 | -52.4% | -0.32 |
| 4 | +2.73 | +2.46 | +65.5% | +2.19 |

**Aggregate:** mean t = +0.40, std = 1.09; mean excess net = -11.5%/y.

**Robust verdict: FAIL.**

## Why this is the right answer

Earlier today three sequential synthesis docs gave conflicting verdicts:
1. `tri_factor_validation_2026_04_29.md` → FAIL (catastrophic). Based on standalone halves with phase 0 sampling.
2. `methodology_audit_2026_04_29.md` → "FAIL was phase-aliasing artifact; phase-aligned half 2 shows t=+2.24, retract FAIL." Based on a single phase-4 sample.
3. (this doc) → FAIL phase-robust. Based on full distribution across phases 0-4.

The first verdict was right by accident — phase 0 had alpha t = -0.82 / excess -40%, looking catastrophic. The audit correctly pointed out single-phase samples are unreliable, then a single-phase rerun (phase 4) showed t=+2.24 and looked like a reversal. **Both single-phase samples were misleading**; only the aggregate of all 5 phases is honest.

The strategy genuinely has **no detectable edge** in 2019-2022:
- mean alpha t = +0.34 (≪ 1.5 gate, ≪ 2.0 deployment threshold)
- mean excess net = **negative** (-8.5%/y)
- standard deviation across phases is enormous (45pp/y in excess), meaning even the mean is poorly estimated

The original `tri_factor_combo.md` "OOS 2023-2026 Sharpe 1.04, t=2.08" result is a single-phase point estimate inside this huge dispersion. There's no reason to believe it was the typical outcome — it could just as easily have been -0.5 in a different phase.

## What this confirms about methodology

1. **Single-phase Sharpe should never be reported in isolation** for any strategy stability or deployment decision. Today's three-step learning sequence — single-phase FAIL → single-phase reversal → phase-robust FAIL — is the cautionary tale.
2. The patches landed today (`feedback_phase_aliasing_in_strided_backtests.md`) are necessary infrastructure, not optional polish. Every future stability check should invoke `audit_multi_phase.py` (or stride=1) by default.
3. The `multi_phase.robust_verdict` heuristic (mean t ≥ 1.5 + no-majority-negative + every-phase-pass for top tier) successfully discriminated this case as FAIL where any single-phase view would have given conflicting answers.

## Action

- Tri-factor → **CLOSED** for capital deployment. Add `__status__ = "CLOSED"` evidence with phase-robust audit reference once we decide the right home for tri-factor code (currently lives in `scripts/experiment_tri_factor_edgar.py`, RESEARCH-style not a screener).
- Update `MEMORY.md`: tri-factor verdict is final, supersedes today's earlier conflicting docs.
- Mom+lowvol fallback: same multi-phase audit recommended before any final verdict on it. Could either confirm the FAIL (if mom+lowvol is also phase-fragile) or reverse it (if mom+lowvol is more stable across phases). **Current best estimate: FAIL likely**, since mom+lowvol single-phase results showed similar-magnitude phase variance.
