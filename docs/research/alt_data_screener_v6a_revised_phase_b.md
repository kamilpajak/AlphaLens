# alt_data_screener_v5_long_only Phase B — holdout reveal (long-only top-decile vs SPY)

**Pre-registration:** `alt_data_screener_v6a_revised_2026_05_01` (class `alt_data_screener_search_2026_04_30`, n=5 in-class but program-level n=10 -> primary |t|>=3.50, stretch |t|>=4.00)
**Pivot provenance:** auto-pivot from v10 (analyst_alt_data_v10) Phase A gate 2 yfinance survivorship FAIL (delisted/active event-rate ratio=0.003, z=620).
**HARKing flag (explicit):** Path β was post-hoc designed against observed v4 long-leg performance (+20.6%/y); per Kerr 1998 + Simmons et al. 2011 this is classical hypothesis-mining. Pivot trigger from v10 was pre-registered objective; Path β design itself is HARKing-confounded. Capital deploy OFF-TABLE regardless of verdict; fresh-OOS replication mandatory before any escalation.
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection (single-leg long only, ~30bps round-trip)
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted
**Primary metric:** Carhart-4F alpha t-stat on (long_only_return - SPY_return) series; PASS at |t| >= 3.50 primary, |t| >= 4.00 stretch.

## Verdict: FAIL (log to ledger)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean decile size (long) | 87.1 |
| turnover/rebal long | 13.7% |
| Sharpe (gross, Lo-adj) | -0.93 |
| Sharpe (gross, naive) | -1.10 |
| Sharpe (net, Lo-adj) | -1.05 |
| alpha (gross, 4F) annualised | -37.58% |
| alpha (net, 4F) annualised | -39.65% |
| alpha t-stat (HAC, maxlags=5) | -0.67 |
| primary threshold passed (|t|>=3.50) | False |
| stretch threshold passed (|t|>=4.00) | False |
| **Mkt-RF beta (Carhart 4F)** | -0.150 |
| beta in descriptive bound (|β| ≤ 0.5) | True |
| Excess vs SPY (gross) ann | -16.30% |
| Excess vs SPY (net) ann | -18.37% |
| long leg ann return | +20.09% |
| benchmark ann return | +36.39% |
| Max drawdown (net cum excess) | -45.15% |
| Cost drag annualised (~30bps RT, single leg) | 2.07% |
| Carhart aggregated n | 87 |
| in-CV IR (mean fold IC / std) | 0.402 |
| holdout mean per-asof rank-IC | +0.0238 |
| holdout asofs with valid IC | 94 |

## Regime stratification (descriptive — NOT a verdict gate)

| Sub-period | n rebal | Excess ann | Long ann | Bench ann |
| --- | ---: | ---: | ---: | ---: |
| 2024_partial | 34 | -8.67% | +34.08% | +42.75% |
| 2025_full | 50 | -27.86% | +12.42% | +40.28% |
| 2026_partial | 10 | +15.53% | +10.88% | -4.65% |

## Train/holdout sizes

- feature rows in train pool: 200297
- train rows after target NaN-drop: 200297
- feature rows in holdout: 87403
- holdout rows scored: 87403

## Global Lasso fit (rank-target, 20d-forward)

- n_train: 200297
- lambda chosen: 0.002565
- nonzero coefs: 2 / 10
- CV mean MSE: 0.08333

### Nonzero coefficients

  - `rank_short_interest_pct_float` = -0.0057
  - `filing_density_4q` = +0.0025

## Comparison to prior in-class results

Prior in-class (`alt_data_screener_search_2026_04_30`):
- v1 (FINRA daily flow): ABANDONED (infra block)
- v2 (Polygon SI, raw return target, top-30): alpha-t=+0.05, FAIL (0/10 coefs)
- v3 (rank target, top-30): alpha-t=-4.32, FAIL (2/10 coefs, +0.0260 holdout rank-IC)
- v4 (rank target, decile L/S, SI<=15%): alpha-t=-2.57, FAIL (2/10 coefs, short leg returned +108.5%/y in squeeze regime crushing the spread)

v5 in-class continuation: ONLY selection rule changed (decile L/S -> long top-decile only vs SPY benchmark).
