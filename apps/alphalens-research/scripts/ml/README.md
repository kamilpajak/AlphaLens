# ML exercises (exploratory, hypothesis-generation only)

Home for ad-hoc ML scripts over the EDGE / brief panels. Everything here is
**exploratory**: outputs may inform hypotheses and display ideas, never a
selection gate, ordering input, or exit rule (ADR 0013 R1/R2; promotion path =
the hypothesis-budget ledger + fresh-data pre-registration).

## House rules (every script)

1. **Unit = ticker-episode**, never raw rows — dedup first via
   `alphalens_research.diagnostics.options_retro.ticker_episode_dedup`
   (raw rows overstate N ~2×). That function chains episodes on
   `session_on_or_after(brief_date)`, NOT on the rule-2 arrival, so the
   collapse clock and the cluster labels are two different definitions of
   arrival. Every registered number in this directory was computed that way;
   reconciling them would move every panel, so it stays, and it is tracked on
   #1419. The same disclosure sits in `episode_panel.py`, section "What this
   module deliberately does NOT fix".
2. **Validation = GroupKFold by ARRIVAL SESSION** —
   `ladder_arrival_session(brief_date, "XNYS")`, the first NYSE session
   STRICTLY AFTER the brief date (owner decision D4; `market/calendar.py`,
   #1416), NOT raw `brief_date`: Friday, Saturday and Sunday briefs all
   arrive on the same Monday, so several brief_dates share one session and
   grouping by brief_date under-clusters. `session_on_or_after(brief_date)`
   is NOT that session — it returns the brief date itself whenever that date
   is a session, and the two disagree on 296 of 466 burnt rows
   (`2026_09_sel_ar_20_burnt_probe.py`, section OUTCOME AND ANCHOR). On the
   stamped selection label read the `anchor_session` column instead of
   recomputing either (rule 13); the pre-registry `car_10` scripts keep
   `session_on_or_after`, the anchor their registration froze — that is the
   step-2 answer for those scripts (#1419), not an oversight. Candidates
   sharing an arrival session share the same market path; per-row CV leaks.
   Report per-fold spread, not just the mean (at ~20 session-clusters the
   spread IS the result).
3. **EPV budget** — features ≤ minority-class events / 10. Pre-commit the
   feature list and the primary hyperparameters in the module docstring BEFORE
   looking at CV output.
4. **Selection-target models train on the stamped selection label**
   (`sel_ar_h` / `sel_zar_h`, primary `sel_ar_20`, both fill-independent;
   `docs/research/ml_label_registry_design_2026_09_16.md` §5.1 and owner
   decision D1) and **pre-trade features only** — no trade-setup outputs
   (the tp1_r collider, `docs/research/tail_filter_tp1r_collider_2026_07_14.md`),
   no post-trade columns, no families with <60% coverage. A look registered
   before the registry keeps its registered outcome (`car_10`,
   `car_10_ex0`, `car_20_event` — registry §7).
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
   non-retroactivity.** The primary horizon is h = 20 sessions (`sel_ar_20`,
   owner decision D1) and arrival clusters accrue at about one per session,
   so two adjacent arrivals share up to 19 of their 20 outcome sessions and
   even GroupKFold leaks through the LABELS (López de Prado
   purged/embargoed CV is the clean fix). **The embargo is the primary
   horizon**: purge and embargo h sessions on each side of every validation
   block, and say in the registration what that costs — at h = 20 about 20
   of the 73 `ladder_arrival_session` clusters the label registry counted on
   2026-09-16 (`ml_label_registry_design_2026_09_16.md` §4, the "Reconciling
   the counts" row; its §6 states the same cost as 20 of 74, under the
   superseded `session_on_or_after` count). Until a panel can pay
   that: use arrival-session groups, treat CV numbers as mildly optimistic,
   and prefer folds built from contiguous h-session BLOCKS (20 sessions at
   the primary horizon) when feasible (merge a runt final block into its
   predecessor). Revisit full purging once history exceeds ~6 months.
   Separately: the grouped-daily store is split-adjusted only
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
13. **A model READS the stamped label; recomputing a label in a script is a
   new definition and needs its own version**
   (`docs/research/ml_label_registry_design_2026_09_16.md` §8 step 5, and
   its principle 9). Take `sel_ar_h`, its `sel_label_status_h` and its
   `anchor_session` from `~/.alphalens/selection_labels/` rather than
   recomputing any of them. Recorded case: the first run of
   `2026_09_sel_ar_20_burnt_probe.py` recomputed the anchor with
   `session_on_or_after`, which disagrees with the stamped `anchor_session`
   on 296 of 466 burnt rows; that run was discarded and an AST source gate
   now forbids the call (`tests/test_sel_ar_20_burnt_probe.py`,
   `tests/test_jev_discovery_burnt.py`).

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
| `2026_07_ladder_fill_tiny.py` | LADDER (T5): does the first tier fill within TTL? Survival (time-to-fill) primary, NO_FILL logistic secondary | Cox PH: ma50-extension HR 1.23 z +5.54 (extended names fill faster), e1-depth HR 0.87 z −2.23 (mechanics confirmed); binary view uninformative at 22 minority events. Ladder-layer look — outside the §3 selection family. |
| `2026_07_ladder_fill_depth_cr.py` | LADDER v2: how DEEP does the ladder fill? Continuation-ratio on stacked tier transitions (censoring-aware; levels enter as spacing-to-next-tier in ATR) | spacing −1.10 z −5.5 (mechanics confirmed), ma50-extension +0.84 z +4.0 (extended names fill deeper), both bootstrap CIs exclude 0, drop-3-clusters sign-stable. T5-layer look outside the §3 family. |
| `2026_09_experts_last_look.py` | Cluster 15's LAST look: do the expert-panel signals (6 ambiguous members + a 7-feature elastic net) predict held-out car_10 beyond ATR? | RUN 2026-09-29 (registered 2026-09-01) — verdict **RETIRE**, cluster 15 SPENT at 3 of 3 looks. Part A smallest p_wcb 0.30 against the .05/7 = .00714 bar across all 6 members; Part B elastic-net delta vs −ATR −0.125 (p 0.0666), i.e. worse than the fit-free baseline. All six 99.286% partial-Spearman CIs still cover an absolute ρ of 0.10 or more, so the pre-committed language is inconclusive — retired OPERATIONALLY, not falsified. Experts stay display-only; memo `docs/research/experts_last_look_2026_09.md`, ledger §4 row 2026-09-29. |
| `2026_07_tail_filter_features_gkfold.py` | Do 23 pre-trade features separate below-SPY terminal trades under grouped CV? | N = 80 EPISODES (freeze-capped 2026-07-05): L1 keeps ATR+ma50 only, CV AUC 0.643; depth-2 boosting train AUC 1.000 vs CV 0.606 (memorization); fixed-direction ATR alone 0.747 — both models lose to the univariate baseline. *Deliberately breaks rules 3-4 (fill-dependent target, EPV ignored) as the overfitting demo; its coverage pre-filter runs on the full panel, so treat numbers as mildly optimistic. Recorded burn: the first (pre-cap) run consumed 2 post-freeze episodes.* |
| `2026_09_a20_atr_confirmation.py` | #1227, the ONE-SHOT held-out ATR confirmation: does z(`technical_atr_pct`) keep its discovery slope on `sel_ar_20` out of sample? | REGISTERED 2026-09-23 (the module docstring IS the registration), NOT RUN — family of one, bar one-sided 0.05, B = 9999, seed 20260924, smallest actionable effect 0.10, `RUN_NOT_BEFORE` 2026-10-09. `--run` is the owner's call and spends the look; `--preflight` is outcome-blind and spends nothing. Preflight on the VPS 2026-11-06 10:32 UTC: 1079 held-out briefed label rows (779 `ok`, 297 immature, and one row each for `no_close_at_horizon`, `no_open` and `split_unchecked`), 123 brief parquets, THE PANEL 385 ticker-episodes in 63 arrival clusters against floors of 300 episodes and 30 clusters — "the panel clears both floors" (206 episodes / 33 clusters on 2026-09-24). Memo `docs/research/atr_confirmation_prereg_2026_09.md`. |
| `a20_power.py` | Is #1227 powered, and if not, from what date? | Outcome-blind, not a look, no ledger charge: the held-out side contributes STRUCTURE only through an explicit column allowlist, and the effect sizes come from the burnt discovery panel. Measured 2026-11-06, `--population briefed --family-size 1`: ATR power at the gate 91.5% ± 1.4% against the 80% bar, at seed 20260922 with n_sims 400 (quote both beside the figure), and 92.2% ± 1.3% at seed 7 with the same n_sims, panel and family, on burnt 198 episodes and held-out 398 episodes in 64 clusters, accrual 0.941 clusters/session, sd(y) 0.1681, icc 0.05. On 2026-09-24 the corrected figures were 66.7% (family of one) and 46.2% (Holm across three). The 398/64 here and the 385/63 above are the same panel: the confirmation script inner-joins the brief store and this module does not. Its own opening paragraph still scopes #1227 as three signals under Holm, and `--family-size` still defaults to that family; the registration narrowed to a family of one on 2026-09-23, so `--family-size 1` is what reproduces the figure above — left for #1227's own decision, like the stale figure in `a20_overlap_power.py`. It keeps no "Last run" block (see Running) while #1227's look is unspent: this row and `a20_power_preflight_2026_09.md` §8 are the snapshot. |
| `a20_overlap_power.py` | Size before power: does the #1227 inference survive the 20-session overlap, when adjacent arrivals share up to 19 of their 20 outcome sessions? | Outcome-blind simulator, no charge. Re-confirmed on the corrected panel at 4.2-6.1% rejection against a nominal 5% and a pre-specified 7.5% bar (`docs/research/a20_overlap_inference_2026_09.md`); #1227 records it as a cleared pre-run gate. Its own WHY THIS EXISTS section still quotes the WITHDRAWN 83.9% power figure — read the ledger's 2026-09-24 correction instead. |
| `split_adjustment_gate.py` | Can a corporate action reach the #1227 estimand, and does the label's split guard see it? | PREMISE WITHDRAWN: the ATR bars are one uniformly adjusted yfinance fetch, so the "raw ATR against an adjusted label" exposure does not exist; the patchwork is the LABEL store (one parquet per session, adjusted as of its own fetch date), where a split leaves exactly one unadjusted step. The band hole is real and is NOT closed: a 3-for-2 leaves a step of 0.667, inside [0.55, 1.8], so neither the label status nor the beta-window drop fires; on the measured panel the band is empty, which is luck rather than design. Memo `docs/research/atr_split_adjustment_gate_2026_09.md`. |
| `2026_09_sel_ar_20_burnt_probe.py` | With ATR and the MA50 extension already in the model, does any NEWS-axis term carry an independent coefficient on the BURNT panel (`catalyst_age_h`, `catalyst_strength`, `technical_ma200_slope_pct_per_day`; the 2026-10-01 variant swapped in the two LLM self-grades)? | RUN 2026-09-29 (second run) — NULL on the news axis: 391 pre-dedup rows collapse to 198 ticker-episodes in 26 arrival clusters, 194 complete cases, no candidate near the 0.0167 reference line. Ledger §4.1 rows 2026-09-29 and 2026-10-01, charge 0 (exploration). The 2026-10-01 variant adds the power reading the first run lacked: MDE 0.167 and 0.200 standardised against the 0.10 floor, so it reports absence of evidence and deliberately avoids the word null. The FIRST run was DISCARDED: it recomputed the arrival anchor with `session_on_or_after` instead of reading `anchor_session`, which disagrees on 296 of 466 burnt rows and made 65 articles look published after their own anchor open (rule 13). |
| `2026_10_jev_feature_layer.py` | BUILDER, not an exercise: turn the stored news and brief candidates into calibrated System One features, replacing hand-chosen text-to-number mappings (`EVENT_TYPE_TIER`, the extractor self-grade, the `soi_count / 5` term, the mapper self-grade) | Reads NO outcome column, so not a look and no ledger charge. Writes `~/.alphalens/jev_features/{article,candidate}/<date>.parquet` under a `jev_feature_version` token. Body cap 32000 chars, MEASURED (gain AUC 0.739 / 0.767 / 0.798 / 0.783 at 2k / 8k / 32k / 100k; the easy question flat at 0.847). Whole history cost $2.894739 for the article leg (23826 rows) plus $0.047553 for the candidate leg (1120 rows) under the current version token, measured from the vendor's own `jev_cost_usd` on 2026-11-06; the docstring's $1.21 was a pre-run projection of the ARTICLE leg alone, so 2.4x too low against that leg's $2.894739, because it counted article bodies and not the re-sent question documents. Smoke-run 2026-10-01; the whole history HAS since been built — the VPS store holds 136 `article` and 134 `candidate` parquets, newest 2026-09-30 (read 2026-11-06), and the module's title cap and transport-retry notes are measurements from that run (23823 article calls, one non-transport give-up). Dates after 2026-09-30 are not built. The FIRST join of these columns to `sel_ar_20` IS a look and needs its own row. |
| `2026_10_jev_discovery_burnt.py` | Do the six System One columns carry a coefficient on the BURNT panel beside ATR and the MA50 extension — and crucially, is there enough power to detect one? | RUN 2026-10-01; ledger row 2026-10-01 (b) was written BEFORE the run and charges 0. The purpose was an EFFECT SIZE for a power simulation, not a finding — a confirmation cannot be registered without one — and the deliverable is the per-candidate minimum detectable effect against the 0.10 floor. 198 episodes / 26 arrival clusters, 195 complete cases: NO candidate carries a coefficient and NOT ONE IS POWERED (standardised betas −0.026 to +0.040, p 0.49 to 0.84, MDE 0.123 to 0.251 against that 0.10 floor). Controls reproduce the published burnt table (ATR −0.347 vs −0.378, MA50 −0.327 vs −0.303), so the instrument works. From the best MDE: ~295 episodes to test a true 0.10, ~1180 for 0.05, ~3278 for the observed ~0.03. A first run was DISCARDED — the candidate join matched 0 of 391 rows because the builder wrote `brief_date` as a string while the label store carries `datetime.date`. Registering is still not the next step while #1227 has not run. |
