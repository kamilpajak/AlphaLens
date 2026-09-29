# Experts × EDGE calibration — pre-registration of the final cluster-15 look

**Status:** RUN AND CLOSED. Registered (frozen) 2026-09-01, run 2026-09-29
inside the registered window. Verdict **RETIRE** — see §6. Cluster 15 has now
spent all 3 of its lifetime looks.
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

## 5b. Pre-hoc amendments recorded on 2026-09-29, before the run

Two facts appeared after the 2026-09-01 freeze. Both were found by the
outcome-blind preflight on the run date, both are recorded here and on #541
before any feature-vs-outcome statistic was computed, and neither costs a
charge (the abort clause covers join integrity and population composition).

**A. The join tripwire was widened by three names.** `population_ladders` and
`thematic_briefs` both gained `source` and `event_overlap` (#1307 / #1340,
2026-09-06) and `brief_published_at` (#1482, 2026-09-16). The tripwire asserts
that the two stores share only the join keys, because a new shared non-key
column would be silently suffixed by a `pandas.merge`. This script performs no
merge — it reads the brief side through `bix.loc[(brief_date, ticker)]` on an
explicitly selected `BRIEF_COLS` list, and none of the three names is in that
list — so the hazard cannot reach the panel. The allowlist now names the three
columns, and a positive control keeps it from rotting into "admit anything".

**B. The insider-cluster event lane is excluded.** The lane (#1307, #1340) went
live on 2026-09-06, five days after the freeze, and lands on the same
`(brief_date, ticker)` key as the thematic lane. The frozen PANEL text carried
no source filter only because a single lane existed on 2026-09-01. Counted
outcome-blind on the run-date store:

| lane | matured rows joining a brief |
|---|---|
| thematic | 548 |
| insider_cluster | 6 |

Six rows, six distinct tickers, four brief dates (2026-09-05, 09-09, 09-10,
09-11). The owner chose to
**restrict the panel to `source == "thematic"`**, keeping the estimand the one
that was designed and reviewed (the thematic screened candidate population)
rather than silently widening it to two screeners for a 1% row gain. A row
carrying no lane stamp is dropped too and counted, because a missing stamp
cannot be assumed thematic.

### Preflight on the run date, after the filter

```
panel: 260 episodes | 47 arrival-session clusters (62 brief-dates) | 205 tickers | 2026-07-06 -> 2026-09-14
guards: split-dropped 0 | car_10-missing 0 | immature 124 | qual PIT-nulled rows 0
lane filter: kept source == 'thematic' | dropped 18 rows {'insider_cluster': 18}
cluster sizes: mean 5.5 | max 18 (7% of episodes) | cv 0.68
coverage (post-dedup complete-case episodes / clusters):
  buffett_quality_score               147 ep /  43 clusters
  buffett_roic_3y_avg                 132 ep /  40 clusters
  expert_spread                       146 ep /  43 clusters
  oneil_earnings_growth_yoy_pct       153 ep /  41 clusters
  candor_ord                          251 ep /  47 clusters
  understandable_f                    256 ep /  47 clusters
  [model] technical_atr_pct           100%
  [model] oneil_pct_off_52w_high      100%
  [model] oneil_ma200_slope_pct_per_day  100%
  [model] oneil_ma200_distance_pct    100%
  [model] oneil_earnings_growth_yoy_pct   59%
  [model] candor_ord                   97%
  [model] understandable_f             98%
  mfr-veto subset (earnings & mfr & atr)    70 ep /  24 clusters
power sim inputs: discovery sd(y)=0.1994 icc=0.01 | held-out clusters=47 episodes=260
  rho=0.1: P(clear family bar) ~ 8%  (300 sims)
  rho=0.2: P(clear family bar) ~ 59%  (300 sims)
  rho=0.3: P(clear family bar) ~ 96%  (300 sims)
  rho=0.4: P(clear family bar) ~ 100%  (300 sims)
```

**Two lane counts appear, and they measure different cuts.** The diagnostics
line reports 18 because the filter runs on the held-out *plannable* rows, where
all 18 insider-cluster rows sit. Only 6 of those reach the matured panel; the
other 12 have brief dates from 2026-09-17 onward and were excluded by calendar
maturity anyway. The cost of the decision is therefore the 6 rows, and the
panel confirms it exactly: 266 episodes before the filter, 260 after, with the
cluster count unchanged at 47.

Every feasibility floor (>= 50 episodes and >= 15 arrival-session clusters) is
met after the filter, including the `magic_formula_rank` veto subset, which was
46 / 15 at registration and is 70 / 24 now. Simulated power is unchanged to
within the simulation noise.

Compared against the registration-time preflight (§5): 171 -> 260 episodes and
29 -> 47 clusters, against the ~240 / ~50 that justified waiting for the sunset
window.

The registered script was NOT edited to obtain any number in §5 — those came
from a scratch copy with the tripwire alone widened. The amendments above
landed as their own commit before the run, which is why §6 can still be filled
by a results commit that touches no executable code.

## 6. Results (run 2026-09-29, single execution, verdict printed by the code)

Panel as it went in: **260 episodes / 47 arrival-session clusters / 205
tickers**, brief dates 2026-07-06 → 2026-09-14. Guards: 0 split-dropped, 0
car_10-missing, 124 immature, 0 PIT-nulled, 18 held-out plannable rows dropped
by the lane filter (of which 6 would have reached the matured panel).

### Part A — six per-member tests, family bar 0.00714

| member | n | clusters | beta | t_cr2 | p_wcb | partial rho | 99.286% CI |
|---|---|---|---|---|---|---|---|
| `buffett_quality_score` | 147 | 43 | +0.0007 | +1.09 | 0.3003 | −0.030 | [−0.307, +0.239] |
| `buffett_roic_3y_avg` (residual-vs-ATR) | 132 | 40 | +0.0001 | +0.93 | 0.3649 | +0.128 | [−0.138, +0.362] |
| `expert_spread` | 146 | 43 | +0.0001 | +0.46 | 0.6492 | +0.018 | [−0.191, +0.230] |
| `oneil_earnings_growth_yoy_pct` | 153 | 41 | +0.0000 | +0.88 | 0.3840 | +0.071 | [−0.182, +0.287] |
| `management_candor` (ordinal) | 251 | 47 | −0.0040 | −0.29 | 0.7732 | +0.052 | [−0.156, +0.260] |
| `understandable` (0/1) | 256 | 47 | −0.0101 | −1.10 | 0.5670 | −0.105 | [−0.316, +0.120] |

No member came within an order of magnitude of the bar; the smallest p is
0.30. Every member met its feasibility floor, including the
`magic_formula_rank` veto subset (70 episodes / 24 clusters against 50 / 15),
so the mfr veto never had to fall back to its conservative-null branch and
member 4 was tested as designed.

### Part B — elastic net vs the fixed −ATR baseline, purged block folds

Purge counts per fold (train / purged / validation): 126/58/76, 104/93/63,
114/110/36, 151/54/55, 184/46/30.

| alpha | pooled OOF rank Spearman | note |
|---|---|---|
| 0.0051 (0.05 × sd) | +0.011 | descriptive sensitivity |
| **0.0154 (0.15 × sd)** | **−0.036** | **PRIMARY, pre-committed** |
| 0.0514 (0.5 × sd) | −0.003 | degenerate in 4 of 5 folds |

Baseline −ATR pooled rank-within-fold Spearman **+0.089**. Model minus
baseline **delta = −0.125**, cluster-bootstrap two-sided **p = 0.0666**,
99.286% CI [−0.304, +0.055], 0 of 10,000 draws skipped. The delta is
negative: the panel-feature model ranked car_10 *worse* than the fit-free ATR
baseline. It does not clear the bar in either direction.

### Verification battery

Not run. It is defined only for a clearing member or a clearing model, and
nothing cleared.

### VERDICT — RETIRE, and the pre-committed language that goes with it

`VERDICT: RETIRE — cluster 15 closed (operational stop rule).`

Which of the three pre-committed conclusions applies is decided per member by
the CI, not by the analyst. **Every one of the six CIs still covers
|ρ| ≥ 0.10**, so branch (ii) applies across the board:

> inconclusive; family retired OPERATIONALLY, not scientifically falsified.

Branch (iii) — *evidence against actionable effects* — requires a CI lying
entirely inside (−0.10, +0.10), and no member produced one. **This result is
not evidence that the expert panel carries no signal.** It is a decision to
stop spending looks on it. The simulated power recorded in §5b is the reason:
a true |ρ| of 0.2 would have cleared the family bar only about 59% of the
time, so a real but moderate effect was always more likely than not to end
here. That asymmetry was accepted at registration as the price of a hard look
cap over monthly peeking.

Consequence, as registered: the experts stay **display-only**. Nothing enters
`_BRIEF_SORT_KEYS` or the selection funnel. No promotion object was frozen,
because a promotion object is created only on SURVIVES.

### Descriptive (whole panel, never cut by any signal)

`ladder_classification`: NO_FILL 67, OPEN 51, TIME_STOP 47, TP_FULL 46,
PARTIAL_TP_OPEN 33, SL_HIT 12, PARTIAL_TP_THEN_SL 4.
`realized_r`: n=109, mean +0.222, median +0.298.
`market_excess_return` present on 260 rows; its sign differs from car_10 on 61.

These are composition figures only. Cutting any of them by a signal would be a
separate §4.1 charge and was not done.

### Deviations log

Two, both pre-hoc, both recorded in §5b and on #541 before the run, both
merged as PR #1615 ahead of the run commit:

1. The join tripwire allowlist was widened by three columns the two stores
   gained after the freeze.
2. The panel was restricted to `source == "thematic"`, excluding the
   insider-cluster event lane that went live five days after the freeze.

No deviation was made after any feature-vs-outcome statistic was computed. The
run was a single execution; there was no second run, no alpha rescue, no
horizon shopping and no re-encoding.

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
