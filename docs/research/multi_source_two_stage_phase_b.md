# multi_source_two_stage Phase B — holdout reveal

**Pre-registration:** `multi_source_two_stage_2026_04_30`
**Phase offset:** 0 (stride 5)
**Train:** 2014-01-01 → 2024-04-29
**Holdout:** 2024-04-30 → 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection (round-trip 2× one-way)

## Verdict: PASS (provisional — pending multi-phase audit)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances | 99 |
| mean top-N | 30.0 |
| turnover / rebal | 18.0% |
| Sharpe (gross) | 1.41 |
| Sharpe (net) | 1.19 |
| α (gross, 4F) annualised | +83.62% |
| α (net, 4F) annualised | +80.91% |
| α t-stat (HAC) | +2.01 |
| Excess vs SPY (gross) ann | +10.07% |
| Excess vs SPY (net) ann | +7.36% |
| Max drawdown (net cum) | -9.82% |
| Cost drag annualised | 2.72% |

## Train/holdout sizes

- feature rows in train pool: 310517
- train rows after target NaN-drop: 310517
- feature rows in holdout: 97451
- holdout rows scored: 97451

## Per-regime fits

| Regime | n_train | λ chosen | nonzero coefs | CV mean MSE |
| --- | ---: | ---: | ---: | ---: |
| Q1_calm | 86643 | 0.0001363 | 15 | 0.003296 |
| Q2 | 82220 | 0.002861 | 0 | 0.004593 |
| Q3 | 76342 | 0.001309 | 0 | 0.005716 |
| Q4_stress | 65312 | 0.008212 | 0 | 0.007253 |

## Pre-registration discipline

- 21-feature whitelist FROZEN; not modified mid-experiment.
- λ grid (25 points), embargo (60d), n_folds (3) — all per pre-reg.
- ONE-shot holdout, no peek-and-tune.
- Carhart attribution is post-hoc; target is raw 5d-forward excess return.
