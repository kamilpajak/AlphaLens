# v7 design — magnitude-aware loss (Quantile L1 on raw return)

**Status:** **REJECTED 2026-05-01 PM — never registered.** Adversarial review (zen +
perplexity) surfaced two FATAL flaws: (1) `sklearn.QuantileRegressor` LP solver is
compute-infeasible on 222k rows × 25 λs × 3 folds (sklearn docs warn against
n_samples > 10,000); (2) Quantile-L1 on raw vs Lasso-L1 on rank are
mathematically near-equivalent in monotone-signal regimes — predicted scores
expected ~95% Spearman-correlated with v3-v4, would burn the holdout to re-sort
the same predictions. Project pivoted directly to Path B (skip v5, fresh class
LightGBM) — see `v8_lightgbm_quantile_design_2026_05_01.md` (also rejected) and
`v9_lightgbm_mse_design_2026_05_01.md` (locked).

**Original class plan:** Same-class continuation of `alt_data_screener_search_2026_04_30` (n=5 → |t|≥2.58).

## Hypothesis (one-liner)

v4 settled rank-blindness as the fatal flaw of the v3-v4 architecture: rank-
target Lasso compresses heavy-tailed return distribution to bounded [-0.5, +0.5]
target, which makes the model invariant to magnitudes. Selection rules of any
tail aggressiveness (top-30 in v3, decile L/S in v4) get crushed by tail return
events the model couldn't see. **v5 swaps the inner-solver loss from
MSE+L1-on-rank-target to Quantile(τ=0.5)+L1-on-raw-return-target.** Quantile
regression at the median is robust to heavy tails (linear loss in residual
magnitude, not quadratic) AND magnitude-aware (target stays as raw return).
This bypasses both v2's heavy-tail collapse (which forced λ_max → zero coefs)
and v4's rank-blindness (which lost magnitude info).

## What changes vs v4

| Variable | v4 | v5 |
|---|---|---|
| Features | 10-feature alt-data | **same 10 features** |
| Target raw | 20d-forward excess return | **same raw target** |
| Target transform | per-asof percentile rank − 0.5 | **NONE — raw return** |
| Inner loss | Lasso = MSE + L1 | **QuantileRegressor τ=0.5 = pinball + L1** |
| CV objective | argmin mean fold MSE on rank target | **argmin mean fold pinball on raw target** |
| n_folds / embargo / grid points | 3 / 60d / 25 | **same** |
| Universe + ADV / train / holdout | identical | **same** |
| Stride / holding / HAC / Sharpe | identical | **same** |
| Selection rule | long top-decile EW − short bottom-decile EW with SI≤15% | **same (settled in v4)** |
| Cost mechanic | 60bps round-trip + 1.5% borrow | **same** |
| Bonferroni n | 4 → \|t\|≥2.50 | **5 → \|t\|≥2.58** |

ONE variable changes: **the inner-solver loss**. Selection rule + portfolio
construction is locked from v4; only the model that produces the score changes.

## Why quantile (not Huber)

Three options considered:

1. **Huber + L1 on raw return (REJECTED).** sklearn's `HuberRegressor` only
   supports L2 penalty. Custom L1+Huber implementation = scope creep, no
   pre-registered library.
2. **Quantile τ=0.5 + L1 on raw return (CHOSEN).** sklearn 1.8 native
   `QuantileRegressor(quantile=0.5, alpha=...)` with HiGHS interior-point
   solver. Pinball loss = absolute residual at τ=0.5 (= median regression).
   Linear in residual magnitude, magnitude-aware in target, sparsity via L1.
3. **Quantile τ=0.9 + L1 on raw return (DEFERRED).** Predicting upper-decile
   conditional return (τ=0.9) would be MORE aligned with long-leg selection,
   τ=0.1 with short leg. But mixing τ across legs = TWO design variables
   (loss + selection asymmetry); HARKing if added now. If v5 fails, v6 can
   isolate τ.

## Detailed specification

### Inner solver swap (the only change)

