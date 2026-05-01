# alt_data_screener_v4 Phase B — holdout reveal (long/short decile spread)

**Pre-registration:** `alt_data_screener_v4_2026_05_01` (class `alt_data_screener_search_2026_04_30`, n=4 -> Bonferroni |t|>=2.50)
**Burnt-holdout caveat:** v4 is DIAGNOSTIC ONLY. Capital deploy off-table regardless of verdict.
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection per leg
**Short-leg HTB filter:** SI %% float <= 15% (zen Objection #2 mitigation)
**Borrow fee scenario:** 1.5% annualized on short notional (separate accounting)
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted

## Verdict: FAIL (in-CV IR < 0.5; got 0.338)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean decile size (long) | 96.8 |
| mean decile size (short) | 83.5 |
| turnover/rebal long | 11.8% |
| turnover/rebal short | 17.1% |
| Sharpe (gross, Lo-adj) | -1.34 |
| Sharpe (gross, naive) | -2.55 |
| Sharpe (net, Lo-adj) | -1.41 |
| alpha (gross, 4F) annualised | -533.26% |
| alpha (net, 4F) annualised | -537.64% |
| alpha t-stat (HAC, maxlags=5) | -2.57 |
| **Mkt-RF beta (Carhart 4F)** | -0.285 |
| beta-neutrality OK (\|β\| ≤ 0.20) | False |
| Excess vs SPY (gross) ann | -143.98% |
| Excess vs SPY (net) ann | -148.36% |
| long leg ann return | +20.57% |
| short leg ann return | +108.51% |
| Max drawdown (net cum L/S) | -85.70% |
| Cost drag annualised (60bps round-trip) | 4.38% |
| Borrow fee drag (separate) | 1.50% |
| alpha net after borrow scenario | -539.14% |
| Carhart aggregated n | 87 |
| in-CV IR (mean fold IC / std) | 0.338 |
| holdout mean per-asof rank-IC | +0.0260 |
| holdout asofs with valid IC | 94 |

## Regime stratification (descriptive — NOT a verdict gate)

| Sub-period | n rebal | L/S ann | Long ann | Short ann |
| --- | ---: | ---: | ---: | ---: |
| 2024_partial | 34 | -91.51% | +29.85% | +121.36% |
| 2025_full | 50 | -113.56% | +16.20% | +129.76% |
| 2026_partial | 10 | +52.29% | +10.86% | -41.44% |

## Train/holdout sizes

- feature rows in train pool: 221920
- train rows after target NaN-drop: 221920
- feature rows in holdout: 97107
- holdout rows scored: 97107

## Global Lasso fit (rank-target, 20d-forward)

- n_train: 221920
- lambda chosen: 0.00289
- nonzero coefs: 2 / 10
- CV mean MSE: 0.08333

### Nonzero coefficients

  - `rank_short_interest_pct_float` = -0.0063
  - `filing_density_4q` = +0.0016

## Comparison to prior in-class results

Prior in-class (`alt_data_screener_search_2026_04_30`):
- v1 (FINRA daily flow): ABANDONED (infra block)
- v2 (Polygon SI, raw return target, top-30): alpha-t=+0.05, FAIL (0/10 coefs)
- v3 (rank target, top-30): alpha-t=-4.32, FAIL (2/10 coefs, +0.0260 holdout rank-IC)

v4 in-class continuation: ONLY selection rule changed (top-30 long -> long top-decile EW MINUS short bottom-decile EW with SI<=15% short filter).
