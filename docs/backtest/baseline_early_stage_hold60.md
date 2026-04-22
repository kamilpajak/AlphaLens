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
| Sharpe (gross) | +1.201 |
| Sharpe (moderate 100 bps) | +1.184 |
| Sharpe (conservative 150 bps) | +1.176 |
| Annual return (moderate net) | +70.29% |
| Max drawdown | -68.14% |
| Calmar ratio | +1.057 |
| Hit rate (vs universe median) | 50.7% |
| Mean Rank IC | +0.0069 |
| IC t-stat | +1.66 |
| IC positive windows (20d) | 56.0% |
| Turnover (daily rebalance) | 76.0% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.201 | +72.00% |
| aggressive | 75 | +1.188 | +70.72% |
| moderate | 100 | +1.184 | +70.29% |
| conservative | 150 | +1.176 | +69.44% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +1.023 | +53.75% | +0.0220 | 47.9% |
| bear | 132 | +2.998 | +812.68% | -0.0391 | 53.8% |
| flat | 415 | +0.483 | +12.61% | +0.0072 | 52.5% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.434 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 118 / 944 (12.5%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| biotech | 35.3% | 411 (43.5%) |
| quantum | 22.5% | 188 (19.9%) |
| ai | 22.1% | 202 (21.4%) |
| semis | 20.1% | 143 (15.1%) |
```

**Alert**: 12.5% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +26.13  +65.85%    +2.01  0.000    944
FF3                        +25.84  +65.12%    +1.95  0.003    944
Carhart-4F                 +25.73  +64.83%    +1.96  0.004    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