For each fold in 3-fold expanding CV with 60d embargo:
1. Standardize features on fold-train (StandardScaler, fit on train only).
2. Fit `QuantileRegressor(quantile=0.5, alpha=λ)` for each λ in glmnet-style
   log-spaced grid (25 points, λ_min/λ_max = 1e-3).
3. Compute fold-validation pinball loss = mean |y_val − y_pred|.
4. Select λ minimizing mean fold pinball loss across 3 folds.
5. Refit at chosen λ on full train pool.

Note: `QuantileRegressor`'s `alpha` is multiplicative on the L1 penalty
(equivalent to Lasso's `alpha`). λ_max grid construction reuses the v4
`_lambda_grid` helper since `‖X' y‖_∞ / n` is the same closed form for
selecting smallest λ that zeros all coefs at τ=0.5 (median LP).

### Why this might break the v3-v4 pattern

The pinball loss at τ=0.5 is:
  ρ(r) = |r| (independent of sign at the median)

Compared to MSE = r²:
- For small residuals (|r| < 1), MSE penalizes LESS; quantile penalizes MORE.
- For large residuals (|r| > 1), MSE penalizes MUCH MORE (quadratic blow-up);
  quantile penalizes only linearly.

In v2, raw return target had |r| frequently > 1 (e.g. 100%+ moves on small
caps in 20d). MSE forced the optimizer to drive coefs toward zero to avoid
MSE blow-up on tail observations. Result: λ_max → all zero coefs.

Quantile regression treats |r|=2 the same way it treats |r|=0.02 (linear
scaling). The optimizer can fit a coefficient that's "right on the median"
without being penalized for tail prediction errors. This may permit nonzero
coefs without the heavy-tail collapse.

If the features carry magnitude-aware signal (rather than just rank-aware
signal), v5 should produce nonzero coefs whose predictions are more
magnitude-correlated with realized returns than v3-v4 rank predictions.

### Why this might NOT break the pattern

Two failure modes preserve v4's FAIL:

1. **Selection rule still picks tails.** Even if predicted scores are now
   magnitude-aware, the long top-decile / short bottom-decile selection still
   samples the extreme tails of the score distribution. If the 2024-2026
   short-squeeze regime is structurally adversarial to ANY signal that shorts
   high-attention names, v5 fails like v4. The diagnostic value: v5 isolates
   loss alone, so a v5 FAIL means selection rule (not loss) is the
   bottleneck → next class needs different selection geometry (long-only?
   factor-weighted? reversal trigger?).

2. **Features genuinely don't carry magnitude signal.** Bulk +0.0260 rank-IC
   (v3+v4) shows ordinal weak signal. If features only give rank info (not
   magnitude info), magnitude-aware loss extracts no additional signal and
   v5 fails like v4 with similar score-rank ordering. Diagnostic: feature
   space is dead, pivot to option E (different features) for next class.

### Pass / fail criteria (in-class n=5 → |t|≥2.58)

| Gate | Pass-rule | Source |
|---|---|---|
| Carhart-4F α t-stat (HAC=5) on L/S | ≥ 2.58 | Bonferroni n=5 |
| Lo-2002 Sharpe (net) on L/S | ≥ 0.5 | same as v4 |
| α annualized (gross) | ≥ 3% | same as v4 |
| MaxDD (net) | ≥ −35% | same as v4 |
| ≥1 nonzero coef | ≥ 1 / 10 | same as v4 |
| In-CV "IR equivalent" (mean fold rank-IC / std fold rank-IC) ≥ 1.0 | from v4 | catches noise-overfit λ |
| Holdout mean per-asof rank-IC > 0 | from v4 | true-OOS sanity |
| Mkt-RF beta (descriptive) | \|β\| ≤ 0.20 | sanity, not gating |
| MID rule | α t ≥ 1.5 AND Sharpe ≥ 0.3 AND ≥1 coef AND IR ≥ 0.5 AND holdout IC > 0 | refine + re-pre-reg |
| FAIL rule | anything else | log to ledger |

The IR + holdout-IC gates are RETAINED unchanged from v4 even though the
inner loss is now pinball, not MSE. The reason: rank-IC measures cross-
sectional ordering accuracy regardless of loss function; a magnitude-aware
quantile fit should produce predictions that are STILL ordinally correlated
with realized returns.

### Burnt-holdout caveat (locked, intensifying)

5th experiment on 2024-04-30 → 2026-04-30 holdout. Pre-reg discipline +
Bonferroni n=5 mitigates but the HARKing concern accumulates with each
additional test. **v5 is DIAGNOSTIC ONLY** — same as v4. Capital deploy
remains off-table regardless of verdict. PASS verdict triggers v6 design on
fresh data (post-2026-04-30 continuation as it accrues, OR class B LightGBM
with quantile loss on truly independent test set).

## Implementation plan (small)

1. **`alphalens/screeners/multi_source_two_stage/model.py`**: add
   `fit_quantile_l1_global(target_transform='raw', quantile=0.5)` parallel
   to `fit_global`. Reuses `_expanding_splits_with_embargo` + `_lambda_grid`.
   Returns same `RegimeFit`-shaped bundle with `model = QuantileRegressor`
   and `feature_medians` for NaN imputation. Backwards-compatible additions.
2. **`scripts/experiment_alt_data_quantile_lasso_longshort_20d.py`** (new):
   Clone of `experiment_alt_data_lasso_longshort_20d.py`. Replace
   `fit_global(... target_transform='rank')` with
   `fit_quantile_l1_global(... target_transform='raw', quantile=0.5)`.
   Output paths: `docs/research/alt_data_screener_v5_phase_b.md` and
   `..._v5_audit.json`. Verdict thresholds bumped to PASS_T=2.58.
3. **Tests:** new `tests/test_quantile_fit_v5.py` covering:
   - `fit_quantile_l1_global` on synthetic data with known quantile signal
     produces nonzero coefs at appropriate λ.
   - Rank-IC of quantile predictions vs realized > rank-IC of constant
     prediction (sanity).
   - Lambda grid + CV machinery converges within sensible compute time.

## Adversarial review summary (to be filled in)

Stage 1 (zen / gemini-3-pro-preview, thinking_mode=high) — pending.
Anticipated concerns:
- HARKing intensifies (5th test on burnt holdout)
- Selection rule still picks tails — magnitude-aware loss may not save it
- Quantile regression on 222k rows × 10 features × 25 λ × 3 folds = ~1875 LP
  solves; HiGHS interior-point should handle but may be ~10× slower than Lasso
- Why τ=0.5 not τ=0.9? (= why median not upper conditional?)

Stage 2 (perplexity / sonar-reasoning-pro) — pending.
Anticipated framing:
- Quantile/median regression in alt-data screening is well-precedented
  (Koenker-Bassett 1978, Engle-Manganelli 2004 CAViaR, recent ML quant lit)
- HARKing concern same as v4
- Borrow constraint same as v4

## Honest expectation

**Scenario 1 (P ≈ 30%): MID — magnitudes carry.** Quantile fit produces
non-trivial nonzero coefs, predicted scores are magnitude-correlated with
realised returns, decile spread α t-stat in 1.5-2.5 range. v6 candidate:
escalate to LightGBM + quantile loss on fresh data.

**Scenario 2 (P ≈ 40%): FAIL — features rank-only.** Quantile fit produces
similar score ordering to v3-v4 Lasso (since features only give rank info),
α t-stat near 0 or slightly negative. Diagnostic: feature space is dead,
pivot to option E (different features) for next class.

**Scenario 3 (P ≈ 22%): FAIL — selection rule still squeezed.** Magnitude-
aware loss does its job (predictions are magnitude-correlated) but bottom-
decile selection still gets crushed by 2024-2026 short squeeze even with
SI≤15% filter. Diagnostic: selection geometry is the bottleneck, not
loss/architecture. Next class: long-only, factor-weighted, or reversal-
trigger constructs.

**Scenario 4 (P ≈ 8%): PASS-DIAGNOSTIC.** All gates clear. v6 escalates to
fresh-data continuation OR LightGBM with same loss. Capital deploy still
off-table per pre-reg.

The honest weighting is FAIL > MID > PASS. Run v5 because info value is high
under every scenario AND the cost is low (data infra reused from v4).
