# insider_form4_opportunistic — Phase B postmortem (2026-05-09)

**Verdict: PASS_MARGINAL** — first phase-robust positive signal in AlphaLens
project history (after 11 paradigm failures across Layer 2b/2c/2d/2e/2f/2g
+ tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum
+ vol-target overlay, plus 2 INCONCLUSIVE retrospectives v9D and
pc_abnormal_volume).

## Headline numbers (R2000 PIT, OOS 2018-2023, 5 phases × ~1500 daily obs)

| Gate | Value | Threshold | Pass |
|---|---:|---:|---|
| G1 pooled mean αt (Carhart 4F, HAC=126) | **+2.710** | ≥ 3.1237 (Bonferroni n=28) | ❌ |
| G2 every-phase αt floor | min=+2.48, max=+2.93 | every ≥ 1.5 | ✅ |
| G3 excess_net mean | **+17.68%/y** | ≥ 0% | ✅ |
| G4 dispersion (excess_net max-min) | **1.3pp** | ≤ 70pp | ✅ |
| G5 block-bootstrap αt lower bound | **+1.539** | > 0 | ✅ |

Per-phase observed αt: +2.61, +2.81, +2.72, +2.93, +2.48
Per-phase Sharpe net: +0.82, +0.88, +0.85, +0.89, +0.87
Per-phase excess_net: +17.1%, +18.4%, +17.2%, +18.2%, +17.5%

Block-bootstrap (1000 reps × stationary block size 126 trading days,
synchronous-across-phases per pre-reg v2 lock):
2.5%/97.5% bounds on pooled mean αt = **[+1.54, +4.20]**.

## What "PASS_MARGINAL" means here

Per pre-reg ledger `insider_form4_opportunistic_2026_05_08_v2` ->
`success_criteria.verdict_classification`:

> "R2000_mean_alpha_t_in_2.50_3.12_AND_all_other_gates_pass":
> "PASS_MARGINAL — signal validated at adjusted threshold; capital deploy
> off-table; meta-analytic combination with prior INCONCLUSIVEs (v9D, v11)
> recommended in postmortem."

The signal cleared every other gate by a large margin — particularly the
phase-stability gates (G2 + G4) which have killed every prior strategy in the
program. Cross-phase αt range is 0.45 (smallest in project history); excess_net
dispersion is 1.3pp vs the 70pp threshold (54x undershoot). The bootstrap CI
straddles the strict Bonferroni threshold but firmly excludes zero. Capital
deploy stays OFF-TABLE per the pre-reg `capital_deploy_clause` and the project
doctrine that PASS_MARGINAL does not unlock allocation.

## Why this is meaningfully different from the 11 prior failures

The 11 paradigm failures clustered around three repeating modes:
1. **In-sample → out-of-sample collapse** (Layer 2b/2c/2d Lakonishok-Lee-style
   insider clusters; tri-factor; mom+lowvol_combo; quality+momentum). IS
   αt 2.0-3.5 → OOS αt 0-1.0.
2. **Single-phase artifact** (regime-gate rescue, vol-target overlay). One
   phase looks great, others 0.
3. **Sign-flip / data-snoop artifacts** (Layer 2e/2f/2g; v10 drawdown
   overlay). Sign of edge depends on regime cut.

