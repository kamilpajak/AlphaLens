# Historical Layer 3 Acceptance — momentum

- Picks source: `docs/backtest/compare_momentum_2026-04-21.csv`
- Samples per regime: 10
- Seed: 42
- Analysts: market, news, fundamentals (social excluded for PIT rigor)
- Total samples: 30  (attempted), 30 (completed)

## Acceptance rate

| Regime | n | Accepted | Rate |
| --- | ---: | ---: | ---: |
| bull | 10 | 1 | 10.0% |
| bear | 10 | 2 | 20.0% |
| flat | 10 | 1 | 10.0% |
| **overall** | **30** | **4** | **13.3%** |

## Rating distribution

- BUY: 4
- OVERWEIGHT: 0
- HOLD: 0
- UNDERWEIGHT: 1
- SELL: 25

## Forward returns by Layer 3 decision (vs SPY benchmark)

Higher α on accepted rows = Layer 3 correctly picked winners. If rejected rows have similar/higher α, Layer 3 is destroying value. `n` reflects rows with a valid alpha at that horizon — recent picks may be missing at longer horizons.

| Horizon | Accepted α (n) | Rejected α (n) | Δ (accepted − rejected) |
| --- | ---: | ---: | ---: |
| 5d | +0.89% (4) | +0.03% (26) | +0.87% |
| 20d | +11.23% (4) | +4.59% (25) | +6.64% |
| 60d | +15.60% (4) | -4.30% (24) | +19.90% |
| 120d | +38.83% (4) | -9.31% (24) | +48.13% |