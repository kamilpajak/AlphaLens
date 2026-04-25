# Cost Model Validation — Layer 2b

- **Backtest window**: 2021-06-01 → 2026-04-17
- **Portfolio value**: $10,000,000
- **ADV window**: 21 trading days (trailing, lookahead-safe)
- **Baseline Sharpe (gross)**: +1.418

Tiered flat-bps cost model + scale-path validation. Addresses the
third Perplexity-flagged gap after survivorship (PR #9 PASS) and
walk-forward (PR #11 PASS).

## Tier definitions (AQR-anchored)

| Tier | ADV percentile | bps annual |
| --- | --- | ---: |
| mega | 80-100% | 3.0 |
| large | 60-80% | 10.0 |
| mid | 40-60% | 25.0 |
| small | 20-40% | 50.0 |
| micro | 0-20% | 100.0 |

Per Frazzini-Israel-Moskowitz (2018), *Trading Costs* (AQR /
SSRN 3229719). Empirical institutional execution costs: small-caps
30-50 bps (not 100+), mid-caps 15-35, large-caps 5-15, mega 1-3.
Micro-tier is an extrapolation past the paper's universe coverage.

## Scale-path results

| Metric | Value |
| --- | ---: |
| Total pick-days | 5155 |
| Median participation | 1.22% |
| Q95 participation | 36.27% |
| Max participation | 80000.00% |
| Fraction > 15.0% ADV | 8.87% |

### Worst offenders (top 10)

| Date | Ticker | Rank | Tier | Participation | $position | $ADV |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| 2022-09-07 | COHR | 3 | mid | 80000.00% | $2,000,000 | $1,000 |
| 2023-09-13 | ALNY | 2 | mid | 56363.64% | $2,818,182 | $1,000 |
| 2022-07-15 | COHR | 4 | mid | 47272.73% | $1,181,818 | $1,000 |
| 2022-08-30 | COHR | 4 | mid | 47272.73% | $1,181,818 | $1,000 |
| 2022-07-01 | COHR | 3 | mid | 40000.00% | $2,000,000 | $1,000 |
| 2022-09-02 | COHR | 3 | mid | 40000.00% | $2,000,000 | $1,000 |
| 2022-09-06 | COHR | 3 | mid | 40000.00% | $2,000,000 | $1,000 |
| 2022-07-05 | COHR | 4 | mid | 23636.36% | $1,181,818 | $1,000 |
| 2022-07-18 | COHR | 5 | mid | 14545.45% | $363,636 | $1,000 |
| 2022-08-31 | COHR | 5 | mid | 7272.73% | $363,636 | $1,000 |

### Per-tier peak participation

| Tier | Median | Max |
| --- | ---: | ---: |
| mega | 0.26% | 4.87% |
| large | 0.92% | 11.71% |
| mid | 2.27% | 80000.00% |
| small | 4.50% | 53.70% |
| micro | 25.56% | 1884.02% |

## Tiered vs flat cost comparison

| Scenario | Sharpe | Annual drag (bps) |
| --- | ---: | ---: |
| Gross (no drag) | +1.418 | 0 |
| Flat 100 bps (current production) | +1.403 | 100 |
| Tiered (AQR-anchored) | +1.414 | 28 |

Tier distribution on the last rebalance date:
  - mega: 23 tickers
  - large: 22 tickers
  - mid: 23 tickers
  - small: 22 tickers
  - micro: 22 tickers

## Decision gate

- **fraction**: FAIL — fraction pick-days > 15.0% ADV = 8.87% (threshold < 5%)
- **max**: FAIL — max participation = 80000.00% ADV (threshold < 20.0%)

**Overall: FAIL**

**Interpretation.** Strategy exceeds institutional-grade execution thresholds at $10.0M AUM: fraction-gate: 8.87% of pick-days exceeded 15% ADV (threshold was <5%); max-gate: max participation 80000.00% ADV exceeded 20% ceiling. Do NOT wire the tiered model into production. Keep flat 100 bps as the production cost model and document this AUM ceiling in the strategy disclosure. To lift the ceiling: reduce top-N, widen the universe, extend the holding period, or deploy at a smaller AUM.

## Limitations

- **Rolling 21-day ADV window** chosen per zen over 60/252 because
  momentum strategies have theme-driven volume spikes; shorter
  window tracks current execution reality but reassigns borderline
  tickers on noisy days.
- **Per-date tier bucketing** — lookahead-safe (tiers computed
  from bars STRICTLY before each rebalance date). Tickers with no
  ADV history on a given date fall back to `mid` with silent
  imputation; borderline for new IPOs in the first ~21 days after
  listing.
- **AQR tier bps are empirical but generic** — calibrated on US
  large/mid institutional universe, not Layer 2b-specific. Micro
  tier is extrapolation past AQR's coverage.
- **No Almgren-Chriss market-impact modeling** — defensible
  because scale-path hard-caps participation. Participation cap +
  tiered bps proxies the square-root impact law for sub-$30M AUM
  per zen review. Not a substitute for real impact modeling at
  institutional AUM.
- **Polygon Basic volume data** — ADV derived from Lean CSV
  (Polygon grouped-daily) which aggregates across venues. Adequate
  for bucketing; not precise enough for real execution planning.