Phase B insider_form4_opportunistic falls in NONE of these modes:
- It was tested directly on a fresh OOS class (no IS↦OOS bridge — the only
  prior look at this scorer was the Phase A density/breadth/direction check,
  which doesn't reveal αt).
- All 5 phase offsets agree to within 0.45 αt.
- All 5 phase offsets have positive excess_net within 1.3pp of each other.

The Cohen-Malloy-Pomorski (2012) opportunistic-insider mechanism therefore
appears genuinely robust on R2000 PIT 2018-2023 — distinct from the
"Lakonishok-Lee net-buy" mechanism that failed as Layer 2d cluster scorer
(closed 2026-04-24, αt IS=2.14 → OOS=0.68).

## Stealth multiplicity acknowledgement (pre-reg required)

Per ledger `success_criteria.bonferroni_accounting.stealth_multiplicity_prose_required: true`:

The choice to test "Cohen-Malloy opportunistic Form-4" specifically was
conditioned on prior failures within the insider class (Layer 2d cluster
scorer FAIL'd 2026-04-24). The fresh-class Bonferroni count of n=28 does NOT
formally penalize for the "next adjacent class" hypothesis search across the
broader insider program. A purist multi-stage Bonferroni would push the
threshold higher than 3.12.

The project policy (and Gemini Z2 amendment per pre-reg) is to acknowledge
this in postmortem prose rather than apply a higher numeric n bump (else
infinite regress: "what's the multiplicity penalty for the program-level
Bonferroni convention?"). Bayesian reading: the prior of finding a positive
result in the next insider variant after one class FAIL'd is non-zero but
discounted vs a fully fresh class. The PASS_MARGINAL verdict already
incorporates this discount via the "marginal" framing.

## Audit operational notes (worth recording for next audit)

**Wall time:** 5h45m for 5 phases parallel on Mac M2 (10 cores). Per-phase
~5h44m (vs initial 1.5-2h estimate). Universe grew across the 6yr window
(~1500-2000 ticker union vs smoke's 984 over 2018-2019), and BLAS thread
contention with 5 numpy processes hurt more than expected. Future audit
runs at this scale should either ship to runpod CPU pod (memory above
recommended for parallel local runs) or run sequentially overnight.

**v1 abort cost:** 38 minutes wasted before mid-flight code review caught
the `hac_maxlags=126` units mismatch (rebalance-cadence n=72 vs intended
daily n=1500). v1 abort detail in ledger entry
`insider_form4_opportunistic_2026_05_05.outcome`. Lesson saved as memory.

**Block-bootstrap perf:** 1000 reps × 5 phases × ~1508 daily obs each ran
in ~17 seconds (vs initial conservative estimate of 2-3 min). Synchronous
resampling across phases works as designed.

## Comparison to prior INCONCLUSIVE retrospectives

| Class | Period | Pooled αt | Bounds CI | Verdict |
|---|---|---:|---|---|
| v9D (options-implied) | retrospective pre-2018 (2009-2017) | +2.45 | bounds-lower not RW (Andrews-Manski +2.15) | INCONCLUSIVE |
| pc_abnormal_volume | retrospective pre-2018 (2009-2017) | +2.65 | bounds-lower +1.98 | INCONCLUSIVE |
| **insider_form4_opportunistic (this run)** | **OOS 2018-2023** | **+2.71** | **block-boot lower +1.54** | **PASS_MARGINAL** |

The three sit in a tight αt cluster (+2.45 to +2.71) — coincident with the
empirical "options_implied class triangulated ~+2.2-2.45 ceiling" hypothesis
captured in memory. Crucially, the insider_form4 result is on a fresh OOS
window (2018-2023, never touched by prior insider testing), while v9D and
pc_abnormal were retrospective pre-2018 audits on data shown to multiple
prior screeners. Therefore the PASS_MARGINAL is the first claim of a
genuinely-novel positive result.

## What unlocks (and what stays locked)

**Unlocks:**
- Layer 4 overlay test eligibility (vol-target, drawdown control) on this
  insider_form4_opportunistic base — per pre-reg `capital_deploy_clause`.
  These tests would compose Layer 2 (this scorer) × Layer 4 (overlay) per
  ADR 0007, paying their own Bonferroni cost.
- Forward paper-trade observation period — analogous to v9D and pc_abnormal
  paper-trade activations after their INCONCLUSIVE retrospectives.

**Stays locked:**
- Capital deploy gate stays OFF-TABLE per project policy. PASS_MARGINAL is
  not a full PASS.
- Final lock 2024-2026 OOS validation — pre-reg specifies a follow-on lock
  on data not seen by any insider screener. Schedule for separate session
  with fresh-head pre-reg discipline (not now).

## Methodology lessons (saved as feedback memory candidates)

1. **statsmodels HAC silently inflates t-stats when maxlags > n_obs**.
   Bartlett kernel weights `w(j) = 1 - j/(L+1)` stay near 1 for all valid
   lags `j < n` when `L > n`, producing degenerate variance estimates and
   ~3x t-stat inflation. No warning emitted. Lesson: always express
   pre-reg `hac_maxlags` in observation units of the regression input,
   not in calendar days; or compute the regression on a daily series so
   the unit-conversion is implicit. (Already saved to v1 ledger outcome
   block; needs a feedback memory entry too.)

2. **Synchronous block-bootstrap is required for pooled-mean inference
   across phases** that hold near-identical baskets. Independent per-phase
   resampling destroys cross-phase covariance and artificially narrows the
   pooled-mean CI. Verified in test
   `tests/test_synchronous_block_bootstrap.py::test_perfectly_correlated_phases_yield_pooled_mean_equal_to_per_phase`.

3. **Daily continuous-holding return reconstruction** is the right input
   for HAC-overlapping-signal regressions, not the rebalance-cadence
   `engine.portfolio_returns` series. The engine emits 1-day forward
   returns at each rebalance for Sharpe annualization; that's NOT the
   right unit for residual autocorrelation analysis when the holding
   horizon stretches across multiple days. Helper:
   `alphalens.backtest.daily_continuous_returns.daily_continuous_returns`.

## Next steps

1. ✅ Close v2 ledger entry with PASS_MARGINAL outcome (done in this session).
2. ✅ Write this postmortem (done).
3. Update memory file `project_insider_form4_opportunistic_locked_2026_05_05.md`
   to reflect PASS_MARGINAL verdict.
4. Optionally update CLAUDE.md to reference this as the first phase-robust
   positive in the project status block.
5. Defer: Layer 4 overlay test design memo (next session, fresh head).
6. Defer: 2024-2026 final lock (next session, fresh-head pre-reg discipline).

## References

- Pre-reg ledger v2: `docs/research/preregistration/ledger.json` entry
  `insider_form4_opportunistic_2026_05_08_v2`
- Pre-reg ledger v1 (aborted): same file, entry
  `insider_form4_opportunistic_2026_05_05` (status `execution_aborted_units_mismatch`)
- Phase A canonical: `docs/research/insider_form4_opportunistic_phase_a_2026_05_08.json`
- Phase B canonical: `docs/research/insider_form4_opportunistic_phase_b_2026-05-09.json`
- Phase B human report: `docs/research/insider_form4_opportunistic_phase_b_2026-05-09.md`
- Design memo: `docs/research/insider_form4_opportunistic_design_2026_05_05.md`
- ADR 0007 (layer architecture): `docs/adr/0007-layer-architecture.md`
