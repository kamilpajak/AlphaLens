# Design memo — Slippage stress test on `insider_form4_opportunistic` (2026-05-12)

**Status:** LOCKED 2026-05-12 (pre-compute; gates frozen BEFORE looking at numbers)
**Class:** DIAGNOSTIC (not a new pre-registered hypothesis test — Bonferroni overlay-class budget unspent)
**Owner:** Kamil Pająk (solo)
**Compute envelope:** ~5–10h pod time for full audit re-run; ~10 min local diagnostic pass
**Plan reference:** `/Users/jacoren/.claude/plans/staged-munching-tiger.md`
**Base under stress:** `insider_form4_opportunistic` (Cohen-Malloy 2012 opportunistic Form-4 classifier; PASS_MARGINAL on 2018-2023 OOS αt=+2.71 and 2024-2026 final lock αt=+2.69)

## 1. What this memo is

This is a **diagnostic**, not a new hypothesis test. It re-evaluates the existing
`insider_form4_opportunistic` Carhart-4F regression under varying transaction
cost assumptions. It does NOT propose a new screener, gate, overlay, or
combination — therefore it does NOT consume Bonferroni budget. But the cost grid,
regime amplification function, decision gates, and failure modes ARE
pre-registered here BEFORE any compute, to prevent post-hoc anchoring on
favorable thresholds.

The test answers a single question: *does the +24.4%/y / αt=+2.71 alpha of
`insider_form4_opportunistic` survive realistic R2000 microstructure costs,
particularly under regime-conditional spread amplification in the Q5-panic
quintiles where the alpha concentrates (+68.85%/y at Q5 IWM-vol vs −25.65%/y at
Q1)?*

## 2. Adversarial review summary (BEFORE compute burn)

### 2.1 Zen Round 1 (gemini-3-pro-preview, high thinking, 2026-05-12)

Asked: rank Option A (selection-gate on PASS_MARGINAL base) vs Option B (fresh
Layer 2 screener) vs Option C (retrospective triangulation on
`pc_abnormal_volume` paper-trade).

**Verdict introduced Option D**: pre-Option-A slippage diagnostic. Rationale:
+68.85%/y Q5 alpha may be a microstructure mirage — Polygon OHLCV has no order
book depth, so spread costs are not measured in current backtests. Form-4
filings have no timing discretion → fills happen at arrival prices, which in
panic regimes carry materially wider spreads. Spending a Bonferroni hit on
Option A before knowing if the underlying alpha is realizable is bad sequencing.

### 2.2 Perplexity Round 1 (sonar-reasoning-pro, high context, 2026-05-12)

Concurred with Option D as essential triage, not busywork. Empirical priors
cited:

- **Lesmond, Schill & Zhou (2004 JFE)**: transaction costs consume 50–70% of
  documented small-cap anomalies; effective half-spreads on smallest Russell
  deciles 200–400 bps in 1995–2002.
- **Hasbrouck (2009)** + **Corwin-Schultz (2012) HL estimator**: R2000 median
  effective half-spreads 30–80 bps post-decimalization, with right tail to
  200–300 bps during 2008/2020 stress.
- **Novy-Marx & Velikov (2016)**: in small-caps, holding period × cost dominates
  alpha ranking for net profitability.
- **Chordia-Roll-Subrahmanyam (2001)** + **Naes-Skjeltorp-Odegaard (2011)**:
  small-cap effective spreads roughly ~2× between vol decile 5 and decile 10.

Push-back vs zen's gate proposal: zen suggested shock at 5–10 bps. Perplexity
recommended **500–1000 bps round-trip during Q5 panic** as the stress floor.
Adopted: cost grid extends to 500 bps half-spread (= 1000 bps round-trip).

### 2.3 Plan agent (claude opus thinkdeep, 2026-05-12)

