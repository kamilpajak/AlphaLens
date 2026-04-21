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
| Sharpe (gross) | +0.981 |
| Sharpe (moderate 100 bps) | +0.962 |
| Sharpe (conservative 150 bps) | +0.953 |
| Annual return (moderate net) | +46.13% |
| Max drawdown | -60.35% |
| Calmar ratio | +0.789 |
| Hit rate (vs universe median) | 52.8% |
| Mean Rank IC | +0.0175 |
| IC t-stat | +3.76 |
| IC positive windows (20d) | 64.2% |
| Turnover (daily rebalance) | 71.7% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.981 | +47.59% |
| aggressive | 75 | +0.967 | +46.49% |
| moderate | 100 | +0.962 | +46.13% |
| conservative | 150 | +0.953 | +45.40% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.923 | +40.80% | +0.0112 | 54.2% |
| bear | 138 | +3.029 | +724.57% | +0.0189 | 52.2% |
| flat | 464 | +0.057 | -7.87% | +0.0225 | 51.7% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.413 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 87 / 999 (8.7%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 28.9% | 291 (29.1%) |
| ai | 26.4% | 311 (31.1%) |
| quantum | 26.2% | 221 (22.1%) |
| biotech | 18.5% | 176 (17.6%) |
```

**Alert**: 8.7% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +19.86  +50.05%    +1.66  0.000    971
FF3                        +19.43  +48.95%    +1.60  0.003    971
Carhart-4F                 +19.41  +48.90%    +1.61  0.003    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.
