# Walk-Forward OOS Validation — Layer 2b

- **Total test windows**: 38
- **Window size**: 252 trading days
- **Step**: 21 trading days
- **Benchmark**: SPY
- **Top-N**: 5
- **Holding period**: 5 trading days
- **Full-range baseline Sharpe**: +1.418
- **Full-range baseline Carhart α t-stat HAC**: +2.53

Rolling 252-day test windows stepped monthly across the baseline
backtest span. All per-window metrics computed by slicing the
baseline's `rebalance_results` — the engine is deterministic for fixed
scorer/config, so sliced metrics match a per-window re-run at a
fraction of the wall time.

MomentumScorer has fixed equal weights (1/7 per metric), so this
is a **performance-stability** test of a deterministic scorer, not
a parameter-refit walk-forward.

## Per-window results

| test_start | test_end | regime | Sharpe gross | Sharpe mod | α_t HAC | IC_t | MaxDD | Turnover | CumRet |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2022-03-01 | 2023-03-01 | flat ↻ | +0.21 | +0.19 | +0.15 | +3.33 | -44.6% | 35% | -3.8% |
| 2022-03-30 | 2023-03-30 | flat ↻ | +0.71 | +0.69 | +0.80 | +4.48 | -44.6% | 34% | +27.2% |
| 2022-04-29 | 2023-05-01 | flat ↻ | +0.85 | +0.84 | +1.00 | +4.57 | -37.2% | 35% | +38.8% |
| 2022-05-31 | 2023-05-31 | flat ↻ | +1.13 | +1.12 | +1.56 | +3.70 | -36.1% | 37% | +65.0% |
| 2022-06-30 | 2023-06-30 | flat ↻ | +2.29 | +2.27 | +2.50 | +3.27 | -18.8% | 38% | +189.3% |
| 2022-08-01 | 2023-08-01 | flat ↻ | +2.36 | +2.35 | +2.38 | +4.79 | -18.8% | 39% | +227.4% |
| 2022-08-30 | 2023-08-30 | flat ↻ | +2.28 | +2.26 | +2.21 | +5.35 | -18.9% | 37% | +212.5% |
| 2022-09-29 | 2023-09-29 | flat ↻ | +2.12 | +2.11 | +1.98 | +6.31 | -22.9% | 36% | +183.7% |
| 2022-10-28 | 2023-10-30 | flat ↻ | +1.99 | +1.97 | +1.79 | +5.70 | -27.4% | 36% | +158.7% |
| 2022-11-29 | 2023-11-29 | flat ↻ | +1.87 | +1.85 | +1.75 | +4.40 | -30.7% | 35% | +148.1% |
| 2022-12-29 | 2023-12-29 | flat ↻ | +1.79 | +1.77 | +1.65 | +2.73 | -30.7% | 38% | +142.1% |
| 2023-01-31 | 2024-01-31 | flat ↻ | +1.77 | +1.75 | +1.63 | +4.07 | -30.7% | 38% | +138.1% |
| 2023-03-02 | 2024-03-01 | bull ↻ | +1.97 | +1.95 | +1.75 | +2.34 | -30.7% | 39% | +179.5% |
| 2023-03-31 | 2024-04-02 | bull ↻ | +1.23 | +1.21 | +1.05 | +2.16 | -31.6% | 39% | +72.0% |
| 2023-05-02 | 2024-05-01 | bull ↻ | +1.27 | +1.25 | +1.10 | +1.04 | -36.2% | 38% | +76.2% |
| 2023-06-01 | 2024-05-31 | bull ↻ | +0.90 | +0.88 | +0.76 | +2.01 | -36.2% | 38% | +41.9% |
| 2023-07-03 | 2024-07-02 | bull ↻ | +0.93 | +0.91 | +0.80 | +2.40 | -36.2% | 37% | +43.8% |
| 2023-08-02 | 2024-08-01 | bull ↻ | +0.05 | +0.03 | +0.02 | +1.49 | -36.2% | 38% | -10.2% |
| 2023-08-31 | 2024-08-30 | bull ↻ | +0.22 | +0.20 | +0.21 | +1.46 | -36.2% | 38% | -3.3% |
| 2023-10-02 | 2024-10-01 | bull ↻ | +0.38 | +0.36 | +0.23 | +0.79 | -36.2% | 40% | +5.5% |
| 2023-10-31 | 2024-10-30 | bull ↻ | +0.42 | +0.40 | +0.25 | -0.00 | -36.2% | 41% | +7.6% |
| 2023-11-30 | 2024-11-29 | bull | +1.52 | +1.51 | +1.33 | +1.48 | -36.2% | 42% | +132.4% |
| 2024-01-02 | 2024-12-31 | bull | +2.59 | +2.58 | +2.33 | +2.30 | -36.2% | 42% | +549.7% |
| 2024-02-01 | 2025-02-03 | bull | +1.99 | +1.98 | +1.75 | +1.85 | -39.8% | 42% | +309.9% |
| 2024-03-04 | 2025-03-05 | flat | +1.85 | +1.84 | +1.66 | +3.04 | -39.8% | 42% | +258.5% |
| 2024-04-03 | 2025-04-03 | flat ↻ | +1.78 | +1.76 | +1.61 | +2.81 | -47.8% | 41% | +233.9% |
| 2024-05-02 | 2025-05-05 | flat ↻ | +1.81 | +1.80 | +1.55 | +1.98 | -49.9% | 41% | +241.1% |
| 2024-06-03 | 2025-06-04 | flat ↻ | +2.00 | +1.99 | +1.75 | +0.81 | -49.9% | 40% | +316.8% |
| 2024-07-03 | 2025-07-07 | flat ↻ | +1.85 | +1.84 | +1.60 | -0.11 | -49.9% | 40% | +265.2% |
| 2024-08-02 | 2025-08-05 | flat ↻ | +2.08 | +2.07 | +1.79 | +0.20 | -49.9% | 40% | +346.8% |
| 2024-09-03 | 2025-09-04 | bull ↻ | +2.06 | +2.04 | +1.78 | -0.05 | -49.9% | 42% | +326.0% |
| 2024-10-02 | 2025-10-03 | bull ↻ | +2.53 | +2.52 | +2.22 | +0.18 | -49.9% | 42% | +546.3% |
| 2024-10-31 | 2025-11-03 | bull ↻ | +2.52 | +2.51 | +2.23 | +0.80 | -49.9% | 44% | +553.3% |
| 2024-12-02 | 2025-12-03 | bull ↻ | +1.86 | +1.85 | +1.65 | +0.08 | -49.9% | 43% | +226.6% |
| 2025-01-02 | 2026-01-05 | bull ↻ | +0.90 | +0.88 | +0.70 | -0.05 | -49.4% | 42% | +44.7% |
| 2025-02-04 | 2026-02-04 | bull ↻ | +1.55 | +1.53 | +1.48 | +1.32 | -40.7% | 44% | +108.4% |
| 2025-03-06 | 2026-03-06 | bull ↻ | +1.39 | +1.37 | +1.54 | +0.48 | -26.1% | 45% | +91.8% |
| 2025-04-04 | 2026-04-07 | bull ↻ | +1.95 | +1.94 | +2.28 | +0.70 | -27.3% | 45% | +174.3% |

