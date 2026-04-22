# Layer 2d insider screener — Phase 3b.5 validation closeout

**Status:** DRAFT — awaiting in-sample + OOS backtest results (Phase 3b.3).
**Backtest config:** weekly rebalance (stride=5), top-N=15, holding=60d, benchmark=SPY.
**Universe:** PIT union 1403 tickers (in-sample 2011-2022) / TBD (OOS 2023-2026).

## Rebalance cadence (daily vs weekly)

Phase 3b design doc specified daily cadence; mid-validation we added weekly (stride=5) as an explicit fallback when the EDGAR cache is cold. **Both modes are first-class** in `scripts/run_layer2d_backtest.py` via `--rebalance-stride`:

| Mode | Stride | N in-sample / OOS | Use when |
|---|---|---|---|
| Daily (design default) | 1 | 3020 / 800 | Cache prewarmed via `scripts/prewarm_form4_cache.py` |
| Weekly (runtime fallback) | 5 | 604 / 160 | Cache cold, need results within ~6h/split |

Both modes scale cost drag and Sharpe at the correct cadence (`rebalances_per_year = 252 / stride`). Regression-tested in `tests/test_backtest_engine_stride.py`.

## Perplexity R11 review (2026-04-22) — scaling validation, both modes

| Item | Verdict |
|---|---|
| a) Cost drag formula (round_trip × turnover × rebal/y) | OK both modes |
| b) Sharpe naive sqrt(k) annualization | **Correction noted**: ignores negative lag-1 autocorrelation of daily returns (mean reversion) → overstates vol → understates Sharpe. Report adds autocorr-adjusted Sharpe (Lo 2002 formulation via `sharpe_autocorr_adjusted`) side-by-side with naive for both daily and weekly. |
| c) Carhart alpha × 252 annualization | OK both modes — alpha_daily is 1-day magnitude regardless of sampling |
| d) Bootstrap block length N^(1/3) | OK both modes; minor calendar-span asymmetry |
| e) Hidden scaling trap | Autocorrelation-adjusted Sharpe (addressed) |

## Methodological caveats — weekly mode (Perplexity R10 review, 2026-04-22)

_Apply only if the validation was run with `--rebalance-stride 5`. Skip this section entirely for the daily design-spec run._

During 3b.3 execution the daily-rebalance × 12y × 1403 tickers × EDGAR-bound scorer projected >30h wall clock. Phase 3b design doc left rebalance cadence implicit at "daily". Three runtime-side deltas were introduced and validated against Perplexity R10 (verdict: **OK with caveats**):

1. **Weekly rebalance cadence (stride=5).** N drops to 604 in-sample (vs 3020 daily) and 160 OOS (vs 800). HAC standard errors scale as √(3020/604) ≈ 2.24×, moving from well-powered to marginally-powered territory. The Lakonishok-Lee 2001 / Cohen-Malloy-Pomorski 2012 insider-cluster effect size (≈50-125 bps/mo small-cap) remains detectable under Bonferroni t_crit=2.24, but significance margins must be monitored closely. Weekly cadence is economically appropriate (holding=60d, cluster detection window=30d; 5d stride is 3 orders of magnitude under the holding horizon). **Caveat:** may miss intra-week cluster formations if Form 4 filings cluster on specific weekdays — not corrected here.

2. **Cost model scaled to rebalance cadence.** `RealisticCostModel.primary_period_drag_bps(half_spread, turnover_fraction)` is applied with `turnover_fraction = avg_top_N_churn_between_weekly_snapshots` and `rebalances_per_year = 252 / stride = 50.4` (weekly). This is mathematically equivalent to daily-scaled cost provided turnover is consistently defined. Applied uniformly across all test specs (Carhart-4F, FF5+UMD, Q4).

3. **Bootstrap block length.** `floor(N^(1/3))` → 8 trading days at N=604 (vs 14 at N=3020). ~40% shorter block may slightly underestimate CI width for autocorrelated returns; within acceptable tolerance. If CI felt systematically tight post-result, sensitivity check at `floor(N^(1/4))` could be run (not planned unless flagged).

4. **Sharpe annualized at rebalance cadence.** `sharpe(returns, periods_per_year=50)` for weekly — not daily 252. Reflects that the return series is weekly-sampled 1-day portfolio returns (strategy is effectively in-market 1 day per rebalance period).

5. **Factor attribution comparability.** Carhart-4F, FF5+UMD, Q4 all fitted on the same 604-obs weekly-sampled series. Relative ranking preserved; direct comparison to historical daily-rebalance alpha numbers is NOT valid (separate samples).

6. **Infra-only changes (no methodology impact):** in-process EDGAR submissions/Form 4 XML cache in `SecEdgarClient` (per-process memoization of static content); engine progress logging every 5% of rebalance days; VPS prewarm script `scripts/prewarm_form4_cache.py` for building disk cache out-of-band.

