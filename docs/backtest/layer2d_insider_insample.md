# Layer 2d insider backtest — insample (2011-01-01 to 2022-12-31)

- Universe (PIT union): 1403 tickers
- Holding period: 60 days
- Top-N: 15
- Benchmark: SPY
- Rebalance stride: 5 trading day(s) (50/y)
- Avg per-rebalance turnover: 27.9%
- Sharpe (gross, annualized at rebal cadence): 0.95

## Factor attribution

| Spec | α (ann) | α t-stat | R² | n |
|---|---:|---:|---:|---:|
| Carhart-4F | 103.48% | 2.14 | 0.005 | 603 |
| FF5+UMD    | 100.09% | 2.19 | 0.012 | 603 |
| Q4         | 100.15% | 2.16 | 0.009 | 603 |

## Cost sensitivity

- Primary (half-spread 5 bps): drag = 2.81%/y → **net α = 100.66%**
- Stress (half-spread 15 bps, ~k=0.15 proxy): drag = 5.63%/y → **net α = 97.85%**

## Bootstrap CI (Carhart α, annualized)

- Block-bootstrap 95% CI: [26.30%, 214.87%]
- Excludes zero: **True** (10000 iters, block n^(1/3))

## Regime breakdown (Carhart α t-stat, HAC)

| Regime | α t-stat |
|---|---:|
| bull | 2.51 |
| bear | 1.47 |
| flat | 1.67 |

## Decision matrix

**Verdict: PAPER_TRACK**

| Gate | Pass |
|---|:---:|
| carhart_alpha_bonferroni | ✗ |
| ff5_umd_alpha | ✓ |
| ff5_umd_attenuation | ✓ |
| net_alpha_primary | ✓ |
| net_alpha_stress_k15 | ✓ |
| bootstrap_ci | ✓ |
| sharpe_net | ✗ |
| regime_collapse_bull | ✓ |
| regime_collapse_bear | ✗ |
| regime_collapse_flat | ✓ |

### Failing gates

- carhart_alpha_bonferroni
- sharpe_net
- regime_collapse_bear

### Notes

- Q4 α_t = 2.16 (diagnostic only)
