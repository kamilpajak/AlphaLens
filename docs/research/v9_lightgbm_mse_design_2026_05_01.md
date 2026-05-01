# v9 design — LightGBM MSE (revised post-v8 adversarial review)

**Status:** LOCKED 2026-05-01 PM. Synthesis of zen + perplexity adversarial reviews
on v8 (LightGBM quantile, REJECTED).

**Class:** NEW `nonlinear_alt_data_search_2026_05_01` (n=1 in-class).

## What's the design

Identical to v8 (LightGBM-quantile, rejected) EXCEPT three changes addressing
the adversarial review findings:

| Variable | v8 (rejected) | v9 (locked) | Source |
|---|---|---|---|
| LightGBM `objective` | `'quantile'` | **`'regression'`** (MSE) | zen rec #1 |
| LightGBM `alpha` | 0.5 | n/a (not used by MSE) | zen rec #1 |
| `max_depth` | 3 | **5** | perplexity rec #3 |
| `min_child_samples` | 2000 | **500** | perplexity rec #3 |
| Threshold | fresh-class 1.96 | **program-level 2.74** | zen rec #2 |

All other variables locked from v8 design (which inherited from v4):
- 10 alt-data features unchanged
- Train 2018-01-01 → 2024-04-29
- Holdout 2024-04-30 → 2026-04-30 (BURNT, see caveat)
- Selection rule: v4 long top-decile EW − short bottom-decile EW with SI≤15%
- Stride 5d / holding 20d / HAC=5 / Lo-2002 Sharpe / 60bps cost / 1.5% borrow

## Why MSE instead of quantile

zen's review of v8 surfaced a fatal flaw: LightGBM with `objective='quantile'`
sets the second derivative (hessian) to a constant 1.0 because the pinball
loss has zero second derivative. Tree boosting requires real gradient AND
hessian for split-gain calculation; with hessian=1.0, the model loses the
Newton-step advantage and becomes "sluggish, prioritizing splits that
separate large chunks of bulk data rather than identifying nuanced signal."

Combined with `alpha=0.5` (median quantile), trees would be tail-blind by
design while the selection rule (decile L/S) trades the extreme tails. This
rebuilds v4's rank-blindness in nonlinear form.

`objective='regression'` (MSE) gives LightGBM a real second derivative
(hessian = 1 for L2 loss but still drives the right Newton steps because
the gradient scales with residual magnitude). MSE is naturally
magnitude-aware: tail observations contribute proportionally to gradient,
not just sign.

The known concern with MSE on raw return target — heavy-tail collapse that
killed v2's Lasso — is mitigated by tree boosting's robustness: each weak
learner only needs to capture a fraction of residual, regularization
(reg_alpha + reg_lambda + bagging) prevents tail-memorization.

## Why max_depth=5 / min_child_samples=500

perplexity flagged v8's max_depth=3 + min_child_samples=2000 as
over-regularized for the 222k × 10 feature space. With n/p ≈ 22,200 and
learning-curve theory favoring deeper trees when sample-to-feature ratio
is high, max_depth=3 prevents the secondary splits where alt-data
conditional signals live (e.g., "high SI AND high filing density").

max_depth=5 (32 leaves max) + min_child_samples=500 (~0.2% of train pool
per leaf, vs 0.9% in v8) permits more granular conditional splits while
still preventing pure noise memorization.

## Why program-level threshold 2.74

zen Objection #2 on v8: lowering threshold from |t|≥2.58 (alt_data class
n=5) to |t|≥1.96 (nonlinear_alt_data class n=1) when the SAME holdout has
been observed 8 times across classes is "statistical self-deception."
Same features, same target, same holdout, same selection rule = NOT
independent tests. Bagging seed at 1.96 = unacceptable false-positive risk.

v9 acknowledges this directly: in-class n=1 → fresh-class 1.96 is the
internal Bonferroni; **the verdict gate is program-level n=8 → |t|≥2.74**.
Pre-reg explicitly states: PASS at 2.74 is the only threshold that triggers
follow-up; PASS in [1.96, 2.74) is reported as "in-class PASS, program-FAIL"
and treated as FAIL for actionability.

## Hyperparameters (locked)

```python
LGBMRegressor(
    objective='regression',     # MSE — magnitude-aware via real hessian
    max_depth=5,                # ~32 leaves max; permits 5-level interactions
    num_leaves=32,              # = 2^max_depth
    min_child_samples=500,      # ~0.2% of train pool per leaf
    learning_rate=0.05,
    n_estimators=200,           # tunable via early stopping in CV
    reg_alpha=0.1,              # L1 penalty on leaf values
    reg_lambda=0.1,             # L2 penalty on leaf values
    feature_fraction=1.0,
    bagging_fraction=0.8,
    bagging_freq=1,
    verbose=-1,
    random_state=42,
)
```

