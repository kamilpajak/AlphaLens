# v8 design — LightGBM quantile loss (fresh class)

**Status:** **REJECTED 2026-05-01 PM — never registered.** Adversarial review (zen +
perplexity) surfaced three flaws: (1) FATAL — LightGBM with `objective='quantile'`
hardcodes hessian=1.0 (pinball loss has zero second derivative), degrading
tree split-gain calculations and producing tail-blind splits, while selection
rule trades extreme tails → rebuilds v4's rank-blindness in nonlinear form;
(2) max_depth=3 + min_child_samples=2000 OVER-regularizes for 222k×10
(n/p≈22,200 supports deeper trees per perplexity rec); (3) fresh-class
threshold 1.96 dodges multiplicity since same 8 prior tests on this holdout.
Synthesis design v9 addresses all three: see `v9_lightgbm_mse_design_2026_05_01.md`
(LOCKED, registered as `nonlinear_alt_data_v1_lightgbm_mse_2026_05_01`).

**NEW class (still applies):** `nonlinear_alt_data_search_2026_05_01` (fresh, n=1 → |t|≥1.96).

## Why a fresh class

Class `alt_data_screener_search_2026_04_30` is **EXHAUSTED on linear-Lasso
architecture** after 4/4 FAILs. v3-v4 settled the diagnostic: linear-Lasso
with rank target is rank-blind to magnitudes, and selection rules of any tail
aggressiveness get crushed by 2024-2026 short-squeeze regime even with HTB
filtering. Continuing in the linear-Lasso class with v5 (quantile L1 on raw
return) would be largely mathematically redundant per the v5 adversarial
review (zen Objection #3: predictions ~95% Spearman-correlated with v3-v4).

The honest pivot is to a **structurally different model class**: gradient-
boosted trees with quantile loss. This changes:
- Functional form: linear → nonlinear (tree splits + boosting)
- Loss: MSE on rank → quantile pinball on raw return (magnitude-aware)
- Interaction modeling: features can interact via splits, Lasso couldn't
  capture this

These three changes are bundled into ONE class hypothesis (LightGBM with
quantile loss on these features). Fresh class → fresh n=1 → |t|≥1.96. This
is NOT cheating: each new model class deserves its own pre-registration,
provided the program-level burnt-holdout caveat is acknowledged.

## Class state and Bonferroni accounting

- `alt_data_screener_search_2026_04_30` (CLOSED): 4/4 FAIL, exhausted on
  linear-Lasso. Threshold for any further test in THAT class would be |t|≥2.58.
- `nonlinear_alt_data_search_2026_05_01` (NEW): 0 prior tests. v1 fresh
  threshold |t|≥1.96.

**Program-level cumulative** count (across both classes + multi_source class):
- multi_source_two_stage_search_2026_04_30: 3 tests, all FAIL
- alt_data_screener_search_2026_04_30: 4 tests, 4 FAIL (1 abandoned)
- nonlinear_alt_data_search_2026_05_01: v1 = 8th overall test on this
  2024-04-30 → 2026-04-30 holdout window

The fresh-class Bonferroni argument is methodologically defensible because
each class tests a structurally distinct hypothesis (different features,
different model class, different target transform). Program-level threshold
would be n=8 → |t|≥2.74 if treating ALL tests as one family. v1-LGBM accepts
**diagnostic-only framing**: PASS at fresh-class |t|≥1.96 does NOT trigger
capital deploy; it triggers a v2-LGBM design on truly fresh data (post-
2026-04-30 continuation OR different feature universe). The capital-deploy
threshold remains program-level n+1 → |t|≥2.81 even on PASS.

## Hypothesis (one-liner)

LightGBM with `objective='quantile'`, `alpha=0.5` (median quantile), trained
on raw 20d-forward excess return target with the same 10 alt-data features
(short interest + insider + earnings SUE + filing density), produces
nonlinear feature interactions and magnitude-aware predictions that
linear-Lasso could not capture. Predicted scores are used in v4's settled
selection rule (long top-decile EW − short bottom-decile EW with SI≤15% HTB
filter). Tests whether tree-based magnitude-aware modeling extracts
additional signal from the same feature space.

## Detailed specification

### Features + target (carried forward from v4)

Same 10 alt-data features, same raw 20d-forward excess return target, same
PIT-correctness, same train/holdout split:
- Train: 2018-01-01 → 2024-04-29
- Holdout: 2024-04-30 → 2026-04-30 (BURNT — see caveat below)

NO rank transform. LightGBM with quantile loss handles heavy tails natively
via pinball loss + tree-based residual modeling.

### Model + hyperparameters (locked, conservative regularization)

```python
LGBMRegressor(
    objective='quantile',
    alpha=0.5,                # τ=0.5 = median (matches v5's planned setup)
    max_depth=3,              # shallow trees, prevents overfit on 10 features
    num_leaves=8,             # = 2^max_depth, consistent with depth=3
    min_child_samples=2000,   # large; we have 222k rows; prevents leaf overfit
    learning_rate=0.05,       # standard
    n_estimators=200,         # tunable via early stopping in CV
    reg_alpha=0.1,            # L1 penalty on leaf values
    reg_lambda=0.1,           # L2 penalty on leaf values
    feature_fraction=1.0,     # use all 10 features (no subsampling)
    bagging_fraction=0.8,     # row subsampling per tree
    bagging_freq=1,           # bagging every iteration
    verbose=-1,
    random_state=42,
)
```

Per zen's prior LGBM-on-small-features advice: with only 10 features and
222k rows, the overfit risk is in tree depth + leaf granularity, not in
feature subsampling. Conservative regularization above forces the model to
find robust splits.

### CV strategy: simple, minimum-HARKing-flexibility

Pre-reg locks ALL hyperparameters EXCEPT `n_estimators`. CV tunes only
`n_estimators` via early stopping on fold-validation pinball loss. This
minimizes design surface (one hyperparameter tuned, not a 5-dim grid).

3-fold expanding-window CV with 60-day embargo (same machinery as v3-v4).
For each fold:
1. Train LGBMRegressor on fold-train with `early_stopping_rounds=20`
   monitoring fold-val pinball loss.
2. Record optimal n_estimators.
3. Mean optimal n_estimators across 3 folds → final n_estimators.
4. Refit on full train pool with that fixed n_estimators.

### Selection rule (carried forward from v4, settled)

Identical to v4:
- Long-eligible universe: full post-ADV cross-section.
- Short-eligible universe: post-ADV ∩ {short_interest_pct_float ≤ 0.15}.
- Long leg: top decile by predicted score, EW.
- Short leg: bottom decile by predicted score, EW.
- Per-asof return: long_mean − short_mean.
- Cost: 60bps round-trip (2 × 30bps per leg) + 1.5% borrow scenario.
- HAC maxlags=5, Lo-2002 Sharpe.

### Pass / fail criteria (fresh class n=1 → |t|≥1.96)

| Gate | Pass-rule | Source |
|---|---|---|
| Carhart-4F α t-stat (HAC=5) on L/S | ≥ 1.96 | n=1 fresh class |
| Lo-2002 Sharpe (net) on L/S | ≥ 0.5 | same as v4 |
| α annualized (gross) | ≥ 3% | same as v4 |
| MaxDD (net) | ≥ −35% | same as v4 |
| ≥1 nonzero tree split (n_estimators ≥ 1) | trivial | sanity |
| In-CV "IR equivalent" (mean fold rank-IC / std fold rank-IC) ≥ 1.0 | from v4 | catches noise-overfit n_estimators |
| Holdout mean per-asof rank-IC > 0 | from v4 | true-OOS sanity |
| Mkt-RF beta (descriptive) | \|β\| ≤ 0.20 | sanity, not gating |
| MID rule | α t ≥ 1.5 AND Sharpe ≥ 0.3 AND IR ≥ 0.5 AND holdout IC > 0 | refine + re-pre-reg |
| FAIL rule | anything else | log to ledger |

The IR + holdout-IC gates are RETAINED unchanged from v4. Rank-IC measures
cross-sectional ordering accuracy regardless of model class.

### Burnt-holdout caveat (locked, intensifying further)

8th cumulative test on 2024-04-30 → 2026-04-30. Fresh-class threshold |t|≥1.96
is the IN-CLASS Bonferroni; capital deploy requires program-level cumulative
n+1 → |t|≥2.81. **v1-LGBM is DIAGNOSTIC ONLY** like v3 and v4. PASS at
fresh-class threshold triggers v2-LGBM on truly fresh data (post-2026-04-30
continuation, or different features). FAIL at fresh-class threshold
strongly implicates the FEATURE SPACE (option E) as the next-class pivot.

## Implementation plan

1. **`alphalens/screeners/multi_source_two_stage/model.py`**: add
   `fit_lightgbm_quantile_global` parallel to `fit_global` /
   `fit_quantile_l1_global`. Returns RegimeFit-shaped bundle:
   - `model`: LGBMRegressor instance (fitted)
   - `feature_names`: tuple from caller
   - `feature_medians`: column medians for NaN imputation
   - `chosen_alpha`: best n_estimators (overloaded field name)
   - `cv_mean_mse`: actually CV mean pinball (overloaded for compatibility)
   - `n_train_obs`: train pool size
   - `n_nonzero_coefs`: tree feature_importances_ > 0 count (overloaded)
2. **`scripts/experiment_lightgbm_quantile_longshort_20d.py`** (new):
   Clone of `experiment_alt_data_lasso_longshort_20d.py`. Replace
   `fit_global(... target_transform='rank')` with
   `fit_lightgbm_quantile_global(... quantile=0.5)`. Output paths:
   `docs/research/nonlinear_alt_data_v1_phase_b.md` and
   `..._v1_audit.json`. Verdict thresholds set to PASS_T=1.96.
3. **Tests:** `tests/test_lightgbm_quantile_v1.py`:
   - `fit_lightgbm_quantile_global` on synthetic data with known monotone
     signal produces nonzero feature importances.
   - Quantile predictions are in-distribution for raw target (not rank).
   - CV early stopping converges within sensible compute time.

## Adversarial review (anticipated; review pending)

Stage 1 (zen / gemini-3-pro-preview, thinking_mode=high) — pending.
Anticipated concerns:
- Tree overfit on 10 features × 222k rows — even with conservative regularization
- Why fresh class (lower bar) NOT same class with stricter Bonferroni? Honest answer above
- LightGBM stochasticity (bagging seed) introduces randomness vs deterministic Lasso
- Quantile loss in trees is well-supported but LightGBM-specific quirks?

Stage 2 (perplexity / sonar-reasoning-pro) — pending.
Anticipated framing:
- Tree-based methods on alt-data are well-studied (Gu-Kelly-Xiu 2020 etc.)
- HARKing concern same as v4-v5 (program-level burnt holdout)
- Conservative hyperparameter locking is the right HARKing-mitigation discipline

## Honest expectation

**Scenario 1 (P ≈ 35%): MID — nonlinear interactions help modestly.** LightGBM
finds tree splits that linear Lasso couldn't. α t-stat in 1.5-2.5 range. v2
candidate: same model on fresh data when calendar permits.

**Scenario 2 (P ≈ 35%): FAIL — features remain weak even nonlinearly.**
LightGBM rediscovers the same 2-feature signal (rank_SI + filing_density)
that Lasso found, with slightly better prediction quality but not enough
to overcome 2024-2026 squeeze regime. Diagnostic: feature space is dead,
pivot to option E (different features).

**Scenario 3 (P ≈ 18%): FAIL — overfit on noise.** Conservative regularization
is insufficient; trees memorize 2018-2023 patterns that don't generalize.
α t-stat near 0 with high variance. Diagnostic: increase regularization or
abandon nonlinear class.

**Scenario 4 (P ≈ 12%): PASS-DIAGNOSTIC.** All gates clear at fresh-class
|t|≥1.96. v2-LGBM on fresh data → real test. Capital deploy still off-table
until program-level threshold |t|≥2.81 cleared.

The honest weighting is FAIL ≈ MID > PASS. Run because info value is high
under every scenario (settles linear-vs-nonlinear variable definitively)
AND compute is acceptable (~5-10 min LightGBM training on 222k×10 vs
hours-or-days for QuantileRegressor LP).
