# MVP1 Backtest Report

- **Window**: 2021-04-19 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 5 trading days
- **Screener universe**: 782 tickers
- **Backtest days**: 1031

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +0.509 |
| Sharpe (moderate 100 bps) | +0.483 |
| Sharpe (conservative 150 bps) | +0.471 |
| Annual return (moderate net) | +12.06% |
| Max drawdown | -69.63% |
| Calmar ratio | +0.189 |
| Hit rate (vs universe median) | 49.4% |
| Mean Rank IC | -0.0015 |
| IC t-stat | -0.30 |
| IC positive windows (20d) | 51.2% |
| Turnover (daily rebalance) | 64.8% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.509 | +13.19% |
| aggressive | 75 | +0.490 | +12.34% |
| moderate | 100 | +0.483 | +12.06% |
| conservative | 150 | +0.471 | +11.50% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | -0.026 | -6.93% | +0.0134 | 49.9% |
| bear | 155 | +1.483 | +59.04% | -0.0744 | 46.5% |
| flat | 479 | +0.617 | +19.24% | +0.0099 | 49.9% |

## IC by score decile (tail-concentration test)

**Tail concentration score**: 1.21 (>1.5 = strong tails, ~1.0 = flat)

| Decile | n samples | Mean return | Std | Sharpe within |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 69,122 | +0.263% | 4.205% | +0.99 |
| 2 | 68,551 | +0.220% | 3.514% | +0.99 |
| 3 | 68,466 | +0.225% | 3.123% | +1.14 |
| 4 | 68,579 | +0.188% | 2.972% | +1.01 |
| 5 | 68,678 | +0.153% | 2.898% | +0.84 |
| 6 | 68,372 | +0.156% | 2.864% | +0.86 |
| 7 | 68,452 | +0.162% | 2.799% | +0.92 |
| 8 | 68,593 | +0.202% | 2.667% | +1.20 |
| 9 | 68,451 | +0.126% | 2.652% | +0.76 |
| 10 | 69,017 | +0.170% | 2.890% | +0.93 |

## Regime vol decomposition (is top-N just defensive?)

```
Regime | Days | Top-N Vol | Med Vol | Vol Ratio | Top-N Ret | Med Ret | Excess
----   | ---- | --------- | ------- | --------- | --------- | ------- | ------
bull   |  397 | +35.31% | +15.97% | +2.21 | -0.93% | -10.13% | +9.20%
bear   |  155 | +35.51% | +28.80% | +1.23 | +52.66% | +77.03% | -24.37%
flat   |  479 | +43.21% | +17.55% | +2.46 | +26.65% | -7.63% | +34.28%
```

Interpretation: if **vol_ratio < 0.8 AND excess_return near zero**, top-N is capturing defensive low-vol positioning rather than predictive alpha.

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 1031 dni, próg koncentracji = 70%
Średni HHI: 0.883 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 0 / 1031 (0.0%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| biotech | 2.4% | 103 (10.0%) |
| quantum | 2.3% | 115 (11.2%) |
| ai | 1.9% | 81 (7.9%) |
| semis | 1.2% | 19 (1.8%) |
```

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                        +7.36  +18.55%    +0.95  0.001   1003
FF3                         +6.99  +17.61%    +0.90  0.002   1003
Carhart-4F                  +7.06  +17.79%    +0.90  0.002   1003
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
