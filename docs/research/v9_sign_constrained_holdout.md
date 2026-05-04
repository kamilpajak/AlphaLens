# v9 sign-constrained holdout reveal — FAIL (αt=+1.24 < 3.13 program-Bonferroni n=17)

**Date:** 2026-05-03
**Pre-reg:** v9_sign_constrained_options_implied_2026_05_03
**Model:** Lasso with mechanically-enforced `coef_options ≤ 0` (Xing prior)

## Headline (PRIMARY = LONG TOP decile by sign-constrained Lasso)

| Metric | Value |
| --- | ---: |
| n holdout rebalances | 101 |
| Sharpe (gross) | 0.79 |
| Sharpe (net 30bps RT) | 0.79 |
| Carhart-4F α (gross, ann) | +684.65% |
| Carhart-4F α (net, ann) | +684.35% |
| α t-stat (HAC=5) | **+1.24** |
| Excess vs MDY (gross, ann) | +121.69% |
| Excess vs MDY (net, ann) | +121.39% |
| Max drawdown (net cum) | -26.63% |

## L/S diagnostic (top − bottom decile, NOT primary verdict)

| Metric | Value |
| --- | ---: |
| Sharpe (gross) | -1.17 |
| Sharpe (net 60bps RT) | -1.17 |
| Carhart-4F α (gross, ann) | -950.79% |
| α t-stat (HAC=5) | -1.25 |

## Sign-constrained Lasso fit (standardized features, train period)

- α (penalty): 0.001433
- n_train: 204968, CV-MSE: 0.06615
- nonzero coefs: 4 / 7 (options-feature subset: 2 / 4)

| Feature | Coef (standardized) |
| --- | ---: |
| ivp30 | -0 |
| ivx30 | -0 |
| ivx180_minus_ivx30 | -0.001171 |
| ivx30_over_hv20 | -0.000594 |
| reversal_1m | +0.002833 |
| momentum_6m | +0 |
| rv_30d | +0.01269 |

## Coverage

- Holdout-scored / total holdout rows: 100.0%

## Pre-reg discipline

- Sign constraint MECHANICALLY enforced — `coef_options ≤ 0` cannot violate Xing prior.
- Equity controls free-sign (encoded via positive/negative-pair augmentation).
- ONE-shot holdout, no peek-and-tune.
- Carhart-4F (HAC=5) attribution post-hoc.
- L/S diagnostic reported as power-loss check, NOT additional Bonferroni test.
- Threshold |αt| ≥ 3.13 program-Bonferroni n=17 (one-up from v8's n=16).
- Selection-mechanism gate: ≥1 nonzero options coef required for non-degenerate fit.
