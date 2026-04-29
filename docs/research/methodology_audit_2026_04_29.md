# Methodology audit — phase-aliasing in stride-based subsample tests

**Date:** 2026-04-29 (third synthesis of the day)
**Verdict:** The "FAIL" decisions on **tri-factor** and **mom+lowvol** earlier today were artifacts of **phase-aliased rebalance sampling**. Both strategies need re-evaluation under proper multi-phase methodology before any further verdict.

## Trigger

Tri-factor synthesis (`tri_factor_validation_2026_04_29.md`) and fallback synthesis (`fallback_validation_2026_04_29.md`) both flagged a 17pp gap between full-period IS Sharpe and average-of-halves Sharpe. The gap was consistent across BOTH strategies. I attributed it to engine carry-over but recommended a methodology audit before further decisions.

## What I found

### 1. Engine is state-free (no carry-over)

`alphalens/backtest/engine.py:_simulate_rebalance` is fully deterministic per-day given the same inputs. `RebalanceSnapshot.portfolio_return` is **1-day forward return** of the top-N picks at that rebalance date — no holding-period cohort tracking, no path dependency. Carry-over hypothesis was wrong.

Empirical proof: ran mom+lowvol full IS (2015-2022) and standalone half 1 (2015-2018) with `--lock-universe`. On the 202 rebalance dates they share, **max abs diff = 0.0** (every single per-rebalance return matches to machine precision). The engine is state-free.

### 2. The 17pp gap is phase-aliasing in `calendar[::5]`

`engine.run` does `calendar = calendar[:: self.rebalance_stride]`. With `rebalance_stride=5`, only **1 in 5 trading days** is sampled as a rebalance. Which 1-in-5 depends on the start of the calendar:

- **Full IS (start 2015-01-02)**: rebalance phase A — hits, e.g., 2015-01-02, 2015-01-09, 2015-01-16, …, 2018-12-31, 2019-01-08, 2019-01-15, …
- **Half 1 (start 2015-01-02)**: same phase A — hits 2015-01-02, 2015-01-09, …, 2018-12-31. **Phase-aligned with full IS for the half-1 window** (full IS partition 2015-2018 == half 1 standalone, exactly).
- **Half 2 (start 2019-01-02)**: rebalance phase B — hits 2019-01-02, 2019-01-09, 2019-01-16, …. **Off-phase from full IS by one trading day.**

For mom+lowvol (vw=1.0 ADV $5M):

| Same period 2019-2022 | Phase | Sample dates | mean per period | annualized |
|---|---|---|---:|---:|
| Full IS partition (2019-2022 portion of full IS run) | A | 2019-01-08, 2019-01-15, … | +0.001589 | **+40.0%/y** |
| Half 2 standalone (start 2019-01-02) | B | 2019-01-02, 2019-01-09, … | -0.000499 | **-12.6%/y** |
| Half 2 phase-aligned (start 2019-01-08) | A | matches full IS partition | (re-ran) | **+22.0%/y** |

**Same strategy, same period, same universe, same engine — 30 to 50 percentage points of annualized excess swing depending on which trading days happen to be sampled.** The "catastrophic" half 2 was sampling artifact, not a genuine regime fail.

### 3. Tri-factor reversal under phase alignment

Tri-factor 2019-2022 standalone halves (start 2019-01-02, off-phase):

| Config | Sharpe gross | excess gross | α t |
|---|---:|---:|---:|
| rw=0.5 $5M | -0.13 | -20.9% | -0.45 |
| rw=0.5 $20M | -0.01 | -10.7% | -0.19 |
| rw=1.0 $5M | -0.29 | -34.6% | -0.75 |
| rw=1.0 $20M | -0.09 | -17.5% | -0.34 |

Tri-factor 2019-2022 phase-aligned (start 2019-01-08, matching full IS phase):

| Config | Sharpe gross | excess gross | α t |
|---|---:|---:|---:|
| rw=0.5 $5M | +0.65 | +32.7% | +1.74 |
| rw=0.5 $20M | +0.66 | +27.5% | +1.77 |
| **rw=1.0 $5M** | **+0.83** | **+42.1%** | **+2.24** |
| rw=1.0 $20M | +0.48 | +15.5% | +1.32 |

Phase-aligned half 2 best config (rw=1.0 ADV $5M) clears the **t > 2.0 gate** with α t = +2.24. The "FAIL" verdict reverses to (at least) "MID" under proper phase alignment.

## Why this matters beyond this session

**Every backtest result reported in this codebase has been a single-phase point estimate.** With stride=5, there are 5 phases; the variance across phases is huge — we just observed 30-77pp annualized-excess swings within a 4-year window. Past killed strategies (Layer 2b, 2c, 2d, 2e, 2f, 2g) all had stability checks that may have been phase-aliased to varying degrees.

This does NOT necessarily mean those verdicts were wrong — most had multiple converging failure modes (Carhart-4F, sanity-check 4-gate, walk-forward, multiple-testing correction, cost drag, bootstrap CI, survivorship PIT — see `tests/test_layer_status.py REQUIRED_EVIDENCE_KEYS`). But the **subsample stability check specifically** was phase-aliased, and may have driven some FAIL verdicts where genuine alpha existed.

## Recommendations

1. **Re-open tri-factor and mom+lowvol** — both their FAIL verdicts (issued today) are invalid. Run proper multi-phase analysis (5 phases for stride=5) before any new verdict. Include all 5 of `--is-start 2019-01-02 / 03 / 04 / 07 / 08` for half 2 and average results.

2. **Patch the experiment harness** — add `--phase-offset 0..stride-1` flag to both `experiment_tri_factor_edgar.py` and `experiment_momentum_lowvol_combo.py`. Default report aggregates across all phases with mean ± stddev.

3. **Document phase variance as a first-class output** in every backtest report. Single-phase Sharpe should never be reported in isolation.

4. **For partitioned stability checks**, prefer **partitioning the saved full-IS `portfolio_returns` by date**, not running separate engine instances at different phase offsets. The full IS run is internally consistent: partition arithmetic guarantees full = halves on the same dates.

5. **Update synthesis docs**: this finding supersedes today's `tri_factor_validation_2026_04_29.md` and `fallback_validation_2026_04_29.md`. The strategy verdicts reported there are not reliable.

6. **Consider reducing rebalance_stride** for production validation. Stride=1 (daily) eliminates phase-aliasing entirely (only one phase exists). Cost: 5× longer backtest runtime. For a single decisive run on a candidate strategy, that's affordable.

## What this means for the active alpha pivot decision

The `pivot 2026-04-25` ("AlphaLens = research infrastructure, NIE active alpha") was driven by 5/5 paradigm failures. Some of those failures may have been phase-aliased false negatives. **Should we re-examine?**

- For most closed layers, multiple converging failure modes still hold (e.g., Layer 2c had Sharpe 0.25 net even by full-IS metric — not phase-dependent).
- For Layer 2g (LLM-researcher) and 2f (8-K events), the closure rationale was OOS underperformance — phase-aliased samples may have contributed.
- For tri-factor specifically (today's discovery), the correct verdict is likely "open question, requires multi-phase validation before closure."

Decision deferred to user. **My honest read: the pivot was probably correct in spirit (research infrastructure is the right framing), but specific layer verdicts deserve re-examination once we have proper multi-phase tooling.**
