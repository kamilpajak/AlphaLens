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
| Sharpe (gross) | +0.763 |
| Sharpe (moderate 100 bps) | +0.745 |
| Sharpe (conservative 150 bps) | +0.737 |
| Annual return (moderate net) | +30.43% |
| Max drawdown | -46.45% |
| Calmar ratio | +0.683 |
| Hit rate (vs universe median) | 52.5% |
| Mean Rank IC | +0.0154 |
| IC t-stat | +2.56 |
| IC positive windows (20d) | 57.8% |
| Turnover (daily rebalance) | 35.1% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.763 | +31.74% |
| aggressive | 75 | +0.750 | +30.76% |
| moderate | 100 | +0.745 | +30.43% |
| conservative | 150 | +0.737 | +29.78% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.532 | +15.26% | +0.0851 | 52.1% |
| bear | 132 | +0.428 | +6.08% | -0.1299 | 52.3% |
| flat | 415 | +1.096 | +60.40% | -0.0050 | 53.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 944 dni, próg koncentracji = 70%
Średni HHI: 0.437 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 107 / 944 (11.3%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 31.5% | 292 (30.9%) |
| quantum | 26.1% | 223 (23.6%) |
| ai | 23.7% | 258 (27.3%) |
| biotech | 18.7% | 171 (18.1%) |
```

**Alert**: 11.3% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +15.04  +37.91%    +1.32  0.003    944
FF3                        +14.00  +35.27%    +1.22  0.009    944
Carhart-4F                 +14.03  +35.36%    +1.23  0.009    944
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
