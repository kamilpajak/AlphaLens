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
6. **Prefer CONTINUOUS targets over binary.** Dichotomizing a continuous
   outcome (below/above SPY) discards ~1/3 to 1/2 of the sample's power
   (Altman & Royston 2006; Cohen 1983: a median split cuts explained variance
   to ~0.65 r^2). Model `car_k` directly (cluster-robust linear / rank-based
   with day-cluster bootstrap) — which is what the attribution pipeline's
   `cluster_ols` + wild cluster bootstrap already do; a binary classifier is
   the exception (needs a stated reason), not the default.
7. **Correlated features → elastic net, not pure L1** (lasso picks one of a
   correlated pair ~arbitrarily; e.g. ATR vs ma50-distance).
8. **Comparing two models/metrics at ~30 clusters:** naive tests (incl.
   DeLong for AUC) are INVALID under day clustering — use a permutation test
   shuffling outcomes by WHOLE day-cluster, or a day-cluster bootstrap.
   Differences like AUC 0.609 vs 0.632 are not distinguishable at this N.
9. **Known validation caveat — overlapping label windows.** k=10 outcome
   windows of adjacent brief days share ~9/10 sessions, so even GroupKFold
   leaks through the LABELS (López de Prado purged/embargoed CV is the clean
   fix). At ~30 days of history full purging (±10 sessions) would destroy the
   training set, so for now: use day groups, treat CV numbers as mildly
   optimistic, and prefer folds built from contiguous 10-session BLOCKS when
   feasible. Revisit purged CV once history exceeds ~6 months.
10. Data readiness per layer (measured 2026-07-14): SIGNAL marginal (tiny models
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
| `2026_07_signal_car10_continuous.py` | Rules-6-9 upgrade: does continuous-target modeling (elastic net + cluster-OLS/WCB inference + block folds + cluster-bootstrap comparison) beat ATR? | NO — primary significantly worse (mis-scaled alpha, lesson recorded), best secondary ≈ baseline. Inference recovered log10_mcap (p_wcb .033, invisible to the binary model) — below the 7-test family bar, exploratory note. |
| `2026_07_tail_filter_features_gkfold.py` | Do 30 pre-trade features separate below-SPY terminal trades under grouped CV? | L1 keeps ATR+ma50 only; depth-2 boosting: train AUC 1.000 vs CV 0.758 vs ATR-alone 0.730 (memorization). *Deliberately breaks rules 3-4 (fill-dependent target, EPV ignored) as the overfitting demo; its coverage pre-filter runs on the full panel, so treat numbers as mildly optimistic.* |
