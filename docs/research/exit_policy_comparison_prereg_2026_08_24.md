# Exit policy comparison — brief take-profit ladder vs the ATR bracket — pre-registration

**Status:** LOCKED
**Date:** 2026-08-24 (drafted against the #1112 incident record; committed 2026-08-25)
**Author:** Kamil Pajak
**Issue:** #1115. Motivating incident: #1112. Instrument prerequisite: #1114 / PR #1117.
**Design ancestors:** `bezpazery_lens_design_2026_07_16.md` (the bracket geometry and its
2026-08-24 anchor amendment), `exit_geometry_reward_risk_2026_06_30.md` §7 (the ~2026-09
exit walk-forward this look charges), `broker_sizing_declared_frame_design_2026_08_12.md`
(the live sizing frame).
**Evidence tier:** forward, confirmatory-eligible. The cohort opens **after** this memo is
locked; no outcome in it exists at lock time. The exploratory read quoted in issue #1115 is
**not** part of the sample (§3.4).
**Ledger:** one policy look, `edge_hypothesis_budget_2026_07.md` §4.1 annex. The row is
quoted verbatim in §9 and appended in the first implementation PR, before any look.

---

## 1. The question, stated precisely

Issue #1112 ended with an open question that is bigger than the anchor bug that produced it:
**should the live rail place the research take-profit levels the brief publishes, or a single
volatility-derived bracket, with the brief levels reduced to display and measurement?**

The question is not settled by argument, and it is not settled by the exploratory read that
prompted it (§3). It needs a measurement whose result can be believed, specified before any
outcome is looked at. This memo is that specification.

One correction to the framing in #1115 before anything else, because it changes what is being
compared. Since #1112 step 3 landed, the live take-profit is **not** a pure ATR bracket:
`paper/sizing.py::build_exit_geometry_spec` raises the bracket target to the brief's own first
take-profit tranche whenever the bracket lands below it, and `clamp_reanchor_target` enforces
the mirror rule on the stop. The live policy therefore already consumes one of the brief's
research levels. The real contrast is:

* **Arm A — the brief's ladder.** Up to three staged take-profit tranches at the levels the
  research published, plus the brief's disaster stop.
* **Arm B — the live operational policy.** One 100% target at
  `max(planned-anchor ATR bracket target, brief first tranche)`, a stop that starts at
  `planned blend - 1.5 x ATR` and re-anchors to the realised fill on fill, never below the
  brief's disaster stop, plus the declared behaviour for the cases where that geometry cannot
  be built (§5.3).

So the question is really: **stage the exit across three research levels, or take the whole
position off at one volatility-derived level floored at the first research level.** The rest
of this memo uses "arm A" and "arm B" in exactly this sense.

## 2. Why the existing read cannot answer it

Issue #1115 records an ad-hoc read over 97 sessions (2026-05-19..2026-08-23): 919 rows, 536
matured, 300 with both arms, paired mean difference +0.157 R in favour of the bracket, paired
t = 3.73. Those figures are quoted here as the **provenance of the question**, not as evidence,
and they are not re-derived in this memo. They are also provisional in the ordinary way brief
parquets are provisional: the store is rewritten on all six daily slots, so a count from one
read is a count as of that slot (`feedback_single_day_sample_is_provisional_2026_08_20`).

Four defects were raised against that read. Each is sufficient on its own, each is carried
forward here, and §4-§7 say what this design does about it.

### 2.1 The p-values assume 300 independent pairs

They are not independent. About 3.1 candidates share each brief day and therefore share that
day's market move; holding windows of 42 sessions overlap almost completely across a
three-month span; the whole span is one regime. Under plausible within-day correlation the
t-statistic falls to roughly 2.1-3.4, and if the effective number of independent market
episodes is a few dozen it falls to roughly 1.0-1.5, at which nothing survives.

Pairing on the same price path does **not** fix this. Pairing removes the *level* of the
market move, but the quantity being estimated is precisely the *interaction* between the
market move and the two exit geometries: a market-wide rally is what makes arm B hit its close
target while arm A keeps running. That interaction is common to every candidate on the same
day and to every candidate whose window overlaps. §6 is built around this.

### 2.2 Arm B's R is bounded and arm A's is not

Under the bracket, stop and target sit the same distance from the same anchor, so realised R
lives in [-1, +1] by construction; the observed maximum was exactly +1.00. Arm A reached
+3.33. A difference of means across a bounded and an unbounded arm mixes target accessibility,
payoff cap, stop distance and tranche weighting into one number.

**Medians and win-rates make this worse, not better.** Both reward the closer target by
construction: a policy that takes profit sooner wins more often and has a higher median almost
by definition, and pays for it only in a tail that the median and the win-rate are designed not
to see. Neither may be the primary contrast, and §8.2 pre-commits that they are reported only
beside the full distribution, never as a verdict.

The fix is not a better summary statistic on the R scale. R itself is the problem: the
denominators differ between the arms (arm A's risk is `blended entry - disaster stop`, arm B's
is `1.5 x ATR`), so the two "R"s are different units wearing the same name. §5.4 replaces the
scale.

### 2.3 236 of 536 matured candidates had no arm B

44% missing, for reasons — missing ATR, nothing fills in the first walk, bracket not
constructible — that are mechanically related to the outcome arm B would have had. This is a
selection mechanism, not empty cells, and #1114 made it worse in a specific way worth naming:
the bracket-constructibility gates run **after** the anchor is chosen and are anchor-dependent,
so the planned-anchor and realised-anchor lenses null on **different rows, not at random**
(`bezpazery_lens_design_2026_07_16.md` §7.5).

Two of the three reasons are handled by moving them out of the missingness problem and into
the treatment (§5.2, §5.3): a candidate that does not fill has a defined outcome under both
arms (zero), and a candidate whose bracket cannot be built has a defined outcome under arm B
(the declared fallback). Only the genuinely pre-outcome reasons stay as exclusions, applied
identically to both arms.

### 2.4 Maturity is right-censoring with competing risks

Marking an unresolved position to the last close truncates future gains and future losses, and
favours whichever arm carries more unresolved positions at the cut-off date. Entering the
sample "when it happens to mature" is the same defect seen from the other side. §5.5 removes
it by fixing one horizon for every candidate and closing the cohort a full horizon before the
read, so maturity stops being a selection event.

## 3. Instrument identity

### 3.1 The lens identifiers, verified against the branch

Verified by reading `origin/refactor/1114-lens-anchor-mode-explicit`, not by trusting a report:

| Name | Where | Value |
|---|---|---|
| `alphalens_pipeline.feedback.ladder_replay.ANCHOR_PLANNED` | `ladder_replay.py:59` | `"planned"` |
| `alphalens_pipeline.feedback.ladder_replay.ANCHOR_REALISED` | `ladder_replay.py:64` | `"realised"` |
| `AnchorMode` | `ladder_replay.py:70` | `Literal["planned", "realised"]` |
| `replay_ladder_atr_bracket(..., *, anchor: AnchorMode, ...)` | `ladder_replay.py:742` | required, no default |
| `atr_bracket_anchor(trade_setup, bars, *, anchor)` | `ladder_replay.py:712` | the anchor-price seam |
| lens `atr_bracket_1p5` | `breakeven_lenses.py` | `anchor_mode="realised"` |
| lens `atr_bracket_1p5_planned` | `breakeven_lenses.py` | `anchor_mode="planned"` |

`atr_bracket_1p5_planned` is the anchor the live rail places against. `atr_bracket_1p5` is the
realised-fill anchor and is **not** the live policy; every value it stamped before 2026-08-24
describes the realised-fill anchor, including the read in §2.

### 3.2 Neither lens is arm B

This is the most important sentence in this section. The registered planned-anchor lens still
diverges from the live rail on the take-profit, **deliberately**:
`build_exit_geometry_spec`'s own docstring records that live applies the #1112 step-3 clamp and
"the replay lens does not, on purpose, because clamping there would rewrite the historical
what-if series issues #1114 / #1115 measure against."

The take-profit is not the only divergence. The live rail's stop is **dynamic** — it re-anchors
to the realised fill through `ReanchorOnFill` — while the lens stop is **static** by
pre-registration. So `atr_bracket_1p5_planned` is the correct *anchor* and the wrong *target
rule* and the wrong *stop behaviour*.

Consequence, pre-committed: **the primary contrast cannot be read off the stamped columns.**
Both stamped columns are also R-denominated with incompatible denominators (§2.2). The primary
requires a purpose-built replay (§10.1) that computes both arms in net cash on one scale. The
lenses serve this memo as pre-specified sensitivities (§8.3), not as the primary instrument.

### 3.3 Frozen instrument tokens

The measurement is frozen by four tokens that must be constant across the accrual window:

| Token | Pins |
|---|---|
| `setup_builder_config_version` | the entry ladder and the brief's tranche levels — arm A's entire geometry, and arm B's floor |
| `ladder_config_version` | the replay's entry TTL and fill conventions, shared by both arms |
| the bezpazery v1 parameters (`bezpazery_lens_design_2026_07_16.md` §2) | `stop_atr_mult` 1.5, `tp_atr_mult` 1.5, `tp_floor_frac` 0.006, the 52-week ceiling |
| the cost constants in `broker_contract/costs.py` | `MIN_COMMISSION_USD`, `COMMISSION_RATE`, `FX_ROUND_TRIP_RATE`, `EXIT_EDGE_MIN_BPS` |

A change to any of them ends the accrual window and starts a new cohort, on the same terms
`channel_feature_forward_prereg_2026_08_19.md` §3 sets for its own tokens. A mid-window change
is an amendment committed **before** the change deploys.

### 3.4 The historical span is not in the sample

The 2026-05-19..2026-08-23 span was already looked at, on the question this memo asks. Reusing
it would be the burnt-holdout error the project has written down before: re-testing on data
whose outcome is known does not become clean because the estimator changed. It is used here for
exactly two purposes, both descriptive, both spending no slot:

1. the missingness flow table and the missingness-vs-outcome diagnostic (§7);
2. the planning standard deviation that sets the pair floor (§6.4), read once, before the
   cohort opens.

Neither may carry a verdict word. Use 2 needs a guard, because the dispersion of `d_i` cannot
be computed without computing `d_i`, and `d_i` on the historical span is one `mean()` away from
the answer. **Pre-committed:** the planning-sd script returns only `sd_d`, the pair count and
the cluster counts, and returns no mean, no median, no sign and no per-row `d_i`; a test pins
that its result object carries none of them. The operator reads that output and nothing else.
If the guard is bypassed, the historical read is a look, and this memo's slot is forfeit.

## 4. The estimand — one choice, and why the other two are different questions

Three estimands were available. **The chosen one is the third: the operational policy including
its no-result cases.**

> **Primary estimand.** Over candidates the pipeline ships that satisfy the common feasibility
> rule of §5.1, the expected difference in net cash outcome per candidate, at a fixed common
> notional and a fixed horizon, between running arm A as a complete policy and running arm B as
> a complete policy — where "complete" means each policy returns an answer on **every**
> candidate it is handed, including the candidates where its preferred geometry cannot be built.

Why this one: the decision on the table is which of two *policies* the live rail runs. A policy
that answers on only 56% of candidates is not a deployable policy; it is half a policy plus an
unstated fallback. Writing the fallback down (§5.3) is what turns arm B into something that can
be chosen, and it is also what converts the 44% missingness from a selection mechanism into
part of the treatment. This is the only one of the three estimands in which the missingness
stops being a threat to validity instead of being argued about.

**Why not "all candidates".** That estimand averages over every row the pipeline produces,
including rows with no plannable trade setup and rows the entry ladder never fills. It answers
"how much does the exit choice move the whole shipped book", which is a capital-allocation
question, not an exit-policy question. Its answer is dominated by the fill rate and by data
coverage: rows where the difference is exactly zero by construction shrink the estimate toward
zero by an amount set by how often the pipeline finds a fill, not by how the two policies
differ. It is a legitimate question and it is not this one.

**Why not "jointly feasible candidates".** This is the estimand the exploratory read used, and
it is the one §2.3 kills. Feasibility for arm B depends on the anchor and on the ATR, and the
anchor-dependent gates fall on the ceiling-capped rows — the rows the comparison exists to
examine. Conditioning the sample on a post-treatment variable makes the contrast something
other than a policy contrast. It is retained as a **pre-specified sensitivity** (§8.3), where
it is informative about how much the feasibility restriction moves the answer, and it can never
be the verdict.

## 5. The measurement, fixed in advance

### 5.1 Common feasibility rule — by rule, not by which arm computed

A candidate enters the population if and only if all four hold. Every one of them is a function
of the brief row and the bar store alone, evaluated **before** either policy runs, so neither
arm can select the sample:

1. `parse_ladder` returns status `OK` on the candidate's `trade_setup` — entry tiers and a
   disaster stop are present and well formed;
2. `trade_setup["atr"]` is finite and strictly positive;
3. at least one take-profit tranche is present with a finite, positive target;
4. cached RTH minute bars exist for the whole evaluation window (§5.5).

A candidate failing any of the four is excluded from **both** arms and counted, by reason, in
the flow table (§7). Nothing downstream of these four is a feasibility criterion. In particular:

* **not filling is an outcome, not an exclusion.** A candidate whose entry tiers never trade
  has a defined net cash outcome of exactly zero under both arms.
* **bracket not constructible is an outcome, not an exclusion.** It triggers arm B's declared
  fallback (§5.3) and the candidate stays in the sample with a computed value.

Rule 2 is deliberately strict about ATR. A missing ATR is a property of the brief row, known
before any price path is walked, and it makes arm B undefinable at the specification level
rather than at the outcome level. Excluding those rows from both arms is therefore a
restriction of the population, stated up front, and not a selection on outcome. The count is
reported.

### 5.2 The two policies, completely specified

Both arms are replayed over the **same** cached RTH minute path, with the **same** entry tiers,
the **same** entry TTL and the **same** fill convention. Only the exit differs.

**Arm A — brief ladder.**
* Take-profit: the brief's `tp_tranches` in order, each selling its own `tranche_pct` of the
  held position.
* Stop: the brief's disaster stop, static.
* Position TTL: §5.5.

**Arm B — live operational policy.**
* Anchor: `planned_blended_entry` over all intended tiers (`ANCHOR_PLANNED`), matching
  `build_exit_geometry_spec`.
* Take-profit: `max(tp, first_brief_tp_target)` where `(stop, tp) = atr_bracket_levels(anchor,
  atr, stop_atr_mult=1.5, tp_atr_mult=1.5, tp_floor_frac=0.006, ceiling_price=ceiling)`, one
  tranche at 100% — the ATR bracket target raised to the brief's first tranche, exactly the
  #1112 step-3 clamp the live rail applies. The floor outranks the 52-week ceiling.
* Stop, **dynamic, not static**: it starts at the same `stop` from `atr_bracket_levels`, that
  is `planned blend - 1.5 x ATR`, and the rail's `ReanchorOnFill` reaction plan then re-anchors
  it to `realised average fill - 1.5 x ATR`, passed through `clamp_reanchor_target` so it is
  never below the brief's disaster stop and never closer to price than the min-distance floor.
  In the replay a tier fills at its limit, so the "realised average fill" is the
  allocation-weighted blend of the tier limits that filled, plus the declared entry slippage of
  §5.4. This is one more reason no registered lens is arm B: the bezpazery lens stop is static
  by pre-registration (`bezpazery_lens_design_2026_07_16.md` §2).
* Position TTL: §5.5, identical to arm A.
* No-result behaviour: §5.3.

Neither arm models the #1112 arm-time disarm gate (`arms_inside_exit_region`), which can
suppress an entry under arm B that arm A would take. That is a real coupling between the exit
policy and the entry set, and it is **not** in the instrument. Consequence, stated rather than
hidden: the measured contrast is the exit-geometry contrast holding the entry rule fixed, and
it therefore does not include whatever the disarm gate contributes. Named again in §11.

### 5.3 Arm B's declared no-result behaviour

When arm B's geometry cannot be built — `atr_bracket_levels` returns nothing because the
ceiling sits at or below the anchor's cost floor, or because the bracket stop lands at or below
zero — `build_exit_geometry_spec` returns `None`, the armed intent carries no exit geometry,
and the rail falls back to the classic per-tier bracket, which takes
`tp_tranches[min(tier_index, len - 1)].target_price` (`brokers/execution.py:262`).

**Pre-registered:** arm B's no-result behaviour in the replay is that same classic per-tier
bracket, on the brief's own tranche levels and the brief's disaster stop. It is a rule already
present in the live code, not one invented for this memo, and the implementation PR pins the
equivalence with a test.

Two consequences are accepted openly. First, arm B is a hybrid on those rows and its measured
value there is close to arm A's by construction, which biases the contrast **toward zero** —
the conservative direction for a change to a live rail. Second, the share of rows that take the
fallback is itself a reportable quantity (§8.1) and belongs in the reading of the result: an
arm B that falls back on most rows is not the policy anyone thinks they are choosing.

### 5.4 Common economic scale — net cash, not R

The primary outcome is **net cash profit and loss per candidate, in USD, at a common notional**.
R is not used, for the reason in §2.2: the arms have different risk denominators, so R compares
two different units.

* **Notional.** Every candidate is allocated the same gross notional `N0`, identical across
  arms, so the share count on each fill is the same in both arms and only the exit differs.
  `N0` is the live rail's per-candidate notional under declared-frame sizing
  (`ALPHALENS_BROKER_SIZING_EQUITY` x `ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC` /
  `ALPHALENS_BROKER_MAX_OPEN`) as pinned at cohort open, recorded as one fixed USD number in
  the cohort-open amendment (§13 item 2) and used unchanged for every candidate. It is fixed, not
  time-varying: the measurement must not import an equity path.
* **Commission and FX.** Charged per fill through `broker_contract.costs.round_trip_fee_bps`
  with `fx_applies=True` and `min_commission_applies=True`, the declared constants the live
  gates already use. The per-fill USD minimum is charged **per fill**, so arm A's staged
  tranches pay it up to three times on the sell side and arm B pays it once. That asymmetry is
  a genuine economic difference between the policies and is one of the main things R hides.
* **Spread and slippage.** Every buy fill is charged `+S` bps and every sell fill `-S` bps
  against the replayed price. Primary `S = 40 bps`. That figure comes from **one** observation
  — the SMG entry that filled at 59.9261 against a 59.786017 limit, about 40 bps through it —
  and one observation is not a distribution. It is used as the primary because it is the only
  measured value available and because using zero would be a known-optimistic choice; the grid
  `S in {0, 20, 40, 80}` is a pre-specified sensitivity (§8.3) and the value of `S` at which the
  sign of the contrast flips, if any, is reported.
* **Marking.** Any position still open at the horizon is marked at that session's close, in the
  same way in both arms.

Equal notional, not equal risk, is the primary because equal notional is what the live rail
does: it sizes from a declared cash frame, not from a risk budget. Equal risk is a pre-specified
sensitivity (§8.3) and would change the answer non-linearly through the per-fill minimum, since
arm B's tighter stop would buy a much larger position for the same dollar risk.

### 5.5 Fixed, censoring-aware horizon

* **H = 42 trading sessions** from the brief date, for every candidate, in both arms. This is
  `TIME_STOP_DAYS` (`paper/constants.py:33`), the position TTL the policy already declares, so
  it is the policy's own maximum lifetime rather than a horizon invented for the analysis.
* Every candidate has a **defined value at H**: exited earlier by its own rule and holding cash
  to H, or marked to the close of session H. There is nothing to censor, because the horizon is
  fixed and identical rather than being whenever the position resolved.
* **The cohort closes H sessions before the read.** A candidate whose brief date is later than
  `analysis session - H` is not in the sample at all. Nothing enters the sample by maturing, so
  the competing-risks problem of §2.4 does not arise: both arms are evaluated on the same
  window with the same mark rule, and neither can benefit from carrying more unresolved
  positions past a cut-off.
* Entry TTL is unchanged from the brief (`order_ttl_days`, default 7 sessions) and identical
  across arms.

## 6. Inference

### 6.1 Unit and estimator

* **Unit:** the candidate, keyed `(brief_date, ticker)`. One paired difference per candidate,
  `d_i = net_B(i) - net_A(i)` in USD.
* **Point estimate:** `Delta = mean_i(d_i)`. It is the same number under every clustering
  choice; clustering changes only the interval.
* **Winsorization:** none on the primary. Winsorizing a paired difference whose whole question
  is a tail trade-off would remove the effect being measured. A 1% two-sided winsorized variant
  is reported as a sensitivity, and the tail-contribution readout of §8.1 carries the same
  information without deleting anything.

### 6.2 Clustering — a sensitivity range, not one number

Exactly five pre-specified inference arms, all computed and all reported side by side:

| # | Arm | Resamples | Role |
|---|---|---|---|
| 1 | iid over candidates | analytic + 10,000 bootstrap | reference only, **never the verdict** |
| 2 | one-way cluster: brief day | 10,000 cluster bootstrap | shares the day's market move |
| 3 | one-way cluster: ticker | 10,000 cluster bootstrap | the same name recurring across brief days |
| 4 | two-way cluster: brief day x ticker | 10,000 cluster bootstrap | both at once |
| 5 | moving-block bootstrap, block length H = 42 sessions | 10,000 | overlapping holding windows |

**Pre-committed decision rule: the verdict is read from the WIDEST interval among arms 2-5.**
Arm 1 is reported so the cost of the dependence is visible and is never the verdict. Fixing
"widest" in advance is what stops the result being shopped across clustering choices after the
fact; it is deliberately the conservative reading.

The seed for every bootstrap is fixed and recorded in the results memo.

### 6.3 The block arm is the binding constraint, and it is not computable today

Arm 5 is the honest treatment of §2.1, and it is also the arm that says how little independent
information a three-month span carries. With H = 42 sessions, a 97-session span holds about
**2.3** non-overlapping blocks. A block bootstrap on two blocks is not an inference; it is a
number with a confidence interval printed next to it.

**Pre-committed floor: no verdict before the cohort spans at least 10 non-overlapping
H-blocks**, that is at least 420 trading sessions from the cohort boundary, roughly 20 calendar
months. Ten is a judgement, recorded as one: below roughly ten blocks a block bootstrap's
coverage is badly anti-conservative, and no derivation of the exact number is offered.

If the look happens with fewer than 10 blocks — it must not, but the rule has to say what a
breach means — the output is **descriptive only**, may carry no verdict word, and does not
consume the slot.

### 6.4 Pair-count floor

A second floor, subordinate to the block floor. Before the look and after it, the realised
standard deviation of `d_i` is reported, and the pair count required for 80% power at the
minimum economically meaningful difference is computed from the pre-committed paired formula

```
n = (z_0.975 + z_0.80)^2 * (sd_d / Delta_min)^2
```

**`Delta_min` is defined by rule, not chosen:** the round-trip fee on one candidate at the
primary notional, `round_trip_fee_bps(N0) * N0 / 10_000` USD. A policy difference smaller than
the cost of one round trip cannot justify changing a live rail, so it is the natural floor on
what counts as economically meaningful, and it is computable from the declared cost constants
without looking at any outcome.

The planning `sd_d` is read once from the historical span (§3.4) before the cohort opens, under
the no-mean guard stated there, and recorded in the cohort-open amendment. If the realised
`sd_d` exceeds it, the pair floor is recomputed upward from the same formula **before** the
look, never after seeing `Delta`.

## 7. Missingness audit

Two deliverables, both descriptive, both spending no slot, both run **before** the primary.

### 7.1 Flow table over the historical span

A single table over all rows in the 2026-05-19..2026-08-23 span (919 rows, 536 matured as the
exploratory read counted them, re-counted at a recorded read timestamp), reporting counts and
shares by reason, in this order:

| Level | Reason | n | share |
|---|---|---|---|
| all rows in span | — | | |
| dropped | `plannable = False` / `NO_STRUCTURE` | | |
| dropped | not terminal at the read | | |
| terminal | arm A value present | | |
| terminal, arm B null | unparseable or non-OK setup / missing disaster stop | | |
| terminal, arm B null | no bars | | |
| terminal, arm B null | ATR missing, non-finite or <= 0 | | |
| terminal, arm B null | nothing fills in walk-1 | | |
| terminal, arm B null | risk <= 0 | | |
| terminal, arm B null | bracket not constructible: ceiling <= anchor x 1.006 | | |
| terminal, arm B null | bracket not constructible: bracket stop <= 0 | | |
| terminal | both arms present | | |

The stored parquet records only the `None`, never the reason, so this table requires re-running
the lens with reason instrumentation over the cached bars. That re-run **must not** rewrite any
stamped value; it writes a separate diagnostic artifact (§10.2).

The same table, with the same rows, is mandatory in the results memo for the forward cohort.

### 7.2 Does missingness predict arm A's outcome

A direct check, run on the historical span: regress the indicator "arm B is null" on arm A's
realised outcome, with day-clustered errors, and report the same contrast as a simple
difference in arm A's mean outcome between rows where arm B is null and rows where it is not.
If the two differ materially, the jointly-feasible estimand of §4 is confirmed as
selection-on-outcome and the sensitivity in §8.3 must be read as such rather than as a robustness
check.

This diagnostic touches arm A's outcome on the historical span, which is already-looked-at data,
and it never computes the A-vs-B contrast. It is a property of the mechanism, not a test of the
policies.

### 7.3 No imputation

Pre-committed, and binding: **arm B is never imputed from its own mean**, or from a model of
its own values, under any circumstance. Every row in the sample carries a *computed* arm B
value, either from its bracket or from the declared fallback of §5.3. Rows that cannot carry one
are excluded from both arms by the common rule of §5.1 and counted. There is no third option.

## 8. What is reported

### 8.1 Always, whatever the answer

1. **The full paired distribution of `d_i`** — histogram, all deciles, minimum and maximum. Not
   a mean with a standard error.
2. **Tail contribution** — the share of the total sum of `d_i` contributed by the largest 5% by
   absolute value, and `Delta` recomputed with the single largest positive and the single
   largest negative pair removed. If a handful of pairs carry the estimate, the reader sees it.
3. **Holding time per arm** — the distribution of `holding_days_elapsed`, median and 95th
   percentile, per arm. A policy that exits sooner ties up capital for less time, which the
   per-candidate contrast does not price.
4. **Maximum adverse excursion per arm** — `mae` and `mae_pct` distributions. The path pain a
   policy imposes is part of whether it is runnable.
5. **Fill counts and total commission per arm** — the number of chargeable fills and the summed
   fee, because the per-fill minimum is a first-order difference between a staged ladder and a
   single exit at the live notional.
6. **Share of arm B rows taking the declared fallback** (§5.3), and the same for the
   ceiling-capped subset.
7. **Every one of the five inference arms** of §6.2, with cluster counts.
8. **The flow table** of §7.1 on the forward cohort.

### 8.2 The one primary contrast, declared now

> **Primary:** `Delta = mean_i(net_B(i) - net_A(i))` in USD at notional `N0` and horizon
> H = 42 sessions, over the §5.1 population, **two-sided**, alpha = 0.05, **one look**, read
> from the widest of inference arms 2-5.

**Two-sided is deliberate.** The exploratory read points one way, and it is precisely the read
the four defects of §2 invalidate. Pre-committing to that direction would launder an
uninterpretable point estimate into a directional hypothesis. The project has made this call
before, for the same reason, in `channel_feature_forward_prereg_2026_08_19.md` §4.

**Median and win-rate are not the primary and cannot become it.** They are reported beside the
full distribution, with the §2.2 sentence attached each time they appear: both reward the closer
target by construction, so a bracket advantage on either is expected under the null and is not
evidence.

### 8.3 Pre-specified sensitivities — no slots, cannot replace the verdict

Each is the identical estimator with one thing changed, reported next to the primary:

1. **Jointly-feasible subpopulation** — the §4 estimand the exploratory read used, restricted to
   rows where arm B's bracket is constructible. Read against §7.2.
2. **Realised anchor** — arm B geometry from `ANCHOR_REALISED` instead of `ANCHOR_PLANNED`, that
   is the policy #1112 step 4 would move live onto. Compared with the primary **only on rows
   where both are non-null**, per `bezpazery_lens_design_2026_07_16.md` §7.5.
3. **Unclamped, static-stop arm B** — the pure planned-anchor bracket without the #1112 step-3
   take-profit floor and without the fill re-anchor, that is exactly what the registered
   `atr_bracket_1p5_planned` lens computes. This is the bridge between this memo and the
   `/edge` series.
4. **Notional grid** — `N0` in {live declared frame, $1,000, $10,000}. The conclusion may
   legitimately depend on notional, because the per-fill USD minimum dominates small notionals
   and the ad-valorem rate dominates large ones. If the sign flips across the grid, that IS the
   finding and it must be reported as one.
5. **Slippage grid** — `S` in {0, 20, 40, 80} bps.
6. **Equal risk budget** instead of equal notional.
7. **1% two-sided winsorized** paired difference.
8. **R-space contrast**, reported once, purely as the bridge to the historical read, always with
   the §2.2 caveat attached.

None of these may carry a verdict word, and none may be substituted for the primary after the
fact.

## 9. Ledger

This is a **policy** counterfactual evaluated against realised outcomes, so it charges the
§4.1 policy-and-ladder annex of `edge_hypothesis_budget_2026_07.md` — the ~2026-09 exit
walk-forward multiplicity budget — and not a §3 selection-covariate cluster. Per ADR 0013 R4,
every evaluated policy counts, registered lens or not.

**Cost: one policy look.** It is *additional* to the two registrations the two bracket lenses
already carry (`atr_bracket_1p5`, 2026-07-16; `atr_bracket_1p5_planned`, 2026-08-24). Those
two rows charge for the lenses existing and being read; this row charges for the A-vs-B
contrast, which is a different question asked of the same store.

Row to be appended verbatim to §4.1, in the first implementation PR, before any look:

```
| 2026-08-24 | Exit-policy head-to-head: brief tranche ladder (arm A) vs the live operational ATR-bracket policy (arm B, planned anchor + #1112 step-3 clamp + declared fallback); pre-registered `exit_policy_comparison_prereg_2026_08_24.md` | net USD P&L per candidate at a common notional, horizon H=42 sessions, forward cohort only | PENDING — one two-sided look, no interim peeks; floor is >= 10 non-overlapping 42-session blocks, not a pair count | 1 policy look -> Sep exit walk-forward (ADR 0013 R4) | Additional to the two lens registrations above: those charge for the lenses, this charges for the contrast. Primary is NOT computable from the stamped columns (the lenses omit the live take-profit clamp and the two stamped R columns have different denominators) |
```

Registration timing, binding: the slot is registered when that row lands, which must be before
any computation of the contrast on cohort rows. The slot is consumed when the outcome join
runs, and is returned only if the experiment halts with no contact between the two arms and the
outcome ever having occurred.

A null, an inverted or an underpowered result is logged and **cannot** be retried with a
different horizon, a different anchor, a different feasibility rule, a re-cut window or a
different notional. A second question needs a second slot.

## 10. What has to be built

### 10.1 The net-cash replay (blocking for the primary)

A research-side module that, for one candidate, replays both arms over the same cached minute
path and returns net USD at `N0` and horizon H, including per-fill commission through
`broker_contract.costs`, the FX leg, and the declared slippage. It must:

* take the arm as an explicit, defaultless argument, the same discipline #1114 imposed on the
  anchor;
* implement arm B exactly as §5.2 and §5.3 specify, with a test pinning the fallback against
  `brokers/execution.py`'s classic per-tier bracket;
* be pinned on the SMG 2026-08-24 incident numbers, reusing
  `apps/alphalens-research/tests/incident_1112_fixture.py`;
* never write to `~/.alphalens/population_ladders/`.

### 10.2 The missingness instrumentation (blocking for §7)

A diagnostic pass that recomputes the bracket lens over the historical span and records the
**reason** each null arose, into a separate artifact. It must not rewrite any stamped value.

### 10.3 The planning-sd script (blocking for §6.4)

A separate entry point over the historical span that returns `sd_d`, the pair count and the
cluster counts, and nothing else, with a test pinning that its result carries no mean, no
median, no sign and no per-row differences (§3.4).

### 10.4 The analysis script

Winsorization, paired bootstrap, the five clustering arms, the moving-block arm, and the power
helper. Where the channel-experiment script already provides an equivalent primitive
(`apps/alphalens-research/scripts/stage1_retro_outcome_inference.py`), reuse it rather than
writing a second one; any new helper ships with its own `unittest.TestCase` before the look.

### 10.5 Two-stage commit protocol

The cohort extract is written and its sha256 committed **before** the outcome join runs, and the
analysis script refuses to run against a mismatching hash. Same protocol as the channel
experiment.

## 11. Integrity conditions and HALT

The experiment is void, and the slot returned, if any of the following is found before the look:

1. **Arm B stops being the live policy.** If #1112 step 4 ships and the live rail moves onto the
   realised average-fill anchor, arm B as specified in §5.2 is no longer what runs. The cohort
   ends, a new one opens, and the sensitivity in §8.3 item 2 becomes the new primary arm B in
   the successor memo. The point of this measurement is that the thing measured is the thing
   that runs.
2. **Any frozen token moves** (§3.3) — the entry ladder builder, the replay ladder config, the
   bezpazery parameters, or the cost constants.
3. **Peeking.** No partial `Delta`, no interim paired means, no dashboard of the contrast, no
   "quick check on the first month". The readouts legitimately available during accrual are the
   flow table, the fallback share, the fill counts, and the block count — none of which touches
   the A-versus-B difference.
4. **The instrument diverges from the rail again.** If a change makes the replayed arm B differ
   from `build_exit_geometry_spec` plus the clamp, the accrued sample measures a policy nobody
   runs, which is exactly the #1114 defect. The implementation PR carries a parity test; that
   test going red during accrual is a HALT, not a flake.
5. **Fewer than 10 non-overlapping H-blocks at the intended look date** (§6.3) — the look does
   not happen, the slot is not consumed, and the accrual continues to the sunset.

**Known limitation, not a HALT:** the arm-time disarm gate of #1112 step 1 is not modelled
(§5.2). It couples the exit policy to the entry set on the live rail and it is absent from the
instrument, so the measured contrast is the exit-geometry contrast at a fixed entry rule. The
size of that gap is not estimated here.

## 12. Stopping rule and what each outcome decides

### 12.1 Stopping rule

**One look.** It happens on the first scheduled research window at which all of the following
hold, and never before:

1. at least 10 non-overlapping 42-session blocks in the cohort (§6.3);
2. the pair-count floor of §6.4 met at the realised `sd_d`;
3. no frozen token has moved (§3.3) and no HALT condition has fired (§11);
4. §10.1-§10.5 are built, tested and committed, and the extract hash is recorded.

**Sunset: 2028-12-31.** If the floors are unmet by then, the slot closes without a verdict, the
accrual is reported descriptively, and any future test of this question is a new design with a
new slot. The results memo `exit_policy_comparison_results_<date>.md` is written whatever the
answer is, including "the floor was never met".

### 12.2 The decision table, fixed before any outcome exists

Read from the widest of inference arms 2-5, two-sided at alpha = 0.05:

| Result | Decision |
|---|---|
| Interval excludes 0, `Delta > 0` (arm B better) | The live rail **keeps** the ATR-bracket policy. The brief's take-profit tranches are formally demoted to display and measurement only, and the card and the API stop presenting them as the plan that will be executed. |
| Interval excludes 0, `Delta < 0` (arm A better) | The live rail **switches** to placing the brief's tranche ladder. The ATR bracket stays as a `/edge` lens. This is a change to a live rail and goes through its own implementation issue with its own soak. |
| Interval includes 0 | The two policies are **not distinguishable** at this span. The rail keeps what it runs today (the ATR bracket), because a null is not a reason to change a live rail. |
| Sign flips across the notional grid (§8.3 item 4) | **That is the finding.** The right exit policy depends on position size, and the decision is made per notional band rather than globally. Reported as such, not collapsed into one verdict. |
| Floors never met by the sunset | Same standing decision as the null row, recorded as unresolved rather than as evidence of equivalence. |

**In every row, including the null and the sunset, one action is required and is not
conditional on the result:** the brief card and the API must stop presenting the take-profit
tranches as the executed plan while the rail places a single volatility-derived target. That is
a truthfulness fix, owed to #1112's second comment, and it does not depend on which policy wins.

### 12.3 What cannot be done afterwards

The result may not be re-cut by horizon, by anchor, by sub-period, by feasibility definition, by
notional, by slippage assumption or by summary statistic in search of significance. The
sensitivities of §8.3 exist to qualify the reading, never to replace it. Any of them promoted to
a verdict would be a new test drawing a new slot.

## 13. Deliverables

1. This memo, locked before the implementation PR merges and before any cohort row matures.
2. A **cohort-open amendment** appended here on the day `atr_bracket_1p5_planned` deploys,
   recording: the cohort boundary brief date, the fixed `N0` in USD with the sizing inputs it
   came from, the planning `sd_d` read from the historical span, and the resulting `Delta_min`
   and pair floor.
3. The §9 ledger row, appended to `edge_hypothesis_budget_2026_07.md` §4.1 in the first
   implementation PR, before any look.
4. The §10 code, with tests, and the §7 diagnostic artifacts.
5. A results memo `exit_policy_comparison_results_<date>.md` reporting the primary, all eight
   sensitivities, the flow table, the full paired distribution and every §8.1 readout — written
   whatever the answer is.
6. A one-line status update on this memo when the slot is consumed or sunset.
