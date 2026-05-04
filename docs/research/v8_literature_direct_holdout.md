# v8 literature-direct holdout reveal — FAIL (αt=+2.08 < 2.95 program-Bonferroni n=15)

**Date:** 2026-05-03
**Pre-reg:** v8_literature_direct_options_implied_2026_05_03
**Score:** -ivp30 (Xing 2010 1y-rolling IV-percentile).

## Headline (PRIMARY = LONG TOP decile by -ivp30 = LOW-IV names)

| Metric | Value |
| --- | ---: |
| n holdout rebalances | 101 |
| Sharpe (gross) | 2.05 |
| Sharpe (net 30bps RT) | 2.05 |
| Carhart-4F α (gross, ann) | +3340.99% |
| Carhart-4F α (net, ann) | +3340.69% |
| α t-stat (HAC=5) | **+2.08** |
| Excess vs MDY (gross, ann) | +627.63% |
| Excess vs MDY (net, ann) | +627.33% |
| Max drawdown (net cum) | -12.93% |

## L/S diagnostic (top − bottom decile, NOT primary verdict)

| Metric | Value |
| --- | ---: |
| Sharpe (gross) | -0.78 |
| Sharpe (net 60bps RT) | -0.78 |
| Carhart-4F α (gross, ann) | -411.90% |
| α t-stat (HAC=5) | -0.30 |

## Coverage

- Non-NaN ivp30 / total feature rows: 108160 / 108160 = 100.0%

## Pre-reg discipline

- DETERMINISTIC scorer = -ivp30 (no fit, no sign-flip surface).
- Direction LOCKED ex-ante per Xing 2010 / Bali-Hovakimian 2009 NEGATIVE-sign prior.
- ONE-shot holdout, no peek-and-tune.
- Carhart-4F (HAC=5) attribution post-hoc on top-decile vs MDY-excess.
- L/S diagnostic reported as power-loss check, NOT additional Bonferroni test.
- Single-bar PASS rule (no stretch tier): αt ≥ 2.95 program-Bonferroni n=15.
