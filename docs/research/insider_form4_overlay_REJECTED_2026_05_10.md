# Layer 4 overlay on insider_form4_opportunistic — REJECTED 2026-05-10

**Status:** REJECTED (pre-registration ledger NOT touched)
**Pre-reg id:** NONE — Bonferroni overlay-class budget unspent
**Date:** 2026-05-10
**Base:** `insider_form4_opportunistic` v2 (Cohen-Malloy 2012 opportunistic-insider Form-4 classifier; PASS_MARGINAL on 2018-2023 αt=+2.71 and 2024-2026 final lock αt=+2.69)
**Outcome:** Empirical evidence (TDD-verified) shows base alpha is **EXTREME counter-cyclical** — concentrated in high-vol regimes (Q5: +68.85%/y) and negative in calm regimes (Q1: -25.65%/y). All Layer 4 pro-cyclical de-leveraging overlays (vol-target Moreira-Muir 2017, drawdown-control step-function, CPPI) are structurally mismatched with this signal class. Overlay registration would burn Bonferroni budget on a test with near-deterministic FAIL prior.

## What this memo is

This is a REJECTION memo for the proposed Layer 4 overlay test on the
insider_form4_opportunistic base. The proposal was reviewed by zen
(gemini-3-pro-preview, high thinking, two rounds) and perplexity
(sonar-reasoning-pro, two rounds). After bug-corrected empirical
analysis, the conclusion is to reject the test class entirely for this
base. **No pre-registration ledger entry was created**, so the
overlay-class Bonferroni counter remains at 2 (vol-target FAIL on
mom+lowvol 2026-04-30; drawdown-control FAIL on v9D options 2026-05-04).

This memo follows the v8_lightgbm_quantile REJECTED precedent — full
documentation of methodology + reviews + empirical evidence + verdict
rationale, archived for the project's anti-pattern catalog.

## Phase 0 — Abstract conceptual spec (what was being considered)

Per the review-first workflow (`/Users/jacoren/.claude/plans/graceful-honking-wave.md`),
no design memo file existed during reviewer briefing. The conceptual
spec carried:

- **Variant candidates**: A (vol-target Moreira-Muir 2017) and B (drawdown-control step-function), both no-leverage.
- **Window candidates**: 2018-2023 OOS only, 2024-2026 final lock only, or both (joint-PASS).
- **Threshold candidates**: ΔSharpe ≥ +0.15 / +0.20 / +0.25 / +0.30.
- **Gate skeleton**: G1 Sharpe-diff Ledoit-Wolf inference, G2 ΔSharpe floor, G3 MaxDD reduction, G4 base drift guard, G5 leverage range, G6 phase robustness.
- **Inference framework**: `alphalens.backtest.sharpe_inference.block_bootstrap_sharpe_diff` (paired circular block-bootstrap, block_size=21, n=10000), confirmed shipped + consumed by `scripts/experiment_v10_drawdown_overlay.py`.

## Adversarial review — Round 1 (false-MDD prior)

Both reviewers were briefed with a **fact error in my Phase 0 spec**:
I claimed insider_form4 base MaxDD was <15% (this was a hallucination —
the postmortems do not document MaxDD anywhere; I conflated this base
with v9D options-implied which IS <15% MaxDD).

### zen Round 1 verdict
- Approved Variant B (drawdown-control) with binding modifications:
  - **Z3:** per-window t≥2.0 (joint-PASS conjunction provides aggregate power)
  - **Z4:** REPLACE relative MaxDD ratio with discrete activation-kill condition (overlay must trigger ≥3 distinct times AND localized MDD during activation regimes < base)

### perplexity Round 1 verdict
- **REJECTED** with mechanism-starvation argument: low-MDD base + 71 rebalances/window × ~10-20% probability of optimal de-lever timing = only 2-3 effective trigger events per window. Insufficient signal to beat costs + slippage.
- Conceded Z3 (per-window t≥2.0 with joint-PASS family-wise control)
- Recommended REJECTED memo