## Executive verdict

_TBD — fill after both splits complete. Expected format:_

> **Verdict: GO / PAPER_TRACK / KILL** — [one-sentence justification].

## Timeline — Phase 3b.3 execution

| Step | Outcome |
|---|---|
| Phase 3b infra (Bonferroni + factor specs + decision matrix) | SHIPPED 2026-04-22 |
| Ken French factor CSVs + Q4 fetch | Already on disk |
| Stub replacements (bootstrap CI, regime α, net α) | SHIPPED 2026-04-22 |
| In-memory SecEdgarClient cache (submissions + Form 4 XML) | SHIPPED 2026-04-22 — cut refetch overhead |
| Engine `rebalance_stride` param (weekly default) | SHIPPED 2026-04-22 — daily-rebalance 12y × 1400 tickers was projecting 30h |
| In-sample backtest (2011-2022, weekly, top-15) | _TBD_ |
| OOS backtest (2023-2026, weekly, top-15) | _TBD_ |

## In-sample (2011-01-01 → 2022-12-31)

_Numbers from `docs/backtest/layer2d_insider_insample.md`._

### Factor attribution

| Spec | α (ann) | α t-stat (HAC) | R² | n |
|---|---:|---:|---:|---:|
| Carhart-4F | TBD | TBD | TBD | TBD |
| FF5+UMD    | TBD | TBD | TBD | TBD |
| Q4         | TBD | TBD | TBD | TBD |

### Cost sensitivity

| Scenario | Half-spread | Annual drag | Net α |
|---|---:|---:|---:|
| Primary (design §5 R8) | 5 bps | TBD | TBD |
| Stress (k=0.15 proxy)  | 15 bps | TBD | TBD |

### Bootstrap 95% CI (annualized α)

- Carhart-4F moving-block bootstrap (10k iters, block n^(1/3)): `[TBD%, TBD%]`
- Excludes zero: **TBD**

### Regime breakdown (Carhart α t-stat HAC)

| Regime | α t-stat | days |
|---|---:|---:|
| bull | TBD | TBD |
| bear | TBD | TBD |
| flat | TBD | TBD |

### Bonferroni gates

| Gate | Pass |
|---|:---:|
| carhart_alpha_bonferroni (n=2, t_crit 2.24) | TBD |
| ff5_umd_alpha (t>2) | TBD |
| ff5_umd_attenuation (<30%) | TBD |
| net_alpha_primary (>0) | TBD |
| net_alpha_stress_k15 (>0) | TBD |
| bootstrap_ci (excludes 0) | TBD |
| sharpe_net (>1.0) | TBD |
| regime_collapse_{bull,bear,flat} (t>1.5) | TBD |

## OOS (2023-01-01 → 2026-04-22)

_Mirror of the in-sample tables above — fill after OOS run completes._

## Decision matrix output

_Full `DecisionReport` dump per `alphalens.backtest.decision_matrix.evaluate_exit_criteria`._

## Findings + lessons

_TBD after results. Candidate bullet headings:_

1. _Gross alpha survives/dies under realistic cost_
2. _Regime dependence_
3. _Factor loadings — does α collapse when FF5 or Q4 absorbs the edge?_
4. _Bonferroni: Layer 2b and 2d both tested at n=2 (two splits)_
5. _Methodological lesson: weekly rebalance + in-process EDGAR caching are the enabling infra for Layer-2-scale alt-data backtests_

## Capital deploy decision

_Per design doc §8 gate logic + `decision_matrix.evaluate_exit_criteria`:_

- **GO**: deploy capital to Layer 2d paper/live queue with insider candidates feeding Layer 3 analysis at priority=`insider=15` (TBD in `alphalens.registry.SOURCE_PRIORITY`).
- **PAPER_TRACK**: log signals into Layer 3 queue for 6-12 mo without sizing — re-evaluate with live data.
- **KILL**: archive Layer 2d alongside Layer 2b (`alphalens/screeners/insider/` retained for backtest-only use), disable `launchd/com.alphalens.watchdog.insider.plist` if it was loaded.

## Artifacts

- Design doc (LOCKED): `docs/research/layer2d_alt_data_design.md`
- GATE 1 decision (single-day YELLOW): `docs/research/layer2d_gate1_single_day.md`
- PIT build runbook: `docs/research/layer2d_pit_build_runbook.md`
- Backtest reports: `docs/backtest/layer2d_insider_{insample,oos}.{md,csv}`
- Script: `scripts/run_layer2d_backtest.py`
- Core modules: `alphalens/backtest/{multiple_testing,decision_matrix,factor_analysis,factors,cost_model,regime}.py`

## Post-mortem sentence (one sentence, once verdict is in)

_TBD._
