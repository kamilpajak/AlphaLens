# alt_data_screener_v2 Phase B — holdout reveal

**Pre-registration:** `alt_data_screener_v2_2026_04_30` (class `alt_data_screener_search_2026_04_30`, n=2 -> Bonferroni |t|>=2.24)
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted

## Verdict: FAIL (zero-coef structural artifact — see prior class v2-v3 finding)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean top-N | 30.0 |
| turnover / rebal | 13.8% |
| Sharpe (gross, Lo-adj) | 0.80 |
| Sharpe (gross, naive) | 1.43 |
| Sharpe (net, Lo-adj) | 0.78 |
| alpha (gross, 4F) annualised | +7.79% |
| alpha (net, 4F) annualised | +5.70% |
| alpha t-stat (HAC, maxlags=5) | +0.05 |
| Excess vs SPY (gross) ann | +9.41% |
| Excess vs SPY (net) ann | +7.32% |
| Max drawdown (net cum) | -61.52% |
| Cost drag annualised | 2.09% |
| Carhart aggregated n | 87 |

## Train/holdout sizes

- feature rows in train pool: 221920
- train rows after target NaN-drop: 221920
- feature rows in holdout: 97107
- holdout rows scored: 97107

## Global Lasso fit (20d-forward target)

- n_train: 221920
- lambda chosen: 0.004431
- nonzero coefs: 0 / 10
- CV mean MSE: 0.02908

### Nonzero coefficients

  - (all zero)

## Comparison to prior class (3/3 FAIL)

Prior class `multi_source_two_stage_search_2026_04_30` v1-v3:
- v1 (4-regime, 5d): mean phase alpha-t=+0.65, 4.9pp dispersion, FAIL
- v2 (global, 5d): mean phase alpha-t=+0.55, 5.6pp dispersion, FAIL — 0/21 coefs
- v3 (global, 20d): single phase alpha-t=+1.32, FAIL — 0/21 coefs

v4 fresh class with 10-feature alt-data whitelist (8 of 10 features new vs prior).
