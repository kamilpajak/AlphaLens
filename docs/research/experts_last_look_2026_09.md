# Experts × EDGE calibration — pre-registration of the final cluster-15 look

**Status:** REGISTERED (frozen 2026-09-01). Run window: **2026-09-29/30** (the
cluster's 2026-10 sunset). Results: §6 (placeholder until the run).
**Script (canonical spec):** `apps/alphalens-research/scripts/ml/2026_09_experts_last_look.py`
— the module docstring is the frozen registration; this memo carries the
rationale, the outcome-blind sample measurements, the power table and the
review trail.
**Ledger:** fills the PENDING §4 row of
[`edge_hypothesis_budget_2026_07.md`](edge_hypothesis_budget_2026_07.md)
("Experts ticker-episode re-look | cluster 15 | held-out | car_10 | **last
re-look** | retire if null"). Closes epic **#541** (the calibration was the
epic's stated purpose: "log now, decide weights/sort-slot later").

## 1. Why this study, why now

The expert panel (Buffett value/quality + O'Neil momentum + the disagreement
scalar) has been stamped on every brief since 2026-06-11 under the log-now
discipline, display-only, pending an Expert×EDGE calibration at N≥30 matured
outcomes. On 2026-09-01 the data condition was measured MET (matured
panel-stamped episodes: 370 / 63 brief-date clusters, all stamps under a
single `panel_config_version`). Cluster 15 spent 2 of its 3 lifetime looks in
the June/July sweeps (verdict: "SPURIOUS or NULL across the board", at a
row unit later shown to be pseudo-replicated ~2:1). This is the third and
final look: honest ticker-episode unit, held-out panel (≥ 2026-07-06) that no
prior analysis has touched.

## 2. Timing: register now, run 2026-09-29/30

One irreversible shot → maximize power inside the sunset. Measured on
2026-09-01: ~150 post-dedup episodes / ~30 arrival-session clusters today
(80%-power detectable |ρ| ≈ 0.40 at the family bar) vs ~240 / ~50 at the
sunset window (|ρ| ≈ 0.32). Both the internal design review and the external
statistical review (Perplexity, 2026-09-01) independently recommended
waiting: "the additional observations are not merely more rows; they add
roughly 20 independent arrival-session clusters — that is the part of the
design that matters." The registration commit merged to `main` weeks before
the run is the tamper-proof pre-commitment; the script refuses `--run`
before 2026-09-29 without a logged deviation flag.

## 3. Outcome-blind sample measurements (2026-09-01, VPS stores)

No feature-vs-outcome statistic was computed. Counts only:

- Held-out (brief_date ≥ 2026-07-06, `panel-v1r-absdiff-2x` only — the sole
  version present; `buffett_qual_config_version` = `buffett-pre-registry-v0`,
  also sole): **186 matured-ladder episodes / 40 brief-dates / 98 tickers**
  (2026-07-06 → 2026-08-23). The car_10 panel at run time will be larger
  (car_10 needs only grouped-store maturity) and then shrinks under
  `ticker_episode_dedup`.
- Note: the percentages below are from the matured-LADDER join (the only
  sample measurable on the VPS that day); the car_10 preflight panel (§5) is
  a different, slightly smaller cut, so its coverage differs by a few points
  per column. The registration decisions (exclusions, floors) key off both.
- Coverage (held-out, per column): O'Neil price terms 99-100%,
  `oneil_score` 98%, qual pillars 95%, `buffett_roic_latest` 68%,
  `oneil_earnings_growth_yoy_pct` 61%, `buffett_quality_score` 53%,
  `expert_spread` 51%, `buffett_roic_3y_avg` 48%, `magic_formula_rank` 32%,
  `buffett_owner_earnings_yield_pct` 18%, `buffett_margin_of_safety_pct` 12%.
- Consequences frozen at registration: margin_of_safety and
  owner_earnings_yield are **excluded as not estimable** (cannot meet the
  ≥50-episode / ≥15-cluster floor); they are closed UNTESTED, never "null".
  The `magic_formula_rank` collider control is a veto-sensitivity, not a
  primary covariate (32% coverage cannot carry a primary).

## 4. Design summary (canonical text in the script docstring)

- **Family = 7 tests, Bonferroni bar 0.05/7 ≈ 0.00714** — six per-member
  cluster-OLS/WCB tests (quality_score, roic_3y_avg-as-residual,
  expert_spread, earnings_growth+mfr-veto, candor ordinal, understandable)
  + one elastic-net model-vs-ATR test. One program charge (cluster 15).
- Outcome: continuous car_10; clusters = arrival sessions; split guard;
  PIT guard on qual timestamps; prior clean nulls not re-tested.
- Model: 7 features (ATR control + 4 O'Neil numerics + 2 qual encodings),
  **purged** contiguous-session-block folds (outcome-window overlap dropped
  from training — pre-hoc deviation from the July template, adopted from the
  external review because the label-overlap leak asymmetrically favors the
  fitted model over the fit-free ATR baseline), pre-committed α grid
  {0.05, 0.15, 0.5}×sd(y) with primary 0.15, degenerate-fold null rule,
  B=10,000 cluster bootstrap.
- Verification battery with numeric thresholds (reproduce, LOBO/LOTO
  worst-case p<0.05, ATR-partialled Spearman, ticker-collapse ≥50%
  magnitude, car_5/20 sign notes); verdict printed by code.
- **Verdict language (three-way, frozen):** cleared → association under the
  registered estimand; not cleared with the 99.286% partial-Spearman CI still
  covering |ρ| ≥ 0.10 → *inconclusive, family retired operationally*; CI
  inside (−0.10, +0.10) → evidence against actionable effects as
  instrumented. Equivalence bound Δ = |ρ| 0.10 = smallest actionable effect.
  Retirement is a resource-allocation stop rule — the results section must
  never render an underpowered null as "experts are useless".
- **Budget hygiene:** realized_r stays descriptive (whole-panel composition
  only; signal-conditioned realized_r inference would be a separate §4.1
  charge). No LightGBM/boosting (an unbudgeted extra look at this N).

## 5. Preflight (outcome-blind diagnostics + simulated power)

Output of `--preflight` (registration-time; to be re-run at the run date for
the final sample):

```
panel: 171 episodes | 29 arrival-session clusters (39 brief-dates) | 147 tickers | 2026-07-06 -> 2026-08-15
guards: split-dropped 0 | car_10-missing 0 | immature 126 | qual PIT-nulled rows 0
cluster sizes: mean 5.9 | max 18 (11% of episodes) | cv 0.72
coverage (post-dedup complete-case episodes / clusters):
  buffett_quality_score                90 ep /  27 clusters
  buffett_roic_3y_avg                  81 ep /  24 clusters
  expert_spread                        89 ep /  27 clusters
  oneil_earnings_growth_yoy_pct        99 ep /  25 clusters
  candor_ord                          165 ep /  29 clusters
  understandable_f                    168 ep /  29 clusters
  [model] technical_atr_pct           100%
  [model] oneil_pct_off_52w_high      100%
  [model] oneil_ma200_slope_pct_per_day  100%
  [model] oneil_ma200_distance_pct    100%
  [model] oneil_earnings_growth_yoy_pct   58%
  [model] candor_ord                   96%
  [model] understandable_f             98%
  mfr-veto subset (earnings & mfr & atr)    46 ep /  15 clusters
power sim inputs: discovery sd(y)=0.1922 icc=0.00 | held-out clusters=29 episodes=171
  rho=0.1: P(clear family bar) ~ 4%  (300 sims)
  rho=0.2: P(clear family bar) ~ 33%  (300 sims)
  rho=0.3: P(clear family bar) ~ 76%  (300 sims)
  rho=0.4: P(clear family bar) ~ 98%  (300 sims)
```

Registration-time notes (all outcome-blind):
- The first preflight draft nulled 261 qual rows: the PIT guard compared the
  qual stamp date against ARRIVAL, while the enrich convention stamps every
  brief the NEXT morning pre-open (`computed_at − brief_date == +1` on
  478/478 held-out rows). The guard was amended BEFORE registration to flag
  only stamps later than brief_date+1 (out-of-convention backfills); the
  standard D+1-pre-open timing is disclosed as limitation §8.3.
- `oneil_earnings_growth_yoy_pct` measures 58% on the car_10 panel (61% on
  the matured-ladder join); retained in the model deliberately (it is also
  family member 4; imputation is inside the fold pipeline).
- The mfr-veto subset sits at 46 episodes / 15 clusters today (floor: 50/15);
  four more weeks of accrual should clear the floor — if not, the veto's
  conservative-null branch applies by construction.
- Simulated power uses the burnt discovery panel's outcome scale (sd 0.192,
  ICC ≈ 0 by brief-date clusters) with today's held-out cluster structure;
  the run-date panel (~240 episodes / ~50 clusters) will sit above these
  numbers. Caveat: the discovery scale is measured on the stored
  `market_excess_return` (position-window), not car_10 — the two are close in
  scale but not identical, so the power table is indicative, not exact.

Interpretation guardrail: the look can only rescue a large effect; a real but
moderate |ρ| ≈ 0.2 signal will most likely NOT clear and the family will
retire with an *inconclusive* label. That asymmetry is the accepted price of
"retire if null" at the look cap, chosen over the alternative (optional
stopping / monthly peeking), which manufactures false discoveries.

## 6. Results (placeholder — filled by the results commit, which must not touch executable code)

- Part A per-member table: PENDING
- Part B model vs ATR: PENDING
- Verification: PENDING
- VERDICT: PENDING
- Deviations log: PENDING (empty = none)

## 7. Review trail

- Internal adversarial design review (Plan agent, 2026-09-01): 11 findings,
  all resolved at registration — single family bar across members AND model;
  coverage-based exclusions moved to registration (outcome-blind); mfr
  control converted to a veto with a feasibility floor; LightGBM cut;
  degenerate-fold rule; numeric verification thresholds; abort clause;
  arrival-session (not brief-date) clustering throughout.
- External statistical review (Perplexity `reason`, 2026-09-01): recommended
  waiting for the sunset window (adopted); purged CV (adopted); B→10,000
  (adopted); cluster-size distribution reporting (adopted); simulation-based
  power (adopted, §5); three-way verdict language + equivalence bound
  (adopted); Westfall–Young/max-T instead of Bonferroni (rejected — house
  standard is Bonferroni, "simplest and hardest to attack" per the review
  itself); full rolling-origin CV (rejected — at ~60 held-out sessions it
  destroys most training data; the purge addresses the identified leak);
  two-way session×sector clustering (rejected — LOTO worst-case covers the
  theme dependence at this N).

## 8. Known limitations (frozen wording)

1. **Screened-population estimand** — results generalize only to stocks that
   entered the candidate pipeline, not to the market.
2. **car_10 is an active return** (β=1 vs SPY, split-adjusted closes, no
   dividends) — consistent with every prior sweep; a market-model residual is
   a possible secondary in future studies, never added post-hoc here.
3. **LLM-derived features carry non-classical measurement error** under one
   frozen prompt regime (`buffett-pre-registry-v0`); the candor 0/1/2
   encoding assumes equal spacing. Timing: qual verdicts are stamped the next
   morning PRE-OPEN (D+1 00:30–08:30 UTC), while the car_10 window includes
   day D — the `--scuttlebutt` web channel could in principle embed day-0
   news (same day-0-overlap class as every brief feature, which are computed
   from day-D closes). A null on candor/understandable is a null for THIS
   instrument, not for the qualitative construct.
4. Cluster sizes are unequal (one hot session can dominate); the run prints
   the size distribution and the LOBO worst-case exists precisely for this.
