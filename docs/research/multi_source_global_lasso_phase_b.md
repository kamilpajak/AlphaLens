# multi_source_global_lasso Phase B — holdout reveal

**Pre-registration:** `multi_source_global_lasso_2026_04_30` (class `multi_source_two_stage_search_2026_04_30`, n=2 → Bonferroni |t|≥2.24)
**Phase offset:** 0 (stride 5)
**Train:** 2014-01-01 → 2024-04-29
**Holdout:** 2024-04-30 → 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection

## Verdict: MID (refine and re-pre-register before deploy)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances | 99 |
| mean top-N | 30.0 |
| turnover / rebal | 13.2% |
| Sharpe (gross) | 1.49 |
| Sharpe (net) | 1.31 |
| α (gross, 4F) annualised | +77.44% |
| α (net, 4F) annualised | +75.45% |
| α t-stat (HAC) | +2.03 |
| Excess vs SPY (gross) ann | +8.51% |
| Excess vs SPY (net) ann | +6.52% |
| Max drawdown (net cum) | -10.10% |
| Cost drag annualised | 1.99% |

## Train/holdout sizes

- feature rows in train pool: 310517
- train rows after target NaN-drop: 310517
- feature rows in holdout: 97451
- holdout rows scored: 97451

## Global Lasso fit

- n_train: 310517
- λ chosen: 0.002566 (idx in 25-pt grid: depends on data)
- nonzero coefs: 0 / 21
- CV mean MSE: 0.005944

## v2 vs v1 (Stage 1 ablation)

v1 had per-regime Lassos with 3/4 regimes shrinking to zero coefs (only Q1_calm fitted real signal). v2 single global Lasso processes all regimes uniformly.

## Pre-registration discipline

- 21-feature whitelist FROZEN; identical to v1.
- λ grid (25 points), embargo (60d), n_folds (3) — all per pre-reg.
- ONE-shot holdout, no peek-and-tune.
- Carhart attribution post-hoc; target is raw 5d-forward excess return.
- Architecture is the SOLE variable changed vs v1.
