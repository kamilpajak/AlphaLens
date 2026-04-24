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
| Sharpe (gross) | +1.567 |
| Sharpe (moderate 100 bps) | +1.553 |
| Sharpe (conservative 150 bps) | +1.545 |
| Annual return (moderate net) | +129.49% |
| Max drawdown | -50.10% |
| Calmar ratio | +2.630 |
| Hit rate (vs universe median) | 54.2% |
| Mean Rank IC | +0.0193 |
| IC t-stat | +3.51 |
| IC positive windows (20d) | 54.5% |
| Turnover (daily rebalance) | 39.7% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.567 | +131.79% |
| aggressive | 75 | +1.556 | +130.06% |
| moderate | 100 | +1.553 | +129.49% |
| conservative | 150 | +1.545 | +128.34% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +2.181 | +263.91% | +0.0711 | 56.9% |
| bear | 132 | +0.036 | -17.11% | -0.0471 | 48.5% |
| flat | 415 | +1.407 | +108.80% | -0.0090 | 53.5% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.471 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 198 / 944 (21.0%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| biotech | 34.6% | 386 (40.9%) |
| ai | 23.3% | 235 (24.9%) |
| quantum | 21.6% | 181 (19.2%) |
| semis | 20.6% | 142 (15.0%) |
```

**Alert**: 21.0% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +40.10 +101.04%    +2.74  0.002    944
FF3                        +38.98  +98.22%    +2.66  0.007    944
Carhart-4F                 +38.87  +97.95%    +2.66  0.007    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
