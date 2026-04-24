# Layer 2d insider backtest — oos (2023-01-01 to 2026-04-22)

- Universe (PIT union): 1536 tickers
- Holding period: 60 days
- Top-N: 15
- Benchmark: SPY
- Rebalance stride: 5 trading day(s) (50/y)
- Avg per-rebalance turnover: 30.5%
- Sharpe (naive sqrt-k, rebal cadence): 0.42
- Sharpe (autocorr-adjusted, Perplexity R11): 0.42

## Factor attribution

| Spec | α (ann) | α t-stat | R² | n |
|---|---:|---:|---:|---:|
| Carhart-4F | 21.56% | 0.68 | 0.008 | 154 |
| FF5+UMD    | 25.95% | 0.82 | 0.043 | 154 |
| Q4         | 41.46% | 0.97 | 0.052 | 101 |

## Cost sensitivity

- Primary (half-spread 5 bps): drag = 3.07%/y → **net α = 18.48%**
- Stress (half-spread 15 bps, ~k=0.15 proxy): drag = 6.15%/y → **net α = 15.41%**

## Bootstrap CI (Carhart α, annualized)

- Block-bootstrap 95% CI: [-45.34%, 84.84%]
- Excludes zero: **False** (10000 iters, block n^(1/3))

## Regime breakdown (Carhart α t-stat, HAC)

| Regime | α t-stat |
|---|---:|
| bull | 1.24 |
| bear | 0.00 |
| flat | -0.44 |

## Decision matrix

**Verdict: KILL**

| Gate | Pass |
|---|:---:|
| carhart_alpha_bonferroni | ✗ |
| ff5_umd_alpha | ✗ |
| ff5_umd_attenuation | ✓ |
| net_alpha_primary | ✓ |
| net_alpha_stress_k15 | ✓ |
| bootstrap_ci | ✗ |
| sharpe_net | ✗ |
| regime_collapse_bull | ✗ |
| regime_collapse_bear | ✗ |
| regime_collapse_flat | ✗ |

### Failing gates

- carhart_alpha_bonferroni
- ff5_umd_alpha
- bootstrap_ci
- sharpe_net
- regime_collapse_bull
- regime_collapse_bear
- regime_collapse_flat

### Notes

- Q4 α_t = 0.97 (diagnostic only)
