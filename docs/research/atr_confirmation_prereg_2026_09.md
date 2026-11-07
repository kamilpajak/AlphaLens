# ATR × EDGE — pre-registration of the cluster-1 held-out confirmation (#1227)

**Status:** **RUN AND SPENT 2026-11-07** — verdict RETIRE, conclusion
`inconclusive`, live scorer tilt `unchanged`. Result, provenance and the
retrospective corrections are in §11. Registered and frozen 2026-09-23, before any
feature-vs-outcome statistic was computed on the held-out panel; everything between
this line and §11 is left as it was frozen.
**Cluster:** 1 (`technical_atr_pct`), §3 of [`edge_hypothesis_budget_2026_07.md`](edge_hypothesis_budget_2026_07.md)
**Looks:** CORRECTED 2026-11-07 — **4 cumulative on cluster 1 after this run**. This
line read "2 used (Jun sweep, Jul re-run — both DISCOVERY), 1 charged by this study".
The ledger's appended note of 2026-11-06 settled the pre-run count at 3 (Jun sweep,
Jul re-run, ML corner 2026-07-14) and this study charged the fourth.
**Family:** 1 (ATR only). MA50 extension and the press gate are NOT in this look.
**Run trigger:** AMENDED 2026-09-24 — on or after **2026-10-09**, and only once the
panel clears the registered floors. The original trigger ("on or after 2026-09-24,
no further accrual condition") rested on a power figure that was wrong; see the
correction box. **Both conditions were verified met on 2026-11-06:** the date has
passed and the panel holds 385 ticker-episodes in 63 arrival clusters against floors
of 300 and 30 (update box below). The trigger is unchanged; it was satisfied, and the
look RAN on **2026-11-07 at 11:08:38Z** on a panel of 394 episodes in 64 clusters (§11).
**Sunset:** none (cluster 1 carries no sunset date)
**Frozen code:** [`apps/alphalens-research/scripts/ml/2026_09_a20_atr_confirmation.py`](../../apps/alphalens-research/scripts/ml/2026_09_a20_atr_confirmation.py)
— its module docstring is the operative registration; this memo is the rationale
and the arithmetic behind it.

Ledger rule 4 makes this terminal. A cluster that cleared discovery and then fails
its held-out confirmation is retired and is not re-tested. There is one look, it
cannot be repeated, and the decision rule below was fixed before it ran.

> ### CORRECTION — 2026-09-24 (the run has NOT happened)
>
> **The unblocking condition is not met.** The power figures this registration cites
> in §1 were computed on a held-out panel counted in the wrong unit: distinct
> `(brief_date, ticker)` pairs instead of the collapsed ticker-episodes ledger rule 5
> defines and this registration's own panel loader applies. Account and re-run in
> [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md); fix in `bf2f7b9a`.
>
> | | as registered | corrected |
> |---|---|---|
> | held-out panel | 419 episodes, 35 clusters | **206 episodes, 33 clusters** (2026-09-24) |
> | ATR power at the gate, Holm(3) | 83.9% | **46.2%** |
> | ATR power at the gate, family of 1 | 92.9% | **66.7%** |
>
> **Nothing below is withdrawn except the run trigger.** The estimand, the panel
> definition, DELTA, the decision rule, the conclusion language and the tilt mapping
> all stand exactly as frozen on 2026-09-23. They were frozen before any held-out
> feature-vs-outcome statistic was computed and that is still true: no held-out
> `sel_ar_20` value has been read, then or since. The correction is to a power
> calculation, not to the test.
>
> **What changed in the code.** `RUN_NOT_BEFORE` moves from 2026-09-24 to 2026-10-09.
> That is a TIGHTENING — the rail can only refuse more runs than before, never allow
> one — which is why it is applied here rather than deferred. The date is arithmetic
> on the frozen `MIN_EPISODES = 300` floor and the measured accrual of 0.917 clusters
> per session, not on the power search, whose stopping point is not resolved at the
> precision it was run at (preflight memo §7.1, §7.2).
>
> `MIN_EPISODES` and `MIN_CLUSTERS` are **unchanged**. Both were set against the
> miscounted panel and `MIN_EPISODES` is now stricter than the power requirement
> needs, but lowering a floor after watching a panel fail it is the move this
> document exists to prevent. The cost of leaving it is three days.
>
> **How this was caught, and how it was not.** Not by review: two adversarial passes
> read this registration and neither questioned the panel counts, because a reviewer
> checks the argument and the counts arrive from code looking like facts. It was
> caught by the owner asking for the preflight to be run after merge, which printed
> the panel and did not match the remembered number.

> ### UPDATE — 2026-11-06 (the run has still NOT happened)
>
> **The unblocking condition is now met.** The sentence "The unblocking condition is
> not met" in the box above described the panel of 2026-09-24. It is left as written
> and this box is the correction. Re-measured on the VPS production stores on
> 2026-11-06, with `2026_09_a20_atr_confirmation.py --preflight` and with
> `a20_power.py --population briefed --family-size 1`:
>
> | | 2026-09-24 (corrected) | 2026-11-06 |
> |---|---|---|
> | held-out panel | 206 episodes, 33 clusters | **385 episodes, 63 clusters** |
> | registered floors (300 / 30) | not met | **met** |
> | ATR power at the gate, family of 1 | 66.7% | **91.5% ±1.4%** (seed 20260922, n_sims 400) and **92.2% ±1.3%** (seed 7, n_sims 400) |
>
> Construction of both numbers:
> [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md) §8. The power
> figure was read at two seeds, one run each: 91.5% ±1.4% at seed 20260922 and 92.2%
> ±1.3% at seed 7, both at n_sims 400 and both on 2026-11-06. They agree inside Monte
> Carlo error. No run at a higher n_sims is recorded. A repeat at one seed would be a
> determinism check and not a reproduction, because the simulation draws all of its
> entropy from `np.random.default_rng(seed)` and is bit-identical at a fixed seed,
> panel and n_sims.
>
> **Nothing is withdrawn and nothing is added.** No frozen value moved —
> `MIN_EPISODES = 300`, `MIN_CLUSTERS = 30`, `MIN_VOL_EPISODES = 4`,
> `RUN_NOT_BEFORE = 2026-10-09`, `DISCOVERY_CUTOFF = "2026-07-05"`,
> `POPULATION = "briefed"`, `SEED = 20260924` and `N_BOOT = 9999` all stand as frozen,
> read from the script on 2026-11-06. The estimand, DELTA, the decision rule, the
> conclusion language and the tilt mapping stand exactly as frozen on 2026-09-23. No
> held-out `sel_ar_20` value has been read, then or since: the preflight reads no
> outcome column and the power simulation injects effects into simulated outcomes.
>
> **This is not a look and it is not a decision to run.** Ledger rule 4 makes the look
> terminal, so spending it is the owner's decision. Two questions are posted on #1227
> for that decision: the cumulative look count on cluster 1, which §3 of the ledger
> reads as "2 (Jun,Jul)" while two §4 rows read 3, against rule 4's cap of 2 re-looks;
> and the fact that one of the three outcomes in §6 bumps `SCORER_CONFIG_VERSION` with
> a cohort reset, which cuts the panel a later model would train on.
>
> **Appended note 2026-11-06 — the first of those two questions has moved; the text above
> is left as written.** §3 of the ledger no longer reads "2 (Jun,Jul)": its cells for
> clusters 1, 2, 3 and 9 were corrected to the §4 trail the same day, so cluster 1 reads
> 3 (Jun, Jul, ML corner). The arithmetic is settled and the construction is in the
> appended note at the end of §4 of `edge_hypothesis_budget_2026_07.md`. What is NOT
> settled is the one that decides whether this look may run: whether rule 4's cap of two
> re-looks covers a rule-3 confirmation. The ledger's own text cuts both ways in one rule
> and that note records both readings, so it was an owner interpretation. **The owner took
> it the same day: the cap governs sweep re-looks and this confirmation sits outside it, so
> the look is available to be spent.** The second question, the `SCORER_CONFIG_VERSION`
> bump, is unchanged, and whether to spend the look is still a separate decision.
>
> **The operative registration still reads as blocked, and this box does not touch
> it.** The registration itself is the module docstring of
> `apps/alphalens-research/scripts/ml/2026_09_a20_atr_confirmation.py`, and it still
> carries the 2026-09-24 "WHY IT MAY NOT RUN YET" section written when the floors were
> not met. Amending the operative registration is itself a dated act, and it belongs
> with the owner's decision on whether to spend the terminal look — not inside the
> change that reports the gate as clear. Until that act is taken the script's docstring
> and this box disagree about the gate, and this box is the later read.
>
> **Still open, and not changed by this box.** The §1 operating-characteristics table
> and the implied SE 0.0552 were back-derived from the withdrawn power curve (§9), and
> §1 says they must be re-derived once the panel reaches the floors. The panel has now
> reached them; the re-derivation has not been done. So is the direction mismatch in
> `a20_power.py` recorded in the preflight memo §7.3 (the simulator measures two-sided,
> this registration tests one-sided).
>
> **Appended 2026-11-07 — the re-derivation IS now done; the sentence above is left as
> written.** §10 holds it, measured on the panel the run will use, across several seeds.
> The outcome is mostly a CONFIRMATION: §4's implied SE of 0.0552 sits inside the
> measured range [0.0525, 0.0553], its significance boundary sits inside [−0.0910,
> −0.0863], and every row of its promotion table is inside the between-seed spread.
> One published figure is wrong — §5's "about 13%" for arm (iii) is **21.6% ±0.7pp** on
> 3200 panels across 8 seeds. §10 also records two things never published: under a true
> zero the run lands on `retired` 49.8% of the time (§10.3), and the frozen `POPULATION`
> constant names a wider population than the join produces (§10.6). The direction
> mismatch in §7.3 and rule 2's α-spending are still open and are NOT addressed.

## 1. Why now, and not later

#1227 wrote its own unblocking condition: run when an outcome-blind power
simulation shows at least 80% power at 50% of the July discovery effect for the
ATR test. Three preflights answered it, and all three are recorded elsewhere:

| question | answer | where |
|---|---|---|
| Is the ATR test powered? | ~~83.9% under a 3-member Holm family; **92.9% at the family of one used here**~~ **WITHDRAWN 2026-09-24: 46.2% and 66.7%. Not powered.** **RE-MEASURED 2026-11-06 on a panel of 385 episodes in 63 clusters: 91.5% ±1.4% at the family of one (seed 20260922, n_sims 400) — powered.** | [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md) §7, §8 |
| Does the 20-session window overlap break the inference? | No. Arrival-session WCB holds its level in every cell, worst corner 7.125% (95% CI [6.87, 7.38]) against a pre-specified 7.5% bar | [`a20_overlap_inference_2026_09.md`](a20_overlap_inference_2026_09.md) |
| Does mixed price adjustment reach a held-out label window? | No. The premise was withdrawn — both sources are split-adjusted | [`atr_split_adjustment_gate_2026_09.md`](atr_split_adjustment_gate_2026_09.md) |

The yfinance stale-OHLCV cache census ran for the overlap memo: three logged
fallback events in the whole journal, all inside the burnt window, none of the
three rows present in the panel, zero in the held-out window.

**Waiting longer was considered and rejected, in writing, before the run.** The
panel grows about one matured arrival cluster per session, so waiting to ~70
clusters would take roughly seven weeks. What that buys, and what it does not:

The table below is **superseded**; it describes a panel twice the size of the real
one. It is kept because the argument it supported — that waiting does not change
any decision under rule 4 — was made on it, and because the reasoning is what the
record needs, not the arithmetic. Operating characteristics must be re-derived once
the panel reaches the floors, and the implied SE they rest on was always
back-derived rather than measured (§9).

| true \|ρ\| | ~~P(promote) at 35 clusters~~ | ~~at ~70 clusters~~ |
|---|---|---|
| 0 | 3.5% | 0.5% |
| 0.05 | 18% | 10% |
| 0.10 | 50% | 50% |
| 0.15 | 82% | 90% |
| 0.189 | 95% | 99% |

The strongest argument for waiting was that it lifts the reachability of the
equivalence arm (§5) from about 13% to about 64%. That argument was **withdrawn**:
under ledger rule 4 both "inconclusive" and "evidence against" retire the cluster,
so the improvement changes the wording of a postmortem and not a single decision.
Against the remaining 8-point gain at ρ = 0.15 stand three costs: the gate was
pre-registered and is met, so waiting is discretion outside the register; the
seven weeks are not guaranteed, because the `sel-label-v3` recompute can move the
matured-cluster count in either direction; and the ATR decision has already slipped
twice (the July kill line folded in on 2026-09-17 with its trigger met, and D5
doubling the maturity the same day).

## 2. The estimand, frozen

| item | fixed as | why |
|---|---|---|
| Outcome | stamped `sel_ar_20` | Owner decision D5. Read from the label store, never recomputed — recomputing a label is a new definition. |
| Signal | `technical_atr_pct` as stamped on the brief | The discovery column unchanged. |
| Covariates | `technical_ma50_distance_pct`, `n_gates_passed` | The July verdict for ATR is the partialled one; a marginal slope would re-import a confound discovery already controlled for. |
| Statistic | standardised partial slope, jointly fitted | So a slope reads on the same scale as the discovery ρ and the 25/50/75% shrinkage grid means what it says. |
| Unit | ticker-episode, chained 5-session collapse | Ledger rule 5. |
| Cluster | arrival session | The dependence that matters is same-day co-movement; verified against the 20-session overlap in the overlap memo. |
| Population | BRIEFED only | Pre-committed in the power memo §2.1 before any curve existed. Discovery found ATR predicts outcomes *among briefed names*. **The panel is narrower than this names — see §10.6:** the signal columns live only in the brief store, so the join drops 19 of 779 resolved briefed held-out rows, all `pre_open_recovered` names the journal stores without signals. The same join runs on the burnt side, so the exclusion is symmetric with discovery. |
| Weighting | episode-weighted | One vote per episode, consistent with the unit. |
| Winsorization | none | Discovery reported a rank statistic; a trim here would be a new choice invisible in the comparison. |
| Test | one-sided restricted wild cluster bootstrap, alternative "less", α 0.05, B = 9999, seed 20260924 | Direction fixed from discovery. |

## 3. The smallest actionable effect

**DELTA = |ρ| 0.10**, an owner decision of 2026-09-23.

On this estimand's own scale, with sd(`sel_ar_20`) = 0.1653 measured on the burnt
panel, ρ = 0.10 means **1.65 percentage points** of 20-session market-adjusted
return per one standard deviation of ATR. (**Re-measured 2026-11-06: the burnt
panel now holds 198 episodes and sd is 0.1681, so DELTA is 1.68 pp.** The bound is
an owner decision on the ρ scale and does not move with it; the translation does.) A realistic selection tilt that drops
the top ATR quintile moves the book's mean z(ATR) by about 0.4 sd, so about
**0.66 pp per episode**. It is also about 26% of the discovery effect (−0.378), so
the bound says in plain terms: *if the true effect is under roughly a quarter of
what discovery claimed, do not act on it.*

**Why it is justified here rather than imported.** The sibling registration
`2026_09_experts_last_look.py` froze the same 0.10 as its equivalence bound, but
for an ATR-partialled **Spearman**, which is not the same statistic as a
standardised partial slope from a joint fit — a partial correlation is bounded in
[−1, 1] and a standardised slope is not, and the mapping between them depends on
the covariance matrix. The two agreeing is corroboration; the economic
translation above is the reason.

## 4. What a PASS proves, and what it does not

The promotion rule is `p_one_sided < 0.05 AND slope ≤ −0.10`.

**The floor, not the p-value, is the binding condition.** At the implied precision
the one-sided significance boundary sits at about −0.091, so any estimate that
clears −0.10 has already cleared the p-value. The effective rule is
`slope ≤ −0.10`, whose one-sided false-positive rate under a true zero is about
3.5% rather than the nominal 5%.

A PASS therefore means: *the held-out estimate crossed the actionable line with a
slope of the registered sign.* It does **not** mean *the true effect is at least
0.10 in magnitude.* That second claim needs a shifted null (H₀: ρ ≥ −0.10), and
it was rejected for two measured reasons: it has about 49% power at the planning
effect, far under the pre-registered 80% gate; and it would retire a true effect
of −0.15 about 60% of the time, which under rule 4 is permanent.

> **SUPERSEDED 2026-11-07 by the run itself, §11.5 — the two paragraphs above are
> left as written.** Both figures describe a simulated sampling distribution. On the
> panel the look actually used, three independent readings of its own output put the
> slope SE at 0.0716 to 0.0773 rather than 0.0538, which moves the one-sided 0.05
> boundary out to about −0.118 to −0.127 — PAST the floor. The p-value leg, not the
> floor, was the binding condition, and the "effective rule is `slope ≤ −0.10`"
> sentence does not describe this run. The 3.5% false-positive figure goes with it.

**Operating characteristics, disclosed in advance.** Normal approximation at the
implied SE 0.0552, itself **back-derived from the recorded power curve rather
than measured** (0.189 / SE = 1.96 + 1.465; the same SE predicts 25% power at the
25%-shrinkage row against the 22% the power memo reported):

| true \|ρ\| | 0 | 0.05 | 0.10 | 0.15 | 0.189 | 0.25 |
|---|---|---|---|---|---|---|
| P(promote) | 3.5% | 18% | 50% | 82% | 95% | 99.7% |

**RE-DERIVED 2026-11-07 and CONFIRMED — §10.** The row above was never measured. It
has now been measured on the panel the run will use, at 3000 panels per point across
four seeds: 3.1% / 17.9% / 48.9% / 82.5% / 95.2% / 99.7%. Every gap is inside the
between-seed spread. The SE is 0.0538 ± 0.00125 and the boundary −0.0885 ± 0.0021,
so the back-derived 0.0552 and the stated −0.091 both sit inside the measured range
and the FLOOR still binds on every seed. §10.3 adds the two arms this row never
showed.

The programme accepts these. Note that retiring a true effect of 0.05 is the
*correct* decision under a 0.10 floor, not an error; the rows that cost something
are 0.10 and 0.15. At the threshold itself the decision is a coin flip whatever
the sample size, which is a property of every threshold rule.

## 5. What a non-pass means

Anything that is not a PASS retires cluster 1 for SELECTION and ORDERING, under
ledger rule 4. The conclusion language is pre-committed three ways, graded on the
**90% two-sided cluster-bootstrap interval** — the TOST-consistent level for a
one-sided 0.05 test; a 95% interval would make arm (iii) unreachable by
construction:

1. **cleared** — evidence of association under the registered estimand.
2. **inconclusive** — not cleared, but the interval still includes |ρ| ≥ 0.10.
   The cluster is retired **operationally, not scientifically falsified.**
3. **evidence against** — the interval lies entirely inside (−0.10, +0.10).

**Disclosed before the run:** arm (iii) requires a point estimate inside roughly
(−0.009, +0.009) and so has about a 13% chance even if the true effect is exactly
zero. **CORRECTED 2026-11-07 — §10.4: measured with this registration's own bootstrap
interval on today's panel, the arm fires 21.6% ±0.7pp under a true zero, not 13%**
(3200 panels across 8 seeds). This is the ONE figure in the memo that the
re-derivation overturned; §4's precision figures and its whole promotion table came
back confirmed. The reason this one is sensitive is that it fires on the difference
between DELTA and the interval's half-width, two nearly equal numbers, while every
other figure depends on the SE through a ratio. Two further corrections to the
sentence above: the firing condition is NOT the symmetric band it implies — the
estimates that actually fired ran to ±0.027, not ±0.009 — and a single 400-panel run
cannot state this figure to a binomial error, because the arm sits in a joint tail
(§10.4). The correction runs toward reachability, so arm (iii) is less of a long shot
than disclosed, not more. In practice this three-way will almost always land on (i) or (ii). Saying
so now is the difference between a limitation and an excuse. The interval is an
equal-tail percentile interval over resampled clusters, which is wider than a
studentised or BCa one would be; that makes arm (iii) *harder* to reach, so the
conservatism runs toward "inconclusive" rather than toward a false "evidence
against". Containment is strict — an interval whose endpoint sits exactly on
±0.10 has not excluded it.

**What retirement does not touch.** ATR's use in execution geometry — the bracket
and stop sizing, the registered `atr_bracket_1p5` lenses — is a different estimand
on the §4.1 budget and is untouched by any outcome here. "Retired" here means
"closed for brief selection and ordering", never "ATR is useless".

**VOID is not a retirement.** If the panel falls below 300 episodes or 30 arrival
clusters, the join comes back empty, a signal column has no variance, or the
computed statistic is non-finite, the run is abandoned and the look returns
**unspent**. The last two were added during the review of this PR and are worth
naming, because both fail *silently* rather than loudly: a zero-variance column
standardises to zeros and the shared OLS uses a pseudo-inverse, so it returns a
slope of exactly zero; and every comparison in the decision rule is False against
NaN, so a non-finite statistic would have fallen through to "the July kill
trigger fired, revert the live scorer". A statistic that does not exist is not a
finding about ATR. That is the absence of a run, not a result. Precedent:
[`exit_policy_comparison_prereg_2026_08_24.md`](exit_policy_comparison_prereg_2026_08_24.md)
voided itself before its cohort opened and its slot was returned. The moment any
feature-vs-outcome statistic is emitted, the look is spent.

## 6. The live scorer tilt — the July kill line, folded in

The live scorer carries
`selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)` under
`SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"`.
[`selection_score_v2_ext_tilt_decision_2026_07_06.md`](selection_score_v2_ext_tilt_decision_2026_07_06.md)
§5 registered a kill line for that tilt whose trigger is a fresh-data ATR effect
of the **wrong sign**. The 2026-09-17 decision on #1227 folded that line into this
look so the two cannot spend the same held-out episodes. Three outcomes, and they
are deliberately not the same:

| outcome | cluster 1 | the live tilt |
|---|---|---|
| slope ≤ −0.10 and p < 0.05 | PROMOTE | **confirmed** — keeps its support |
| slope < 0 but no promotion | RETIRE | **unchanged** — its own kill trigger did not fire |
| slope ≥ 0 | RETIRE | **retired** — the July trigger fires; `SCORER_CONFIG_VERSION` bump with a cohort reset, executed under that memo |

Collapsing the middle row into the bottom one would revert a live scorer on
evidence its own registration does not call a failure.

## 7. The one registered interaction

The July registration carried exactly one interaction hypothesis: the ATR effect
conditional on the market-state volatility axis. It runs **mechanically** here and
its result is printed whatever it is. The unit is independent regime **episodes**
— a maximal run of consecutive arrival sessions sharing one regime label — not
days, and the floor is 4. "Insufficient, not estimated" is an expected and
acceptable outcome, and it is reported rather than dropped: declining to spend
the charge is legitimate, declining to disclose it is not.

Expected in advance: `market_state_vix` is frozen at a single value across the
held-out dates (a separate known defect, out of scope here), so the axis may well
report one or two episodes and decline to estimate.

## 8. Post-promotion monitoring

Owner decision of 2026-09-23: **the frame is fixed now, the numeric thresholds are
not.** Thresholds land in a separate promotion registration and are explicitly a
*post-confirmation operational specification*, not pre-registered evidence.
Calibrating them to the measured effect does not invalidate this test, but it is
still a choice made after seeing a result and will be labelled as one.

Fixed now:

- **Monitoring estimand:** this same partial slope, on the **full stamped
  population** (briefed plus LLM-proposed), never on the live book alone. Once
  the tilt is live the book is conditioned on ATR, the in-book slope is
  attenuated, and only the names the tilt demotes restore identification.
- **Unit and horizon:** ticker-episode, `sel_ar_20`, unchanged.
- **Baseline:** the estimate this look produces — not zero, not the discovery
  estimate.
- **Intervention is one-sided:** monitoring may only ever REMOVE the tilt, never
  add or strengthen one, which bounds the cost of a false alarm to a reversion to
  today's state. This does **not** remove multiplicity: repeated looks inflate
  cumulative false removal, so the promotion registration must fix a finite number
  of scheduled looks and a per-look level, and state the cumulative figure.
- **Hard pre-commitment, made now:** the tilt must act at a pipeline stage
  **after** the candidate-proposal stage. The label stamper writes `sel_ar_*` for
  every name in `proposal_shadow` whether or not the brief carried it, and the
  shadow is written at the mapping stage, before scoring and before brief
  selection (verified in the tree 2026-09-23). A tilt acting at or before proposal
  would delete its own counterfactual, and no later registration could recover it.

## 9. Limitations, stated before the numbers

- **The shrunken effect that justifies the power gate is an in-sample quantity.**
  The three signals were selected on the burnt panel; a different label does not
  undo that. If true out-of-sample shrinkage is worse than 50%, the gate passes
  while the test is underpowered, and nothing here would detect it.
- **The implied SE 0.0552 is a normal approximation back-derived from a bootstrap
  power curve**, not a measured standard error. Every probability in §4 moves if
  the real sampling distribution is skewed or the bootstrap critical values behave
  differently at 35 clusters. The verdict itself does not depend on it — the
  decision rule uses the bootstrap p-value and the bootstrap interval directly.
  **2026-09-24:** this limitation turned out to be the smaller of two. The curve it
  was derived FROM was itself computed on the wrong panel, so the number is wrong by
  more than approximation error. Recorded here because a limitation that names the
  right risk and misses the larger one is worth seeing again.
  **2026-11-07: CLOSED by measurement, §10.** Measured across four seeds the SE is
  0.0538 ± 0.00125, and the back-derived 0.0552 sits inside that range: the number was
  wrong in PROVENANCE and right in VALUE. Every probability built on it came back
  confirmed, except arm (iii), which moved from 13% to 21.6% because it alone depends
  on the SE through a difference rather than a ratio. Worth recording how nearly this
  limitation was repeated: the first draft of §10 quoted a SINGLE-seed SE of 0.0531 and
  called it a 4% improvement on 0.0552. Four seeds showed that was seed noise. A
  simulation run quoted as a measured constant is this same defect by another route.
- **The July ATR figure was row-level and early-weighted** (ρ = −0.35 over 523
  rows, 231 of which repeat a ticker within 3 days), and
  `selection_score_v2_ext_tilt_decision_2026_07_06.md` records that pure ATR rank
  collapses late in the discovery window. This estimate is episode-level on a
  different label, so it is a fresh estimate rather than a restatement — but it
  inherits the same regime dependence.
- **`sel_ar_20` is beta-adjusted against IWM** with a raw 250-session OLS beta and
  no intercept. The largest common component is removed before any of this, which
  is a reason the measured ICC is small and also why none of this carries over to
  an unadjusted outcome without re-measuring.
- **Romano-Wolf would dominate Holm under dependence** and
  `alphalens_research/backtest/romano_wolf.py` already exists. It is moot at a
  family of one and is recorded only so the choice is visible.

## 10. Operating characteristics, measured 2026-11-07

**Why this section exists.** §1 and §9 both say the operating characteristics must
be re-derived once the panel reaches its floors, because §4's table and §5's
reachability figure rest on an implied slope SE of 0.0552 that §9 states was
*back-derived from the recorded power curve rather than measured* — and that curve
was withdrawn on 2026-09-24. The panel reached the floors in October. This is the
re-derivation. **No frozen value moves and no verdict about ATR is reported here.**
The estimand, DELTA, the decision rule, the conclusion language and the tilt
mapping stand exactly as frozen on 2026-09-23.

**The headline is a confirmation, not a correction.** §4's implied SE turns out to
be indistinguishable from the measured one, so §4's whole table stands. Exactly one
published figure is wrong — §5's "about 13%" for the equivalence arm — and two
things that were never published are recorded here for the first time.

**Why it had to be simulated.** The real sampling distribution of the registered
slope is a fact about held-out `sel_ar_20` values, and reading those IS the
one-shot look. So it is measured the only other way open: simulate on the real
held-out cluster structure with `a20_power.simulate_panel`, then apply this
registration's own `atr_slope` and `slope_ci` to each simulated panel. Burnt
outcomes supply scale, ICC and the signal correlation structure, which is what the
burnt side is for under ledger rule 3. Every outcome any slope here is computed
against is simulated. No held-out `sel_ar_*` value was read, so this charges
nothing.

**Construction.** `apps/alphalens-research/scripts/ml/2026_11_a20_operating_characteristics.py`,
run on the VPS production stores 2026-11-06 and 2026-11-07. Both modes run on the
panel the confirmation will actually use — 385 episodes in 63 arrival clusters,
taken through this registration's own blind path
(`_held_out_episodes(with_signals=False)`), not through `a20_power`'s label-only
count of 398 in 64. Burnt panel 198 episodes, sd(`sel_ar_20`) 0.1681, ICC 0.050,
burnt standardised effects ATR −0.358, MA50 −0.315, press +0.006. Nuisance signals
injected at 50% of their burnt effects, the power gate's own shrinkage.

| mode | panels per point | seeds | what it answers |
|---|---|---|---|
| `--mode slopes` | 3000 | 20261106, 51, 52, 53 | the SE, the significance boundary and the three tilt arms, from the point estimate |
| `--mode arms` | 400 | 20261107, 20261206, 31–36 (ρ=0); 20261107, 20261206, 41–44 (ρ=0.03) | the registered cluster-bootstrap interval, which the equivalence arm needs |

**Every figure below is read across SEVERAL seeds, and that is not decoration.**
The first draft of this section quoted a single-seed SE of 0.0531 against the
published 0.0552 and called it a 4% improvement. Four seeds show the SE is
0.0538 ± 0.00125 and that 0.0552 sits inside the observed range. The apparent
improvement was seed noise. Quoting one simulation run as a measured constant is
the same class of error as the curve this section exists to replace, arrived at by
a different route, so each row carries its between-seed spread.

### 10.1 §4's precision figures: confirmed

| quantity | §4 publishes | measured, 4 seeds × 3000 panels | verdict |
|---|---|---|---|
| slope SE | 0.0552 (back-derived) | **0.0538 ± 0.00125**, range [0.0525, 0.0553] | published value is INSIDE the range |
| one-sided 0.05 boundary | about −0.091 | **−0.0885 ± 0.0021**, range [−0.0910, −0.0863] | published value is INSIDE the range |
| does the FLOOR bind? | yes | **yes on all four seeds** | confirmed |

The floor binding is the load-bearing claim of §4 and it holds on every seed: the
boundary sits closer to zero than −0.10 in all four, so an estimate that clears the
floor has already cleared the p-value. The probe refuses to print a promotion table
at all if that ever stops being true (`FloorDoesNotBindError`), because the
slope-only arm would then overstate promotion by the whole p-value leg.

> **SUPERSEDED 2026-11-07 by the run itself, §11.5 — the table and the paragraph
> above are left as written.** "Confirmed" was the right verdict against the
> simulator and the wrong one against the panel. The realized slope SE read off the
> run's own output is 0.0716 (interval half-width), 0.0731 (CR2 t) and 0.0773
> (bootstrap p), i.e. 1.33 to 1.44 times the 0.0538 measured here, and §10.4's
> simulated mean half-width of 0.087 against the realized 0.1177 corroborates the
> same factor of 1.353 from a second direction. At that precision the boundary is
> about −0.118 to −0.127 and **the floor does NOT bind**. `FloorDoesNotBindError`
> did not fire before the run because it was evaluated against the simulated SE,
> which is the only one that existed then. §10.2's and §10.3's tables inherit the
> same optimism; §11.5 carries the realized figures.

### 10.2 §4's promotion table: confirmed

| true \|ρ\| | §4 publishes | measured | between-seed sd | gap |
|---|---|---|---|---|
| 0 | 3.5% | **3.1%** | 0.20 | −0.4 |
| 0.05 | 18% | **17.9%** | 0.55 | −0.1 |
| 0.10 | 50% | **48.9%** | 1.34 | −1.1 |
| 0.15 | 82% | **82.5%** | 0.45 | +0.5 |
| 0.189 | 95% | **95.2%** | 0.25 | +0.2 |
| 0.25 | 99.7% | **99.7%** | 0.22 | +0.0 |

Every gap is inside the between-seed spread; the largest, 1.1pp at ρ = 0.10, is
smaller than that row's own sd of 1.34. The back-derived SE was wrong in
PROVENANCE and right in VALUE, which is worth separating: §9's limitation named a
real defect in how the number was obtained, and the number itself survived.

### 10.3 All three arms, which §4 never tabulated

§4 published P(promote) alone. Two of the three outcomes are a "no", and they are
not the same "no". `unchanged` retires cluster 1 for selection and leaves the live
tilt running; `retired` fires the July kill trigger, which is a
`SCORER_CONFIG_VERSION` bump with its own cohort reset (§6). Mean of four grids,
3000 panels each, between-seed sd in brackets:

| true \|ρ\| | confirmed → PROMOTE | unchanged → live tilt keeps running | RETIRED → version bump, cohort reset |
|---|---|---|---|
| 0 | 3.1 [0.20] | 47.1 [0.70] | **49.8 [0.75]** |
| 0.03 | 9.7 [0.45] | 61.9 [1.48] | 28.4 [1.63] |
| 0.05 | 17.9 [0.55] | 64.4 [1.12] | 17.6 [0.81] |
| 0.10 | 48.9 [1.34] | 47.6 [1.16] | 3.5 [0.38] |
| 0.15 | 82.5 [0.45] | 17.2 [0.34] | 0.3 [0.17] |
| 0.189 | 95.2 [0.25] | 4.7 [0.26] | 0.0 [0.05] |
| 0.25 | 99.7 [0.22] | 0.3 [0.22] | 0.0 [0.00] |

**Disclosed here because it was not disclosed before: under a true zero the run
retires the live tilt about half the time.** That is not a defect introduced by
this look. `retired` means the slope came out non-negative, and under a true zero
the sign is a coin flip, so the frequency follows from the kill trigger the July
memo registered. It is written down because the registration quantified its
promote arm and its equivalence arm and left this one unquantified, and because
the consequence — a cohort boundary under ADR 0013 R3 — is the most expensive
thing any outcome here can trigger.

### 10.4 §5's equivalence arm: the one figure that is wrong

§5 discloses that the "evidence against" arm "has about a 13% chance even if the
true effect is exactly zero", needing a point estimate inside roughly
(−0.009, +0.009). Measured with the registered interval — the equal-tail cluster
bootstrap over arrival sessions at `CI_BOOT` = 2999 draws, not a normal
approximation:

| at a true effect of | §5 discloses | measured | panels | seeds |
|---|---|---|---|---|
| exactly zero | about 13% | **21.6% ±0.7pp** | 3200 | 8 |
| 0.03 (pessimistic, §10.5) | not stated | **18.0% ±0.8pp** | 2400 | 6 |

The ordering is the one theory requires: moving the true effect away from zero
pushes mass out of the firing band, so 0.03 fires less often than 0. The
difference is 3.6pp against a pooled error of 1.1pp.

**Why §5's figure is the sensitive one.** The arm fires on the DIFFERENCE between
DELTA and the interval's half-width, two nearly equal numbers, so it moves far
more than the SE does. Every other figure in §4 depends on the SE through a ratio,
which is why §10.1 and §10.2 came back confirmed while this one did not.

**The firing condition is NOT a symmetric band, and quoting one is misleading.**
The registered interval is an equal-tail percentile interval over resampled
clusters and is not symmetric about the point estimate. A symmetric reading off
the mean half-width gives |slope| < 0.014; the estimates that actually fired the
arm ran to ±0.027:

| | symmetric reading | MEASURED firing range |
|---|---|---|
| ρ = 0 | \|slope\| < 0.0142 | **[−0.0275, +0.0254]** |
| ρ = 0.03 | \|slope\| < 0.0135 | **[−0.0314, +0.0232]** |

**One 400-panel run does not pin this figure, and the binomial error it prints
understates the spread.** The arm needs a panel with BOTH a small |slope| and an
unusually narrow interval, and the half-width carries an sd of 0.0127 against a
mean of 0.087 — 15% of its own size — so the firing event sits in a JOINT tail.
Eight seeds at a true zero, each 400 panels, each reproducible at its seed:

| 18.50 | 19.75 | 19.75 | 20.00 | 20.75 | 21.75 | 24.25 | 28.00 |
|---|---|---|---|---|---|---|---|

Between-seed sd 3.11pp against a binomial 2.06pp. Seven of the eight sit
binomially around 20.7%; the eighth is 3.5 standard errors above and reproduces
exactly, and no explanation for it was found. A paired experiment on identical
panels ruled out the bootstrap seed scheme as the cause: switching schemes moved
the mean half-width by 0.5% and changed the arm on 3 of 120 panels. The pooled
figure above is what the record carries; a single run is a datapoint.

### 10.5 The 0.03 point is a stress test, not ATR's effect

0.03 is **NOT ATR's burnt estimate** — that is −0.358 on this label and this panel,
printed on every run of the probe. 0.03 is the order of magnitude the NEWS-axis
candidates showed on the burnt panel (`2026_10_jev_discovery_burnt.py`:
standardised betas −0.026 to +0.040), carried here as "ATR's true out-of-sample
effect has collapsed to under a tenth of its in-sample value", far worse than the
50% shrinkage the power gate plans for.

Read against §10.3: if ATR's true effect really has collapsed to 0.03, the single
most likely outcome of the run is `unchanged` at 61.9% — the look is spent and
nothing changes. At the planning effect of 0.189 the same rule promotes 95.2% of
the time. The two rows bracket the decision: the test is decisive about the effect
it was powered for and near-silent about one an order of magnitude smaller, which
is the trade the 0.10 floor was chosen to make (§3).

For the record, the grid's 0.189 is half of the JULY row-level discovery effect
−0.378 (§3), the number the power gate was written against. Half of the
episode-level −0.358 would be 0.179; the grid was not re-cut when D5 moved the
primary label.

### 10.6 The population the test will run on is narrower than `POPULATION` names

`POPULATION = "briefed"` is frozen and §2 fixes the population as BRIEFED only.
The panel the run builds is narrower, and the gap belongs on the record before the
verdict exists rather than after.

The confirmation joins the label store to the brief store, because the signal
columns live only in the brief store. Measured on the VPS 2026-11-06: of 779
resolved briefed held-out label rows, **19 have no matching brief row**, all on
three dates (2026-08-02: 10, 2026-08-19: 7, 2026-08-18: 2). After the rule-5
episode collapse that is 13 episodes and one arrival cluster — the difference
between `a20_power`'s 398 in 64 and this registration's 385 in 63.

**The cause is known and it is not data loss.** `apply_pre_open_population` in
`alphalens_pipeline/feedback/selection_label.py` sets `briefed_any_theme` to True
unconditionally for names recovered from the pre-open brief journal (#1494), under
its own population token `pre_open_recovered`. Its docstring states that such a
name "comes back with no theme, because the journal records names only", so those
names have no signal columns and never could have joined. Confirmed by measurement
on the BURNT side, where every column may be read: of 466 resolved briefed burnt
rows, 75 have no brief-store match and **all 75** carry
`population = pre_open_recovered`, with zero `briefed_or_llm_proposed` rows
unmatched. The held-out `population` column was NOT read — it sits outside
`a20_power`'s allowlist — so the mechanism is carried across by the code path, not
by a held-out read.

Two things follow and they point opposite ways:

- **For the estimand.** The exclusion is SYMMETRIC: `burnt_panel` applies the same
  inner join, so the discovery effect this test compares itself against was
  estimated with the same class of row excluded by the same mechanism.
- **Against, and not cured.** Brief-store membership is settled after the fact — a
  name drops out because a later run dropped it. That is selection on membership,
  not on outcome. It is small on the held-out side (19 of 779, 2.4%) and larger on
  the burnt side (75 of 466, 16.1%), so the two panels differ in composition in a
  way no frozen constant describes.

### 10.7 What this section does not settle

- **The measured SE is the spread of a SIMULATED sampling distribution**, not of
  the real one. It inherits every assumption `a20_power.simulate_panel` makes:
  normal cluster and residual draws, ICC applied to residual variance, and real
  burnt signal rows resampled into the held-out cluster sizes. If the true outcome
  distribution is skewed, these figures are optimistic by an unmeasured amount.
> **ANSWERED 2026-11-07 — the first bullet above is now measured, and it was
> wrong about which assumption mattered.** The amount is 1.36x on the one reading
> that is a direct SE. The cause is NONE of the three assumptions this bullet
> names: normal draws, the ICC on residual variance, and burnt rows resampled into
> held-out sizes each move the simulated SE by 3% or less. The cause is a mechanism
> not on the list — arrival sessions differ in noise scale and the scale tracks the
> session's own volatility, the quantity the regressor measures. Measured on the
> burnt panel at +0.360 (se 0.101) and put back into the simulator, it predicts
> 0.0707 against the realized 0.0731, closing 88% of the gap. The bullet's own
> framing — "if the true outcome distribution is skewed" — points at the one
> candidate the measurement rules out. Full construction, every table and the
> verbatim output: `a20_simulator_precision_gap_2026_11_07.md`. That memo reads
> held-out SIGNAL columns, which this registration's preflight allowlist barred
> before the run; its §10 states why that is legal now and leaves the judgement
> with the owner.

- **`--mode slopes` grades the promote arm as `slope ≤ −DELTA` alone**, relying on
  the floor binding. The floor does bind on all four seeds, but that is an argument
  from the measured SE rather than a bootstrap p-value computed on each simulated
  panel, which would cost 3000 × 9999 fits per grid point.
- **The pooled equivalence-arm figures mix two bootstrap seed schemes.** The two
  earliest runs used `seed + index`; the rest use `seed + 1e6 + index`, which keeps
  a panel draw and the resampling of that same panel out of one seed. The paired
  experiment bounds the effect at 0.5% of the half-width and 3 arms in 120 panels.
- **The unexplained seed.** One of eight seeds puts the equivalence arm 3.5 standard
  errors above the other seven and reproduces exactly. It is in the pooled figure
  rather than excluded, because excluding a reproducible observation for being
  inconvenient is not a method.
- **Not addressed:** the direction mismatch in `a20_power.py` recorded in the
  preflight memo §7.3 (the simulator measures two-sided, this registration tests
  one-sided), and rule 2's α-spending for this confirmation.

## 11. Results

**Status: the look is SPENT.** It ran once, on 2026-11-07 between 11:08:38Z and
11:08:59Z, against the VPS production stores. The verdict was computed and printed
by `decide()`, never chosen: **RETIRE**, conclusion **`inconclusive`** (arm (ii) of
the pre-committed three-way), live scorer tilt **`unchanged`**. It is terminal under
ledger rule 4 and is recorded in §4 of the ledger, row 2026-11-07.

Two things in this memo are wrong for the panel the run used, and this section
corrects both rather than inheriting them: §4's and §10.1's claim that the FLOOR
binds (§11.5), and §10's operating characteristics, which were measured on a panel
one day staler than the one that produced the verdict (§11.5).

### 11.1 Provenance

The script writes nothing to disk and prints no provenance of its own, so the
surrounding fields were captured into the same log around the run.

| field | value |
|---|---|
| run window (UTC) | 2026-11-07T11:08:38Z to 11:08:59Z |
| host | `vmi2478967` (the VPS production host) |
| repo `HEAD` at the run | `43dd277ecaae2f6ed271461f9f54790c903e2b7b` |
| script sha256 | `80881456a8b055cddf174f40eda7b243daedfded425caa4b492606493be29f97` |
| captured log sha256 | `79ff479200275ca909fc90d964436bdd55bd50d35cd07459c5e95e166f3f39fc` |
| labels store | `~/.alphalens/selection_labels`, passed explicitly as `--labels-dir` |
| briefs store | `~/.alphalens/thematic_briefs`, passed explicitly as `--briefs-dir` |
| frozen rule, echoed from the module | `SEED=20260924 N_BOOT=9999 CI_BOOT=2999 ALPHA=0.05 DELTA=0.1 CI_LEVEL=0.9 ALT=less POP=briefed CUTOFF=2026-07-05 FLOORS=300/30` |
| libraries | pandas 3.0.6, numpy 2.5.0, scipy 1.18.0 |
| labels fingerprint | `42d81fab645b78ea9fccf9304d978b034ef4dc2460fb3a0a1258283459f3e313` |
| briefs fingerprint | `6fdab96786ef80863576bfe7ea862d9d72ba1c7ec30b9035c6ea12ff94344f1a` |

**What the fingerprint covers, and what it does not.** It is
`sha256` over the sorted list of `(file name, byte size)` for every `*.parquet` in
that directory, 172 files each. It does NOT read parquet contents, so computing it
spends nothing. It pins which files were present at which size and cannot detect a
same-size rewrite.

**Why the directories were passed explicitly.** `main` resolves both stores from
`Path.home()` and never echoes them. A run under a different `HOME` would silently
read a different tree, and this repository has already paid once for that exact
mechanism: a container's `Path.home()` resolved a default path to the wrong tree and
deleted nine dates in production. Passing the paths puts them in the captured shell
line.

**The registration's promised status census is not printed by `--run`.** The frozen
PANEL text says of the maturity filter "Counts per status are printed, because an
excluded row is a disclosure, not a detail", but only `preflight()` calls
`status_census`. `--preflight` was therefore run into the same log in the same minute,
immediately before `--run`, so the census and the verdict describe one store state.
It is outcome-blind and spends nothing.

**Three environment facts were checked before the run, not after.** The script on
the production host was byte-identical to the one on `origin/main` (same sha256);
the host's pandas and numpy are exactly what `uv.lock` pins; and `HEAD` on the host
is an ancestor of `origin/main`, so the registration had not moved under it. `HEAD`
`43dd277e` predates the merge of §10 (`cb44dc46`, 2026-11-07T00:57Z) by about eight
hours, which does not matter for the computation — that merge did not touch the
confirmation script — but it means the host's checkout did not contain §10 or this
section at the moment of the run.

### 11.2 The outcome-blind preflight, verbatim

Run into the same log in the same minute as the look, immediately before it.

```
PREFLIGHT — outcome-blind. Nothing here spends the look.

held-out briefed label rows past 2026-07-05: 1107
  immature                301  (27.2%)
  no_close_at_horizon       1  (0.1%)
  no_open                   1  (0.1%)
  ok                      804  (72.6%)

held-out brief parquets available to join: 124
resolved rows available to the panel: 804

THE PANEL: 394 ticker-episodes in 64 arrival clusters
  (rows above, episodes here — ledger rule 5 collapses a ticker reappearing within 5 sessions)
registration floors: >= 300 episodes, >= 30 arrival clusters
VERDICT: the panel clears both floors.
A panel below either floor is a VOID, not a RETIRE — the look returns unspent.
```

No VOID condition was reachable: the panel clears both floors with 94 episodes and
34 clusters of margin, the join is not empty, no signal column is degenerate (the
design's condition number is 1.4 and all three VIFs are defined), and no statistic
came back non-finite — had one, `decide()` would have raised `VoidError` rather than
returning a verdict.

**A statement in the registration about this count is wrong, in the unsafe
direction.** `_held_out_episodes` and `panel_shape` both document the blind count as
an upper bound on the run's, by however many resolved rows carry a null outcome or
signal. That is not a bound. Dropping an incomplete middle row splits one chained
episode into two under the rule-5 collapse, so the with-signals path can report MORE
episodes than the blind path. Here the two agreed exactly at 394, so nothing turned
on it, and the error is recorded rather than fixed in a frozen file.

### 11.3 The three registered statistics, each with its construction

| statistic | value | construction |
|---|---|---|
| standardised partial slope | **−0.0713** | slope of `z(technical_atr_pct)` on `z(sel_ar_20)`, fitted JOINTLY with `technical_ma50_distance_pct` and `n_gates_passed`, episode-weighted, one vote per episode, no winsorization |
| one-sided p | **0.1782** | restricted wild cluster bootstrap on arrival-session clusters, alternative `less`, B = 9999, seed 20260924; returned as `(1 + hits) / (1 + valid)`, so 0.1782 is exactly 1782/10000 |
| 90% interval | **[−0.1864, +0.0490]** | equal-tail percentile interval over 2999 resamples of arrival sessions, re-standardised per draw |

Diagnostics, which the registration labels as reported and never a clearing path:
CR2 t on ATR **−0.976** over 64 clusters; condition number of the design **1.4**;
VIF `technical_atr_pct` 1.09, `technical_ma50_distance_pct` 1.11, `n_gates_passed`
1.05; univariate slope **−0.0703**, a sensitivity read and not a second chance.

**The interval and the p-value agree, and the small gap between them is method, not
noise.** For a `less` alternative the 90% equal-tail interval's upper endpoint is the
one-sided 95% upper limit, so `hi = +0.0490 > 0` is the same statement as `p > 0.05`.
Read as a normal quantity the interval implies a one-sided p of 0.1595 against the
printed 0.1782. The 0.019 gap is larger than the Monte Carlo noise of either figure
(sd about 0.004) and comes from the two procedures differing: a restricted wild
bootstrap with CR1 studentisation against an equal-tail percentile interval.

### 11.4 The verdict, as computed

```
VERDICT: RETIRE — conclusion 'inconclusive'
LIVE SCORER TILT: unchanged
```

Each field traces to one clause of `decide()`, and re-running
`decide(beta=-0.0713, p_value=0.1782, ci=(-0.1864, 0.0490))` on the printed numbers
reproduces the triple exactly:

- `RETIRE` because `promoted = p_value < ALPHA and beta <= -DELTA` fails on **both**
  legs. The p-value missed by 0.1282 (0.1782 against 0.05) and the slope missed by
  0.0287 (−0.0713 against −0.10). This was not a narrow miss on one leg.
- `inconclusive` because `inside = lo > -DELTA and hi < DELTA` fails on the LOWER
  endpoint only: −0.1864 sits past −0.10, while +0.0490 sits 0.0510 inside +0.10.
  The interval still covers actionable effects of the registered sign, so arm (iii)
  is shut and the cluster retires operationally rather than being falsified.
- `unchanged` because of the `elif beta < 0` branch. The July kill trigger for the
  live tilt fires on a sign flip; the slope is negative, so it did not fire.

**No threshold is close enough for the print precision to matter.** `full_run`
prints the slope at four decimals while `decide` compares full precision, so the
rounding hides at most 0.00005. The nearest slope-scale boundary is 574 times
further away than that. The p-value is not rounded at all.

**Rule 4's word is "fails" and this arm is "inconclusive"; the retirement is still
automatic.** It is this registration's own pre-commitment, §5: anything that is not
a PASS retires cluster 1 for selection and ordering. It is not a finding that ATR
failed. The run's own closing text says "Cluster 1 is RETIRED for selection and
ordering under ledger rule 4", and that scope is imported from rule 3 and §4.1
rather than from rule 4, which carries no scope of its own and whose operative
content is that the cluster is not re-tested in later sweeps.

### 11.5 The realized precision — §4's and §10.1's binding condition is WRONG for this run

> **CORRECTION — 2026-11-07, after the run.** §4 states, and §10.1 reports as
> confirmed, that "the floor, not the p-value, is the binding condition" because the
> one-sided significance boundary sits at about −0.091. **That is false on the panel
> this run used.** Both sentences are left as written; this box is the correction.
> The same claim appears in the module docstring under "WHAT A PASS DOES AND DOES
> NOT PROVE" and in ledger §4 note 23.B. Note 23.B is superseded by note 07.F of the
> ledger's 2026-11-07 row. **The module docstring is NOT superseded** — it still
> carries the sentence unmarked, because amending the operative registration is a
> separate dated act (§11.11 item 1).

The run's own output measures its precision three independent ways, and they agree
with each other and disagree with §10:

| reading | arithmetic | implied slope SE |
|---|---|---|
| 90% interval half-width | 0.1177 / 1.6449 | **0.0716** |
| printed CR2 t | 0.0713 / 0.976 | **0.0731** |
| printed bootstrap p | SE a normal needs for p = 0.1782 at β = −0.0713 | **0.0773** |
| §10.1, simulated, 4 seeds × 3000 panels | — | 0.0538, between-seed range [0.0525, 0.0553] |

The three realized readings sit 1.33 to 1.44 times the simulated figure. The gap
survives the sampling noise of a single-panel SE estimate: at 64 clusters the CR2
reading's own 95% band is [0.0603, 0.0858], whose lower end still clears the top of
§10.1's range. A fourth, independent corroboration points the same way: §10.4
publishes a simulated mean interval half-width of 0.087 with sd 0.0127, and the
realized half-width of 0.1177 is 2.4 of those standard deviations above it, a ratio
of 1.353 — the same factor.

**What that changes.** The one-sided 0.05 boundary on this panel was about −0.118 to
−0.127, not −0.0885. It therefore sits PAST the −0.10 floor on all three readings, so
the p-value leg, not the floor, was the binding condition: a slope of exactly −0.10
would have earned a p of **0.081 to 0.098** — 0.0811 at the interval reading, 0.0855 at
the CR2 reading, 0.0979 at the bootstrap reading — and would not have promoted. This is the exact state in which
`2026_11_a20_operating_characteristics.py` refuses to publish a promotion grid, via
`FloorDoesNotBindError`. That refusal did not fire before the run because it was
evaluated against the simulated SE, which is the only SE that existed then.

**§10.2's and §10.3's tables therefore overstate this look's power**, and the
realized figures belong beside them. Recomputed informally by normal approximation
at the realized SE — not a re-run of the probe:

| true \|ρ\| | §10.2 publishes | at the realized SE |
|---|---|---|
| 0.10 | 48.9% | **36.3% to 40.2%** |
| 0.15 | 82.5% | **61.6% to 67.4%** |
| 0.189 | 95.2% | **78.8% to 84.0%** |

Each range is the spread across the three SE readings; promotion is scored as
`P(slope ≤ −max(1.6449 × SE, 0.10))`, so the binding leg is whichever of the two is
stricter — which, at these standard errors, is the p-value leg throughout.

**The unblocking condition that released this run is the uncomfortable number, and it
is not the 0.189 row.** That condition is at least 80% power at 50% of the discovery
effect, and the power preflight computes it against the BURNT standardised ATR effect
of −0.358, so the gate point is **−0.179** rather than the −0.189 §10's grid uses
(which is half the July ROW-LEVEL −0.378). It was measured at **91.5% ± 1.4%** on
2026-11-06 at seed 20260922, and 92.2% ± 1.3% at seed 7. At the realized precision the
same point reads **74.9% to 80.4%**, which STRADDLES the 80% bar rather than clearing
it: only the most favourable of the three SE readings reaches it at all. **The gate was met honestly and outcome-blind
before the run**, on the only measurement available; this is a retrospective
limitation, not a protocol breach. §10.7 had already named the mechanism in advance:
"the measured SE is the spread of a SIMULATED sampling distribution, not of the real
one ... these figures are optimistic by an unmeasured amount". The amount is now
measured, and it is a factor of 1.33 to 1.36 on the standard error.

**§10's panel is not the run's panel.** §10 measured 385 ticker-episodes in 63
arrival clusters, read 2026-11-06; the verdict came from 394 in 64. The stores are
mutable and moved overnight by rows entering EXISTING files, which is measured and is
not the same thing as maturity alone. The file count stayed 172 with the newest stem
unchanged at `2026-11-06`, yet the census total went 1079 to 1107. Maturity
transitions conserve that total and can only DECREASE `immature`, and `immature` went
297 to 301, so at least **28 rows entered the census that were not in it on
2026-11-06**. The mechanism is that the nightly stamper REWRITES per-date label
parquets for PAST brief dates: read on the host 2026-11-07, the files with stems
`2026-10-24` and `2026-10-25` both carry an mtime of 2026-11-07 08:09, and every stem
from `2026-10-30` to `2026-11-06` was rewritten that morning. Two moves are therefore
mixed into the 1079 → 1107 step — statuses maturing (resolved rows 779 to 804, and the
single `split_unchecked` row resolving away) and rows being added. WHY rows are added
to a past date's file is NOT established here and is not needed for the verdict. What
it does establish is that a store fingerprint over names and sizes is the only thing
pinning which panel produced this result, and that a same-size rewrite would defeat
even that. The direction is harmless for the SE — a larger
panel can only tighten it, and rescaling 0.0538 by `sqrt(385/394)` gives 0.0532 — so
it explains none of the 1.35x gap above. §10's figures describe a panel one day
stale and must not be quoted as measured on the panel that produced the verdict.

> **NARROWED 2026-11-07, same day.** The 1.33-to-1.44 range above mixes one
> measurement with two back-derivations and should be read with that stated. Only
> the CR2 reading of 0.0731 is a direct SE estimate; the 0.0716 assumes the
> registered interval is symmetric, which §10.4 measured that it is not, and the
> 0.0773 assumes the wild cluster bootstrap null distribution is normal. The single
> honest figure is **1.36x**. Separately, the realized reading sits at about the
> 99th percentile of the SIMULATED distribution of a one-panel CR2 SE, which has a
> relative standard deviation of 13.3% over 12000 panels — so the gap was never
> impossible under the published simulator, and the case for a biased centre rests
> on the mechanism measured in `a20_simulator_precision_gap_2026_11_07.md` §7,
> not on the percentile. Nothing above is withdrawn: the FLOOR still did not bind
> and the p-value leg was still the binding one.

### 11.6 The conclusion arm: (ii), and why not (iii)

Arm (iii) — evidence against actionable effects of this signal, as instrumented —
was not close on either characterisation of its firing condition:

| characterisation | firing region | where −0.0713 sits |
|---|---|---|
| §10.4, measured on 3200 panels across 8 seeds | slope in [−0.0275, +0.0254] | 0.0438 below the lower firing edge |
| the withdrawn disclosure the docstring still carries | slope in about (−0.009, +0.009) | 0.0623 below the lower firing edge, i.e. 6.9 half-widths beyond it (7.9 measured from the band's centre) |

**Quote §10.4's figures, not the docstring's.** The operative registration still
reads "roughly a 13% chance even if the true effect is exactly zero" with a band of
about (−0.009, +0.009), and unlike its other withdrawn figure that sentence carries
no supersession marker. §5 and §10.4 supersede it: the arm fires **21.6% ± 0.7pp**
under a true zero, and its firing range is asymmetric and about three times wider
than a symmetric reading implies. Amending the docstring is a separate dated act
and is not taken here.

Containment is strict by design. The rule is `lo > -0.10 and hi < 0.10`, and §5
states that an interval whose endpoint sits exactly on ±0.10 has not excluded it;
`test_an_interval_touching_the_bound_is_not_evidence_against` pins that.

### 11.7 The live scorer tilt, and the July three-way's open branch

**Nothing in the live tier changes, and nothing changes automatically.** The July
kill trigger as folded into this registration fires on a sign flip and the slope
stayed negative, so `atr_penalty` and
`SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"` keep running: no version bump, no cohort reset, and the EDGE poolability cohort is
not cut. The question posted on #1227 before the run — whether one of the three
outcomes would cut the panel a later model trains on — resolved in the benign
direction. No code, test, configuration, systemd unit or CI workflow reads the
#1227 verdict or any cluster status, so every consequence of this run is
documentary.

**What "the trigger did not fire" does and does not cover.** The July line is a
two-prong OR: retire the tilt if fresh-data ATR-rank rho10 is at most 0, **or** if
C_l4only is no longer worse than V1. The 2026-09-17 fold-in carried only the SIGN leg
into this registration, which §6 records — `decide()` takes a slope, a p-value and an
interval and has no input for a config comparison. So the sign leg is settled and did
not fire; **the C_l4only-versus-V1 leg has not been re-run on fresh data at all**, and
this look spent the held-out episodes it would have needed. The live tilt keeps
running because the leg this registration owns did not fire, not because both legs
were checked.

**Branch (c) of the July kill line is now textually in play and is NOT settled by
this output.** That memo's §5 makes the kill line three-way, and the confirmation's
tilt mapping expresses only two branches. Branch (c) reads: ATR alive in exactly one
volatility state means do NOT retire silently, and register a conditional-penalty
candidate (`atr_penalty` active only when `vol=high`) for the FOLLOWING window, as
its own `SCORER_CONFIG_VERSION` and its own Bonferroni entry. The printed per-regime
arms read on their face like that pattern. They cannot adjudicate it, for two
reasons: §5 registered the comparison as an episode-clustered contrast of ATR-rank
rho10 on `car_10` between the two volatility sub-windows, which was not run; and the
printed arms carry no p-value and no interval, so neither "alive" nor "dead" is
decided for either state. Producing that contrast now would be a new look on
held-out episodes the ledger has no slot for. **Nothing may be tilted conditionally
on the strength of −0.3160.** Branch (c) is recorded here as an OPEN owner decision,
not as discharged.

### 11.8 The one registered interaction: estimated, and not a finding

The sufficiency check passed and the interaction was estimated, which both §7 and
the module docstring expected would not happen:

```
VOLATILITY INTERACTION (the one registered interaction):
  4 independent regime episodes — estimated below
  bull_quiet: slope -0.0144 on 322 episodes
  bull_volatile: slope -0.3160 on 72 episodes
```

**These two numbers are a disclosure, not a result, and they are the most seductive
numbers in the output.** Six reasons, each sufficient on its own:

1. **The two slopes are not on a common scale**, so the gap between them is not a
   difference in effect. `atr_slope` re-standardises whatever frame it is handed, so
   each arm reports `beta_raw(arm) × sd(ATR within arm) / sd(y within arm)` — its own
   units. Range restriction in the quiet arm attenuates its number mechanically,
   with no change in the underlying relation. Identical raw slopes can print as
   −0.06 and −0.61.
2. **It passed its floor with zero margin.** `MIN_VOL_EPISODES = 4` and the count was
   exactly 4 — the weakest possible pass, and the only threshold in the entire
   output with no slack. The count is maximal runs of consecutive arrival sessions
   sharing one regime label, so 64 sessions collapsed to 4 runs, i.e. three label
   changes in the whole window. With exactly two states observed that forces **two
   contiguous calendar stretches per arm**. Neither slope rests on 322 or 72
   independent observations of a regime.

   The counter's bias does NOT run only one way, contrary to what
   `volatility_sufficiency`'s own docstring claims. That docstring is right about the
   calendar-gap merging it describes, which can only undercount. It does not cover a
   second path: the function takes one label per arrival with
   `drop_duplicates(subset=["arrival"])` applied BEFORE `sort_values("arrival")`, so
   when several brief dates map onto one arrival session and disagree about
   `market_state`, the row that wins is decided by the panel's incoming order rather
   than by any rule, and an arbitrary pick can manufacture a label change and
   OVERcount runs. At a count of exactly 4 against a floor of 4, one spurious change
   is the whole difference between "estimated" and "insufficient".

   **Measured on this panel 2026-11-07, after the look was spent, so it spends nothing
   and reads no outcome: that path exists but did not fire.** Of the 64 arrivals, **1**
   carries more than one `market_state` value (at most 3 distinct brief dates map onto
   one arrival), and the regime-episode count is **4 under all three pick rules** — the
   script's incoming-order pick, the lexically first label per arrival, and the
   lexically last. The decision to estimate was therefore on the safe side of the
   floor on this panel, by measurement rather than by the argument the docstring gives.
3. **The arms pool across the `market_state_config_version` key**, which the
   market-state registration says is never pooled. `_read_brief` does not read that
   column, so the script cannot partition even in principle. There are **three** cohorts in the
   held-out window, not two: `mstate-v1` (from 2026-07-05), `mstate-v1.1`
   (2026-09-24, #1524) and `mstate-v1.2` (2026-09-25, #1559), the middle one spanning
   the single day between the two bumps. Across the 81 v1 dates `market_state_vix`
   held one value and never reached its threshold, so every volatile label there came
   from the realized SPY-ATR-quantile leg alone, and the v1.2 cohort carries one label
   value across all 33 of its dates.
4. **It is not the registered statistic.** July registered an episode-clustered
   comparison of ATR-rank rho10 between `vol=high` and `vol=low` on a single
   two-state axis, with the full four-state grid explicitly outside the family. What
   ran is two separate standardised partial OLS slopes grouped by the full
   four-state `market_state` label, with no contrast statistic and no clustering on
   the contrast.
5. **There is an alternative mechanism the output cannot exclude.** `sel_ar_20` is
   beta-adjusted against IWM with a raw 250-session beta and no intercept, so
   residual market exposure survives, and it survives largest exactly when realized
   market volatility is high. High-ATR names carry high beta. Two volatile stretches
   with any net market move produce a negative ATR slope with no ATR effect present.
   This was not tested — testing it needs held-out outcomes — and is stated as an
   unexcluded mechanism, not a measurement.
6. **One output, two standards of stringency.** The pooled estimate was not allowed
   to exist below 300 episodes and 30 clusters; an arm slope prints at 72 episodes
   with no per-arm episode floor and no per-arm regime-episode floor.

Under the single most favourable set of assumptions the arm gap is a nominal 2.2
sigma, and it stops being even nominal once the slope is allowed to differ between
calendar stretches, which two stretches per arm cannot measure. The arms also do not
conflict with the pooled verdict: the pooled slope is, to within 2.5%, the
episode-weighted average of the two (−0.0695).

**§7's and ledger note 23.G's prediction was wrong about the data as well as the
outcome.** Both expected "insufficient, not estimated" because `market_state_vix` was
frozen across the held-out dates; it was frozen only to 2026-09-23 and #1524 fixed
it. The July memo reserves one charge for this interaction test, and the ledger row
of 2026-11-07 records that the charge was spent.

`market_state` is point-in-time and is not recomputed: the nightly thematic build
stamps it with `asof` equal to the brief date, and the confirmation reads the stamped
column off the brief parquet. Its two caveats are lags rather than lookahead.

### 11.9 What this result rules out, and what it does not

**The honest description is "insufficient precision to settle it", not "no evidence
of an effect".** The cleanest single fact behind that choice: the 90% interval's
half-width (0.1177) is LARGER than the smallest actionable effect it was asked to
rule out (DELTA = 0.10). A look whose interval is wider than its own decision
threshold cannot separate "zero" from "actionable" in one shot, by construction.

At the realized SE of about 0.072 the estimate sits 1.00 standard errors from a true
zero and only 0.40 from a true −0.10. This is a precision statement on a normal
approximation, offered as such: the registration pre-committed no prior, and §10's
P(promote) figures are frequencies of a DECISION given ρ, never a likelihood for ρ.

**What the interval does rule out is the discovery-scale effect.** The July row-level
discovery figure of −0.378 and the burnt episode-level standardised effect of −0.358
both sit 0.17 to 0.19 outside the lower endpoint, about four realized standard errors
from the estimate. That is the only magnitude claim this result licenses, and it is a
real one: the discovery magnitude did not survive out of sample.

**The effect size the run was UNBLOCKED against is not excluded.** Two different
"half the discovery effect" figures are in play, and they fall on opposite sides of
the interval, so they must not be conflated:

| figure | basis | against the lower endpoint −0.1864 |
|---|---|---|
| **−0.179** — the power gate's own point | half the BURNT standardised ATR effect of −0.358, which `a20_power.py` reports beside the 91.5% (§11.5) | **0.0074 INSIDE the interval** — not excluded at all |
| −0.189 — §10's grid point | half the July ROW-LEVEL discovery figure of −0.378 | 0.0026 outside |

So the effect the unblocking condition was written against remains fully consistent
with the data, and §10's grid point sits at the very edge of what the data still allow
rather than outside it: a margin of 0.0026 is well inside the noise of the interval.

**What the result does NOT rule out is an actionable effect of the registered sign.**
The interval covers the whole band from −0.10 out to −0.1864, so an effect up to
nearly twice DELTA remains consistent with the data. This memo cannot say that ATR
carries no actionable effect for selection.

**A sibling held-out ATR statistic was already on the record, and it does not
corroborate.** `experts_last_look_2026_09.md` §6 Part B published, on 2026-09-29, a
rank correlation of +0.089 for a `−ATR` ordering — so ATR against outcome of about
−0.089 — over 260 episodes in 47 arrival clusters across 205 tickers, on brief dates
2026-07-06 to 2026-09-14. The sign and rough magnitude agree with −0.0713, and that
agreement carries less than it appears to:

- It publishes **no interval of its own**. The p of 0.0666 and the 99.286% interval
  quoted beside it belong to an elastic-net-minus-baseline delta of −0.125, not to
  the baseline correlation, so the figure states a point and no precision.
- Comparability is limited on four axes: outcome column (`car_10` against
  `sel_ar_20`), horizon (10 against 20 sessions), statistic (a pooled out-of-fold
  rank Spearman on a univariate ordering against a standardised partial OLS slope
  fitted jointly with two covariates), and panel (that run was restricted to
  `source == 'thematic'`). §3 already warns that a partial correlation and a
  standardised slope are not the same scale.
- **The two panels are not independent.** The experts window lies entirely inside
  this panel's window, off the same label store, the same brief store, the same
  briefed population and the same rule-5 episode unit, so most of those 260 episodes
  are very likely among these 394. The exact overlap was not measured, because
  counting it means reading the held-out stores. Two reads sharing one upstream store
  are one body of data seen through two instruments.
- **It could not have contaminated this registration.** The docstring froze
  2026-09-23 and that run was 2026-09-29, so +0.089 could not have informed DELTA,
  the direction or the estimand.

One ledger question this raises and does not settle: that figure was a held-out
ATR-versus-outcome statistic charged to cluster 15 rather than cluster 1, while rule 1
says no look happens off-ledger. Whether a fit-free comparator spends cluster 1's
budget is the same class of owner interpretation as the rule-4 cap scope decided on
2026-11-06.

### 11.10 Retrospective limitations

Each with the construction that produced it. The limitations stated in §9 before the
run all still apply; these are the ones the run itself added or measured.

- **The realized SE was about 0.072 against §10's simulated 0.0538**, so the binding
  condition flipped from the floor to the p-value and the look's power was lower than
  the table it was released against (§11.5).
- **The unblocking gate straddles its own bar retrospectively.** The condition that
  released the run — at least 80% power at 50% of the burnt standardised ATR effect,
  so at ρ = −0.179 — was measured at 91.5% ± 1.4% and reads 74.9% to 80.4% at the
  realized precision. It was met honestly and outcome-blind on the only measurement
  that existed beforehand, and it is the single figure in this registration that the
  run's own output most sharply contradicts (§11.5).
- **§10's figures describe a panel one day stale** (385 episodes in 63 clusters
  against the run's 394 in 64), and the stores are mutable (§11.5).
- **The panel is narrower than `POPULATION = "briefed"` names.** §10.6 measured 19 of
  779 resolved briefed held-out rows with no brief row to join, from
  `pre_open_recovered` names under #1494, against 75 of 466 on the burnt side. The
  two panels differ in composition in a way no frozen constant describes, and the
  exclusion is symmetric with discovery but is selection on post-hoc membership.
- **Arm (iii)'s advance disclosure was wrong by a factor of about 1.7 in probability
  and about three in band width** (13% and ±0.009 as disclosed, against 21.6% ± 0.7pp
  on 3200 panels across 8 seeds with an asymmetric firing range to about ±0.027). It
  did not bite, because the arm was not close (§11.6).
- **The blind-path count is not an upper bound on the run's**, contrary to two
  statements in the registration (§11.2).
- **The two items §10.7 leaves open are not closed by this run:** the one-sided
  against two-sided direction mismatch in `a20_power.py` recorded in the preflight
  memo §7.3, and rule 2's α-spending for this confirmation. The second now matters
  retrospectively: the look applied a plain one-sided 0.05 while the ledger's own
  program-level charge is 1.76e-4 per cluster, and at that level the p-value would
  have bound by a far larger margin.
- **Three reporting defects in the interaction block**, none of which can change this
  spent look: `_report_interaction` prints the arrival-cluster count only on the
  branch that REFUSES to estimate, so the run's own record states no per-arm cluster
  count — recovered by a separate outcome-blind read after the look was spent,
  `bull_quiet` 54 arrival clusters over 322 episodes and `bull_volatile` **11** over
  72, the latter barely above `MIN_REGIME_CLUSTERS` = 10; the
  interaction block is the only numeric block printed without a "never a clearing
  path" caveat; and `_report_interaction` has no test.
- **A VOID would have disclosed the statistics it declares unspent.** `decide`'s
  `VoidError` message interpolates the slope, the p-value and the interval, and `main`
  prints it to stderr. The run did not void, so nothing turned on it.
- **`validate="m:1"` on the brief merge raises `MergeError`**, which is a `ValueError`
  and not a `VoidError`, so `main`'s handler would have missed it and the run would
  have died with a traceback and exit 1 rather than the registered exit 8. The look
  would still have been unspent, because the merge precedes every statistic.
- **A degenerate or non-finite OUTCOME column is caught nowhere.** `held_out_panel`'s
  variance loop covers the ATR column and the two covariates only, `_z` zeros a
  degenerate column instead of raising, and the NaN that should have voided is created
  and discarded inside `wild_cluster_bootstrap_p`. On a synthetic panel with a constant
  outcome the run prints `RETIRE / evidence-against / tilt retired` with no void
  anywhere. It was adjudicated before the run and did not block it, because the
  degenerate case announces itself in five places at once — a zero slope, a
  zero-width interval from 2999 resamples, a non-finite CR2 t, a zero univariate
  slope and a runtime warning — and because the producer closes the non-finite half
  by construction (`selection_label._positive` rejects any non-finite value before a
  status of `ok` is written). A pre-agreed reading was recorded before the run: a
  slope printing exactly `+0.0000` beside a zero-width interval was to be treated as
  a VOID in the clause's spirit. It did not apply: the slope is −0.0713 and the
  interval is 0.2354 wide.

### 11.11 What this closes, and what stays open

**Closed permanently.** Cluster 1 for brief SELECTION and ORDERING under ledger
rule 4, with no re-test and no sweep re-look left; and cluster 1's single rule-3
confirmation slot, now spent. The owner's decision of 2026-11-06 states that a
cluster retired under rule 4 has nothing to return to.

**Untouched.** ATR in execution geometry, a different estimand on the ledger's §4.1
budget, whose annex header says it charges the T5/T7 walk-forward budget rather than
the §3 covariate family; both registered lenses there, `atr_bracket_1p5` and
`atr_bracket_1p5_planned`, still read PENDING. ATR as a CONTROL in burnt-panel
probes. The ATR-only baseline that owner decision D5 requires beside every model.
And the live tilt itself.

**Corrected, and not what the registration says.** The docstring's FAMILY = 1
paragraph states that the MA50 extension and the press gate "keep their ledger looks
unspent". This look charges nothing to clusters 2 and 9, but the ledger's appended
note of 2026-11-06 settled that cluster 9 stands AT rule 4's cap of two re-looks and
cluster 2 one PAST it. Neither holds a spare sweep look; each holds one rule-3
confirmation, under the owner's "outside the cap" interpretation of that date.
Cluster 1's cumulative look count reads **4** after this run, not 3.

**Open, and each is an owner decision rather than a gap this memo can fill.**

1. ~~**The dated amendment to the operative registration.**~~ **TAKEN 2026-11-07.**
   The item read: the module docstring still reads "WHY IT MAY NOT RUN YET" with
   "IT IS NOT MET", still carries the withdrawn 13% arm-(iii) disclosure unmarked,
   still cites a withdrawn 419-episode panel in its frozen block, and still states a
   look count the ledger settled against. The amendment is the `THE RUN` block at the
   end of that docstring, with short in-place markers on each superseded passage. It
   adds dated prose only: all 19 frozen constants were compared against the
   pre-amendment file and none drifted, and `decide()` was re-run on six branch cases
   including this run's own verdict with no change. Every superseded sentence is left
   as written and named in the block, per the append-only convention.
2. **Branch (c) of the July kill line** (§11.7): register a conditional-penalty
   candidate for a forward window, or record that the branch is unadjudicable on the
   evidence that exists and close it.
3. **Whether §4 and §10.1 get their own correction PR** beyond the box in §11.5. The
   superseded sentence appears in four places.
4. **Two §3 cluster rows now point at a retired cluster**, and no document says what
   happens to them: cluster 16, the joint loser flag whose primary leg is "mostly ATR
   curvature", at 1 look with a status of "pre-register before ordering use" and a
   sunset of 2026-11 that is now due; and cluster 4 (ROIC), retired with "only
   residual-ROIC×ATR admissible". Whether a cluster whose content is largely ATR
   curvature may still enter ordering after cluster 1 was retired for ordering is not
   written down anywhere.
5. **Rule 2's α-spending for this confirmation** (§11.10), still not addressed.

### 11.12 The captured run, verbatim

The script's own stdout, unedited. The provenance block around it is in §11.1; the
full captured log is hashed there.

```
========================================================================
ATR HELD-OUT CONFIRMATION — #1227, ledger cluster 1, one-shot
========================================================================
panel: 394 ticker-episodes in 64 arrival clusters
population: briefed; outcome: sel_ar_20; held-out > 2026-07-05

standardised partial slope (ATR): -0.0713
one-sided wild cluster bootstrap p: 0.1782  (bar 0.05)
90% cluster-bootstrap interval: [-0.1864, +0.0490]
smallest actionable effect: |rho| >= 0.1

DIAGNOSTICS (reported, never a clearing path):
  CR2 t on ATR: -0.976 over 64 clusters
  condition number of the design: 1.4
  VIF: technical_atr_pct=1.09, technical_ma50_distance_pct=1.11, n_gates_passed=1.05
  univariate slope (SENSITIVITY ONLY, not a second chance): -0.0703

VOLATILITY INTERACTION (the one registered interaction):
  4 independent regime episodes — estimated below
  bull_quiet: slope -0.0144 on 322 episodes
  bull_volatile: slope -0.3160 on 72 episodes

========================================================================
VERDICT: RETIRE — conclusion 'inconclusive'
LIVE SCORER TILT: unchanged
Cluster 1 is RETIRED for selection and ordering under ledger rule 4.
ATR's use in execution geometry is a different estimand on the §4.1
budget and is untouched. See the pre-committed three-way conclusion
language in the docstring for what this null does and does not say.
========================================================================
```
