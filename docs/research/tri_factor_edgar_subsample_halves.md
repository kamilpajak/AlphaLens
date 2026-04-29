# Tri-factor combo (mom + lowvol + ROE) — EDGAR-backed 2015-2022

**RESEARCH ONLY.** Re-validation of `tri_factor_combo.md` (2026-04-29) on an
extended 4-year IS using SEC EDGAR
companyfacts for the ROE component (SimFin's 2020-Q2 floor previously locked
the IS to 2 years).

- Score: z(mom_12_1m) − vol_w × z(vol_60d) + roe_w × z(roe_ttm)
- Top-N: 15, holding-signal: 60d, stride: 5
- Vol weight: 1.0 (fixed)
- ROE weights tested: [0.5, 1.0]
- ADV thresholds: ['$5M', '$20M']

## Results

| Period | roe_w | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | R² | β_MOM |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS 2015-2018 | 0.5 | $5M | 5bp | 15.0 | 21.7% | 0.14 | -0.08 | +9.8% | +7.6% | +5.8% | +0.23 | 0.008 | -0.16 |
| IS 2015-2018 | 0.5 | $5M | 15bp | 15.0 | 21.7% | 0.14 | -0.29 | +9.8% | +5.4% | +5.8% | +0.23 | 0.008 | -0.16 |
| IS 2015-2018 | 0.5 | $20M | 5bp | 15.0 | 16.4% | 0.23 | 0.05 | +12.1% | +10.5% | +8.7% | +0.40 | 0.006 | -0.07 |
| IS 2015-2018 | 0.5 | $20M | 15bp | 15.0 | 16.4% | 0.23 | -0.13 | +12.1% | +8.8% | +8.7% | +0.40 | 0.006 | -0.07 |
| IS 2015-2018 | 1.0 | $5M | 5bp | 15.0 | 16.7% | -0.04 | -0.22 | +0.8% | -0.9% | -3.4% | -0.14 | 0.008 | -0.15 |
| IS 2015-2018 | 1.0 | $5M | 15bp | 15.0 | 16.7% | -0.04 | -0.39 | +0.8% | -2.6% | -3.4% | -0.14 | 0.008 | -0.15 |
| IS 2015-2018 | 1.0 | $20M | 5bp | 15.0 | 14.1% | 0.19 | 0.03 | +10.0% | +8.5% | +6.4% | +0.31 | 0.004 | -0.07 |
| IS 2015-2018 | 1.0 | $20M | 15bp | 15.0 | 14.1% | 0.19 | -0.14 | +10.0% | +7.1% | +6.4% | +0.31 | 0.004 | -0.07 |
| OOS 2019-2022 | 0.5 | $5M | 5bp | 15.0 | 26.1% | -0.13 | -0.29 | -20.9% | -23.5% | -18.7% | -0.45 | 0.068 | 0.16 |
| OOS 2019-2022 | 0.5 | $5M | 15bp | 15.0 | 26.1% | -0.13 | -0.45 | -20.9% | -26.1% | -18.7% | -0.45 | 0.068 | 0.16 |
| OOS 2019-2022 | 0.5 | $20M | 5bp | 15.0 | 20.6% | -0.01 | -0.14 | -10.7% | -12.7% | -7.4% | -0.19 | 0.093 | 0.05 |
| OOS 2019-2022 | 0.5 | $20M | 15bp | 15.0 | 20.6% | -0.01 | -0.26 | -10.7% | -14.8% | -7.4% | -0.19 | 0.093 | 0.05 |
| OOS 2019-2022 | 1.0 | $5M | 5bp | 15.0 | 24.9% | -0.29 | -0.45 | -34.6% | -37.1% | -30.2% | -0.75 | 0.063 | 0.12 |
| OOS 2019-2022 | 1.0 | $5M | 15bp | 15.0 | 24.9% | -0.29 | -0.60 | -34.6% | -39.6% | -30.2% | -0.75 | 0.063 | 0.12 |
| OOS 2019-2022 | 1.0 | $20M | 5bp | 15.0 | 20.6% | -0.09 | -0.22 | -17.5% | -19.6% | -12.9% | -0.34 | 0.090 | 0.02 |
| OOS 2019-2022 | 1.0 | $20M | 15bp | 15.0 | 20.6% | -0.09 | -0.35 | -17.5% | -21.6% | -12.9% | -0.34 | 0.090 | 0.02 |

## Decision criteria

Per `project_next_session_edgar_backfill.md`:

- **PASS**: per-subperiod tri-factor t > 2.0 in 2 of 2 IS halves (2015-2018 vs 2019-2022); R² stable. Run subsample stability check next.
- **MID**: one half t > 2.0, the other marginal — forward-walk with Sharpe ≥ 0.7 gate, regime-conditional sizing.
- **FAIL**: both halves t < 1.5 OR catastrophic in any half → fallback to mom+lowvol; document tri-factor as 2-year-OOS-luck.
