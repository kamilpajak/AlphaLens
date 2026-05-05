# v9D Retrospective Pre-2018 Replication — README

Workspace for the retrospective fresh-OOS replication of the v9D
cross-sectional residual scorer on a pre-2018 window the model has never
been evaluated against. See pre-reg JSON
`docs/research/preregistration/params_v9d_retrospective_pre_2018_2026_05_05.json`
(sha256 `f43ac35785391fd3010efbc128d875fb0c982923df31a5ad7eed4d35f7548b58`)
for locked design.

## Layout

```
docs/research/v9d_retrospective_pre_2018/
├── {U1,U2,U3}_{GFC_recovery,mid_cycle_eu_debt,late_cycle_china_shock}_p{0..4}.json   # 45 per-cell results
├── logs/{universe}_{subperiod}_p{phase}.log                                          # per-cell stdout
└── (verdict files written by aggregator at parent dir)
```

## Reproduce

```bash
# 1. Refresh inventory (fast: ~15s for 4438 parquets)
.venv/bin/python scripts/build_ivol_inventory.py

# 2. Run all 45 cells (parallel-4 ≈ 80 min on local CPU; fully idempotent)
scripts/run_v9d_retrospective_battery.sh --parallel 4

# 3. Synthesize verdict
.venv/bin/python scripts/aggregate_v9d_retrospective_verdict.py
# Writes:
#   docs/research/v9d_retrospective_pre_2018_verdict.{md,json}
```

## Inputs

| Resource | Path | Source |
|---|---|---|
| iVol SMD cache | `~/.alphalens/ivolatility_smd/` | iVolatility $399 retail (probe v5 99.5%) |
| Cache inventory | `~/.alphalens/ivolatility_smd_inventory.parquet` | `scripts/build_ivol_inventory.py` |
| PIT yamls (U1) | `~/.alphalens/pit_universe/*.yaml` | `scripts/build_pit_universe.py` (legacy seed = today's IWM) |
| Polygon delisted CS | `~/.alphalens/polygon_delisted_2007_2018.parquet` | Polygon Starter `/v3/reference/tickers?active=false` |
| Delisting events | `~/.alphalens/survivorship/delisting_events_2008_2018.parquet` | Polygon-derived for terminal-return patch |
| Carhart 4F daily | (cached via `load_carhart_daily`) | Ken French data library |

## Pre-registered decision tree

| Verdict | αt threshold | Stability gates |
|---|---|---|
| `PASS_ROBUST` | αt ≥ +3.5 AND lower-bound CI > 0 | within-sub ≤ 1.5, cross-sub ≤ 2.5 |
| `PASS_MARGINAL` | αt ∈ [+2.5, +3.5) AND lower-bound CI > 0 | — |
| `INCONCLUSIVE` | αt ∈ [+1.0, +2.5) | — |
| `FAIL_ROBUST` | αt < +1.0 | — |
| `REGIME_DEPENDENT` | αt ≥ +3.5 but stability fails | — |
| `RECONSTRUCTION_DOMINANT` | \|U1 − U3\| > 3.0 | — |

Pre-locked bias range B ∈ [1.0%, 2.0%] /y. Bonferroni n=25 family
(Romano-Wolf step-down, Politis-Romano stationary block bootstrap, mean
block 4 weeks, B=10000).

## Status (kept current; updated by Phase 5)

- 2026-05-05: pre-reg locked, Polygon backfill DONE (1339 added), 45-cell
  battery RUNNING.
- _After verdict synthesis: amend with PASS/FAIL outcome + memory file
  pointer._
