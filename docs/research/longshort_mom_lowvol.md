# Long-short mom+lowvol — market-neutral spread test

**RESEARCH ONLY.** Hypothesis: long-only mom+lowvol lost 11%/y in 2017-2022 because
mid-cap-tilted top-15 underperformed mega-cap SPY benchmark. Long-short spread
(top-15 minus bottom-15) hedges benchmark drift; if positive consistently
across all 3 periods, regime-risk reduced.

- Top-N: 15, vol_weight: 1.0, ADV ≥ $5M
- Rebalance stride: 5; cost: 5.0bp half-spread per leg
- Universe: per-subperiod PIT R2000-like

## Long-only (top-15) — for reference vs synthesis report

| Period | N | turn | excess gross | excess net | Sharpe gross | Sharpe net | α 4F | t (4F) | β_MOM |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS_2011_2016 | 302 | 23.5% | +27.3% | +24.9% | +0.82 | +0.58 | +38.9% | +1.93 | 0.09 |
| IS_2017_2022 | 302 | 25.2% | -5.0% | -7.5% | +0.25 | +0.07 | +19.4% | +0.83 | -0.11 |
| OOS_2023_2026 | 165 | 25.4% | +38.1% | +35.6% | +0.86 | +0.65 | +38.6% | +1.24 | 0.08 |

## Long-short spread (top-15 minus bottom-15) — primary test

| Period | N | mean ann | Sharpe gross | Sharpe net | drag/y | α 4F | t (4F) | β_MKT | β_MOM | R² |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS_2011_2016 | 302 | +29.4% | +0.38 | +0.14 | 3.8% | +30.4% | +0.91 | -0.10 | +0.06 | 0.003 |
| IS_2017_2022 | 302 | -66.3% | -0.64 | -0.84 | 4.2% | -55.9% | -1.37 | -0.37 | -0.23 | 0.046 |
| OOS_2023_2026 | 165 | +7.1% | +0.07 | -0.15 | 4.4% | +39.1% | +0.73 | -0.28 | +0.21 | 0.042 |
