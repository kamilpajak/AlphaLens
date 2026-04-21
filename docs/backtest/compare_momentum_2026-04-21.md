# MVP1 Backtest Report

- **Window**: 2021-06-01 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 5 trading days
- **Screener universe**: 113 tickers
- **Backtest days**: 999

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +1.488 |
| Sharpe (moderate 100 bps) | +1.473 |
| Sharpe (conservative 150 bps) | +1.466 |
| Annual return (moderate net) | +117.54% |
| Max drawdown | -50.10% |
| Calmar ratio | +2.389 |
| Hit rate (vs universe median) | 53.8% |
| Mean Rank IC | +0.0248 |
| IC t-stat | +3.97 |
| IC positive windows (20d) | 60.9% |
| Turnover (daily rebalance) | 39.9% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.488 | +119.72% |
| aggressive | 75 | +1.477 | +118.09% |
| moderate | 100 | +1.473 | +117.54% |
| conservative | 150 | +1.466 | +116.46% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +2.181 | +263.91% | +0.0275 | 56.9% |
| bear | 138 | -0.246 | -31.02% | -0.0044 | 47.8% |
| flat | 464 | +1.356 | +101.39% | +0.0311 | 52.8% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.473 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 208 / 999 (20.8%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| biotech | 34.3% | 405 (40.5%) |
| quantum | 22.8% | 210 (21.0%) |
| ai | 22.1% | 235 (23.5%) |
| semis | 20.7% | 149 (14.9%) |
```

**Alert**: 20.8% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +38.84  +97.89%    +2.72  0.001    971
FF3                        +37.50  +94.50%    +2.62  0.007    971
Carhart-4F                 +37.48  +94.44%    +2.62  0.007    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.
