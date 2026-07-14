# ML exercises (exploratory, hypothesis-generation only)

Home for ad-hoc ML scripts over the EDGE / brief panels. Everything here is
**exploratory**: outputs may inform hypotheses and display ideas, never a
selection gate, ordering input, or exit rule (ADR 0013 R1/R2; promotion path =
the hypothesis-budget ledger + fresh-data pre-registration).

## House rules (every script)

1. **Unit = ticker-episode**, never raw rows — dedup first via
   `alphalens_research.diagnostics.options_retro.ticker_episode_dedup`
   (raw rows overstate N ~2×).
2. **Validation = GroupKFold by `brief_date`** — candidates from one brief day
   share the same market path; per-row CV leaks. Report per-fold spread, not
   just the mean (at ~30 day-clusters the spread IS the result).
3. **EPV budget** — features ≤ minority-class events / 10. Pre-commit the
   feature list and the primary hyperparameters in the module docstring BEFORE
   looking at CV output.
4. **Selection-target models use fill-independent outcomes** (`car_k` /
   substrate) and **pre-trade features only** — no trade-setup outputs
   (the tp1_r collider, `docs/research/tail_filter_tp1r_collider_2026_07_14.md`),
   no post-trade columns, no families with <60% coverage.
5. **Baselines to beat, same folds:** base rate + univariate `technical_atr_pct`
   (the only Bonferroni-grade separator). The univariate baseline reports
   `max(auc, 1-auc)` per fold — sign-agnostic, i.e. deliberately strong, so
   beating it means something. A model that loses to one feature is a null
   result — report it as such.
6. Data readiness per layer (measured 2026-07-14): SIGNAL marginal (tiny models
   only), LADDER marginal (P(fill) lean models ~Aug), IN-FLIGHT not enough
   (no policy learning; parametric replay grids only). Counterfactual replay
   variants and within-day market-state values are NOT independent
   observations.

## Running

Scripts assume a synced workspace venv and local `~/.alphalens/` stores
(rsync from the VPS first if stale):

    .venv/bin/python apps/alphalens-research/scripts/ml/<script>.py

Naming: `YYYY_MM_<topic>.py`; keep the results snapshot of the last run in the
module docstring so re-runs can diff against it.

## Catalog

| Script | Question | Last result |
|---|---|---|
| `2026_07_signal_below_spy_tiny.py` | Does a ≤7-feature model predict below-SPY (car_10<0) better than ATR alone? | NO — CV AUC 0.609 vs ATR 0.632; L1 keeps only ma50_distance + ATR (3rd independent confirmation). Re-run ~mid-Aug. |
| `2026_07_tail_filter_features_gkfold.py` | Do 30 pre-trade features separate below-SPY terminal trades under grouped CV? | L1 keeps ATR+ma50 only; depth-2 boosting: train AUC 1.000 vs CV 0.758 vs ATR-alone 0.730 (memorization). *Deliberately breaks rules 3-4 (fill-dependent target, EPV ignored) as the overfitting demo; its coverage pre-filter runs on the full panel, so treat numbers as mildly optimistic.* |