## Methodology bug #1 — false MDD prior

After reviewers split, I verified MDD claim against actual data. Bug
isolated and corrected:

```
=== Verified MaxDD per phase (computed from dumped daily continuous returns) ===
Phase B 2018-2023:                            Final lock 2024-2026:
  phase_0: MaxDD=-45.44% (COVID 62d)            phase_0: MaxDD=-26.28%
  phase_1: MaxDD=-42.27%                        phase_1: MaxDD=-24.38%
  phase_2: MaxDD=-42.18%                        phase_2: MaxDD=-24.28%
  phase_3: MaxDD=-42.16%                        phase_3: MaxDD=-25.92%
  phase_4: MaxDD=-41.06%                        phase_4: MaxDD=-26.23%
  Mean = -42.62%                                Mean = -25.42%
```

Annual intra-year MDDs (Phase B / phase 0): 2018 -31%, 2019 -10%, 2020 -45%, 2021 -17%, 2022 -22%, 2023 -30%. 5 of 6 years with intra-year MDD >16%; annualized vol 30.15%.

**This is a HIGH-MDD base**, not low-MDD as my Phase 0 brief claimed. The error invalidated perplexity Round 1 mechanism-starvation argument (which depended on MDD <15% premise).

## Adversarial review — Round 2 (corrected facts)

### perplexity Round 2 verdict (with corrected MDD)
- **Flipped recommendation**: reopen Variant A (vol-target M-M) as PRIMARY candidate. Reasoning: forward-looking state variable (realized vol), not backward-looking equity curve; continuous gradual scaling vs threshold-based fragile; M-M operating range compatible with 30% annualized vol.
- **Conditional**: empirical vol-state alpha test mandatory (rolling 12-month vol quintiles, segment alpha) before final lock. Hypothesis to test: insider alpha behavior across vol regimes.

### zen Round 2 verdict (cross-check on perplexity flip)
- **Z1 acknowledged**: perplexity's "forward vs backward looking" distinction misframes the mechanism. Both A and B will de-lever during a crash; difference is **amplitude (continuous vs step), NOT direction**. Both are pro-cyclical de-leveragers.
- **Z2 — KEY INSIGHT (load-bearing)**: Cohen-Malloy opportunistic insider buying is **counter-cyclical** — insiders buy the dip. These signals cluster heavily during market panics, earnings gaps, sector-wide selloffs, all environments characterized by high local volatility. Vol-targeting (inverse vol weighting) is inherently **pro-cyclical** regarding capital deployment. Therefore Variant A will systematically starve the portfolio of capital EXACTLY when the signal is generating its highest-conviction alphas.
- **Z3 — stealth multiplicity warning**: vol-state alpha test as conditional logic on overlay activation introduces hidden DOF (vol metric + threshold). Force Variant A vanilla; if alpha can't survive blunt application, it's too fragile to trade.

### perplexity cross-check on zen
- **Conceded Z1**: amplitude vs direction is the substantive distinction.
- **Conceded Z3**: vol-state amendment as overlay-conditioning IS multiple testing leakage.
- **Z2 reframing**: refused to fabricate Cohen-Malloy citation; recommended **empirical vol-regime test as standalone GO/NO-GO diagnostic** (NOT as overlay tuning) — this is methodologically clean because the test informs whether to register the overlay test, not how to design it.

## Empirical insider-timing × vol regime analysis (pre-specified)

The two reviewers converged on: **run the empirical vol-regime conditional analysis as a standalone diagnostic to resolve zen Z2 prediction**.

