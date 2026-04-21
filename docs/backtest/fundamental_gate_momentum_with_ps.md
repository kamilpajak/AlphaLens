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
| Sharpe (gross) | +0.763 |
| Sharpe (moderate 100 bps) | +0.746 |
| Sharpe (conservative 150 bps) | +0.738 |
| Annual return (moderate net) | +30.80% |
| Max drawdown | -46.45% |
| Calmar ratio | +0.691 |
| Hit rate (vs universe median) | 52.4% |
| Mean Rank IC | +0.0334 |
| IC t-stat | +5.19 |
| IC positive windows (20d) | 65.6% |
| Turnover (daily rebalance) | 35.5% |
| Concentration top-5 | 100.0% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.763 | +32.11% |
| aggressive | 75 | +0.751 | +31.12% |
| moderate | 100 | +0.746 | +30.80% |
| conservative | 150 | +0.738 | +30.14% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | +0.532 | +15.26% | +0.0227 | 52.1% |
| bear | 138 | -0.002 | -18.77% | +0.0402 | 50.7% |
| flat | 464 | +1.192 | +71.58% | +0.0406 | 53.0% |

## Factor/theme concentration (factor-aware monitoring)

```
Okno backtestu: 999 dni, próg koncentracji = 70%
Średni HHI: 0.440 (0 = idealna dywersyfikacja, 1 = jeden theme)
Dni z max theme > 70%: 115 / 999 (11.5%)

| Theme | Średnia waga | Dni dominujący |
|---|---:|---:|
| semis | 31.6% | 302 (30.2%) |
| quantum | 27.4% | 260 (26.0%) |
| ai | 22.5% | 258 (25.8%) |
| biotech | 18.6% | 179 (17.9%) |
```

**Alert**: 11.5% dni miało koncentrację w jednym temacie > 70%. Portfolio zachowuje się jak single-theme bet a nie diversified thematic basket w tym oknie.

## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)

```
spec                      α bps/d    α ann   t-stat     R²      n
-----------------------------------------------------------------
CAPM                       +14.77  +37.23%    +1.32  0.003    971
FF3                        +13.52  +34.08%    +1.20  0.010    971
Carhart-4F                 +13.62  +34.33%    +1.21  0.010    971
```

Alpha that survives the Carhart row (with momentum factor Mom) is independent of generic momentum beta. If alpha collapses between FF3 and Carhart-4F, the strategy is re-packaged UMD exposure.

## Decision criteria (MVP1 → paper trade gate)

- [x] Sharpe (net moderate) > 0.3
- [x] IC positive windows > 60%
- [ ] Carhart-4F alpha t-stat > 1.5 (HAC)

**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.
