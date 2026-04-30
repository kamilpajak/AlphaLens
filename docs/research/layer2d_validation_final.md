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

> **Verdict: KILL.** Layer 2d insider-cluster screener exhibits classic overfit pattern — Carhart-4F α collapses from 103.48%/y (t=2.14 in-sample) to 21.56%/y (t=0.68 OOS). Bootstrap 95% CI OOS `[-45.34%, +84.84%]` includes zero. Signal does not generalize 2023-2026. Same failure mode as Layer 2b momentum scorer (#18 post-mortem, train t=2.60 → OOS t=0.82). Daily-stride rerun cannot save it — even with √5 ≈ 2.24× tighter SE, Carhart OOS t ≈ 1.52 stays below 2.24 Bonferroni threshold.

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

## In-sample (2011-01-01 → 2022-12-31, weekly stride=5, N=603)

_Backtest runtime 21h, completed 2026-04-23 16:25. Full report: `docs/backtest/layer2d_insider_insample.md`._

### Factor attribution

| Spec | α (ann) | α t-stat (HAC) | R² | n |
|---|---:|---:|---:|---:|
| Carhart-4F | **103.48%** | **2.14** | 0.005 | 603 |
| FF5+UMD    | 100.09% | 2.19 | 0.012 | 603 |
| Q4         | 100.15% | 2.16 | 0.009 | 603 |

### Cost sensitivity

| Scenario | Half-spread | Turnover/rebal | Annual drag | Net α |
|---|---:|---:|---:|---:|
| Primary | 5 bps | 27.9% | 2.81% | **100.66%** |
| Stress (k=0.15 proxy) | 15 bps | 27.9% | 5.63% | 97.85% |

### Bootstrap 95% CI (annualized α)

- Carhart-4F moving-block bootstrap (10k iters, block n^(1/3)=8): **[26.30%, 214.87%]**
- Excludes zero: **True**

### Regime breakdown (Carhart α t-stat HAC)

| Regime | α t-stat | days | α ann |
|---|---:|---:|---:|
| bull | 2.51 | 221 | 63.24% |
| bear | 1.47 | 72 | 441.37% |
| flat | 1.67 | 310 | 63.34% |

### Bonferroni gates

| Gate | Pass |
|---|:---:|
| carhart_alpha_bonferroni (t>2.24) | ✗ (t=2.14) |
| ff5_umd_alpha (t>2) | ✓ |
| ff5_umd_attenuation (<30%) | ✓ |
| net_alpha_primary (>0) | ✓ |
| net_alpha_stress_k15 (>0) | ✓ |
| bootstrap_ci (excludes 0) | ✓ |
| sharpe_net (>1.0) | ✗ (0.95) |
| regime_collapse_bull (t>1.5) | ✓ |
| regime_collapse_bear (t>1.5) | ✗ (t=1.47) |
| regime_collapse_flat (t>1.5) | ✓ |

### Verdict in-sample: **PAPER_TRACK**

3 marginalne misses, wszystkie w tolerancji weekly-sampling underpower (SE ×2.24 per R10 caveat):
- Carhart t=2.14 vs 2.24 required (Δ=0.10)
- Sharpe 0.95 vs 1.0 required (Δ=0.05)
- Bear regime t=1.47 vs 1.5 required (Δ=0.03)

Daily rebalance (stride=1, N=3020) może podnieść wszystkie 3 nad progi bez zmiany signal efektów size. Pending OOS + daily rerun po VPS prewarm.

## OOS (2023-01-01 → 2026-04-22, weekly stride=5, N=154)

_Backtest runtime 10h52m, completed 2026-04-24 03:35. Full report: `docs/backtest/layer2d_insider_oos.md`._

### Factor attribution

| Spec | α (ann) | α t-stat (HAC) | R² | n |
|---|---:|---:|---:|---:|
| Carhart-4F | 21.56% | **0.68** | 0.008 | 154 |
| FF5+UMD    | 25.95% | 0.82 | 0.043 | 154 |
| Q4         | 41.46% | 0.97 | 0.052 | 101 |

### Cost sensitivity

| Scenario | Half-spread | Turnover/rebal | Annual drag | Net α |
|---|---:|---:|---:|---:|
| Primary | 5 bps | 30.5% | 3.07% | 18.48% |
| Stress (k=0.15 proxy) | 15 bps | 30.5% | 6.15% | 15.41% |

### Bootstrap 95% CI (annualized α)

- Carhart-4F moving-block bootstrap (10k iters, block n^(1/3)=5): **[-45.34%, +84.84%]**
- Excludes zero: **False**

### Regime breakdown (Carhart α t-stat HAC)

| Regime | α t-stat |
|---|---:|
| bull | 1.24 |
| bear | 0.00 |
| flat | -0.44 |

### Bonferroni gates

| Gate | Pass |
|---|:---:|
| carhart_alpha_bonferroni (t>2.24) | ✗ (t=0.68) |
| ff5_umd_alpha (t>2) | ✗ (t=0.82) |
| ff5_umd_attenuation (<30%) | ✓ |
| net_alpha_primary (>0) | ✓ |
| net_alpha_stress_k15 (>0) | ✓ |
| bootstrap_ci (excludes 0) | ✗ |
| sharpe_net (>1.0) | ✗ (0.42) |
| regime_collapse_bull (t>1.5) | ✗ (1.24) |
| regime_collapse_bear (t>1.5) | ✗ (0.00) |
| regime_collapse_flat (t>1.5) | ✗ (-0.44) |

### Verdict OOS: **KILL** (7/10 gates fail)

## Combined decision

| Split | Carhart α t | Verdict |
|---|---:|---|
| In-sample | 2.14 | PAPER_TRACK (marginal) |
| OOS | **0.68** | KILL |

Per design doc §8 Bonferroni n=2: both splits must pass independently. OOS collapse is catastrophic — Carhart t dropped 3.14×, far beyond what weekly-underpower caveat explains. Signal classified as **overfit noise, not tradeable alpha**.

## Findings + lessons

1. **Classic overfit pattern, same as Layer 2b momentum scorer.** Train α t=2.14 / OOS α t=0.68 = 3.1× attenuation. Layer 2b saw 2.60 → 0.82 (3.2× attenuation). Similar signature: small-cap concentrated strategy on curated universe.

2. **Factor attribution unanimous OOS rejection.** Carhart-4F, FF5+UMD, Q4 — all three t-stats are below 1.0 OOS. Not a factor-specification issue; signal genuinely absent in 2023-2026.

3. **Regime collapse across all three regimes.** In-sample bull 2.51 / bear 1.47 / flat 1.67 → OOS bull 1.24 / bear 0.00 / flat -0.44. Bear regime actually negative. Signal did NOT generalize in any market condition.

4. **Daily rebalance cannot save it.** Weekly N=154 OOS gives SE that is √5 ≈ 2.24× larger than daily N=800 would give. Even with ideal tightening, Carhart t OOS ≈ 0.68 × 2.24 = 1.52 — still below Bonferroni 2.24 threshold and even below regime 1.5 threshold. Running daily would confirm KILL but not change it.

5. **Infra investment was not wasted.** `scripts/prewarm_form4_cache.py`, `SecEdgarClient` in-process cache, `BacktestEngine.rebalance_stride`, 5xx retry logic, autocorr-adjusted Sharpe, block-bootstrap CI — all are screener-agnostic and will reuse for any next Layer 2 candidate (Layer 3 classifier, short-interest, options flow).

6. **Methodological discipline paid off.** Perplexity R10/R11 caveats (weekly underpower, autocorrelation adjustment) were correctly identified as non-determinative. The kill signal came from OOS factor attribution + bootstrap CI, not from any scaling subtlety.

## Capital deploy decision

**KILL.** Archive Layer 2d screener:

- `alphalens/archive/screeners/insider/` **retain for backtest-only reference** (no registry removal — same pattern as Layer 2c lean).
- `launchd/com.alphalens.watchdog.insider.plist` — **never loaded** (we did not deploy; no systemd/launchctl action needed).
- No Layer 3 queue integration. `SOURCE_PRIORITY["insider"]` was never registered; leave as-is.
- Memory notes updated: `project_pivot_alt_data.md` marked CLOSED; Layer 2d appended to `project_archive_decisions.md`.

Next candidate (open): **Layer 3 rejection-prediction classifier** per Layer 2b post-mortem §Pivot direction Option B — reuse 6 months of Layer 3 BUY/HOLD/SELL decisions as panel data, train classifier on pre-decision features. Alternative: short-interest or options-flow alt data.

## Post-mortem sentence

**Layer 2d zjadło 3 dni solo-dev + 31h wall-clock backtest (21h in-sample + 11h OOS na weekly stride) żeby potwierdzić że SEC Form 4 cluster buys nie dają OOS alfy — Carhart t=2.14 collapsed do 0.68, bootstrap CI otwiera na zero. Infra (prewarm script, EDGAR cache, rebalance_stride, 5xx retry, autocorr Sharpe) jest reusable dla kolejnej alt-data hipotezy; signal jest nie.**

## Decision matrix output

_Full `DecisionReport` dump per `alphalens.attribution.decision_matrix.evaluate_exit_criteria`._

## Findings + lessons

_TBD after results. Candidate bullet headings:_

1. _Gross alpha survives/dies under realistic cost_
2. _Regime dependence_
3. _Factor loadings — does α collapse when FF5 or Q4 absorbs the edge?_
4. _Bonferroni: Layer 2b and 2d both tested at n=2 (two splits)_
5. _Methodological lesson: weekly rebalance + in-process EDGAR caching are the enabling infra for Layer-2-scale alt-data backtests_

## Capital deploy decision

_Per design doc §8 gate logic + `decision_matrix.evaluate_exit_criteria`:_

- **GO**: deploy capital to Layer 2d paper/live queue with insider candidates feeding Layer 3 analysis at priority=`insider=15` (TBD in `alphalens.core.registry.SOURCE_PRIORITY`).
- **PAPER_TRACK**: log signals into Layer 3 queue for 6-12 mo without sizing — re-evaluate with live data.
- **KILL**: archive Layer 2d alongside Layer 2b (`alphalens/archive/screeners/insider/` retained for backtest-only use), disable `launchd/com.alphalens.watchdog.insider.plist` if it was loaded.

## Artifacts

- Design doc (LOCKED): `docs/research/layer2d_alt_data_design.md`
- GATE 1 decision (single-day YELLOW): `docs/research/layer2d_gate1_single_day.md`
- PIT build runbook: `docs/research/layer2d_pit_build_runbook.md`
- Backtest reports: `docs/backtest/layer2d_insider_{insample,oos}.{md,csv}`
- Script: `scripts/run_layer2d_backtest.py`
- Core modules: `alphalens/backtest/{multiple_testing,decision_matrix,factor_analysis,factors,cost_model,regime}.py`

## Post-mortem sentence (one sentence, once verdict is in)

_TBD._