### Pre-specification (locked BEFORE running analysis)
- **Vol regime variable**: 60-day rolling realized vol of IWM (Russell 2000 ETF benchmark). EXOGENOUS to insider portfolio (avoids endogeneity).
- **Quintile cuts**: 20/40/60/80 percentile of IWM 60d vol within each phase window (per-phase cuts to control for secular vol trends).
- **Insider portfolio measure**: mean daily return per quintile, pooled across 5 phases of Phase B 2018-2023.
- **Decision rule**: R = mean(Q4+Q5) / mean(Q1+Q2). R≥1.5 → REJECT counter-cyclical confirmed; 0.8≤R<1.5 → PROCEED orthogonal; R<0.8 → PROCEED reverse cyclical.
- **Sharpe cross-check**: if R_mean strong counter-cyclical BUT Sharpe is flat across quintiles (alpha paid for by proportional vol), flip to PROCEED.

## Methodology bug #2 — decision rule sign-flip case

Pre-spec rule assumed both mean(Q1+Q2) and mean(Q4+Q5) would be positive (R as positive ratio). When mean(Q1+Q2) is **negative** (insider loses in calm), R becomes negative and loses interpretability — naive rule "R<0.8 → PROCEED reverse cyclical" would mis-route to PROCEED a EXTREME counter-cyclical case.

**Bug fixed via TDD module** `alphalens.attribution.signal_vol_regime`:
- `assign_vol_regime_quintiles(vol_series, n_quintiles=5)` — quintile bucketing
- `aggregate_returns_by_regime(returns, quintiles, periods_per_year)` — per-quintile mean/std/Sharpe
- `classify_cyclicality(summary)` — sign-pattern-aware classification

Test coverage (`tests/test_signal_vol_regime.py`, 18 tests, all green):
- All sign combinations of mean(Q1+Q2) × mean(Q4+Q5): both-positive (3 sub-cases), Q1+Q2-negative + Q4+Q5-positive (EXTREME counter-cyclical), Q1+Q2-positive + Q4+Q5-negative (EXTREME calm-period), both-negative (INCONCLUSIVE), zero-denominator (no division-by-zero crash)
- Sharpe-flat override case (high R_mean BUT flat R_sharpe → PROCEED)
- Edge cases: NaN handling, length mismatch, missing quintiles, too-few-obs

## Methodology bug #1 — TDD verification

Bug #1 (MaxDD computation) verified via cross-check between two independent methods on all 5 Phase B phases:

```
Phase 0: lib=-0.4544, inline=-0.4544, diff=0.00e+00 [OK]
Phase 1: lib=-0.4227, inline=-0.4227, diff=5.55e-17 [OK]
Phase 2: lib=-0.4218, inline=-0.4218, diff=5.55e-17 [OK]
Phase 3: lib=-0.4216, inline=-0.4216, diff=0.00e+00 [OK]
Phase 4: lib=-0.4106, inline=-0.4106, diff=0.00e+00 [OK]
```

Used `alphalens.backtest.metrics.max_drawdown` (project-tested, consumed by paper_trade) as the canonical reference; inline cumulative-product method as independent cross-check. Both agree to floating-point precision. **MaxDD facts confirmed.**

## Empirical result (TDD-verified)

| Vol quintile | n_obs | mean / day | std / day | Sharpe (ann) | Annualized return |
|:---:|---:|---:|---:|---:|---:|
| Q1 (calm)   | 1508 | -0.1018% | 1.1245% | **-1.437** | **-25.65%** |
| Q2          | 1505 | -0.0037% | 1.3836% | -0.043 | -0.94% |
| Q3 (avg)    | 1505 | +0.1436% | 1.5351% | +1.485 | +36.20% |
| Q4          | 1505 | +0.2202% | 1.6888% | **+2.070** | +55.48% |
| Q5 (high)   | 1507 | +0.2732% | 2.9353% | +1.478 | **+68.85%** |

**Pattern monotoniczny** across vol quintiles (mean and Sharpe both increasing).

### TDD-verified verdict

