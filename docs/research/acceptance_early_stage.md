# Historical Layer 3 Acceptance — early-stage

- Picks source: `docs/backtest/compare_early_stage_2026-04-21.csv`
- Samples per regime: 10
- Seed: 42
- Analysts: market, news, fundamentals (social excluded for PIT rigor)
- Total samples: 30  (attempted), 29 (completed)

## Acceptance rate

| Regime | n | Accepted | Rate |
| --- | ---: | ---: | ---: |
| bull | 10 | 0 | 0.0% |
| bear | 10 | 0 | 0.0% |
| flat | 9 | 3 | 33.3% |
| **overall** | **29** | **3** | **10.3%** |

## Rating distribution

- BUY: 3
- OVERWEIGHT: 0
- HOLD: 0
- UNDERWEIGHT: 0
- SELL: 26

## Forward returns by Layer 3 decision (vs SPY benchmark)

Higher α on accepted rows = Layer 3 correctly picked winners. If rejected rows have similar/higher α, Layer 3 is destroying value. `n` reflects rows with a valid alpha at that horizon — recent picks may be missing at longer horizons.

| Horizon | Accepted α (n) | Rejected α (n) | Δ (accepted − rejected) |
| --- | ---: | ---: | ---: |
| 5d | +2.84% (3) | -2.44% (26) | +5.28% |
| 20d | +5.98% (3) | +4.34% (25) | +1.65% |
| 60d | +36.50% (2) | +8.75% (25) | +27.75% |
| 120d | +49.42% (2) | +5.21% (25) | +44.20% |