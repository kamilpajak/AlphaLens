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
| Sharpe (gross) | +0.720 |
| Sharpe (moderate 100 bps) | +0.703 |
| Sharpe (conservative 150 bps) | +0.695 |
| Annual return (moderate net) | +27.55% |
| Max drawdown | -46.89% |
| Calmar ratio | +0.615 |
| Hit rate (vs universe median) | 52.6% |
| Mean Rank IC | +0.0316 |
| IC t-stat | +4.99 |
| IC positive windows (20d) | 64.5% |
| Turnover (daily rebalance) | 35.5% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.720 | +28.83% |
| aggressive | 75 | +0.707 | +27.87% |
| moderate | 100 | +0.703 | +27.55% |
| conservative | 150 | +0.695 | +26.91% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.603 | +20.16% | +0.0221 | 53.1% |
| bear | 138 | -0.598 | -44.12% | +0.0378 | 49.3% |
| flat | 464 | +1.213 | +75.30% | +0.0379 | 53.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.435 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 106 / 999 (10.6%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 31.0% | 297 (29.7%) |
| quantum | 27.6% | 269 (26.9%) |
| ai | 22.5% | 251 (25.1%) |
| biotech | 18.9% | 182 (18.2%) |
```

**Alert**: 10.6% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +14.13  +35.60%    +1.22  0.002    971
FF3                        +12.80  +32.26%    +1.10  0.009    971
Carhart-4F                 +12.81  +32.28%    +1.10  0.009    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