## Distribution summary

| Metric | min | Q25 | median | Q75 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sharpe gross | +0.05 | +0.98 | +1.80 | +2.00 | +2.59 |
| Carhart α_t HAC | +0.02 | +1.01 | +1.62 | +1.79 | +2.50 |

- Windows with Sharpe > 0.5: **87%**
- Windows with Sharpe > 1.0: **74%**
- Windows with Carhart α_t > 1.5: **63%**
- Windows with Carhart α_t > 2.0: **18%**
- Windows with IC t-stat > 1.5: **55%**

## Stability block

- **21-day block-return autocorr (lag-1)**: +0.161
  - Computed on non-overlapping 21-day blocks (~59 independent observations).
  - Low value (< 0.5) = strategy returns do not cluster into regimes.

- **Longest contiguous negative-Sharpe stretch**: 0 windows
- **Momentum-crash coincidence**: 0% of negative-Sharpe windows overlap a benchmark regime reversal
- **Turnover**: max 45%, Q95 44%

## Decision gate

- **C1**: PASS — fraction windows Sharpe>0.5 = 86.84% (threshold ≥ 70%)
- **C2**: PASS — fraction windows Carhart α_t>1.5 = 63.16% (threshold ≥ 50%)
- **C3**: PASS — 21-day block-return autocorr lag-1 = 0.161 (threshold < 0.5)
- **C4**: PASS — longest negative-Sharpe stretch = 0 windows (threshold < 12)
- **C5**: PASS — max per-window turnover = 45.50% (threshold < 100%)

**Overall: PASS**

**Interpretation.** The strategy is stable across OOS rolling windows. 87% of windows have Sharpe > 0.5, and 63% have Carhart α t-stat > 1.5 HAC. Block-return autocorr +0.161 indicates returns do not cluster into one regime. No dark half (0 consecutive negative windows, below the 12-window threshold). The headline Sharpe +1.42 is not a single-regime artifact.

## Limitations

- **Window-size cherry picking**: 252 trading days is conventional but
  arbitrary. Re-running with `--window-days 126` or `378` is the
  robustness check; not automated in this report.
- **Low statistical power**: with ~25-37 windows, distribution
  quantiles have wide confidence intervals. Descriptive, not inferential.
- **Autocorr choice**: explicit rejection of windowed-Sharpe autocorr
  (92% overlap = mechanically near 1; every-12th subsample = n≈4).
  Block-return autocorr over non-overlapping 21-day blocks is the
  defensible alternative. Gate C3 uses this.
- **Path-independence invariant**: Max DD and cumulative return per
  window are recomputed from a fresh cumprod starting at 1 for the
  slice's first day — never inherited from the global equity curve's
  high-water mark.
- **Training window nominal only**: MomentumScorer has fixed weights;
  reported metrics are test-window only. This is a performance-
  stability test, not a classic parameter-refit walk-forward.
- **Regime majority-label collapse**: a window straddling a regime
  transition gets one label (the majority). `regime_reversed_within`
  flags those windows for the momentum-crash indicator.
