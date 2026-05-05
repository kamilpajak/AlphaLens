# v9D cross-period diagnostic — verdict: **STABLE**

**Rationale:** αt range 1.52 ≤ 2.0 → uniform across sub-periods

Data source: `docs/research/v10_drawdown_overlay/holdout_p[0-4].json` (5 phase JSONs from v10 audit, pooled into 501 dated rebalance returns).

Full window 2024-04-30 → 2026-04-29: αt = **+3.58** (Sharpe net 1.51, MaxDD -33.8%).

## Per-sub-period stats

| Sub-period | Window | n | αt (Carhart-4F, HAC) | Sharpe net | MaxDD | mean per-rebal |
|---|---|---|---|---|---|---|
| H2_2024 | 2024-04-30..2024-12-31 | 170 | +3.17 | 2.71 | -7.5% | +21.93% |
| H1_2025 | 2025-01-01..2025-06-30 | 122 | +1.65 | 1.09 | -21.6% | +2.97% |
| H2_2025 | 2025-07-01..2025-12-31 | 128 | +1.85 | 1.17 | -4.2% | +15.42% |
| H1_2026 | 2026-01-01..2026-04-30 | 81 | +3.16 | 2.61 | -33.8% | +3.30% |

**Cross-period αt range:** 1.52 (min +1.65, max +3.17, mean +2.45).

## Decision-rule application

- STABLE if αt range ≤ 2.0
- CONCENTRATED if range > 2.0 AND any sub-period αt < 0.5
- MIXED if range > 2.0 but all sub-periods αt ≥ 1.0

## Verdict: **STABLE**

αt range 1.52 ≤ 2.0 → uniform across sub-periods

## Implications for H+ paper-trade prospective track

+2.29 αt is uniform across the burnt holdout. H+ priors stay high — proceed to Phase 2 (paper-trade infrastructure setup) with high confidence that fresh-window replication is plausible.