Single hyperparameter tuned via CV: `n_estimators` via early stopping
(patience=20) on fold-validation MSE. All others LOCKED.

## CV strategy (unchanged from v8 design)

3-fold expanding-window CV with 60-day embargo (same as v3-v4-v8). For each
fold, fit LGBMRegressor with `early_stopping_rounds=20` monitoring fold-val
MSE. Mean optimal n_estimators across 3 folds → final n_estimators. Refit
on full train with that fixed n_estimators.

## Pass / fail criteria

| Gate | Pass-rule | Notes |
|---|---|---|
| Carhart-4F α t-stat (HAC=5) on L/S | ≥ **2.74** | program-level n=8 (HONEST threshold) |
| Lo-2002 Sharpe (net) on L/S | ≥ 0.5 | same as v4 |
| α annualized (gross) | ≥ 3% | same as v4 |
| MaxDD (net) | ≥ −35% | same as v4 |
| In-CV "rank-IR" ≥ 1.0 | retained from v4 | catches noise-overfit n_estimators |
| Holdout mean per-asof rank-IC > 0 | retained from v4 | true-OOS sanity |
| Mkt-RF beta (descriptive) | \|β\| ≤ 0.20 | sanity, not gating |
| In-class report at 1.96 ≤ t < 2.74 | "in-class PASS, program-FAIL" | for ledger transparency |
| MID rule | α t ≥ 1.5 AND Sharpe ≥ 0.3 AND IR ≥ 0.5 AND holdout IC > 0 | refine + re-pre-reg |
| FAIL rule | anything else | log to ledger |

## Burnt-holdout caveat (locked)

8th cumulative test on 2024-04-30 → 2026-04-30. Capital deploy off-table
regardless of verdict. v9 PASS at program-level 2.74 triggers v2-LGBM on
truly fresh data (post-2026-04-30 continuation OR analyst-revisions feature
expansion in NEW class).

## What v9 settles

ONE structural variable: linear-Lasso vs tree-boosting on this feature
space + selection rule. v3-v4 ruled out linear with rank target. v9 tests
tree with magnitude-aware loss. Other variables (selection rule, feature
space, holdout) UNCHANGED.

If v9 FAIL: strong evidence that selection rule (decile L/S) is the
structural bottleneck — magnitude-aware nonlinear modeling didn't fix the
squeeze-vulnerability. Next experiment: long-only construct (Path β) OR
new feature space (Path γ Option E).

If v9 MID/PASS: tree class works on this data → next experiment with new
features in same model class makes sense (Path γ on top of v9).

## Implementation plan

1. **`alphalens/screeners/multi_source_two_stage/model.py`**: add
   `fit_lightgbm_mse_global` parallel to `fit_global`. Wraps LGBMRegressor
   with the locked hyperparameters; returns RegimeFit-shaped bundle.
2. **`scripts/experiment_lightgbm_mse_longshort_20d.py`**: clone v4 driver,
   swap fit function, update verdict thresholds (PASS_T=2.74, MID_T
   unchanged at 1.5).
3. **`tests/test_lightgbm_mse_v1.py`**: synthetic-data unit tests for the
   new fit function.
4. Run Phase B → write audit + report → close ledger → memory.

## Honest expectation

Per adversarial review trajectory:

**Scenario 1 (P ≈ 30%): MID — trees + MSE recover modest signal.** α t-stat
in 1.5-2.5 range. In-class PASS at 1.96 but program-FAIL at 2.74. Outcome
treated as FAIL for actionability; informational for next-class design.

**Scenario 2 (P ≈ 45%): FAIL — selection rule is the bottleneck.** Trees
find the same 2-feature signal as Lasso, predictions decile-separate,
short leg still gets squeezed in 2024-2025. α t-stat 0 to slightly
negative. Strong evidence to pivot to Path β (long-only) or Path γ
(new features).

**Scenario 3 (P ≈ 18%): FAIL — overfit despite less aggressive regularization.**
Trees memorize 2018-2023 patterns; OOS deteriorates. Recover via more
aggressive reg in a possible v2-LGBM, but more likely abandon nonlinear class.

**Scenario 4 (P ≈ 7%): PASS-DIAGNOSTIC at 2.74.** All gates clear at
program-level threshold. v2-LGBM on truly fresh data (post-2026-04-30 OR
new features) is the deploy gate. Capital still off-table.

Honest weighting: FAIL ≫ MID > PASS. Run because info value is high under
every scenario, compute is cheap (~10 min LightGBM training on 222k×10),
and the variable settled (linear vs tree) was the most-cited next-class
candidate across prior memos.
