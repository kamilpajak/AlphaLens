# v9D Retrospective Pre-2018 Replication — Postmortem 2026-05-05

**Verdict: INCONCLUSIVE.** Pre-reg `v9d_retrospective_pre_2018_2026_05_05` (sha256
`f43ac35785391fd3010efbc128d875fb0c982923df31a5ad7eed4d35f7548b58`) completed
2026-05-05. Class `retrospective_replication_pre_2018` 1/1, intra-class
verdict INCONCLUSIVE; program-level alpha-class counter advances to **n=25**.

## Headline

| Metric | Value | Threshold |
|---|---|---|
| **Primary αt (U3, 5×3=15 cells)** | **+2.45** | PASS_MARGINAL ≥ +2.50 (missed by 0.05) |
| Bounds-adjusted αt CI (Andrews-Manski, B∈[1.0%, 2.0%]/y) | **[+2.39, +2.42]** | excludes 0 |
| Cross-universe consistency \|U1 − U3\| | 1.37 | RECONSTRUCTION_DOMINANT > 3.0 → PASS |
| Cross-sub-period αt range | 1.04 | regime-stable < 2.5 → PASS |
| Within-sub-period αt range (max) | 2.20 | gate ≤ 1.5 (FAIL, but only blocks PASS_ROBUST) |

## Per-universe × per-sub-period αt

| | GFC_recovery (2008-04→2011-12) | mid_cycle_eu_debt (2012→2014) | late_cycle_china_shock (2015→2018-04) |
|---|---|---|---|
| U1 (legacy yaml, max-survivor) | **6.18** | 3.31 | 1.97 |
| U2 (iVol cache) | 2.85 | 3.06 | 1.92 |
| **U3 (NBER cap-band primary)** | **2.01** | **3.06** | **2.28** |

The U1 GFC αt=6.18 is the textbook survivorship signature — under crisis-era stress, only firms that survived 19 years to today are in U1's seed (today's IWM). U3's NBER cap-band reconstruction restores the 2008-2011 universe to a defensible cohort and pulls αt down to +2.01, structurally aligned with the +2.4 mean across the rest.

## Cross-window triangulation

| Window | αt (5-phase mean) | Source |
|---|---|---|
| 2024-04 → 2026-04 (BURNT holdout) | +2.29 | v9D recorded; matches v10 audit base exactly |
| **2008-04 → 2018-04 (FRESH OOS)** | **+2.45** | This experiment, U3 cap-band primary |
| Combined point estimate | ~+2.37 | Independent windows, consistent magnitude |

Pre-2018 αt is **higher** than the burnt-holdout αt by 0.16 — directionally supports the prior expectation that pre-2018 (closer to Xing 2010 1996-2005 in-sample) should be stronger or comparable, not weaker. Not falsified by regime change.

## What this means

1. **Signal directionally validated, magnitude not at deploy threshold.** A +2.45 αt with bounds excluding 0 is real evidence that v9D's cross-sectional residual scorer measures a stable phenomenon across two disjoint 8-10y windows. But +2.45 fails the pre-committed PASS_MARGINAL threshold (+2.50) by 0.05, so the experiment's verdict is **INCONCLUSIVE**, not PASS.

2. **Multiplicity discipline holds.** Program-level alpha-class counter advances 24 → 25; next test threshold tightens (naive Bonferroni n=25 → \|t\| ≥ 2.81). The pre-reg also expected Romano-Wolf step-down to land in [|t|≥2.10, 2.15] — but the bootstrap on this 15-cell layout produced no overlapping returns (sub-periods are disjoint dates), so RW reduced to "informational-only" and naive Bonferroni governs. See Known issues below.

3. **Capital deploy stays OFF-TABLE.** Pre-reg `capital_deploy_clause` requires PASS_ROBUST or PASS_MARGINAL to shorten the prospective replication wait; INCONCLUSIVE keeps the 12-month paper-trade checkpoint at ~2027-05-04.

4. **Survivorship-bias inference works empirically.** Cross-universe gap U1→U3 = 1.37 αt aligns with the perplexity-cited 1.0–1.5%/y bias estimate (assuming αpct/SE ≈ 1, the 1-σ shift maps to ~1 t-unit on this sample size). The pre-reg's [1.0%, 2.0%]/y bias range was conservative-correct.

## Stability diagnostics

- **Best era**: `mid_cycle_eu_debt` (2012-2014) — αt = 3.06 across U2 and U3 with tight ranges (within-sub 0.53/1.34); the v9D scorer is most robust under low-vol bull regimes.
- **Worst era**: `GFC_recovery` (2008-2011) — αt = 2.01 on U3 with within-sub range 2.20 (largest dispersion). The vol-mispricing signal is noisier under crisis-era stress, consistent with the literature on signal degradation during regime breaks.
- **Late-cycle**: 2015-2018-04 αt = 2.28, intermediate.

## Compute / cost