```
Sign pattern    : Q1+Q2 negative, Q4+Q5 positive
R_mean          : -4.677
R_sharpe        : -2.398
Classification  : EXTREME counter-cyclical
PROCEED         : False
Rationale       : Sign-flip pattern. Insider alpha is concentrated in high-vol
                  regimes; pro-cyclical overlays (vol-target, drawdown-control)
                  would de-lever exactly when the signal is most profitable.
                  Overlay class structurally mismatched with this signal.
```

## Mechanism interpretation

Cohen-Malloy 2012 "opportunistic" insider classifier specifically picks **contrarian buying patterns** — insiders who buy on dips, against the prevailing trend. Such buys cluster heavily in market dislocations (panics, earnings gaps, sector selloffs), which are exactly the periods where IWM 60d realized vol is elevated.

Insider buys then anticipate recovery → high returns realized in the high-vol regime. The base portfolio captures this: Q5 returns +68.85%/y, Q4 +55.48%/y, Q3 +36.20%/y. In calm regimes (Q1-Q2) the signal is essentially noise — there are few opportunistic-buy patterns to detect, the cross-section flattens, and the top-200 selection picks losers.

This is empirically the inverse of what factor-portfolio overlays are designed for. Moreira-Muir 2017 and downstream extensions assume **positive vol-clustering autocorrelation** — high vol persists, so de-leveraging on rising vol gives time to recover before the next cycle. Counter-cyclical signals violate this assumption: the vol spike IS the signal opportunity, not a precursor to it.

## Implications for Layer 4 design

Three implications for the project's overlay class:

1. **insider_form4 base is permanently overlay-incompatible** at the pro-cyclical de-leveraging overlay class. No combination of M-M vol-target, drawdown-control, CPPI, time-stop, or volatility-budget overlays will improve risk-adjusted return on this base — they all systematically de-lever during the regime where the alpha is concentrated.

2. **Counter-cyclical signal classes generally** (insider opportunistic buying, distress-anomaly, post-earnings-announcement-drift on negative surprises, contrarian reversal) likely share this property. Layer 4 design should add a **mechanism-screen pre-test** before any overlay registration: aggregate base returns by exogenous vol quintile; if the pattern is monotonically increasing in Q3-Q5 with sign-flip in Q1-Q2, REJECT pro-cyclical overlay class for that base.

3. **`alphalens.attribution.signal_vol_regime` module is the reusable artifact.** Future overlay design memos can call `classify_cyclicality()` as a standalone GO/NO-GO diagnostic in Phase 0, eliminating the need for full review cycles when the base signal class has structural conflict with the overlay class.

## What this work does NOT do

- **Does NOT register a pre-reg ledger entry.** Overlay-class Bonferroni counter remains at 2; alpha-class counter remains at 28.
- **Does NOT touch the base strategy.** `insider_form4_opportunistic` v2 stays validated, paper-trade stays active (`insider_form4_opportunistic_paper_trade_2026_05_09`).
- **Does NOT close other Layer 4 design space.** Counter-overlay variants (e.g., **anti-cyclical** overlays that LEVER UP on vol spikes when insider density >threshold) remain a research question, but they require fresh review pass + careful Bonferroni accounting (anti-cyclical overlay is inherently asymmetric to v10/Failure 10 prior tests).
- **Does NOT preclude compound experiments.** The PASS_MARGINAL base remains eligible for cross-data-class compound testing (memory `project_compound_experiments_roadmap.md`) — overlay-class incompatibility is layer-specific.

## Reusable artifacts produced

- `alphalens/attribution/signal_vol_regime.py` (180 lines) — sign-pattern-aware vol-regime conditional analysis with TDD coverage
- `tests/test_signal_vol_regime.py` (18 tests, all green) — covers all sign combinations of mean(Q1+Q2) × mean(Q4+Q5), Sharpe-flat override, edge cases
- This memo as anti-pattern catalog entry for counter-cyclical signal × pro-cyclical overlay structural mismatch

## References

