# nonlinear_alt_data_v1 Phase B — holdout reveal (LightGBM MSE, decile L/S)

**Pre-registration:** `nonlinear_alt_data_v1_lightgbm_mse_2026_05_01` (NEW class `nonlinear_alt_data_search_2026_05_01`; in-class n=1 fresh |t|>=1.96, but PASS gate is PROGRAM-LEVEL n=8 -> |t|>=2.74 across all classes on this holdout)
**Burnt-holdout caveat:** v1 is DIAGNOSTIC ONLY. Capital deploy off-table regardless of verdict.
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection per leg
**Short-leg HTB filter:** SI %% float <= 15% (zen Objection #2 mitigation)
**Borrow fee scenario:** 1.5% annualized on short notional (separate accounting)
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted

## Verdict: FAIL (holdout mean rank-IC <= 0; got -0.0123)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean decile size (long) | 96.8 |
| mean decile size (short) | 83.5 |
| turnover/rebal long | 35.4% |
| turnover/rebal short | 47.9% |
| Sharpe (gross, Lo-adj) | 0.04 |
| Sharpe (gross, naive) | 0.06 |
| Sharpe (net, Lo-adj) | -0.21 |
| alpha (gross, 4F) annualised | +110.72% |
| alpha (net, 4F) annualised | +98.12% |
| alpha t-stat (HAC, maxlags=5) | +0.70 |
| **Mkt-RF beta (Carhart 4F)** | +0.119 |
| beta-neutrality OK (\|β\| ≤ 0.20) | True |
| Excess vs SPY (gross) ann | -53.97% |
| Excess vs SPY (net) ann | -66.56% |
| long leg ann return | +65.77% |
| short leg ann return | +63.69% |
| Max drawdown (net cum L/S) | -55.86% |
| Cost drag annualised (60bps round-trip) | 12.60% |
| Borrow fee drag (separate) | 1.50% |
| alpha net after borrow scenario | +96.62% |
| Carhart aggregated n | 87 |
| in-CV IR (mean fold IC / std) | -0.222 |
| holdout mean per-asof rank-IC | -0.0123 |
| holdout asofs with valid IC | 94 |

## Regime stratification (descriptive — NOT a verdict gate)

| Sub-period | n rebal | L/S ann | Long ann | Short ann |
| --- | ---: | ---: | ---: | ---: |
| 2024_partial | 34 | +58.01% | +115.78% | +57.77% |
| 2025_full | 50 | -4.50% | +81.47% | +85.97% |
| 2026_partial | 10 | -155.25% | -182.80% | -27.55% |

## Train/holdout sizes

- feature rows in train pool: 221920
- train rows after target NaN-drop: 221920
- feature rows in holdout: 97107
- holdout rows scored: 97107

## LightGBM MSE fit (raw 20d-forward target, magnitude-aware)

- n_train: 221920
- n_estimators chosen (CV early stopping): 26
- features used by trees: 9 / 10
- CV mean MSE: 0.02891

### Feature importances (split count)

  - `earnings_recency_days` = 136 splits
  - `log1p_days_to_cover` = 110 splits
  - `short_interest_pct_float_change_60d` = 86 splits
  - `earnings_pead_5d_post_decayed` = 83 splits
  - `earnings_sue_naive_4q_decayed` = 72 splits
  - `rank_short_interest_pct_float` = 68 splits
  - `rank_realized_downside_skew_60d` = 55 splits
  - `filing_density_4q` = 25 splits
  - `insider_log_dollar` = 3 splits

## Comparison to prior cross-class results on this holdout

Prior CLOSED class `alt_data_screener_search_2026_04_30` (4/4 FAIL):
- v1 (FINRA daily flow): ABANDONED (infra block)
- v2 (Polygon SI, raw return target, top-30): alpha-t=+0.05, FAIL (0/10 coefs)
- v3 (rank target, top-30): alpha-t=-4.32, FAIL (2/10 coefs, +0.0260 holdout rank-IC)
- v4 (rank target, decile L/S + SI<=15%): alpha-t=-2.57, FAIL (short leg squeezed)

v9 fresh-class pivot: model class linear-Lasso -> tree-boosting LightGBM with MSE objective. Same selection rule as v4 (settled). Tests whether nonlinear magnitude-aware modeling extracts additional signal.