Verified `alphalens_cli/commands/audit.py:66-102` propagates `--cost-half-spreads`
to experiment script via `ctx.args` and `phase_robust_backtesting.audit_multi_phase.run_audit`
subprocess pass-through. No driver patch needed. Caught a critical convention
issue: the experiment's `alpha_net_4f` scalar = `α_gross − drag_ann` is
INSUFFICIENT — the t-stat itself remains the gross-returns t-stat. Diagnostic
script must re-regress on net-of-drag daily series. Memo §6 documents this.

Pushed back on the arbitrary Q5=4× scenario. Replaced with literature-anchored
`half_spread(t) = base × (1 + β × max(0, (σ_60d(t) − σ_median) / σ_median))`,
β=2 as primary anchor.

### 2.4 Zen Round 2 (gemini-3-pro-preview, high thinking, 2026-05-12)

Reviewed initial plan. Three concrete amendments locked:

- **Z1 — Gate floor raised**: β=2 regime-stress gate moved from αt≥1.0 to αt≥1.5.
  Rationale: t=1.0 → p≈0.32 is indistinguishable from noise; a "barely
  realizable" claim cannot rest on a p~0.3 result. αt=1.5 ≈ p≈0.13 is the
  defensible floor.
- **Z2 — Turnover dump architecture rewritten**: original plan had one-phase
  local dump + forward-fill broadcast. Replaced with per-phase auto-dump
  alongside `phase_N_returns.parquet`. Counter-cyclical strategy means
  transactions cluster in narrow Q5 stress windows; forward-fill would smooth
  precisely the Q5 cost spike β=2 is designed to measure.
- **Z3 — Post-drag cyclicality reversal check added as mandatory observation
  gate** (§9 below). If β=2 amplification erases Q5 alpha but leaves Q1 calm-regime
  alpha intact, the strategy's classification flips from counter-cyclical →
  pro-cyclical, which reopens Layer 4 overlay class (vol-target,
  drawdown-control) currently REJECTED.

Final zen verdict: **approve, proceed**.

## 3. Periods (frozen — reuse existing audit windows)

Identical to the pre-reg windows from `insider_form4_opportunistic_design_2026_05_05.md`:

- **OOS retrospective (5-phase audit, primary verdict source)**: 2018-01-01 →
  2023-12-31 (6yr).
- **Final lock (single-phase, prose-discount-flagged in original pre-reg)**:
  2024-01-01 → 2026-04-30 (2.3yr).

Both windows are re-run with the cost grid in §4. No new windows introduced.

## 4. Cost grid (frozen)

`half_spread_bps ∈ {5, 25, 50, 75, 100, 150, 200, 500}`

Anchor points:

| Half-spread | Round-trip | Anchor |
|---|---|---|
| 5 bps | 10 bps | paper-trade floor / current experiment default |
| 25 bps | 50 bps | tight large-cap spread |
| 50 bps | 100 bps | **R2000 post-decimalization median** (Corwin-Schultz 2012, Hasbrouck 2009) |
| 75 bps | 150 bps | upper R2000 typical |
| 100 bps | 200 bps | **stress-regime central** (Lesmond-Schill-Zhou 2004 small-cap mean) |
| 150 bps | 300 bps | high R2000 right-tail |
| 200 bps | 400 bps | **2008/2020 stress observation** (Corwin-Schultz right-tail) |
| 500 bps | 1000 bps | worst-case microcap during liquidity crisis |

Drag formula (per `RealisticCostModel.primary_period_drag_bps()` semantics in
`alphalens/attribution/cost_model.py`):

```
drag_per_period_bps = (half_spread_bps + adverse_selection_bps) × turnover_fraction × 2  # round-trip
adverse_selection_bps = 5 (held constant per RealisticCostModel default)
```

## 5. Regime amplification formula (frozen)

```
effective_half_spread(t) = base_bps × (1 + β × max(0, (σ_60d(t) − σ_median) / σ_median))
```

Where:
- `σ_60d(t)` = trailing 60-day realized volatility of IWM (Russell 2000 ETF) at
  date `t`, daily-rebalanced.
