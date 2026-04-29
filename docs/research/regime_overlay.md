# Regime overlay on mom+lowvol — SPY trailing-vol gate

**RESEARCH ONLY.** Hypothesis: mom+lowvol fails in 2017-2022 due to regime shift
(2018 Q4 drawdown, 2020 COVID crash, 2022 bear). A SPY 60d realized vol gate
could deploy strategy only in low-vol regimes, avoiding the painful periods.

- Strategy spec: vol_weight=1.0, ADV ≥ $5M, top-15
- Regime filter: SPY 60d realized vol < threshold → deploy; ≥ threshold → cash (return = 0)
- Rebalance stride: 5, cost: 5.0bp half-spread
- Vol thresholds tested: [0.12, 0.15, 0.18, 0.22, 0.3]

## Results

| Period | vol_thr | in regime | uncond Sh | uncond excess | cond Sh gross | cond Sh net | cond excess gross | cond excess net | α 4F cond | t cond |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS_2011_2016 | 0.12 | 44% | +0.77 | +28.0% | +0.88 | +0.74 | +19.0% | +17.9% | +33.0% | +2.39 |
| IS_2011_2016 | 0.15 | 77% | +0.77 | +28.0% | +1.00 | +0.80 | +31.4% | +29.6% | +44.9% | +2.47 |
| IS_2011_2016 | 0.18 | 84% | +0.77 | +28.0% | +0.84 | +0.62 | +25.4% | +23.4% | +38.4% | +1.99 |
| IS_2011_2016 | 0.22 | 90% | +0.77 | +28.0% | +0.72 | +0.50 | +22.2% | +20.0% | +35.7% | +1.75 |
| IS_2011_2016 | 0.30 | 97% | +0.77 | +28.0% | +0.70 | +0.47 | +21.9% | +19.6% | +34.8% | +1.63 |
| IS_2017_2022 | 0.12 | 40% | +0.16 | -11.3% | -0.43 | -0.58 | -36.7% | -37.7% | -14.5% | -1.28 |
| IS_2017_2022 | 0.15 | 51% | +0.16 | -11.3% | -0.54 | -0.70 | -45.0% | -46.3% | -22.0% | -1.61 |
| IS_2017_2022 | 0.18 | 68% | +0.16 | -11.3% | -0.16 | -0.34 | -30.0% | -31.7% | -6.5% | -0.43 |
| IS_2017_2022 | 0.22 | 80% | +0.16 | -11.3% | -0.19 | -0.38 | -32.8% | -34.9% | -10.3% | -0.56 |
| IS_2017_2022 | 0.30 | 95% | +0.16 | -11.3% | +0.06 | -0.15 | -19.0% | -21.5% | +3.9% | +0.20 |
| OOS_2023_2026 | 0.12 | 42% | +0.75 | +21.5% | +0.63 | +0.52 | +4.5% | +3.4% | +28.6% | +1.21 |
| OOS_2023_2026 | 0.15 | 71% | +0.75 | +21.5% | +0.69 | +0.54 | +16.7% | +14.9% | +44.8% | +1.62 |
| OOS_2023_2026 | 0.18 | 87% | +0.75 | +21.5% | +0.52 | +0.34 | +8.4% | +6.2% | +34.2% | +1.11 |
| OOS_2023_2026 | 0.22 | 89% | +0.75 | +21.5% | +0.54 | +0.36 | +10.1% | +7.8% | +35.3% | +1.15 |
| OOS_2023_2026 | 0.30 | 93% | +0.75 | +21.5% | +0.66 | +0.47 | +14.6% | +12.3% | +40.3% | +1.20 |

## Decision

Best regime overlay = (vol_threshold, ADV) combination where ALL THREE periods give positive net excess vs SPY. Compare to unconditional baseline (mom+lowvol $5M vol_w=1.0): IS_2011_2016 +28%, IS_2017_2022 -11%, OOS +19% (regime hole in 2017-2022).
