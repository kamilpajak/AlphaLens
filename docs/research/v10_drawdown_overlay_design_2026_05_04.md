# v10 — drawdown-control L4 overlay on v9D options-implied base (2026-05-04)

**Pre-reg id:** `v10_drawdown_overlay_on_v9D_options_2026_05_04`
**Class:** `risk_management_overlay_2026_04_30` (overlay-class test #2; vol-target was #1 and FAIL'd)
**Status:** registered (Phase 0 complete; Phase 1 implementation in progress)

## Why this test

After 10+ paradigm failures across 8 signal classes on the burnt holdout 2024-04-30 → 2026-04-30, the only honest finding is the options-implied class converging across 4 separate tests on **+2.2 — +2.3 Carhart αt** — economically real but under the program-Bonferroni bar. Every additional alpha-class test on this holdout pays an escalating multiplicity penalty for diminishing information.

ADR 0007 explicitly carves out **risk-overlay strategies** as a separate hypothesis class: their primary success metric is **Sharpe-improvement vs. the ungated base**, not Carhart α t-stat, because vol-scaling makes betas time-varying and OLS α inference becomes noisy. This means an overlay test asks a *different* question:

> *Given an honest-but-marginal αt signal, can a properly-designed L4 overlay convert it into deploy-grade risk-adjusted improvement?*

That question has not been tested on the v9D base. Vol-target overlay v1 was tested on a **different** base (mom+lowvol, no honest alpha) and FAIL'd by levering into a drawdown — its failure tells us nothing about drawdown-control on a base with real signal.

## Hypothesis

A purely-realized-state, de-lever-only step-function overlay that scales exposure down during equity-curve drawdowns of the v9D portfolio (without any predictive logic) will:

- raise net Sharpe by ≥ +0.30 absolute,
- truncate the left tail (MaxDD reduction ≥ 30% relative),
- with Ledoit-Wolf block-bootstrap Sharpe-difference t-stat ≥ 2.5 at p < 0.01 (stricter than naive 1.96 because holdout is burnt),
- and remain phase-robust (Sharpe-improvement range ≤ 0.5 across 5 strided phases).

If any of these gates miss, paradigm failure #11 is logged and the class CLOSES on this base.

## Adversarial review (perplexity + zen)

### Perplexity (sonar-reasoning-pro)

Ranked 5 candidates A–E. **B (this design) #1** because:
1. Acceptance reframe per ADR 0007 doesn't burn alpha-class Bonferroni budget (different metric, not adding to αt-class count).
2. Kelly/CPPI/drawdown-control literature post-2020 supports overlay robustness in retail-accessible universes.
3. Asymmetric directionality: PASS sharply shifts deploy-readiness priors; FAIL falsifies L4-on-this-base only (cheap information).

### Zen (gemini-3-pro-preview, high thinking)

Seconded B and contributed three non-obvious hardenings, all folded into the pre-reg:

**Hardening 1 — discard Jobson-Korkie Sharpe-diff inference.**
Jobson-Korkie 1981 assumes IID-normal returns. An options-implied L/S-decile portfolio has heavy tails, kurtosis, and weekly autocorrelation from option roll cycles. Jobson-Korkie p-values on this data would be unreliably small. Replace with **Ledoit-Wolf 2008-style block-bootstrap** (block_size=21d ≈ one option-roll cycle, n_bootstrap=10000). Yields a 1-sided t-statistic for the null *Sharpe(overlay) ≤ Sharpe(base)* that respects the actual return distribution.

**Hardening 2 — MaxDD reduction is a separate, mandatory gate.**
A vol-smoothing overlay can raise Sharpe purely by reducing volatility without truncating the left tail (Sharpe is dimensionless; reducing both σ and µ proportionally leaves it unchanged but a vol-targeting overlay reduces σ more than µ on average). What we *want* is left-tail truncation — drawdown-control's whole reason for existing. We pre-register **MaxDD(overlay) ≤ 0.7 × MaxDD(base)** as gate G3. Sharpe-improvement without MaxDD-reduction is ruled out as success.

**Hardening 3 — overlay must be computationally dumb.**
Adding macro features (VIX, term-spread, credit-spread, etc.) introduces a new optimization axis with infinite DOF and no anchored prior. Overlay v1 (vol-target) used a continuous scaling formula and a leverage cap of 1.5; on a no-alpha base, when realized vol dropped during a recovery, leverage rose just in time to ride the next leg down (Phase 3: BASE -43.8% → overlay -77.9%). v10 fixes this by:
- **No leverage** (cap = 1.0, floor = 0.0).
- **Realized-state only** (input is portfolio-equity-curve drawdown, no macro, no predictive features).
- **Step function not continuous scaling** (3 levels: full / half / off; binary recovery threshold). Discrete behavior is intentional — it makes the policy auditable and the gate violations easy to detect.
- **T+1 execution** (causality contract: scale[t] from returns[<t] only, same as VolTargeter).

### Zen also flagged

> *"The +2.2 honest base may be a selection effect — you kept testing variants until consistent."*

Mitigation: v10 freezes the base scorer as **the v9D specification literally**, not as "best-of-4" or an average. The frozen-reference αt is +2.29 (recorded). Phase 0 gate G4 requires the v10 audit to replicate +2.29 within ±0.5 — if the base drifts in the v10 run, the overlay test is uninterpretable and the run aborts.

## Acceptance gates — exact rules

All six must PASS for v10 to clear. Any single gate FAIL → class CLOSED on this base.

| Gate | Rule | Source |
|------|------|--------|
| G1 | Sharpe-diff Ledoit-Wolf block-bootstrap t ≥ 2.5 at p < 0.01 | Zen hardening 1 |
| G2 | Sharpe(overlay_net) − Sharpe(base_net) ≥ +0.30 absolute | Moreira-Muir 2017 reports +0.4-0.7 on momentum; +0.3 is conservative |
| G3 | MaxDD(overlay_net) ≤ 0.7 × MaxDD(base_net) | Zen hardening 2 |
| G4 | Base-scorer αt replicates v9D's +2.29 within ±0.5 | Consistency / drift guard |
| G5 | overlay weight ∈ [0.0, 1.0] at every t | Hard leverage-bug guard |
| G6 | Sharpe-improvement range ≤ 0.5 across 5 strided phases | Phase-robustness, mirrors v9 αt-range gate |

## Bonferroni accounting

| Counter | v9D state | v10 state | Increment |
|---------|-----------|-----------|-----------|
| Program-level alpha-class n | 24 | 24 | 0 (overlay metric ≠ αt) |
| Overlay-class n | 1 (vol-target) | 2 (drawdown-control) | +1 |
| Naive intra-class threshold | n/a | \|t\|≥2.24 | derived |
| Effective threshold (zen-escalated for burnt holdout) | n/a | \|t\|≥2.5 at p<0.01 | locked in G1 |

If v10 PASSes, no further intra-overlay-class testing on this base would be run within the same session; the next overlay variant would carry escalated Bonferroni and would need a fresh holdout.

## Falsifiability asymmetry

This test cannot conjure alpha. Six independent gates × five phases = 30 independent kill conditions. Failure modes:

- Overlay never triggers (smoke kill: overlay weight constant 1.0).
- Overlay triggers but smooths vol without truncating tail (G3 kill).
- Overlay triggers and truncates but Sharpe doesn't improve (G2 kill).
- Sharpe improves but inference is noisy (G1 kill).
- Improvement varies wildly across phases (G6 kill).
- Base scorer drifts (G4 kill).
- Implementation bug allows leverage (G5 kill).

The kill-density is intentionally higher than any prior project test.

## What "PASS" would mean

PASS does **not** mean capital deploy is on the table. The burnt-holdout policy in CLAUDE.md applies regardless of this test's outcome. PASS would mean v10 is the project's first phase-robust strategy on the OSS methodology bundle's terms, and would advance to **prospective walk-forward Sharpe-improvement replication** on data accruing post-2026-04-30 at unadjusted p<0.05 single-test before any escalation. Earliest deploy-eligibility window: ~2027-12.

## What "FAIL" would mean

FAIL closes the overlay-on-v9D path. Paradigm failure #11 logged. The honest +2.2 αt base remains a research artifact. Future overlay tests on different bases (or different overlay families like time-stop, CPPI) would carry escalated Bonferroni.

## Implementation deliverables (Phase 1)

- `alphalens/overlays/drawdown_control.py` — `DrawdownControlOverlay` class
- `alphalens/backtest/sharpe_inference.py` — Ledoit-Wolf block-bootstrap Sharpe-diff
- `tests/test_overlays_drawdown_control.py` — overlay invariants + causality
- `tests/test_sharpe_inference.py` — bootstrap on synthetic IID + autocorrelated series
- `scripts/experiment_v10_drawdown_overlay.py` — driver, mirrors v9 cross-sectional residual + applies overlay
- Pre-reg ledger entry via `alphalens preregister add`

## References

- Moreira, A., & Muir, T. (2017). Volatility-Managed Portfolios. *Journal of Finance*, 72(4), 1611–1644.
- Ledoit, O., & Wolf, M. (2008). Robust performance hypothesis testing with the Sharpe ratio. *Journal of Empirical Finance*, 15(5), 850–859.
- Jobson, J. D., & Korkie, B. M. (1981). Performance hypothesis testing with the Sharpe and Treynor measures. *Journal of Finance*, 36(4), 889–908. (Cited as the inference *not* to use here.)
- Politis, D. N., & Romano, J. P. (1994). The stationary bootstrap. *JASA*, 89(428), 1303–1313.
- ADR 0007 (this repo) — Layer architecture and overlay-Sharpe-improvement metric.
