# P/C abnormal volume retrospective pre-2018 — INCONCLUSIVE

**Verdict:** INCONCLUSIVE

**Reason:** αt=2.65 ∈ [1.0, 2.85), bounds-lower-t=1.98

## Headline (U2 primary universe)

- αt mean (5 phases × 3 sub-periods): **+2.65**
- αpct mean: +825.04%/y
- Sharpe net mean: 1.40
- Within-sub-period αt range max: 1.76 (gate ≤1.5)
- Cross-sub-period αt range: 1.79 (gate ≤2.5)

**Andrews-Manski bounds CI** (bias range 1.0-2.0%/y, mapped to t-stat):
- Lower-bound t: +1.98
- Upper-bound t: +2.32
- Excludes 0: YES

## Per-universe × sub-period αt

| | GFC_recovery | mid_cycle_eu_debt | late_cycle_china_shock |
|---|---|---|---|
| **U1** | +1.31 | +2.04 | -0.33 |
| **U2** | +3.61 | +2.53 | +1.82 |

## Bonferroni accounting

- Program-level n=26, naive Bonferroni |t|≥2.85 (binding per pre-reg)
- Romano-Wolf per-strategy critical (issue #66): max |t|=4.47 vs adjusted=20.42, 0/15 rejected (B=10000, mean_block=4.0)
- Note: per-strategy independent block bootstrap on raw `long_net` (not Carhart-4F residuals). Independence destroys cross-strategy correlation; critical is closer to Bonferroni than the pre-reg's `~2.13` aspirational estimate. Heavy-tail data (single-cell outliers) may further inflate the bootstrap critical via t-pivot instability — the αt-vs-PASS_MARGINAL gate remains the binding pre-reg criterion.

## Coverage

- Cells loaded: 30/30

## Pre-reg sha256

- Original lock: `03ddf4b7906ed07049bbb74dcdd599afa29abda1e8c4f6551a1876c78e45e689`
- Post-amendment lock (log_marketCap dropped pre-first-run): `1debf1cc0ae8644d53955e7406007248e0052ab12559cff3f55fde688dbc8922`
