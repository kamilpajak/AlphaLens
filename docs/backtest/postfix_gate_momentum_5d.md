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
| Sharpe (gross) | +0.676 |
| Sharpe (moderate 100 bps) | +0.659 |
| Sharpe (conservative 150 bps) | +0.651 |
| Annual return (moderate net) | +24.23% |
| Max drawdown | -50.34% |
| Calmar ratio | +0.506 |
| Hit rate (vs universe median) | 52.1% |
| Mean Rank IC | +0.0313 |
| IC t-stat | +4.82 |
| IC positive windows (20d) | 64.6% |
| Turnover (daily rebalance) | 35.4% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.676 | +25.48% |
| aggressive | 75 | +0.663 | +24.54% |
| moderate | 100 | +0.659 | +24.23% |
| conservative | 150 | +0.651 | +23.61% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.351 | +3.60% | +0.0197 | 51.9% |
| bear | 138 | -0.299 | -32.26% | +0.0321 | 49.3% |
| flat | 464 | +1.247 | +77.57% | +0.0409 | 53.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.439 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 114 / 999 (11.4%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 31.8% | 304 (30.4%) |
| quantum | 27.4% | 262 (26.2%) |
| ai | 22.7% | 259 (25.9%) |
| biotech | 18.2% | 174 (17.4%) |
```

**Alert**: 11.4% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +12.75  +32.13%    +1.13  0.003    971
FF3                        +11.47  +28.91%    +1.01  0.009    971
Carhart-4F                 +11.59  +29.21%    +1.02  0.010    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
