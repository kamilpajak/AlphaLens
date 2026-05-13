# `run_regression` callers audit — αpct annualization bug scope (2026-05-05)

## Summary

Audited 40 call sites of `alphalens.attribution.factor_analysis.run_regression`. The function uses `alpha_annualized = alpha_daily * periods_per_year` with default `periods_per_year=252`. Callers passing strided per-rebalance returns (e.g. 5d holding) without overriding `periods_per_year` produce αpct inflated by `252 / true_periods_per_year` (e.g. 5× for weekly stride).

## Classification

| Category | Count | Description |
|---|---|---|
| **BUGGY** | 27 | Strided returns + default periods_per_year=252 |
| **LIVE BUGGY** | 1 | Inside live cron path (`paper_trade/verdict.py`) |
| Clean daily | 5 | Daily returns input → default 252 correct |
| Explicit override | 2 | Already passes `periods_per_year=...` |
| Unknown stride | 5 | Manual review needed |

## BUGGY (27 sites — historical FAILed experiments + 2 active retrospective drivers)

These all pass strided portfolio returns but rely on default `periods_per_year=252`. αpct inflated by ~50× for stride=5 (weekly):

- `scripts/experiment_v9d_retrospective_pre_2018.py:241` ⚠️ **active** — re-run + fix in Tier 1
- `scripts/experiment_pc_retrospective.py:193` ⚠️ **active** — re-run + fix in Tier 1
- `scripts/experiment_tri_factor_edgar.py:163` (FAIL'd, historical)
- `scripts/experiment_v9_sign_constrained.py:224` (FAIL'd, historical)
- `scripts/experiment_momentum_lowvol_combo.py:87` (FAIL'd)
- `scripts/experiment_alt_data_lasso_longshort_20d.py:452` (FAIL'd)
- `scripts/experiment_quality_momentum_combo.py:159` (FAIL'd)
- `scripts/experiment_alt_data_v6a_revised.py:466` (FAIL'd)
- `scripts/experiment_constrained_contrarian.py:162-163` (CLOSED)
- `scripts/experiment_constrained_momentum.py:144-145` (CLOSED)
- `scripts/experiment_vol_target_overlay.py:111` (FAIL'd, paradigm 10)
- `scripts/experiment_distress_credit_v1.py:86` (FAIL'd)
- `scripts/experiment_v10_drawdown_overlay.py:193` (FAIL'd, paradigm 11)
- `scripts/experiment_alt_data_lasso_long_only_20d.py:408` (FAIL'd)
- `scripts/experiment_v9_cross_sectional_residual.py:184` (active — v9D source)
- `scripts/experiment_regime_overlay.py:103,105` (CLOSED)
- `scripts/experiment_longshort_mom_lowvol.py:221-222` (CLOSED)
- `scripts/experiment_multi_source_global_lasso_20d.py:301` (FAIL'd)
- `scripts/experiment_lightgbm_mse_longshort_20d.py:456` (FAIL'd)
- `scripts/experiment_v8_literature_direct.py:236` (FAIL'd)
- `scripts/experiment_layer2d_str_and_contrarian.py:188-189` (CLOSED)
- `scripts/experiment_v7_options_implied.py:266` (FAIL'd)
- `scripts/experiment_multi_source_two_stage.py:259` (FAIL'd)

## LIVE BUGGY (1 site — added to Tier 1 fix)

- `alphalens/paper_trade/verdict.py:114` ⚠️ **live cron path**
- `alphalens/paper_trade/verdict.py:153` ⚠️ **live cron path** (per-sub-period αt)
- v9D paper-trade tracker is weekly cadence (5d rebalance per `v9d_long_only_paper_trade_2026_05_04` pre-reg). Default `periods_per_year=252` would inflate `alpha_annualized` field by 5× on every weekly verdict update.
- **Fix scope expanded**: add `periods_per_year=int(252/holding_period_days)` (assume 5 = weekly) at both call sites.
- **Mitigation**: `verdict.py` reports both `alpha_t` (correct, scale-invariant) and `alpha_annualized` (currently wrong). `alpha_t` was the gating field (≥1.96 unadjusted at 26w/52w gates) — verdicts not skewed by bug. But `alpha_annualized` reported to user/Telegram digest would be misleading.

## Clean daily (5 sites — input is daily returns, 252 correct)

- `alphalens/archive/rotation/sanity_checks.py:184` (overrides via parameter)
- `alphalens/attribution/factor_analysis.py:124,193,214` (internal: `run_carhart_attribution` etc, called with daily)
- `scripts/run_layer2d_backtest.py:125`

## Explicit override (2 sites)

- `alphalens/archive/rotation/sanity_checks.py:191` (passes `periods_per_year=periods_per_year`)
- `scripts/quiver_validate.py` (manual check below)

## Unknown stride (5 sites — manual review)

- `scripts/quiver_robustness.py:62`
- `scripts/quiver_validate.py:163,211`
- `scripts/revalidate_carhart.py:104`

These are CLOSED/historical rotation scripts (Layer 2d archived per ADR 0005). Low priority — not in active research path.

## Migration scope (Tier 2.C)

For migration script `migrate_fix_alpha_annualized.py`:
- Active drivers: 2 (v9D retro + PC retro) — handled by re-run in Tier 1.
- Historical FAILed: 25+ experiment cell JSON dirs to consider. Each driver's stride needs to be inferred from its argparse default or hardcoded constant.
- LIVE paper_trade/verdict.py: no JSON output yet (no completed weekly ticks). Migration not needed; just fix code.

## Recommendation

**Tier 1 (this plan, must-fix)** — extend to:
- `experiment_v9d_retrospective_pre_2018.py:241` ✓
- `experiment_pc_retrospective.py:193` ✓
- `paper_trade/verdict.py:114,153` ⬅ **NEW (LIVE BUGGY)**

**Tier 1 sweep (this plan, recommended)** — also fix the 25+ historical drivers in single batch (1-line change per driver). Migration script (Tier 2.C) cannot fix in-script bug — only the live computation. So while migration handles cell JSON outputs, the next time someone re-runs a historical driver, bug returns. Defensive: fix code + migrate JSONs.

**Tier 2 (deferred)** — full `run_regression` API refactor (make `periods_per_year` required) — separate plan.

## 2026-05-13 amendment — Tier 2 landed

Issue #67 closed via PR (kwarg made required + keyword-only). Production-package callers and 1 active script (`scripts/v9d_cross_period_diagnostic.py`) migrated in the same PR. Internal wrappers (`run_carhart_attribution`, `run_ff5_umd_attribution`, `run_q4_attribution`) now hardcode `periods_per_year=252` since they're daily-by-design. Test suite migrated (`tests/test_factor_analysis.py` — 19 calls updated, `test_default_periods_per_year_is_252` renamed to `test_periods_per_year_is_required` asserting TypeError on omission).

~30 archived call sites in closed/FAIL'd paradigm drivers (`scripts/experiment_*.py`, `scripts/quiver_*.py`, `scripts/revalidate_*.py`, `alphalens/archive/rotation/sanity_checks.py`) remain unfixed per ADR 0005 closed-layer policy — they'd raise TypeError if invoked, but the test suite stays green because they aren't exercised. Acceptable tech debt as anti-pattern catalog.
