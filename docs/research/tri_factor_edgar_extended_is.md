# Tri-factor combo (mom + lowvol + ROE) — EDGAR-backed 2019-2023

**RESEARCH ONLY.** Re-validation of `tri_factor_combo.md` (2026-04-29) on an
extended 4-year IS using SEC EDGAR
companyfacts for the ROE component (SimFin's 2020-Q2 floor previously locked
the IS to 2 years).

- Score: z(mom_12_1m) − vol_w × z(vol_60d) + roe_w × z(roe_ttm)
- Top-N: 15, holding-signal: 60d, stride: 5
- Vol weight: 1.0 (fixed)
- ROE weights tested: [1.0]
- ADV thresholds: ['$5M']

## Results

| Period | roe_w | ADV | cost | mean topN | turn | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | R² | β_MOM |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS 2019-2022 | 1.0 | $5M | 5bp | 15.0 | 24.9% | 0.83 | 0.65 | +40.1% | +37.5% | +63.1% | +2.24 | 0.049 | 0.06 |
| OOS 2023-2023 | 1.0 | $5M | 5bp | 15.0 | 23.8% | 2.73 | 2.46 | +67.9% | +65.5% | +141.0% | +2.19 | 0.148 | 0.10 |

## Decision criteria

Per `project_next_session_edgar_backfill.md`:

- **PASS**: per-subperiod tri-factor t > 2.0 in 2 of 2 IS halves (2015-2018 vs 2019-2022); R² stable. Run subsample stability check next.
- **MID**: one half t > 2.0, the other marginal — forward-walk with Sharpe ≥ 0.7 gate, regime-conditional sizing.
- **FAIL**: both halves t < 1.5 OR catastrophic in any half → fallback to mom+lowvol; document tri-factor as 2-year-OOS-luck.
