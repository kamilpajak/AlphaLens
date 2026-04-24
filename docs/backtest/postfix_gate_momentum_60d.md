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
| Sharpe (gross) | +0.668 |
| Sharpe (moderate 100 bps) | +0.651 |
| Sharpe (conservative 150 bps) | +0.642 |
| Annual return (moderate net) | +23.51% |
| Max drawdown | -50.34% |
| Calmar ratio | +0.492 |
| Hit rate (vs universe median) | 52.2% |
| Mean Rank IC | +0.0192 |
| IC t-stat | +3.24 |
| IC positive windows (20d) | 58.1% |
| Turnover (daily rebalance) | 35.0% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.668 | +24.75% |
| aggressive | 75 | +0.655 | +23.82% |
| moderate | 100 | +0.651 | +23.51% |
| conservative | 150 | +0.642 | +22.90% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.351 | +3.60% | +0.0860 | 51.9% |
| bear | 132 | +0.105 | -12.26% | -0.1281 | 50.8% |
| flat | 415 | +1.160 | +66.67% | +0.0022 | 53.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.436 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 106 / 944 (11.2%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 31.8% | 294 (31.1%) |
| quantum | 26.1% | 225 (23.8%) |
| ai | 23.9% | 259 (27.4%) |
| biotech | 18.3% | 166 (17.6%) |
```

**Alert**: 11.2% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +12.96  +32.67%    +1.13  0.003    944
FF3                        +11.89  +29.97%    +1.03  0.009    944
Carhart-4F                 +11.95  +30.11%    +1.04  0.009    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
