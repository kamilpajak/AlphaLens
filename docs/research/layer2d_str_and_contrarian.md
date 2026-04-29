# Layer 2d Experiment — STR factor decomposition + pure-contrarian comparison

**RESEARCH ONLY.** Tests two hypotheses simultaneously:

- H1 (factor): Layer 2d α loads on uncontrolled short-term reversal premium. Adding STR (Jegadeesh 1990, 21d formation) as 5th factor should attenuate α.
- H2 (signal): a pure-contrarian scorer (-60d_return + 0.5 × 5d_return) reproduces Layer 2d α without insider data.

- Top-N: 15
- Holding period (signal-only): 60d
- Rebalance stride: 5
- Pure contrarian bounce weight: 0.5
- Universe: per-subperiod PIT union (full=2011-2026 has 1627 tickers; subperiods filter to contemporaneous snapshots)
- Factor specs: Carhart-4F (Mkt-RF, SMB, HML, Mom) and Carhart-4F + STR

## Full IS 2011-2022

| Strategy | N | Sharpe | Carhart-4F α | t (4F) | R² (4F) | +STR α | t (5F) | R² (5F) | β_STR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0_count | 603 | 0.96 | 103.53% | 2.14 | 0.005 | 89.77% | 2.10 | 0.015 | 0.24 |
| pure_contrarian | 604 | 0.68 | 101.39% | 2.42 | 0.005 | 96.29% | 2.38 | 0.006 | 0.09 |
| cluster_contrarian | 603 | 0.47 | 34.02% | 1.53 | 0.015 | 36.08% | 1.58 | 0.016 | -0.04 |

## IS 2011-2016

| Strategy | N | Sharpe | Carhart-4F α | t (4F) | R² (4F) | +STR α | t (5F) | R² (5F) | β_STR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0_count | 299 | 0.70 | 44.13% | 1.63 | 0.022 | 38.46% | 1.36 | 0.024 | 0.07 |
| pure_contrarian | 302 | 0.51 | 71.91% | 1.22 | 0.001 | 12.24% | 0.25 | 0.029 | 0.69 |
| cluster_contrarian | 296 | 0.30 | 23.39% | 0.53 | 0.025 | 11.78% | 0.27 | 0.028 | 0.14 |

## IS 2017-2022

| Strategy | N | Sharpe | Carhart-4F α | t (4F) | R² (4F) | +STR α | t (5F) | R² (5F) | β_STR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0_count | 301 | 0.70 | 42.94% | 1.60 | 0.054 | 39.93% | 1.65 | 0.060 | 0.10 |
| pure_contrarian | 302 | 0.86 | 112.86% | 2.37 | 0.014 | 114.92% | 2.39 | 0.014 | -0.07 |
| cluster_contrarian | 301 | 0.61 | 47.69% | 1.69 | 0.028 | 51.20% | 1.74 | 0.034 | -0.12 |

## OOS 2023-2026

| Strategy | N | Sharpe | Carhart-4F α | t (4F) | R² (4F) | +STR α | t (5F) | R² (5F) | β_STR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0_count | 154 | 0.42 | 21.56% | 0.68 | 0.008 | 21.17% | 0.67 | 0.009 | -0.02 |
| pure_contrarian | 154 | 0.95 | 150.17% | 1.34 | 0.021 | 148.53% | 1.30 | 0.021 | -0.10 |
| cluster_contrarian | 154 | 0.45 | 23.01% | 0.61 | 0.004 | 21.24% | 0.57 | 0.007 | -0.10 |

## STR-attenuation summary (does adding STR explain Layer 2d α?)

| Period | Strategy | α_4F | α_5F (with STR) | Δα (pp) | Δt (4F→5F) | β_STR |
|---|---|---:|---:|---:|---:|---:|
| Full IS 2011-2022 | V0_count | 103.53% | 89.77% | +13.76pp | +0.04 | 0.24 |
| Full IS 2011-2022 | pure_contrarian | 101.39% | 96.29% | +5.10pp | +0.04 | 0.09 |
| Full IS 2011-2022 | cluster_contrarian | 34.02% | 36.08% | -2.06pp | -0.05 | -0.04 |
| IS 2011-2016 | V0_count | 44.13% | 38.46% | +5.67pp | +0.27 | 0.07 |
| IS 2011-2016 | pure_contrarian | 71.91% | 12.24% | +59.67pp | +0.96 | 0.69 |
| IS 2011-2016 | cluster_contrarian | 23.39% | 11.78% | +11.60pp | +0.26 | 0.14 |
| IS 2017-2022 | V0_count | 42.94% | 39.93% | +3.02pp | -0.05 | 0.10 |
| IS 2017-2022 | pure_contrarian | 112.86% | 114.92% | -2.06pp | -0.03 | -0.07 |
| IS 2017-2022 | cluster_contrarian | 47.69% | 51.20% | -3.50pp | -0.05 | -0.12 |
| OOS 2023-2026 | V0_count | 21.56% | 21.17% | +0.39pp | +0.01 | -0.02 |
| OOS 2023-2026 | pure_contrarian | 150.17% | 148.53% | +1.64pp | +0.04 | -0.10 |
| OOS 2023-2026 | cluster_contrarian | 23.01% | 21.24% | +1.77pp | +0.04 | -0.10 |

## Interpretation guide

- **β_STR significantly positive (e.g. > 0.2)**: portfolio loads on STR; some of the 4F α was reversal residual.
- **Δα > 50% of α_4F** when STR added: STR explains majority of the Carhart-4F residual.
- **V0_count and pure_contrarian have similar α_4F**: insider data is a redundant proxy for the contrarian set.
