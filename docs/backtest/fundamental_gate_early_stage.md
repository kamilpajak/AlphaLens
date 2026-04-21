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
| Sharpe (gross) | +0.959 |
| Sharpe (moderate 100 bps) | +0.941 |
| Sharpe (conservative 150 bps) | +0.932 |
| Annual return (moderate net) | +45.02% |
| Max drawdown | -61.89% |
| Calmar ratio | +0.751 |
| Hit rate (vs universe median) | 52.2% |
| Mean Rank IC | +0.0155 |
| IC t-stat | +3.35 |
| IC positive windows (20d) | 63.2% |
| Turnover (daily rebalance) | 72.0% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.959 | +46.48% |
| aggressive | 75 | +0.946 | +45.39% |
| moderate | 100 | +0.941 | +45.02% |
| conservative | 150 | +0.932 | +44.30% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.803 | +32.54% | +0.0104 | 53.4% |
| bear | 138 | +2.840 | +624.39% | +0.0183 | 53.6% |
| flat | 464 | +0.221 | -0.81% | +0.0190 | 50.6% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.412 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 91 / 999 (9.1%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 28.4% | 290 (29.0%) |
| quantum | 26.8% | 237 (23.7%) |
| ai | 25.9% | 293 (29.3%) |
| biotech | 18.9% | 179 (17.9%) |
```

**Alert**: 9.1% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +19.79  +49.88%    +1.65  0.000    971
FF3                        +19.24  +48.49%    +1.58  0.004    971
Carhart-4F                 +19.16  +48.29%    +1.58  0.004    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.
