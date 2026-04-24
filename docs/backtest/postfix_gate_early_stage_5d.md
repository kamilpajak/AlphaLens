# MVP1 Backtest Report

- **Window**: 2021-04-19 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 5 trading days
- **Screener universe**: 113 tickers
- **Backtest days**: 999

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +0.932 |
| Sharpe (moderate 100 bps) | +0.914 |
| Sharpe (conservative 150 bps) | +0.904 |
| Annual return (moderate net) | +42.39% |
| Max drawdown | -59.14% |
| Calmar ratio | +0.741 |
| Hit rate (vs universe median) | 52.5% |
| Mean Rank IC | +0.0158 |
| IC t-stat | +3.41 |
| IC positive windows (20d) | 63.0% |
| Turnover (daily rebalance) | 71.6% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.932 | +43.82% |
| aggressive | 75 | +0.918 | +42.75% |
| moderate | 100 | +0.914 | +42.39% |
| conservative | 150 | +0.904 | +41.68% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.848 | +35.47% | +0.0099 | 53.9% |
| bear | 138 | +2.862 | +614.70% | +0.0125 | 52.9% |
| flat | 464 | +0.103 | -6.04% | +0.0219 | 51.1% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.412 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 91 / 999 (9.1%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 29.0% | 290 (29.0%) |
| ai | 26.2% | 308 (30.8%) |
| quantum | 26.0% | 219 (21.9%) |
| biotech | 18.7% | 182 (18.2%) |
```

**Alert**: 9.1% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +18.80  +47.38%    +1.58  0.000    971
FF3                        +18.51  +46.64%    +1.54  0.002    971
Carhart-4F                 +18.51  +46.64%    +1.54  0.002    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.