- `σ_median` = full-sample median of `σ_60d` over the test window
  (2018-01-01 → 2026-04-30), computed once, full-sample percentile cut (consistent
  with `assign_vol_regime_quintiles()` convention).
- `β ∈ {0, 1, 2, 3}`.

Interpretation:

| β | Meaning |
|---|---|
| 0 | uniform baseline (no regime amplification; matches existing experiment cost model) |
| 1 | mild amplification (modest spread widening in high-vol) |
| **2** | **PRIMARY anchor**: Chordia-Roll-Subrahmanyam 2001, Naes-Skjeltorp-Odegaard 2011 (small-cap spreads ~2× between vol decile 5 and 10) |
| 3 | high amplification (stress sensitivity scenario) |

A separate `worst_case_quintile` scenario (Q5=4×, Q4=2×, Q1-Q3=1×) is reported
alongside as labeled extremity but **NOT used as a gate** (functional form is
discontinuous, not anchored to a published model).

## 6. αt computation convention (frozen)

The diagnostic must compute αt by **re-regressing the net-of-drag daily series
on Carhart-4F factors with HAC `maxlags=126`** (matches signal window convention
from original pre-reg). The experiment script's `alpha_net_4f` scalar
(= `α_gross − annualized_drag`) is INSUFFICIENT — it leaves the t-statistic
equal to the gross-returns t-stat by construction (drag is constant per
(half_spread, β) combination, but for regime-conditional cases drag is
time-varying; either way, re-regression is required to get the correct SE).

Net daily series construction:

```
drag_t = (effective_half_spread_t + 5_bps) × turnover_t / period_days  # per-day drag in bps
rets_net_t = rets_gross_t − drag_t / 10000
```

Where `turnover_t` is the per-rebalance turnover broadcast to daily over the
21-day post-rebalance window (drag concentrated at execution, NOT smeared
across the holding period via forward-fill).

## 7. Decision gates (frozen — pre-commit BEFORE compute)

For the strategy to be classified **REALIZABLE**, ALL gates must hold across
BOTH OOS (2018-2023) and final-lock (2024-2026) windows:

- **G1 (Primary)**: `αt_net ≥ 2.0` at `half_spread=50bps, β=0` (uniform baseline
  at R2000 median spread).
- **G2 (Stress uniform)**: `αt_net ≥ 1.5` at `half_spread=100bps, β=0` (uniform
  at stress-central spread).
- **G3 (Regime stress)**: `αt_net ≥ 1.5` at `half_spread=50bps, β=2`
  (regime-amplified at median base spread). Raised from αt≥1.0 per zen
  Round 2 — t=1.0 → p≈0.32 is indistinguishable from noise.
- **G4 (Secondary realism)**: `Sharpe_net(50bps, β=2)` over both windows
  pooled `>` `Sharpe_net(SPY-buy-hold)` over same period.

## 8. Failure modes (frozen)

- **KILL** if `αt_net < 1.0` at `half_spread=50bps, β=0` on either window
  (signal cannot survive median R2000 spread under uniform cost; structural
  failure).
- **FLAG** if G1 passes on both windows but ANY of G2, G3, G4 fails (signal is
  realizable only under benign microstructure assumptions; defer paper-trade
  promotion; prioritize Option A gate designs that depress Q5-conditional
  turnover before any further work on this base).
- **REALIZABLE** only if ALL of G1–G4 pass on both windows.

## 9. Post-drag cyclicality reversal check (mandatory observation gate)

After computing net returns under each β scenario, re-run
`classify_cyclicality_excess(net_returns, iwm_returns)` from
`alphalens/attribution/signal_vol_regime.py`. Persist
`R_excess_pre_drag` (currently −2.67 from `feedback_universe_baseline_cyclicality_2026_05_10.md`)
vs `R_excess_post_drag` per `(window, β)` combination.

Decision rule:

- If `R_excess_post_drag − R_excess_pre_drag ≥ +1.0` for `β=2` on EITHER window
  (i.e., classification shifts from EXTREME counter-cyclical toward orthogonal
  or pro-cyclical), flag as **MATERIAL FINDING** in postmortem.
