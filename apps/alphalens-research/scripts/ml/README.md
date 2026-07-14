# ML exercises (exploratory, hypothesis-generation only)

Home for ad-hoc ML scripts over the EDGE / brief panels. Everything here is
**exploratory**: outputs may inform hypotheses and display ideas, never a
selection gate, ordering input, or exit rule (ADR 0013 R1/R2; promotion path =
the hypothesis-budget ledger + fresh-data pre-registration).

## House rules (every script)

1. **Unit = ticker-episode**, never raw rows — dedup first via
   `alphalens_research.diagnostics.options_retro.ticker_episode_dedup`
   (raw rows overstate N ~2×).
2. **Validation = GroupKFold by ARRIVAL SESSION** —
   `session_on_or_after(brief_date, "XNYS")`, NOT raw `brief_date`: weekend
   briefs collapse into Monday, so several brief_dates share one session and
   grouping by brief_date under-clusters. Candidates sharing an arrival
   session share the same market path; per-row CV leaks. Report per-fold
   spread, not just the mean (at ~20 session-clusters the spread IS the
   result).
3. **EPV budget** — features ≤ minority-class events / 10. Pre-commit the
   feature list and the primary hyperparameters in the module docstring BEFORE
   looking at CV output.
4. **Selection-target models use fill-independent outcomes** (`car_k` /
   substrate) and **pre-trade features only** — no trade-setup outputs
   (the tp1_r collider, `docs/research/tail_filter_tp1r_collider_2026_07_14.md`),
   no post-trade columns, no families with <60% coverage.
5. **Baselines to beat, same folds — ONE convention:** base rate + univariate
   `technical_atr_pct` with its **a-priori direction fixed in advance**
   (higher ATR → more likely below-SPY / worse car_k). That fixed-direction
   score is the PRIMARY baseline. The sign-agnostic `max(auc, 1-auc)` is
   allowed only as an explicitly-labelled **pessimistic bound**, never as the
   primary comparison. Under day clustering the permutation null for AUC is
   ~0.58 at this N, not 0.50 — an AUC in the low .60s is marginal, not signal.
   ATR is the anchor separator (one of three Bonferroni-clear per the
   hypothesis-budget ledger §3: ATR, MA50-distance, press-gate). A model that
   loses to one feature is a null result — report it as such.
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
   shuffling outcomes by WHOLE arrival-session cluster, or a session-cluster
   bootstrap. Differences like AUC 0.58 vs 0.60 are not distinguishable at
   this N. When pooling out-of-fold predictions for a rank metric, rank
   WITHIN each validation fold first (normalized by fold size) — pooling raw
   OOF predictions across folds manufactures (anti-)signal whenever a fold's
   predictions are near-constant.
9. **Known validation caveats — overlapping label windows + store
   non-retroactivity.** k=10 outcome windows of adjacent brief days share
   ~9/10 sessions, so even GroupKFold leaks through the LABELS (López de
   Prado purged/embargoed CV is the clean fix). At ~30 days of history full
   purging (±10 sessions) would destroy the training set, so for now: use
   arrival-session groups, treat CV numbers as mildly optimistic, and prefer
   folds built from contiguous 10-session BLOCKS when feasible (merge a runt
   final block into its predecessor). Revisit purged CV once history exceeds
   ~6 months. Separately: the grouped-daily store is split-adjusted only
   as-of each snapshot's own download date, NOT retroactively — a split
   inside an outcome window shows up as a raw close jump; panel builders must
   run the split guard (drop episodes with any day-over-day close ratio
   outside [0.55, 1.8]) and print the drop counter.
10. Data readiness per layer (measured 2026-07-14): SIGNAL marginal (tiny models
   only), LADDER marginal (P(fill) lean models ~Aug), IN-FLIGHT not enough
   (no policy learning; parametric replay grids only). Counterfactual replay
   variants and within-day market-state values are NOT independent
   observations.
11. **Every run that computes a CV metric or p-value against an EDGE outcome
   is a LOOK:** add a row to the looks-log (§4 of
   `docs/research/edge_hypothesis_budget_2026_07.md`) BEFORE the run; a
   feature without a cluster slot gets a new cluster row (raising everyone's
   denominator).
12. **Binding August re-run spec (pre-committed 2026-07-14):** panel = ONLY
   `brief_date >= 2026-07-06` (fresh episodes; no re-use of the burnt
   discovery window); primary target = continuous `car_10`; alpha grid
   pre-committed NOW as multiples of sd(y): {0.05, 0.15, 0.5}×sd(y) with
   PRIMARY 0.15×sd(y); any respec after seeing results counts as a new
   informed look; ledger §4 row goes in BEFORE the run (rule 11).

## Running

Scripts assume a synced workspace venv and local `~/.alphalens/` stores
(rsync from the VPS first if stale):

    .venv/bin/python apps/alphalens-research/scripts/ml/<script>.py

Naming: `YYYY_MM_<topic>.py`; keep the results snapshot of the last run in the
module docstring so re-runs can diff against it.

## Catalog

| Script | Question | Last result |
|---|---|---|
| `2026_07_signal_below_spy_tiny.py` | Does a ≤7-feature model predict below-SPY (car_10<0) better than ATR alone? | NO — model TIES the fixed-direction ATR baseline (CV AUC 0.584 vs 0.598 after the arrival-session unit fix; sign-agnostic pessimistic bound 0.619); both marginal vs a clustered null (~0.58 at this N). L1 keeps only ma50_distance + ATR. Third look at the SAME burnt panel (not independent). August re-run per rule 12. |
| `2026_07_signal_car10_continuous.py` | Rules-6-9 upgrade: does continuous-target modeling (elastic net + cluster-OLS/WCB inference + block folds + cluster-bootstrap comparison) beat ATR? | NO — with the corrected rank-within-fold pooled metric, primary Spearman +0.03 vs -ATR baseline +0.19; cluster-bootstrap CI of the delta [-0.37, +0.07] includes 0 → uninformative at this N. The earlier "significantly worse, CI excludes 0" was a pooled-OOF artifact (primary near-constant in 2/5 folds); no test run for secondaries. Inference recovered log10_mcap (p_wcb .024, invisible to the binary model) — below the .05/7 family bar, exploratory note. |
| `2026_07_tail_filter_features_gkfold.py` | Do 23 pre-trade features separate below-SPY terminal trades under grouped CV? | N = 80 EPISODES (freeze-capped 2026-07-05): L1 keeps ATR+ma50 only, CV AUC 0.643; depth-2 boosting train AUC 1.000 vs CV 0.606 (memorization); fixed-direction ATR alone 0.747 — both models lose to the univariate baseline. *Deliberately breaks rules 3-4 (fill-dependent target, EPV ignored) as the overfitting demo; its coverage pre-filter runs on the full panel, so treat numbers as mildly optimistic. Recorded burn: the first (pre-cap) run consumed 2 post-freeze episodes.* |
