# Tri-factor combo (mom + lowvol + ROE) — EDGAR-backed 2015-2026

**RESEARCH ONLY.** Re-validation of `tri_factor_combo.md` (2026-04-29) on an
extended 8-year IS using SEC EDGAR
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
| IS 2015-2022 | 0.5 | $5M | 5bp | 15.0 | 23.8% | 0.45 | 0.26 | +21.3% | +18.9% | +30.0% | +1.51 | 0.025 | 0.01 |
| IS 2015-2022 | 0.5 | $5M | 15bp | 15.0 | 23.8% | 0.45 | 0.06 | +21.3% | +16.5% | +30.0% | +1.51 | 0.025 | 0.01 |
| IS 2015-2022 | 0.5 | $20M | 5bp | 15.0 | 18.3% | 0.46 | 0.29 | +18.0% | +16.2% | +27.2% | +1.54 | 0.025 | -0.04 |
| IS 2015-2022 | 0.5 | $20M | 15bp | 15.0 | 18.3% | 0.46 | 0.12 | +18.0% | +14.3% | +27.2% | +1.54 | 0.025 | -0.04 |
| IS 2015-2022 | 1.0 | $5M | 5bp | 15.0 | 20.4% | 0.49 | 0.31 | +22.0% | +20.0% | +30.5% | +1.65 | 0.024 | 0.01 |
| IS 2015-2022 | 1.0 | $5M | 15bp | 15.0 | 20.4% | 0.49 | 0.14 | +22.0% | +17.9% | +30.5% | +1.65 | 0.024 | 0.01 |
| IS 2015-2022 | 1.0 | $20M | 5bp | 15.0 | 17.2% | 0.36 | 0.20 | +12.2% | +10.4% | +20.8% | +1.26 | 0.024 | -0.01 |
| IS 2015-2022 | 1.0 | $20M | 15bp | 15.0 | 17.2% | 0.36 | 0.04 | +12.2% | +8.7% | +20.8% | +1.26 | 0.024 | -0.01 |
| OOS 2023-2026 | 0.5 | $5M | 5bp | 15.0 | 26.2% | 0.75 | 0.54 | +20.8% | +18.2% | +45.7% | +1.36 | 0.011 | -0.04 |
| OOS 2023-2026 | 0.5 | $5M | 15bp | 15.0 | 26.2% | 0.75 | 0.33 | +20.8% | +15.5% | +45.7% | +1.36 | 0.011 | -0.04 |
| OOS 2023-2026 | 0.5 | $20M | 5bp | 15.0 | 21.4% | 1.16 | 0.98 | +43.6% | +41.5% | +66.0% | +2.13 | 0.008 | -0.05 |
| OOS 2023-2026 | 0.5 | $20M | 15bp | 15.0 | 21.4% | 1.16 | 0.80 | +43.6% | +39.3% | +66.0% | +2.13 | 0.008 | -0.05 |
| OOS 2023-2026 | 1.0 | $5M | 5bp | 15.0 | 23.9% | 0.65 | 0.45 | +11.5% | +9.1% | +34.0% | +1.04 | 0.008 | -0.05 |
| OOS 2023-2026 | 1.0 | $5M | 15bp | 15.0 | 23.9% | 0.65 | 0.24 | +11.5% | +6.6% | +34.0% | +1.04 | 0.008 | -0.05 |
| OOS 2023-2026 | 1.0 | $20M | 5bp | 15.0 | 20.9% | 1.32 | 1.12 | +46.0% | +43.9% | +65.0% | +2.22 | 0.009 | -0.08 |
| OOS 2023-2026 | 1.0 | $20M | 15bp | 15.0 | 20.9% | 1.32 | 0.93 | +46.0% | +41.8% | +65.0% | +2.22 | 0.009 | -0.08 |

## Decision criteria

Per `project_next_session_edgar_backfill.md`:

- **PASS**: per-subperiod tri-factor t > 2.0 in 2 of 2 IS halves (2015-2018 vs 2019-2022); R² stable. Run subsample stability check next.
- **MID**: one half t > 2.0, the other marginal — forward-walk with Sharpe ≥ 0.7 gate, regime-conditional sizing.
- **FAIL**: both halves t < 1.5 OR catastrophic in any half → fallback to mom+lowvol; document tri-factor as 2-year-OOS-luck.
