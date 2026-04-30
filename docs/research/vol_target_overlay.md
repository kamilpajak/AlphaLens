# Vol-target overlay on mom+lowvol BASE — Moreira-Muir 2017

**RESEARCH ONLY.** Pre-registered hypothesis `vol_target_mom_lowvol_2026_04_30` (signal class `risk_management_overlay_2026_04_30`, Bonferroni n=1, |t|≥1.96).

- BASE: mom+lowvol combo (vol_weight=1.0), top-15, stride 5
- Vol target: 0.10 ann, lookback 5 rebalances, max_leverage 1.5
- Cost model: dynamic per-rebalance (`turnover_t = base_turnover · scale_t + |scale_t − scale_{t-1}|`)

## Results — gross / net (vol-targeted, dynamic cost)

| Period | ADV | cost | n | scale (mean / min / max) | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | β_MOM |
|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| IS 2011-2022 | $5M | 5bp | 604 | 1.07 / 0.18 / 1.50 | 0.34 | 0.26 | +2.7% | -2.3% | +23.0% | +1.32 | -0.10 |
| OOS 2023-2026 | $5M | 5bp | 154 | 0.93 / 0.32 / 1.50 | 0.67 | 0.59 | +11.8% | +7.2% | +36.6% | +1.23 | -0.07 |
