# Multi-source two-stage screener — Phase A sanity report

**Verdict:** PASS (with warnings)

**Warnings:**
- 11 unexpected collinear pair(s) (|ρ| ≥ 0.9)


## Run parameters

- Sample window: 2020-01-01 → 2020-04-01
- Sample tickers: 50 (full available pool: 1923)
- Rebalance stride: 5
- Asof rebalance dates: 13
- Output rows (after per-ticker compute): 494
- FRED DGS3MO available: yes

## Coverage waterfall

| Step | Cells | Note |
| --- | ---: | --- |
| Starting (universe × asof) | 650 |  |
| After per-ticker compute (truncate_to + insider lookup) | 494 | 76.0% of starting |

## Per-feature stats

| feature | n_present | nan_rate | mean | std | min | p25 | p50 | p75 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| insider_log_count | 494 | 0.0000 | 0.0926 | 0.3784 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 2.3026 |
| insider_log_dollar | 494 | 0.0000 | 0.7621 | 3.0749 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 16.5248 |
| insider_cluster_window_days | 494 | 0.0000 | 1.7611 | 7.0593 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 30.0000 |
| vix_level | 494 | 0.0000 | 31.8577 | 22.9990 | 12.3200 | 13.6800 | 18.8400 | 54.4600 | 82.6900 |
| vix_change_20d | 494 | 0.0000 | 0.9088 | 1.3951 | -0.2187 | 0.1104 | 0.5108 | 0.7739 | 5.0446 |
| term_spread_10y_3m | 494 | 0.0000 | 0.2238 | 0.2556 | -0.1500 | 0.0100 | 0.2100 | 0.3400 | 0.7400 |
| ret_20d | 494 | 0.0000 | -0.1168 | 0.2216 | -0.7952 | -0.2472 | -0.0506 | 0.0223 | 0.5204 |
| ret_60d | 494 | 0.0000 | -0.0625 | 0.3341 | -0.8680 | -0.2641 | -0.0316 | 0.0839 | 1.5417 |
| ret_252d | 481 | 0.0263 | 0.0313 | 0.6648 | -0.9105 | -0.3304 | -0.0466 | 0.1883 | 4.6134 |
| vol_realized_20d | 494 | 0.0000 | 0.0432 | 0.0403 | 0.0017 | 0.0138 | 0.0285 | 0.0622 | 0.2179 |
| near_52w_high_distance | 481 | 0.0263 | -0.2877 | 0.2451 | -0.9139 | -0.4885 | -0.2288 | -0.0572 | 0.0000 |
| dollar_volume_z_20d | 494 | 0.0000 | -0.0426 | 1.2025 | -2.2219 | -0.7220 | -0.2347 | 0.3494 | 11.9332 |
| rolling_beta_mkt_252d | 481 | 0.0263 | 1.0660 | 0.5818 | -0.7766 | 0.7565 | 1.0723 | 1.3901 | 2.6107 |
| idiosyncratic_vol_residual_60d | 481 | 0.0263 | 0.0291 | 0.0222 | 0.0023 | 0.0130 | 0.0235 | 0.0382 | 0.1285 |
| rank_momentum_60d | 494 | 0.0000 | 0.5132 | 0.2889 | 0.0263 | 0.2632 | 0.5132 | 0.7632 | 1.0000 |
| rank_lowvol_20d | 494 | 0.0000 | 0.5132 | 0.2889 | 0.0263 | 0.2632 | 0.5132 | 0.7632 | 1.0000 |
| rank_dollar_volume_size | 494 | 0.0000 | 0.5132 | 0.2889 | 0.0263 | 0.2632 | 0.5132 | 0.7632 | 1.0000 |
| interaction_insider_x_vix_high | 494 | 0.0000 | 0.0870 | 0.3694 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 2.3026 |
| interaction_mom20_x_vol_regime | 494 | 0.0000 | -0.0112 | 0.0246 | -0.1537 | -0.0120 | -0.0009 | 0.0002 | 0.0396 |
| interaction_mom20_x_size_quintile | 494 | 0.0000 | -0.0538 | 0.1284 | -0.6097 | -0.0979 | -0.0153 | 0.0066 | 0.4920 |
| interaction_insider_x_mom20 | 494 | 0.0000 | -0.0369 | 0.1696 | -1.3516 | 0.0000 | 0.0000 | 0.0000 | 0.3747 |

## Regime distribution

| Regime | Share |
| --- | ---: |
| Q1_calm | 23.08% |
| Q2 | 23.08% |
| Q3 | 7.69% |
| Q4_stress | 46.15% |

## Holdout extrapolation

- Sample density: 0.7600 obs/cell
- Extrapolated holdout obs (~100 rebalance dates × full ~1923-ticker universe): **146,148**
- Floor (PHASE-B-INFEASIBLE below): 5,000
- Comfortable threshold: 8,000

## Pairwise Spearman collinearity (|ρ| ≥ 0.90)

**Unexpected — investigate before Phase B:**

- `insider_log_count` ↔ `insider_log_dollar` (ρ = +0.999)
- `insider_log_count` ↔ `insider_cluster_window_days` (ρ = +1.000)
- `insider_log_count` ↔ `interaction_insider_x_vix_high` (ρ = +0.961)
- `insider_log_dollar` ↔ `insider_cluster_window_days` (ρ = +0.999)
- `insider_log_dollar` ↔ `interaction_insider_x_vix_high` (ρ = +0.954)
- `insider_cluster_window_days` ↔ `interaction_insider_x_vix_high` (ρ = +0.958)
- `vix_level` ↔ `vix_change_20d` (ρ = +0.940)
- `ret_20d` ↔ `interaction_mom20_x_vol_regime` (ρ = +0.987)
- `ret_20d` ↔ `interaction_mom20_x_size_quintile` (ρ = +0.941)
- `interaction_insider_x_vix_high` ↔ `interaction_insider_x_mom20` (ρ = -0.963)
- `interaction_mom20_x_vol_regime` ↔ `interaction_mom20_x_size_quintile` (ρ = +0.928)

## Notes

- Universe inclusion implication: features needing ≥252-day lookback impose an implicit ≥1-year history filter on participating tickers. Acknowledged, not a bug.
- F4 fire-sale exclusion is active via ParquetInsiderScorer(delisting_events=...). Insider features default to 0.0 when scorer returns None (matches 'no signal').
- VIX-quartile thresholds frozen at end of train period per pre-registration.
- This report covers only Phase A (feature plumbing). Phase B (Lasso, holdout) blocked until verdict above is PASS.
