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
| Sharpe (gross) | +1.135 |
| Sharpe (moderate 100 bps) | +1.118 |
| Sharpe (conservative 150 bps) | +1.109 |
| Annual return (moderate net) | +63.68% |
| Max drawdown | -68.14% |
| Calmar ratio | +0.959 |
| Hit rate (vs universe median) | 50.9% |
| Mean Rank IC | +0.0070 |
| IC t-stat | +1.66 |
| IC positive windows (20d) | 57.5% |
| Turnover (daily rebalance) | 76.2% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +1.135 | +65.32% |
| aggressive | 75 | +1.122 | +64.09% |
| moderate | 100 | +1.118 | +63.68% |
| conservative | 150 | +1.109 | +62.87% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +1.023 | +53.75% | +0.0078 | 47.9% |
| bear | 138 | +2.849 | +695.65% | -0.0035 | 53.6% |
| flat | 464 | +0.441 | +10.24% | +0.0095 | 52.6% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.434 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 124 / 999 (12.4%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| biotech | 35.5% | 439 (43.9%) |
| ai | 22.1% | 216 (21.6%) |
| quantum | 22.1% | 193 (19.3%) |
| semis | 20.2% | 151 (15.1%) |
```

**Alert**: 12.4% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +24.54  +61.84%    +1.93  0.000    971
FF3                        +24.19  +60.97%    +1.86  0.004    971
Carhart-4F                 +24.26  +61.15%    +1.88  0.004    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [x] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
