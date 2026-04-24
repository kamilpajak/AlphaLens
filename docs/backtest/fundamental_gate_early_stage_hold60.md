# MVP1 Backtest Report

- **Window**: 2021-04-19 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 60 trading days
- **Screener universe**: 113 tickers
- **Backtest days**: 944

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +1.010 |
| Sharpe (moderate 100 bps) | +0.992 |
| Sharpe (conservative 150 bps) | +0.983 |
| Annual return (moderate net) | +48.32% |
| Max drawdown | -60.35% |
| Calmar ratio | +0.825 |
| Hit rate (vs universe median) | 53.0% |
| Mean Rank IC | +0.0108 |
| IC t-stat | +2.24 |
| IC positive windows (20d) | 59.8% |
| Turnover (daily rebalance) | 71.3% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.010 | +49.81% |
| aggressive | 75 | +0.997 | +48.69% |
| moderate | 100 | +0.992 | +48.32% |
| conservative | 150 | +0.983 | +47.58% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.923 | +40.80% | +0.0534 | 54.2% |
| bear | 132 | +3.247 | +882.80% | -0.0949 | 53.0% |
| flat | 415 | -0.071 | -12.61% | +0.0035 | 51.8% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.412 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 80 / 944 (8.5%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 28.7% | 274 (29.0%) |
| ai | 26.5% | 295 (31.2%) |
| quantum | 26.4% | 212 (22.5%) |
| biotech | 18.4% | 163 (17.3%) |
```

**Alert**: 8.5% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +19.91  +50.16%    +1.65  0.000    944
FF3                        +19.60  +49.40%    +1.60  0.002    944
Carhart-4F                 +19.40  +48.89%    +1.59  0.003    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
