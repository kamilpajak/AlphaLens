# Early-Stage vs Momentum Scorer — Phase 2 Comparison

Porównanie dwóch scorer'ów Layer 2b na tym samym 113-name curated universe,
2021-06-01 → 2026-04-17, daily rebalance, top-5 × linear, 5-day holding period.

**Cel**: zweryfikować czy EarlyStageScorer (CAN SLIM / Minervini VCP / Jegadeesh 11-1)
wybiera inne stocks niż obecny MomentumScorer, czy picks są wcześniej w rally cycle'u
(test Layer 3 acceptance proxy przez extension features), czy zachowuje sensowny edge.

## Experiment 1 — Headline metrics

| Metric | momentum | early_stage |
| --- | ---: | ---: |
| sharpe_gross | 1.4877 | 1.1346 |
| annual_return_pct | 101.7174 | 66.9085 |
| ic_mean | 0.0248 | 0.0070 |
| ic_tstat | 3.9667 | 1.6616 |
| max_drawdown_pct | -50.1043 | -68.1385 |
| turnover_pct | 39.8998 | 76.1723 |
| ff3_alpha_ann_pct | 94.7101 | 60.9148 |
| ff3_alpha_tstat | 2.7316 | 2.0163 |
| ff3_r2 | 0.0060 | 0.0035 |

## Experiment 2 — Pick overlap

- Days compared: 999
- Mean overlap: **0.050**
- Median overlap: 0.000
- Days with zero overlap: 795 (79.6%)
- Days with full overlap (same 5 names): 0 (0.0%)

## Experiment 3 — Extension at pick time (Layer 3 rejection proxy)

Lower = earlier in rally cycle, more headroom, less likely to be rejected as 'buy at peak'.

| Metric | Momentum | EarlyStage | Δ (lower is better) |
| --- | ---: | ---: | ---: |
| trailing_60d_mean | 0.6569 | 0.0717 | -0.5853 ✓ |
| dist_from_52w_high_mean | 0.1158 | 0.3395 | +0.2237 ✗ |
| rsi_mean | 70.6817 | 55.0644 | -15.6174 ✓ |
| pct_picks_near_high | 0.3365 | 0.0833 | -0.2533 ✓ |
| pct_picks_rsi_overbought | 0.5728 | 0.0643 | -0.5085 ✓ |

## Experiment 4 — Forward return distribution

Mean forward return across all top-5 picks. Higher = better signal.

| Horizon | Momentum | EarlyStage | Δ |
| --- | ---: | ---: | ---: |
| fwd_20d_mean | 0.0490 | 0.0504 | +0.0014 |
| fwd_60d_mean | 0.1273 | 0.1270 | -0.0003 |
| fwd_120d_mean | 0.2066 | 0.2359 | +0.0293 |

## Experiment 5 — Theme HHI (concentration)

Lower HHI = more diversified across themes per day. 1.0 = all 5 picks in same theme.

| Scorer | Mean | Median | p90 |
| --- | ---: | ---: | ---: |
| momentum | 0.462 | 0.429 | 0.680 |
| early_stage | 0.423 | 0.389 | 0.680 |

## Experiment 6 — Hybrid scorer

**Triggered**: True

Trigger conditions:
- early_stage Sharpe > 0.5: True (1.13)
- pick overlap mean < 0.7: True (0.05)
- dist_from_52w_high reduction > 3pp: True (-0.224)

## Recommendation

**GO do Fazy 3** (paper trade): picks różnią się od obecnego scorer'a, Sharpe jest sensowny, i extension features pokazują że picks są wcześniej w rally'u. Layer 3 powinien je częściej akceptować.
