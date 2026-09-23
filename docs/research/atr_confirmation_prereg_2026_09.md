# ATR × EDGE — pre-registration of the cluster-1 held-out confirmation (#1227)

**Status:** REGISTERED (frozen 2026-09-23, before any feature-vs-outcome statistic
was computed on the held-out panel)
**Cluster:** 1 (`technical_atr_pct`), §3 of [`edge_hypothesis_budget_2026_07.md`](edge_hypothesis_budget_2026_07.md)
**Looks:** 2 used (Jun sweep, Jul re-run — both DISCOVERY), 1 charged by this study
**Family:** 1 (ATR only). MA50 extension and the press gate are NOT in this look.
**Run trigger:** on or after 2026-09-24, i.e. once this registration is merged.
No further accrual condition — the power gate #1227 set for itself is met.
**Sunset:** none (cluster 1 carries no sunset date)
**Frozen code:** [`apps/alphalens-research/scripts/ml/2026_09_a20_atr_confirmation.py`](../../apps/alphalens-research/scripts/ml/2026_09_a20_atr_confirmation.py)
— its module docstring is the operative registration; this memo is the rationale
and the arithmetic behind it.

Ledger rule 4 makes this terminal. A cluster that cleared discovery and then fails
its held-out confirmation is retired and is not re-tested. There is one look, it
cannot be repeated, and the decision rule below was fixed before it ran.

## 1. Why now, and not later

#1227 wrote its own unblocking condition: run when an outcome-blind power
simulation shows at least 80% power at 50% of the July discovery effect for the
ATR test. Three preflights answered it, and all three are recorded elsewhere:

| question | answer | where |
|---|---|---|
| Is the ATR test powered? | 83.9% under a 3-member Holm family; **92.9% at the family of one used here** | [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md) |
| Does the 20-session window overlap break the inference? | No. Arrival-session WCB holds its level in every cell, worst corner 7.125% (95% CI [6.87, 7.38]) against a pre-specified 7.5% bar | [`a20_overlap_inference_2026_09.md`](a20_overlap_inference_2026_09.md) |
| Does mixed price adjustment reach a held-out label window? | No. The premise was withdrawn — both sources are split-adjusted | [`atr_split_adjustment_gate_2026_09.md`](atr_split_adjustment_gate_2026_09.md) |

The yfinance stale-OHLCV cache census ran for the overlap memo: three logged
fallback events in the whole journal, all inside the burnt window, none of the
three rows present in the panel, zero in the held-out window.

**Waiting longer was considered and rejected, in writing, before the run.** The
panel grows about one matured arrival cluster per session, so waiting to ~70
clusters would take roughly seven weeks. What that buys, and what it does not:

| true \|ρ\| | P(promote) at 35 clusters | at ~70 clusters |
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
| Population | BRIEFED only | Pre-committed in the power memo §2.1 before any curve existed. Discovery found ATR predicts outcomes *among briefed names*. |
| Weighting | episode-weighted | One vote per episode, consistent with the unit. |
| Winsorization | none | Discovery reported a rank statistic; a trim here would be a new choice invisible in the comparison. |
| Test | one-sided restricted wild cluster bootstrap, alternative "less", α 0.05, B = 9999, seed 20260924 | Direction fixed from discovery. |

## 3. The smallest actionable effect

**DELTA = |ρ| 0.10**, an owner decision of 2026-09-23.

On this estimand's own scale, with sd(`sel_ar_20`) = 0.1653 measured on the burnt
panel, ρ = 0.10 means **1.65 percentage points** of 20-session market-adjusted
return per one standard deviation of ATR. A realistic selection tilt that drops
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

**Operating characteristics, disclosed in advance.** Normal approximation at the
implied SE 0.0552, itself **back-derived from the recorded power curve rather
than measured** (0.189 / SE = 1.96 + 1.465; the same SE predicts 25% power at the
25%-shrinkage row against the 22% the power memo reported):

| true \|ρ\| | 0 | 0.05 | 0.10 | 0.15 | 0.189 | 0.25 |
|---|---|---|---|---|---|---|
| P(promote) | 3.5% | 18% | 50% | 82% | 95% | 99.7% |

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
zero. In practice this three-way will almost always land on (i) or (ii). Saying
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

## 10. Results

*(Placeholder. Filled by the single run of
`2026_09_a20_atr_confirmation.py --run`; the verdict is computed and printed by
the script, never by the analyst.)*
