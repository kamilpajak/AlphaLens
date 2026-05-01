# alt_data_screener_v3 Phase B — holdout reveal (rank-target ablation)

**Pre-registration:** `alt_data_screener_v3_2026_05_01` (class `alt_data_screener_search_2026_04_30`, n=3 -> Bonferroni |t|>=2.39)
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted

## Verdict: FAIL (in-CV IR < 0.5; got 0.338)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean top-N | 30.0 |
| turnover / rebal | 19.2% |
| Sharpe (gross, Lo-adj) | -0.40 |
| Sharpe (gross, naive) | -0.52 |
| Sharpe (net, Lo-adj) | -0.47 |
| alpha (gross, 4F) annualised | -374.07% |
| alpha (net, 4F) annualised | -376.98% |
| alpha t-stat (HAC, maxlags=5) | -4.32 |
| Excess vs SPY (gross) ann | -73.23% |
| Excess vs SPY (net) ann | -76.14% |
| Max drawdown (net cum) | -63.97% |
| Cost drag annualised | 2.91% |
| Carhart aggregated n | 87 |
| **NEW: in-CV IR (mean fold IC / std)** | 0.338 |
| **NEW: holdout mean per-asof rank-IC** | +0.0260 |
| holdout asofs with valid IC | 94 |

## Train/holdout sizes

- feature rows in train pool: 221920
- train rows after target NaN-drop: 221920
- feature rows in holdout: 97107
- holdout rows scored: 97107

## Global Lasso fit (20d-forward target)

- n_train: 221920
- lambda chosen: 0.00289
- nonzero coefs: 2 / 10
- CV mean MSE: 0.08333

### Nonzero coefficients

  - `rank_short_interest_pct_float` = -0.0063
  - `filing_density_4q` = +0.0016

## Comparison to prior in-class + cross-class results

Prior cross-class (`multi_source_two_stage_search_2026_04_30`):
- v1 (4-regime, 5d): mean phase alpha-t=+0.65, 4.9pp dispersion, FAIL
- v2 (global, 5d): mean phase alpha-t=+0.55, FAIL — 0/21 coefs
- v3 (global, 20d): alpha-t=+1.32, FAIL — 0/21 coefs

Prior in-class (`alt_data_screener_search_2026_04_30`):
- v1 (FINRA daily flow): ABANDONED (infra block)
- v2 (Polygon SI, raw return target): alpha-t=+0.05, FAIL — 0/10 coefs

v3 in-class ablation: ONLY target rank-transform changed vs v2.