- Cohen, L., Malloy, C., & Pomorski, L. (2012). Decoding inside information. *Journal of Finance*, 67(3), 1009-1043.
- Moreira, A., & Muir, T. (2017). Volatility-managed portfolios. *Journal of Finance*, 72(4), 1611-1644.
- Ledoit, O., & Wolf, M. (2008). Robust performance hypothesis testing with the Sharpe ratio. *Journal of Empirical Finance*, 15(5), 850-859.
- ADR 0007 — Layer architecture (segregated metric classes, ΔSharpe primary for overlay-bearing strategies).
- v10 drawdown-overlay design + postmortem 2026-05-04 (prior overlay test on different base).
- insider_form4_opportunistic Phase B postmortem 2026-05-09.

## Next steps (deferred to future sessions)

- Compound experiment design: insider × iVolatility, or insider × P/C abnormal volume (memory `project_compound_experiments_roadmap.md`).
- Anti-cyclical overlay design (asymmetric to v10/Failure 10; would need fresh adversarial review pass).
- Long-horizon paper-trade observation continues per `insider_form4_opportunistic_paper_trade_2026_05_09` (26w checkpoint ~2026-11-07, 52w ~2027-05-08).

## Epilogue 2026-05-12 — Counter-cyclical mechanism is a cost-mirage artifact

**Slippage stress diagnostic** (`docs/research/insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md`) on the same `insider_form4_opportunistic` v2 base finds that the EXTREME counter-cyclical mechanism that grounded the overlay REJECTION above is itself a **pre-cost artifact**. Under regime-amplified bid-ask spread cost (β=2 in Chordia-Roll-Subrahmanyam 2001 / Naes-Skjeltorp-Odegaard 2011 form: `half_spread(t) = base × (1 + β × max(0, (σ_60d − σ_median)/σ_median))`), the OOS 2018-2023 post-drag cyclicality collapses across all 5 phases:

| Phase | R_excess_pre_drag | R_excess_post_drag | Δ |
|---:|---:|---:|---:|
| 0 | −2.12 | −0.36 | **+1.75** |
| 1 | −2.44 | −0.54 | **+1.89** |
| 2 | −2.47 | −0.41 | **+2.05** |
| 3 | −2.88 | −0.55 | **+2.33** |
| 4 | −2.95 | −0.53 | **+2.42** |
| **mean** | **−2.57** | **−0.48** | **+2.09** |

Classification flips from "strategy-specific counter-cyclical" → "matches benchmark baseline" / "weakly strategy-specific" on all phases. **The Q5 +68.85%/y annualized return that justified the overlay rejection was driven by gross-of-cost arrival-price crossing during panic regimes when R2000 spreads blow out (Lesmond/Schill/Zhou 2004 + Hasbrouck 2009 + Corwin-Schultz 2012 anchors put R2000 half-spreads at 200–300 bps right-tail during 2008/2020 stress).**

**Consequence**: this DOES NOT reopen Layer 4 overlay class. The base strategy's G1 realization gate (αt_net ≥ 2.0 at H=50bps R2000 median half-spread) FAILED on both OOS (αt=+1.27) and final-lock (αt=+1.95) windows — there is no post-cost edge to overlay. Layer 4 reopening is moot.

**Permanent institutional memory** (warning for future researchers): R2000 long-only signals with EXTREME counter-cyclical Q5 concentration must be screened with regime-conditional cost models BEFORE Layer 4 overlay analysis. The pre-cost `classify_cyclicality_excess` reading is unreliable when applied to strategies that fire on panic-regime trades. Add to overlay design protocol (CLAUDE.md §"Layer 4 overlay design pre-screen (mandatory)"): the cyclicality screen must be run on **net-of-cost** returns under at least β=2 amplification, not gross.

**Side effect, perplexity-preserved**: the post-cost cyclicality improvement (Δ +2.09 mean) does mean the strategy becomes LESS tail-risky under market stress on a net basis. That's a valid CRISIS HEDGING use case — but irrelevant to alpha publication. Different research lane.
