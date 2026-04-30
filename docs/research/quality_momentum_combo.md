# Quality + 12-1m momentum combo — same ADV / cost framework

**RESEARCH ONLY.** Score = z(mom_12_1m) + z(roe_ttm). PIT-correct fundamentals
via SimFin Publish Date filter. Tests whether combining quality with momentum
diversifies signal robustness vs single-factor approaches.

- Top-N: 15, holding-signal: 60d, stride: 5
- ADV thresholds: ['$5M']

## Results — gross / net (cost-stressed)

| Period | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | β_MOM |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS 2015-2022 | $5M | 5bp | 15.0 | 15.5% | 0.49 | 0.40 | +37.0% | +35.4% | +46.3% | +1.55 | -0.07 |
| OOS 2023-2026 | $5M | 5bp | 15.0 | 17.9% | -0.28 | -0.38 | -53.8% | -55.6% | -33.3% | -0.63 | -0.22 |
