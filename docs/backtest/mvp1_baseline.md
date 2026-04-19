# MVP1 Backtest Report

- **Window**: 2024-04-19 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 30
- **Holding period**: 5 trading days
- **Screener universe**: 782 tickers
- **Backtest days**: 275

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +1.758 |
| Sharpe (moderate 100 bps) | +1.711 |
| Sharpe (conservative 150 bps) | +1.688 |
| Annual return (moderate net) | +41.02% |
| Max drawdown | -10.96% |
| Calmar ratio | +3.872 |
| Hit rate (vs universe median) | 56.7% |
| Mean Rank IC | +0.0066 |
| IC t-stat | +0.62 |
| IC positive windows (20d) | 58.6% |
| Turnover (daily rebalance) | 44.4% |
| Concentration top-5 | 16.7% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.758 | +42.43% |
| aggressive | 75 | +1.723 | +41.37% |
| moderate | 100 | +1.711 | +41.02% |
| conservative | 150 | +1.688 | +40.32% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 116 | +1.751 | +39.31% | +0.0272 | 56.9% |
| bear | 43 | +1.599 | +38.50% | -0.1028 | 51.2% |
| flat | 116 | +1.813 | +47.15% | +0.0266 | 58.6% |

## Fama-French 3-factor alpha

```
Fama-French 3-factor regression (n=247)
  alpha (daily)        = +13.73 bps/day
  alpha (annualized)   = +34.60%
  alpha t-stat         = +1.60
  beta[Mkt-RF]         = +0.091
  beta[SMB]            = -0.325
  beta[HML]            = -0.042
  R²                   = 0.025
```

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] FF3 alpha t-stat > 1.5

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