- Material finding consequence: it reopens Layer 4 overlay class (vol-target,
  drawdown-control, CPPI), which was REJECTED on 2026-05-10 based on
  pre-cost counter-cyclical mechanism. The rejection memo
  (`insider_form4_overlay_REJECTED_2026_05_10.md`) would need to be amended
  with a post-cost re-evaluation epilogue.
- This is reported as a SEPARATE verdict line in the postmortem, independent of
  the REALIZABLE / FLAG / KILL classification.

Mechanism: drag is itself counter-cyclically heavy by construction (high
turnover × high spread in Q5 panic regimes). Erasing Q5 alpha while leaving
Q1-Q3 alpha intact mechanically flips the cyclicality classification.

## 10. Post-2010 sanity subsample

Re-run G1–G4 restricted to `>= 2010-01-01` for both windows (only the OOS
window has any observations <2010, but inclusion of the subsample test
hedges against post-2008 microstructure regime change). If verdict flips
between full-sample and post-2010, document in postmortem prose. No gate
adjustment based on subsample.

## 11. Why this is diagnostic (no Bonferroni hit)

- No new screener, gate, overlay, or compound being tested.
- Same hypothesis (`insider_form4_opportunistic` Carhart-4F α > 0) re-evaluated
  under varying cost overhead.
- No new test in the program-level test count; pre-reg ledger entry for
  `insider_form4_opportunistic` is amended in-place with a slippage-test
  verdict line.
- Comparable precedent: cost-sensitivity tables are routinely computed
  post-hoc in `alphalens/attribution/cost_model.py::cost_sensitivity_table`
  without Bonferroni accounting; this diagnostic merely extends that
  convention with regime amplification + re-regression.

If the diagnostic finds the strategy KILL or FLAG, the consequence is to
**withdraw or downgrade** an existing entry, not to add a new entry. Bonferroni
counts new tests, not amendments to existing ones.

## 12. Verification gates (smoke before postmortem)

The diagnostic script must pass these smoke checks before its output is read
for verdict:

1. **Zero-turnover invariance** (unit test): `rets_net = rets_gross` when
   `turnover_t = 0 ∀t`. Drag must be exactly zero.
2. **Constant-spread cross-check** (unit test): at `half_spread=100bps, β=0,
   turnover=0.65 constant`, annualized drag must match
   `RealisticCostModel.primary_period_drag_bps(100, 0.65) × periods_per_year`
   within rounding.
3. **Cost monotonicity** (smoke check): `αt_net` must monotonically decrease in
   `half_spread_bps` for each `(window, β)` combination. Any non-monotonicity
   = bug.
4. **Experiment-scalar cross-check** (smoke check): at `(H=50bps, β=0)`, the
   diagnostic's `α_annualized_net` must match the experiment's emitted
   `alpha_net_4f` scalar within 0.1%. If not, drag formula has drifted.
5. **Regime amplification sanity** (smoke check): at `β=2`, mean per-day drag
   on `σ_60d > 90th percentile` dates must be at least 2× mean per-day drag on
   `σ_60d < 10th percentile` dates. If not, regime broadcast is broken.

## 13. Outputs

- `~/.alphalens/diagnostics/insider_form4_slippage_results_2026_05_12.parquet`
  — wide table indexed by `(window, half_spread_bps, β, subsample)` with
  columns `(αt_net, α_annualized_net, Sharpe_net, drag_ann_bps, R_excess_post_drag,
  Q1_ret, Q2_ret, Q3_ret, Q4_ret, Q5_ret)`.
- `docs/research/insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md`
  — verdict memo with per-gate pass/fail quoted literally from results table.

## 14. Status

**LOCKED 2026-05-12.** No compute has been run. Cost grid, β values, gates,
failure modes, post-drag cyclicality check, and post-2010 subsample are
frozen. Any deviation from this spec post-compute is reported as memo
violation in the postmortem.

User signoff: pending (this memo is the artifact for signoff).
