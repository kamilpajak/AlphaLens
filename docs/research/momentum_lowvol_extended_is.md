# Momentum + low-vol combo — Asness-Frazzini-Pedersen quality proxy

**RESEARCH ONLY.** Pure 12-1m momentum failed catastrophically OOS
(`momentum_constrained.md`: -47% to -89% excess vs SPY at all ADV thresholds).
Hypothesis: low-vol filter (Frazzini-Pedersen 2014 BAB / Asness et al QMJ) hedges
against momentum crashes by filtering out high-vol "junk momentum" names that
collapse hardest in regime shifts.

- Score: z(mom_12_1m) − vol_weight × z(vol_60d) (per-rebalance cross-sectional z)
- Top-N: 15, holding-signal: 60d, stride: 5
- Vol weights tested: [1.0] (0.5 = mostly momentum, 2.0 = mostly low-vol)
- ADV thresholds: ['$5M', '$20M']

## Results

| Period | vol_w | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | β_MOM |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS 2015-2022 | 1.0 | $5M | 5bp | 15.0 | 25.5% | 0.42 | 0.21 | +18.7% | +16.1% | +27.8% | +1.37 | -0.02 |
| IS 2015-2022 | 1.0 | $5M | 15bp | 15.0 | 25.5% | 0.42 | -0.01 | +18.7% | +13.6% | +27.8% | +1.37 | -0.02 |
| IS 2015-2022 | 1.0 | $20M | 5bp | 15.0 | 20.2% | 0.44 | 0.26 | +17.2% | +15.2% | +26.7% | +1.39 | -0.04 |
| IS 2015-2022 | 1.0 | $20M | 15bp | 15.0 | 20.2% | 0.44 | 0.08 | +17.2% | +13.1% | +26.7% | +1.39 | -0.04 |
| OOS 2023-2026 | 1.0 | $5M | 5bp | 15.0 | 25.6% | 0.75 | 0.55 | +21.5% | +18.9% | +48.1% | +1.45 | -0.03 |
| OOS 2023-2026 | 1.0 | $5M | 15bp | 15.0 | 25.6% | 0.75 | 0.35 | +21.5% | +16.3% | +48.1% | +1.45 | -0.03 |
| OOS 2023-2026 | 1.0 | $20M | 5bp | 15.0 | 23.7% | 0.74 | 0.56 | +21.8% | +19.4% | +46.6% | +1.37 | -0.00 |
| OOS 2023-2026 | 1.0 | $20M | 15bp | 15.0 | 23.7% | 0.74 | 0.37 | +21.8% | +17.0% | +46.6% | +1.37 | -0.00 |

## Decision criteria

- **CANDIDATE**: OOS net Sharpe ≥ 0.4 AND OOS excess vs SPY ≥ 0%/y at ADV ≥ $5M with 5bps cost.
- **CLOSED**: OOS net excess vs SPY < 0 across all (vol_w, ADV) configurations at 5bps cost.