- Battery: 45 cells (3 universes × 3 sub-periods × 5 phases) on local M1 CPU
- Initial run: ~80 min for U1+U2 (30 cells), then **U3 stalled** at >45 min/cell on per-asof companyfacts I/O (187k file reads/cell)
- Fix: module-level cache for `TickerCikMap` + companyfacts JSON + SMD parquet history → **1000× speedup** (warm asof: 9.9s, subsequent: 0.3-0.5s)
- U3 restart: 15 cells in ~12 min wall (parallel-4)
- **Total cash: $0** (all local compute; vendor `iVol $399` already paid; `IVOLATILITY_API_KEY` added to `.env` 2026-05-05)
- Pre-experiment infra cost: 179 min iVol backfill (1339 pre-2018 delisted firms added to cache; 4438 parquets total)

## Bonferroni accounting

| Counter | Before | After |
|---|---|---|
| Program-level alpha-class n | 24 | **25** |
| Next test naive Bonferroni \|t\| | ≥ 2.78 | ≥ 2.81 |
| Romano-Wolf adjusted threshold (expected) | n/a | ~|t|≥2.13 (when bootstrap bug fixed) |
| Class `retrospective_replication_pre_2018` | new | 1/1 INCONCLUSIVE |

## Known issues

1. **Romano-Wolf bootstrap reports "no overlapping returns"** — `collect_pooled_returns` concatenates per-cell columns indexed by date and `dropna(how='any')`; since the 15 U3 cells span 3 disjoint sub-period date ranges, no row has data for all 15 cells, the panel collapses, and the bootstrap is skipped. Pre-reg expected adjusted critical |t| in [2.10, 2.15] from the resampling, but on this layout the bootstrap is unreachable. Fix candidates: (a) bootstrap per sub-period and combine, (b) treat phases × sub-periods as serial-stacked into ONE column per universe, (c) retain naive Bonferroni \|t\|≥2.81 fallback (pre-reg permits this). For this verdict the fallback governs and a +2.45 αt clearly fails the n=25 naive threshold (2.81), but well exceeds the much-lower n=15 ad-hoc threshold (~2.39 after RW). Doesn't change INCONCLUSIVE verdict.
2. **αpct annualization scale** — Carhart `alpha_annualized` reports +88.5%/y for U3 mean, vs Sharpe-implied ~30%/y. Likely the regression uses a different periods-per-year convention than the Sharpe calculation. Bounds-adjusted αt is unaffected (uses αt directly, not αpct), so the verdict is unchanged. Worth investigating whether `alpha_annualized` should use rebalances/year (≈50 for stride=5 + holding=1) or a different convention.
3. **U3 within-sub-period range = 2.20** exceeds the 1.5 stability threshold — this would block PASS_ROBUST but does not affect INCONCLUSIVE. The driver of dispersion appears to be GFC_recovery's αt range = 2.20; mid_cycle and late_cycle are tighter (1.34 and 1.19).

## Files produced

- 45 per-cell JSONs: `docs/research/v9d_retrospective_pre_2018/{U}_{sub}_{p}.json`
- Verdict: `docs/research/v9d_retrospective_pre_2018_verdict.{md,json}`
- Pre-reg: `docs/research/preregistration/params_v9d_retrospective_pre_2018_2026_05_05.json` (locked)
- Code:
  - `alphalens/paper_trade/universe_loaders.py` (3 loaders + caches)
  - `alphalens/backtest/bounds_inference.py` (Andrews-Manski)
  - `alphalens/backtest/romano_wolf.py` (Politis-Romano stationary block bootstrap + step-down)
  - `scripts/experiment_v9d_retrospective_pre_2018.py` (per-cell driver)
  - `scripts/aggregate_v9d_retrospective_verdict.py` (verdict synthesizer)
  - `scripts/run_v9d_retrospective_battery.sh` (orchestrator)
  - `scripts/build_ivol_inventory.py` (cache index rebuilder)
- Tests: 53 new (`test_universe_loaders.py`, `test_build_ivol_inventory.py`, `test_bounds_inference.py`, `test_romano_wolf.py`, `test_experiment_v9d_retrospective.py`, `test_aggregate_v9d_retrospective_verdict.py`)
- Backfill artifacts:
  - `~/.alphalens/polygon_delisted_2007_2018.parquet` (2051 historical delisted CS)
  - `~/.alphalens/survivorship/delisting_events_2008_2018.parquet` (1871 events for terminal-return patch)
  - `~/.alphalens/ivolatility_smd_inventory.parquet` (4438 ticker × date-range index)
  - `~/.alphalens/ivolatility_smd/` cache: 4438 parquets, 3.8 GB (was 3096 / 2.0 GB pre-experiment)
- Paradigm-failure ledger: NOT incremented (verdict INCONCLUSIVE, not FAIL).

## Action items

- **Open**: Romano-Wolf bootstrap fix for non-overlapping sub-period panel layout (low priority — pre-reg permits naive Bonferroni fallback).
- **Open**: αpct annualization audit in `attribution/factor_analysis.py:run_regression` — scaling factor between alpha_annualized and αt may not match the Sharpe convention.
- **Closed**: capital-deploy decision stays OFF-TABLE per pre-reg; awaits 12-month paper-trade checkpoint at ~2027-05-04 OR a stronger fresh-OOS replication on a future window.
