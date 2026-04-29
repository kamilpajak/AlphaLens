# Layer 2d random-subset null distribution

RESEARCH ONLY. Tests whether V0_count baseline IS α is rank-driven
or distributional. If V0 sits near null median, ranking added no
information beyond cluster-positive membership.

- Top-N: 15
- Rebalance stride: 5
- N trials: 500
- Universe: PIT union (834 tickers)


## Null distribution — IS_2011_2016

- N trials: 500
- Top-N per rebalance: 15
- Rebalance count: 299
- Sampling: uniform without replacement from cluster-positive set per rebalance

### Distribution quantiles

| Metric | min | p05 | p25 | median | p75 | p95 | max | mean | std |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Carhart α (ann) | 29.99% | 32.22% | 33.42% | 34.63% | 35.66% | 37.02% | 38.73% | 34.59% | 1.53% |
| Carhart t-stat | 0.94 | 1.02 | 1.06 | 1.11 | 1.14 | 1.19 | 1.24 | 1.10 | 0.05 |
| Sharpe | 0.47 | 0.51 | 0.53 | 0.55 | 0.56 | 0.58 | 0.60 | 0.55 | 0.02 |
| R² | 0.0196 | 0.0209 | 0.0222 | 0.0232 | 0.0240 | 0.0252 | 0.0266 | 0.0231 | 0.0013 |

### V0 baseline percentile within null

| Metric | V0 baseline | Null median | Null p95 | V0 percentile in null |
|---|---:|---:|---:|---:|
| Carhart α (ann) | 44.13% | 34.63% | 37.02% | 100th |
| Carhart t-stat | 1.63 | 1.11 | 1.19 | 100th |
| Sharpe | 0.70 | 0.55 | 0.58 | 100th |


## Null distribution — IS_2017_2022

- N trials: 500
- Top-N per rebalance: 15
- Rebalance count: 301
- Sampling: uniform without replacement from cluster-positive set per rebalance

### Distribution quantiles

| Metric | min | p05 | p25 | median | p75 | p95 | max | mean | std |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Carhart α (ann) | 19.29% | 28.59% | 34.90% | 39.61% | 44.18% | 49.70% | 60.25% | 39.54% | 6.55% |
| Carhart t-stat | 0.74 | 1.15 | 1.37 | 1.54 | 1.69 | 1.90 | 2.29 | 1.53 | 0.23 |
| Sharpe | 0.31 | 0.49 | 0.58 | 0.64 | 0.71 | 0.80 | 0.96 | 0.64 | 0.10 |
| R² | 0.0168 | 0.0244 | 0.0326 | 0.0393 | 0.0475 | 0.0625 | 0.0966 | 0.0408 | 0.0114 |

### V0 baseline percentile within null

| Metric | V0 baseline | Null median | Null p95 | V0 percentile in null |
|---|---:|---:|---:|---:|
| Carhart α (ann) | 42.94% | 39.61% | 49.70% | 69th |
| Carhart t-stat | 1.60 | 1.54 | 1.90 | 62th |
| Sharpe | 0.70 | 0.64 | 0.80 | 71th |
