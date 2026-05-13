# v9D Retrospective Pre-2018 — Verdict

**Verdict: INCONCLUSIVE**

_Reason: αt=2.45 in [1.0, 2.5), bounds CI may straddle zero_

## Coverage
- 45 / 45 cells present

## Primary U3 (cap-band NBER rebuild)
- Mean αt across 5 × 3 = 15 cells: **2.45**
- Mean αpct (annualized %): 17.56
- Mean Sharpe net: 1.80
- Within-sub-period αt range max: 2.20
- Across-sub-period αt range: 1.04 (min=2.01, max=3.06)

## Andrews-Manski bounds
- Bias range pre-locked: [1.0, 2.0] %/y
- Unbiased αt CI: [2.15, 2.30]
- Unbiased αpct CI: [15.56, 16.56] %/y
- Lower-bound excludes 0: YES

## Romano-Wolf step-down (n=15 family, U3 cells)
- Max observed |t|: 8.40
- Adjusted critical |t|: 6.18
- Strategies rejected: 1 / 15
- Bootstrap B=10000, mean_block=4.0
- Note: per-strategy independent block bootstrap (issue #66) operates on raw `long_net` returns, not Carhart-4F residuals. Per-strategy independence destroys cross-strategy correlation that would tighten the family-max critical, so this critical is closer to Bonferroni than the pre-reg's `~2.13` aspirational estimate. The αt-vs-PASS_MARGINAL gate remains the binding pre-reg criterion.

## Cross-universe sanity
- |U1 αt − U3 αt|: 1.37 (reconstruction-dominant threshold: 3.0)

## Per-universe × per-sub-period αt

| Universe | Sub-period | n_phases | αt mean | αt range | Sharpe net | n_obs |
|---|---|---|---|---|---|---|
| U1 | GFC_recovery | 5 | 6.18 | 1.28 | 5.53 | 1341 |
| U1 | mid_cycle_eu_debt | 5 | 3.31 | 0.31 | 3.09 | 1096 |
| U1 | late_cycle_china_shock | 5 | 1.97 | 0.56 | 1.42 | 1215 |
| U2 | GFC_recovery | 5 | 2.85 | 1.48 | 1.89 | 1341 |
| U2 | mid_cycle_eu_debt | 5 | 3.06 | 0.53 | 3.04 | 1096 |
| U2 | late_cycle_china_shock | 5 | 1.92 | 0.62 | 1.52 | 1215 |
| U3 | GFC_recovery | 5 | 2.01 | 2.20 | 1.22 | 1341 |
| U3 | mid_cycle_eu_debt | 5 | 3.06 | 1.34 | 2.77 | 1096 |
| U3 | late_cycle_china_shock | 5 | 2.28 | 1.19 | 1.41 | 1215 |
