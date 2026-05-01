# alt_data_screener_v5_long_only Phase B — holdout reveal (long-only top-decile vs SPY)

**Pre-registration:** `alt_data_screener_v5_long_only_2026_05_01` (class `alt_data_screener_search_2026_04_30`, n=5 in-class but program-level n=10 -> primary |t|>=2.81, stretch |t|>=3.20)
**Pivot provenance:** auto-pivot from v10 (analyst_alt_data_v10) Phase A gate 2 yfinance survivorship FAIL (delisted/active event-rate ratio=0.003, z=620).
**HARKing flag (explicit):** Path β was post-hoc designed against observed v4 long-leg performance (+20.6%/y); per Kerr 1998 + Simmons et al. 2011 this is classical hypothesis-mining. Pivot trigger from v10 was pre-registered objective; Path β design itself is HARKing-confounded. Capital deploy OFF-TABLE regardless of verdict; fresh-OOS replication mandatory before any escalation.
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2018-01-01 -> 2024-04-29
**Holdout:** 2024-04-30 -> 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection (single-leg long only, ~30bps round-trip)
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted
**Primary metric:** Carhart-4F alpha t-stat on (long_only_return - SPY_return) series; PASS at |t| >= 2.81 primary, |t| >= 3.20 stretch.

## Verdict: FAIL (log to ledger)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 94 |
| mean decile size (long) | 96.8 |
| turnover/rebal long | 11.8% |
| Sharpe (gross, Lo-adj) | -1.06 |
| Sharpe (gross, naive) | -1.32 |
| Sharpe (net, Lo-adj) | -1.11 |
| alpha (gross, 4F) annualised | -192.69% |
| alpha (net, 4F) annualised | -194.48% |
| alpha t-stat (HAC, maxlags=5) | -3.20 |
| primary threshold passed (|t|>=2.81) | False |
| stretch threshold passed (|t|>=3.20) | False |
| **Mkt-RF beta (Carhart 4F)** | -0.145 |
| beta in descriptive bound (|β| ≤ 0.5) | True |
| Excess vs SPY (gross) ann | -35.47% |
| Excess vs SPY (net) ann | -37.26% |
| long leg ann return | +20.57% |
| benchmark ann return | +56.04% |
| Max drawdown (net cum excess) | -66.70% |
| Cost drag annualised (~30bps RT, single leg) | 1.79% |
| Carhart aggregated n | 87 |
| in-CV IR (mean fold IC / std) | 0.338 |
| holdout mean per-asof rank-IC | +0.0260 |
| holdout asofs with valid IC | 94 |

## Regime stratification (descriptive — NOT a verdict gate)

| Sub-period | n rebal | Excess ann | Long ann | Bench ann |
| --- | ---: | ---: | ---: | ---: |
| 2024_partial | 34 | -45.91% | +29.85% | +75.76% |
| 2025_full | 50 | -51.37% | +16.20% | +67.57% |
| 2026_partial | 10 | +79.53% | +10.86% | -68.67% |

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
- v4 (rank target, decile L/S, SI<=15%): alpha-t=-2.57, FAIL (2/10 coefs, short leg returned +108.5%/y in squeeze regime crushing the spread)

v5 in-class continuation: ONLY selection rule changed (decile L/S -> long top-decile only vs SPY benchmark).
