# MVP1 Backtest Report

- **Window**: 2025-10-01 → 2025-10-10
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 5 trading days
- **Screener universe**: 113 tickers
- **Backtest days**: 8

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +9.683 |
| Sharpe (moderate 100 bps) | +9.673 |
| Sharpe (conservative 150 bps) | +9.667 |
| Annual return (moderate net) | +476316.33% |
| Max drawdown | -5.91% |
| Calmar ratio | +81448.183 |
| Hit rate (vs universe median) | 75.0% |
| Mean Rank IC | +0.0249 |
| IC t-stat | +0.38 |
| IC positive windows (20d) | 0.0% |
| Turnover (daily rebalance) | 65.7% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +9.683 | +480951.80% |
| aggressive | 75 | +9.675 | +477471.01% |
| moderate | 100 | +9.673 | +476316.33% |
| conservative | 150 | +9.667 | +474015.30% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 7 | +7.851 | +120068.10% | +0.0676 | 71.4% |
| flat | 1 | +0.000 | +7925205091.66% | -0.2744 | 100.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 8 dni, próg koncentracji = 70%
Średni HHI: 0.430 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 0 / 8 (0.0%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| quantum | 47.5% | 5 (62.5%) |
| ai | 35.0% | 3 (37.5%) |
| biotech | 15.0% | 0 (0.0%) |
| semis | 2.5% | 0 (0.0%) |
```

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
