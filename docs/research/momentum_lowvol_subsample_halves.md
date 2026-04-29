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
| IS 2015-2018 | 1.0 | $5M | 5bp | 15.0 | 24.7% | 0.20 | -0.03 | +13.5% | +11.0% | +10.4% | +0.39 | -0.04 |
| IS 2015-2018 | 1.0 | $5M | 15bp | 15.0 | 24.7% | 0.20 | -0.27 | +13.5% | +8.5% | +10.4% | +0.39 | -0.04 |
| IS 2015-2018 | 1.0 | $20M | 5bp | 15.0 | 19.3% | 0.15 | -0.07 | +7.7% | +5.8% | +5.9% | +0.27 | -0.01 |
| IS 2015-2018 | 1.0 | $20M | 15bp | 15.0 | 19.3% | 0.15 | -0.28 | +7.7% | +3.8% | +5.9% | +0.27 | -0.01 |
| OOS 2019-2022 | 1.0 | $5M | 5bp | 15.0 | 25.4% | -0.15 | -0.30 | -22.4% | -24.9% | -18.8% | -0.46 | 0.15 |
| OOS 2019-2022 | 1.0 | $5M | 15bp | 15.0 | 25.4% | -0.15 | -0.45 | -22.4% | -27.5% | -18.8% | -0.46 | 0.15 |
| OOS 2019-2022 | 1.0 | $20M | 5bp | 15.0 | 22.1% | 0.04 | -0.10 | -5.8% | -8.0% | -4.3% | -0.11 | 0.06 |
| OOS 2019-2022 | 1.0 | $20M | 15bp | 15.0 | 22.1% | 0.04 | -0.24 | -5.8% | -10.2% | -4.3% | -0.11 | 0.06 |

## Decision criteria

- **CANDIDATE**: OOS net Sharpe ≥ 0.4 AND OOS excess vs SPY ≥ 0%/y at ADV ≥ $5M with 5bps cost.
- **CLOSED**: OOS net excess vs SPY < 0 across all (vol_w, ADV) configurations at 5bps cost.
