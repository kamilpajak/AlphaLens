# multi_source_global_lasso_20d Phase B — holdout reveal

**Pre-registration:** `multi_source_global_lasso_20d_2026_04_30` (class `multi_source_two_stage_search_2026_04_30`, n=3 → Bonferroni |t|≥2.39)
**Phase offset:** 0 (stride 5, holding 20d, overlap 4-tranche)
**Train:** 2014-01-01 → 2024-04-29
**Holdout:** 2024-04-30 → 2026-04-30
**ADV floor:** $5M, 20d median
**Cost:** half-spread 10 bps + 5 bps adverse selection
**HAC maxlags:** 5 (overlap correction)
**Sharpe:** Lo (2002) variance-ratio adjusted

## Verdict: FAIL (zero-coef structural artifact — see v2 finding)

## Headline metrics (holdout)

| Metric | Value |
| --- | ---: |
| n rebalances (overlapping 20d) | 95 |
| mean top-N | 30.0 |
| turnover / rebal | 12.9% |
| Sharpe (gross, Lo-adj) | 1.01 |
| Sharpe (gross, naive) | 1.64 |
| Sharpe (net, Lo-adj) | 0.98 |
| α (gross, 4F) annualised | +125.34% |
| α (net, 4F) annualised | +123.38% |
| α t-stat (HAC, maxlags=5) | +1.32 |
| Excess vs SPY (gross) ann | +22.69% |
| Excess vs SPY (net) ann | +20.73% |
| Max drawdown (net cum) | -61.96% |
| Cost drag annualised | 1.96% |
| Carhart aggregated n | 88 |

## Train/holdout sizes

- feature rows in train pool: 310517
- train rows after target NaN-drop: 310517
- feature rows in holdout: 97451
- holdout rows scored: 97451

## Global Lasso fit (20d-forward target)

- n_train: 310517
- λ chosen: 0.01529
- nonzero coefs: 0 / 21
- CV mean MSE: 0.02404

## v3 vs v1+v2 (horizon ablation)

v1 (4-regime, 5d): mean phase αt=+0.65, dispersion 4.9pp, FAIL.
v2 (global, 5d): mean phase αt=+0.55, dispersion 5.6pp, FAIL — 0/21 nonzero coefs.
v3 (global, 20d): tested whether 20d horizon surfaces signal that 5d doesn't.

## Pre-registration discipline

- 21-feature whitelist FROZEN; identical to v1 + v2.
- λ grid (25 points), embargo (60d), n_folds (3) — all per pre-reg.
- ONE-shot holdout, no peek-and-tune.
- Carhart attribution post-hoc on 20d-aggregated factors with HAC maxlags=5.
- Lo (2002) variance-ratio Sharpe adjustment for overlap autocorrelation.
- Single-variable ablation FROM v2: only target horizon (5d→20d) and inference machinery (HAC=5, Lo Sharpe) change.
