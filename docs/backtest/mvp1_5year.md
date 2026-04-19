# MVP1 Backtest Report

- **Window**: 2021-04-19 → 2026-04-17
- **Benchmark**: SPY
- **Top-N**: 30
- **Holding period**: 5 trading days
- **Screener universe**: 782 tickers
- **Backtest days**: 1031

## Headline metrics

| Metric | Value |
| --- | --- |
| Sharpe (gross) | +0.289 |
| Sharpe (moderate 100 bps) | +0.246 |
| Sharpe (conservative 150 bps) | +0.224 |
| Annual return (moderate net) | +3.05% |
| Max drawdown | -33.25% |
| Calmar ratio | +0.123 |
| Hit rate (vs universe median) | 52.6% |
| Mean Rank IC | -0.0015 |
| IC t-stat | -0.30 |
| IC positive windows (20d) | 51.2% |
| Turnover (daily rebalance) | 39.0% |
| Concentration top-5 | 16.7% |

## Cost sensitivity

| Profile | Drag (bps/yr) | Sharpe | Annual return |
| --- | ---: | ---: | ---: |
| gross | 0 | +0.289 | +4.09% |
| aggressive | 75 | +0.257 | +3.31% |
| moderate | 100 | +0.246 | +3.05% |
| conservative | 150 | +0.224 | +2.54% |

## Regime breakdown

| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| bull | 397 | -0.306 | -9.21% | +0.0134 | 51.1% |
| bear | 155 | +0.830 | +20.80% | -0.0744 | 45.2% |
| flat | 479 | +0.594 | +11.08% | +0.0099 | 56.2% |

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
bull   |  397 | +22.95% | +15.97% | +1.44 | -7.02% | -10.13% | +3.10%
bear   |  155 | +27.23% | +28.80% | +0.95 | +22.61% | +77.03% | -54.42%
flat   |  479 | +21.63% | +17.55% | +1.23 | +12.86% | -7.63% | +20.49%
```

Interpretation: if **vol_ratio < 0.8 AND excess_return near zero**, top-N is capturing defensive low-vol positioning rather than predictive alpha.

## Fama-French 3-factor alpha

```
Fama-French 3-factor regression (n=1003)
  alpha (daily)        = +0.64 bps/day
  alpha (annualized)   = +1.62%
  alpha t-stat         = +0.14
  beta[Mkt-RF]         = +0.056
  beta[SMB]            = -0.129
  beta[HML]            = +0.033
  R²                   = 0.004
```

## Decision criteria (MVP1 → paper trade gate)

- [ ] Sharpe (net moderate) > 0.3
- [ ] IC positive windows > 60%
- [ ] FF3 alpha t-stat > 1.5

**Recommendation: ABANDON** — no edge detected; rely on Layer 1 + 2b only.
