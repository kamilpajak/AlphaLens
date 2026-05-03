# v7 Phase B holdout reveal — FAIL (αt=+2.09 < 2.86 naive Bonferroni)

**Date:** 2026-05-03
**Pre-reg:** v7_smd_options_implied_2026_05_02

## Headline (PRIMARY = LONG TOP decile by Lasso prediction)

| Metric | Value |
| --- | ---: |
| n holdout rebalances | 101 |
| Sharpe (gross) | 0.69 |
| Sharpe (net 30bps RT) | 0.68 |
| Carhart-4F α (gross, ann) | +196.53% |
| Carhart-4F α (net, ann) | +196.23% |
| α t-stat (HAC=5) | **+2.09** |
| Excess vs MDY (gross, ann) | +57.63% |
| Excess vs MDY (net, ann) | +57.33% |
| Max drawdown (net cum) | -45.48% |

## L/S diagnostic (top − bottom decile, NOT primary verdict)

| Metric | Value |
| --- | ---: |
| Sharpe (gross) | -1.06 |
| Sharpe (net 60bps RT) | -1.06 |
| Carhart-4F α (gross, ann) | -1092.45% |
| α t-stat (HAC=5) | -3.25 |

## Lasso fit (standardized features, train period)

- α (penalty): 0.0008058
- n_train: 204968, CV-MSE: 0.06608
- nonzero coefs: 6 / 7 (options-feature subset: 4 / 4)

| Feature | Coef (standardized) |
| --- | ---: |
| ivp30 | +0.003891 |
| ivx30 | +0.01082 |
| ivx180_minus_ivx30 | +0.000979 |
| ivx30_over_hv20 | -0.005463 |
| reversal_1m | +0.0006437 |
| momentum_6m | -0 |
| rv_30d | +0.004513 |

## Phase A gates (TRAIN)

- Coverage: 100.0%
- Max pairwise |corr|: 0.5896
- Offending pair: None
- Feature columns used (post-remediation): ['ivp30', 'ivx30', 'ivx180_minus_ivx30', 'ivx30_over_hv20', 'reversal_1m', 'momentum_6m', 'rv_30d']

## Pre-reg discipline

- 7-feature whitelist FROZEN; multicollinearity remediation per pre-committed hierarchy.
- Single global Lasso, sklearn LassoCV (3-fold, 25 α grid points).
- ONE-shot holdout, no peek-and-tune.
- Carhart-4F attribution post-hoc on portfolio top-decile returns.
- L/S diagnostic reported as power-loss check, NOT additional Bonferroni test.
- Selection convention amended 2026-05-02 (LONG TOP decile per Frazzini-Pedersen / Quantpedia).
