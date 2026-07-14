# Tail-filter sweep and the tp1_r collider — evidence record

**Status:** COMPLETE — exploratory sweep + next-day refutation; primary evidence for ADR 0013 rules R1/R2
**Date:** 2026-07-14 (both the sweep and the refutation)
**Method:** two multi-agent workflows (finder lenses × adversarial verification), frozen frames, episode dedup + wild cluster bootstrap via `alphalens_research/diagnostics/options_retro.py` helpers

## 1. The sweep (user question: filter below-SPY trades without dropping above-SPY ones)

Frame: the /edge telemetry population — 134 closed trades (45 below SPY / 89 above),
82 ticker-episodes (27 below) after chained 5-session dedup, 31 brief-day clusters,
window 2026-05-27..2026-07-07. ~76 filter specifications examined across 5 lenses;
**nothing cleared the family Bonferroni bar (.05/22 ≈ .0023)** — all results
exploratory, fill-dependent, in-sample.

Best candidate surfaced: **drop `tp1_r > 1.5`** (the trade setup's headline
`r_multiple` on the first TP tranche): dedup left-tail recall .70, right-tail loss
.31, asymmetry .395, p_WCB .038; survived a joint cluster-OLS with continuous ATR
(beta −0.106, p .021) and a ±20% threshold wiggle. Baselines for scale (dedup):
`atr_pct > 5.77` asymmetry .356 (p .024); press-gate alone dedup-fragile (p .13);
`pct_off_52w_high` raw asymmetry was pure episode duplication (dedup p .76).

## 2. The refutation (user question: should the ladder stay independent of selection?)

A second workflow decomposed the finding on the full plannable panel (524 rows;
345 with matured fill-independent `car_10`):

- **Fill-independent test:** on `car_10` (fixed 10-session horizon the ladder cannot
  touch), the tp1_r effect is NULL — indicator beta −0.0237 p_WCB .335; with
  ATR+mcap controls **beta −0.0017, p .93** (~1% of the fill-dependent effect size;
  the continuous version flips sign).
- **Same-rows collider test:** on the identical ~65 rows carrying BOTH outcomes:
  ladder-window excess p .008–.015 vs `car_10` p .36–.44. Same names, same dates —
  the outcome *definition* alone flips the result.
- **Mechanics:** among closed trades, P(SL_HIT | tp1_r > 1.5) = **93.9%** vs 6.8%
  below the threshold; P(TP_FULL) = 6.1% vs 88.6%; NO_FILL flat (21.4% vs 19.1%).
  "Closed AND high tp1_r" ≈ "price already crashed through the disaster stop" —
  selection on the realized price path, not prediction of it.
- **Proxy layer:** z(tp1_r) on 7 chart-state features has R² = 0.403 (RSI −0.82,
  ma50_dist +0.44, 52wH −0.32), but the chart-state projection carries NO signal
  (p .34); the ladder-only residual (the stop-width leg) carries all of it.
- **Robustness kills:** dropping the 3 most influential brief-day clusters flips the
  car_10 sign (+0.022, p .155); split halves disagree in sign (−0.030 / +0.005).

**Verdict:** interpretation (c) — ladder-only mechanics. `tp1_r` (and any trade-setup
output) is NOT admissible as a selection covariate. This is the founding evidence for
ADR 0013 R1 (substrate-first) and R2 (setup outputs never feed selection).

## 3. Side findings (recorded, each with its own multiplicity cost if pursued)

- **`res60_atr`** (headroom to the trailing 60-session high in ATR units, computed
  ladder-free from the grouped-daily store): positive orientation (more headroom →
  better `car_10`), ATR-controlled p_WCB .004, survives Bonferroni×6 of its own
  exercise, drop-3-clusters (p .032) and split-half sign-stability. Eligible for ONE
  exploratory pre-registration; NOT a validation of the (opposite-oriented) tp1_r
  rule.
- The stop-width → SL_HIT-dominance mechanism routes to the EXIT workstream (the
  planned September stop-rule walk-forward per
  `exit_geometry_reward_risk_2026_06_30.md` §7), not to selection.
- ML cross-check (GroupKFold by brief day, 30 numeric pre-trade features, 82
  episodes): L1 logistic keeps exactly `technical_atr_pct` + `technical_ma50_distance_pct`
  (the known separators); HistGradientBoosting reaches train AUC 1.000 vs grouped-CV
  .758 vs single-feature ATR .730 — memorization, no incremental structure.

## 4. Look accounting

Sweep ~76 specs + decomposition ~20 specs, all on burnt in-sample panels; recorded
here so the September looks can count them. No selection, ordering, or exit change
was made from any of this.
