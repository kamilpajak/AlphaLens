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
| Sharpe (gross) | +0.959 |
| Sharpe (moderate 100 bps) | +0.940 |
| Sharpe (conservative 150 bps) | +0.931 |
| Annual return (moderate net) | +44.31% |
| Max drawdown | -59.14% |
| Calmar ratio | +0.774 |
| Hit rate (vs universe median) | 52.6% |
| Mean Rank IC | +0.0148 |
| IC t-stat | +3.13 |
| IC positive windows (20d) | 59.6% |
| Turnover (daily rebalance) | 71.2% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.959 | +45.76% |
| aggressive | 75 | +0.945 | +44.67% |
| moderate | 100 | +0.940 | +44.31% |
| conservative | 150 | +0.931 | +43.59% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.848 | +35.47% | +0.0552 | 53.9% |
| bear | 132 | +3.073 | +746.34% | -0.0900 | 53.8% |
| flat | 415 | -0.016 | -10.66% | +0.0095 | 51.1% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.411 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 84 / 944 (8.9%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 28.9% | 273 (28.9%) |
| ai | 26.3% | 292 (30.9%) |
| quantum | 26.2% | 210 (22.2%) |
| biotech | 18.7% | 169 (17.9%) |
```

**Alert**: 8.9% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +18.81  +47.41%    +1.56  0.000    944
FF3                        +18.64  +46.96%    +1.53  0.003    944
Carhart-4F                 +18.45  +46.50%    +1.52  0.003    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
