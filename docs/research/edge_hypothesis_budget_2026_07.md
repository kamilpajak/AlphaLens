# EDGE signal-promotion — program-lifetime hypothesis budget

**Status:** LIVE ledger (append-only). Opened 2026-07-12.
**Purpose:** bound the *program-lifetime* multiple-comparisons exposure of the "log-lots / use-little / validate-forward" approach, which per-sweep Bonferroni does **not** control.
**Parent:** `edge_signal_attribution_2026_07_06.md` (the July re-run: 284 tests, α = 0.05/284 = 1.76e-4, 16 collinearity-dedup clusters). Adversarial-review origin: the 4-lens critique of 2026-07-12 (risk #1, cross-sweep peeking).

---

## 1. The hole this closes

The EDGE attribution re-runs (June, July, planned Aug/Sep) each apply their **own** Bonferroni denominator to the **same accumulating outcome panel** (`population_ladders ⋈ thematic_briefs`, currently 50 brief-dates 2026-04-14..07-05). Re-testing the same ~16 signal clusters on a panel that is the previous panel + 2-4 weeks of new ticker-episodes is **optional-stopping / repeated-peeking**: within-sweep Bonferroni (0.05/284) is genuine but does nothing against looking again as N grows. Across sweeps the true family is thousands of tests; no denominator ever counts them together. That is the mechanism by which *some* null eventually crosses once and looks "confirmed on forward data" while each sweep looks disciplined.

This ledger makes the family **program-wide, fixed, and auditable**.

## 2. The rules (binding on every future EDGE signal test)

1. **Fixed family = the 16 clusters in §3.** These are the promotable hypothesis slots. A new stampable signal (e.g. `options_*`, `market_state_*`, `grounded_in_*_news`) is NOT a free extra test — before its first look it is added to §3 as a new cluster row, which raises the program denominator for everyone. No look happens off-ledger.
2. **α-spending across sweeps, not per-sweep.** Each cluster carries a lifetime α-slice; the program family counts cluster-looks across **all** sweeps. A cluster looked at in June + July has already spent looks; its August look draws on remaining budget (α/(planned looks), O'Brien-Fleming-style), never a fresh 0.05/N. Report cumulative-tests-across-sweeps + the implied family-wise bound at every sweep. Use Benjamini-Hochberg/BY FDR at the program level for the exploratory scan; keep Bonferroni **only** for the final promote gate (rule 3).
3. **Discovery ≠ confirmation.** The panel up to **2026-07-05 is DISCOVERY** (frozen here). Promotion of any cluster into brief SELECTION or ORDERING requires clearing a **pre-registered** threshold on ticker-episodes accumulated **after** the cluster's first-look date, tested **exactly once**. This is what makes "first-look, not verdict" statistically real rather than rhetorical.
4. **Retirement + re-look cap.** A cluster that clears a first-look but fails its held-out confirmation is **retired** — it does NOT get re-tested in later sweeps (extends the existing "clean nulls, don't re-test" discipline from within-sweep to across-sweep). Max **2** re-looks per cluster; each row carries a **sunset date** ("kill by <date> if N<30 or first-look still null").
5. **Unit = ticker-episode.** All first-look p-values are computed with errors clustered by ticker-episode (or one row per episode) from the start (§6 doctrine of the July memo, made default). Effective N = distinct ticker-episodes, reported on every finding; row-level / day-level p-values never appear in a promotion decision.
6. **One primary horizon per cluster.** Pre-register a single decision horizon (default **car_10**); car_5 / car_20 are descriptive only and are NOT counted as separate tests (closes the "clears on *some* horizon" fork).

## 3. The 16 clusters — fixed α slots (verdicts from the July re-run)

α slice = 1.76e-4 program Bonferroni charge per cluster (one charge, not per member). `B-clear` = raw p < 1.76e-4 on the primary horizon AND verification-robust.

| # | Cluster | Primary signal | July verdict | B-clear | Primary horizon | Looks used | Status | Sunset |
|---|---------|----------------|--------------|---------|-----------------|-----------|--------|--------|
| 1 | ATR / choppiness | `technical_atr_pct` | ROBUST | ✓ (5,10,20) | car_10 | 2 (Jun,Jul) | **held — anchor separator** | — |
| 2 | MA50 extension / overbought | `technical_ma50_distance_pct` (+`technical_rsi`) | ROBUST, ATR-orthogonal | ✓ (car_10) | car_10 | 2 | **held — 2nd axis candidate** | — |
| 3 | 52w-low extension | `technical_pct_off_52w_low` | SUGGESTIVE (ATR-loaded) | ✗ | car_10 | 2 | monitor | 2026-10 |
| 4 | Quality (ROIC) | `roic_pct` | SPURIOUS (ATR proxy + pseudo-replication) | ✗ | — | 2 | **RETIRED** (fold into ATR; only residual-ROIC×ATR admissible) | done |
| 5 | FCF axis | `valuation_fcf_margin` | ROBUST, not B-clear | ✗ | car_10 | 2 | **Aug pre-reg (1 look budgeted)** | 2026-12 |
| 6 | Anti-value / growth tilt | `valuation_pe` | ROBUST-watch (car_10 only) | ✗ | car_10 | 2 | monitor (watch, don't act) | 2026-12 |
| 7 | Cheapness multiples | `valuation_ps` | SPURIOUS (ATR/space proxy) | ✗ | — | 2 | **RETIRED** | done |
| 8 | Ordering | `layer4_weighted_score` | SUGGESTIVE (low-score avoid filter) | ✗ | car_10 | 2 | monitor (selection_score incremental pending) | 2026-10 |
| 9 | Press gate | `n_gates_passed` ≡ `pass_press` | ROBUST | ✓ | car_10 | 2 | **held** | — |
| 10 | Gate-unknown / insider-coverage proxy | `n_gates_unknown` | ROBUST in-sample, near-extinct under insider-v2 | ✓ (in-sample) | car_10 | 2 | **RETIRED** (no forward utility, 4/165 rows) | done |
| 11 | Catalyst strength | `catalyst_strength` | SUGGESTIVE (car_10 slow-burn) | ✗ | car_10 | 2 | **re-test scheduled (~40 car_10 days)** | 2026-11 |
| 12 | Event-type / theme / sector categoricals | `catalyst_event_type`, theme, `sector_name` | SPURIOUS (pseudo-replication) | ✗ | — | 2 | **RETIRED** | done |
| 13 | Industry | `industry_name` | SUGGESTIVE (loser arm only; pharma = obesity cluster) | ✗ | car_10 | 2 | monitor | 2026-11 |
| 14 | Insider flow | `insider_score_usd` | SUGGESTIVE (N≈5 events, ticker-dedup null) | ✗ | car_10 | 2 | monitor (needs episodes) | 2026-12 |
| 15 | Experts (panel) | `expert_spread`, `oneil_*`, buffett qual | SPURIOUS or NULL across the board | ✗ | car_10 | 2 | **1 re-look left** (ticker-episode unit ~2026-09); retire if null | 2026-10 |
| 16 | Joint loser flag | ATR-hi × `pct_off_52w_low`-hi (ROIC leg dropped) | SUGGESTIVE (mostly ATR curvature) | ✗ | car_10 | 1 | **forward-log without ROIC leg** (telemetry-only, pre-register before ordering use) | 2026-11 |

**Bonferroni-clear on verified evidence (July): #1 ATR, #2 MA50-extension, #9 press-gate.** These three are the only clusters eligible to skip a first-look confirmation; even they must clear the held-out window (rule 3) before entering SELECTION.

### 3.1 New clusters pending admission (raise the denominator when first looked at)
These are stamped-forward telemetry not yet in the 284-test family. Each gets a §3 row (and re-derives the budget) at its first look — none exists yet:
- **17 (reserved):** `options_*` term-slope / VRP / skew (parquet-only; first-look ~2026-09 at `chain_quality=OK`, N≥30).
- **18 (reserved):** `market_state_*` regime cols (display/telemetry today).
- **19 (reserved):** `grounded_in_{theme,any}_news` / mechanical-vs-LLM proposal (`proposal_shadow`) — see §5.

## 4. Looks-log (append-only)

Every test that draws on a cluster. `looks` = cumulative program looks spent on that cluster after this row.

| Date | Sweep / test | Cluster(s) | Panel version | Horizon | Result | Cluster looks (cum.) | Notes |
|------|--------------|-----------|---------------|---------|--------|---------------------|-------|
| 2026-06-25 | June attribution sweep | 1–16 (partial) | ≤2026-06-23 | car_5/10 | 1 verdict-grade separator (ATR) | 1 each tested | `edge_signal_attribution_2026_06_25.md` |
| 2026-07-06 | July re-run (284 tests, α=1.76e-4) | 1–16 | ≤2026-07-05 (DISCOVERY freeze) | car_5/10/20 | 3 B-clear (#1,#2,#9); ROIC/experts died | 2 each | `edge_signal_attribution_2026_07_06.md` |
| ~2026-08 | Aug re-run (pre-registered) | 5 (FCF-margin, primary) + re-tests 11, 8 | held-out ≥2026-07-06 | car_10 | PENDING | 3 (cluster 5) | 1 budgeted look for #5; re-tests count against #11/#8 budget |
| ~2026-09 | Experts ticker-episode re-look | 15 | held-out | car_10 | PENDING | 3 (cluster 15) — **last re-look** | retire if null |
| — | (future rows appended here) | | | | | | |

## 5. Cross-reference: the mechanical-vs-LLM selection test (own track)

The strongest in-hand lead — a mechanical news-reading rule beating the LLM free-association selection (`proposal_shadow`, design `theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md`) — points at the SELECTION layer, not a display cluster. It is pre-registered as its **own** forward test (reserved cluster 19), NOT folded into the general telemetry sweep: primary horizon car_10, ticker-episode clustering, a numeric kill line committed **before** looking (the H=10-flips-positive ⇒ regime-not-mechanism kill holds even against a live H=21 positive), plus a size-bracket gate requiring the edge to hold within the tool's own $500M–$10B universe (a mega-cap attention artifact is out of scope). First powered look ~2026-09+.

## 6. What this ledger does NOT change

Left exactly as designed (the discipline is right): the SELECTION / ORDERING / DISPLAY enforced-disjoint split; stamp-now-with-`config_version`; the N≥30-forward promotion gate and the refusal to promote the in-sample H=21 mechanical finding; the attribution machinery (Bonferroni/284, one charge per collinear cluster, partial-Spearman vs ATR, ticker-episode unit). The fix is to **extend** multiplicity control across sweeps (this ledger), not to weaken any of the above.
