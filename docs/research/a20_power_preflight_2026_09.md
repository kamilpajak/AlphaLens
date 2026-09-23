# A20 power preflight for #1227 — is the ATR test powered, and if not, when?

**Status:** COMPLETE 2026-09-23. The pre-commitments in §2 were written and
committed in `1621be4b`, **before** the preflight ran; §4 is the recorded
answer. **The gate is met**: ATR power at 50% shrinkage on the pre-committed
briefed population is 83.9% against a bar of 80%. Nothing here registers #1227
and nothing here runs its confirmation.

## 1. Why this exists

#1227 is the single held-out confirmation of the three Bonferroni-clear signals
(ATR, MA50 extension, press gate), Holm step-down at FWER 0.05. It may run only
once a pre-registered, outcome-blind power simulation shows **≥80% power at 50%
of the July discovery effect for the ATR test**.

Owner decision D5 (`ml_label_registry_design_2026_09_16.md` §8 step 3) moved its
primary from `car_10` to `sel_ar_20`, so the July discovery effect has to be
recomputed on the new label before any power number means anything. The ledger
amendment recording that move is the 2026-09-21 row in
`edge_hypothesis_budget_2026_07.md` §4.

## 2. Fixed before looking

These are pre-commitments. They are in this document, in git, before the
preflight ran — which is the only thing that separates a design choice from a
rationalisation of the number it produced.

### 2.1 The confirmation population is the BRIEFED one

`#1227` confirms on names the brief actually carried (`briefed_any_theme`), the
population the July discovery ran on. **Not** on the wider set the label stamper
also writes, which includes names only the LLM proposed.

The reason is the estimand, not the arithmetic. Discovery found that ATR
predicts outcomes *among briefed names*; confirming on names the brief did not
take is a different claim wearing the same label. Measured 2026-09-21, matured
held-out episodes: **396 briefed, 573 including the LLM-proposed arm** — so the
wider set is the better-powered one, which is exactly why this choice has to be
made before the power curves are visible and on grounds that are not "more N".
(Those two counts are left as measured on 2026-09-21, because this section is
the pre-commitment record. §4 reports 419 and 610: the panel grew by one matured
arrival session between writing this and running it.)

The wider population's curve is reported in §4 anyway, as planning information
about a possible FUTURE and separately registered test. It is **not** an option
to fall back on if the briefed arm comes up short: doing that would be choosing
the easier test after seeing the result, the same forking path ledger rule 6
closes one level down.

### 2.2 The estimand, frozen

#1227's body says the exact specification is "frozen at registration". The power
simulation cannot be run without it, so these are the values it uses and the
ones this memo proposes for the registration:

| Item | Fixed as | Why |
|---|---|---|
| Outcome | stamped `sel_ar_20` | D5. Read from the label store, never recomputed in a script (registry memo §8 step 5: recomputing a label is a new definition). |
| Unit of independence | ticker-episode | Ledger rule 5. |
| Cluster | arrival session (`anchor_session`) | The dependence that matters is same-day co-movement. |
| ATR signal | `technical_atr_pct` as stamped on the brief | The discovery column, unchanged; re-deriving it would be a second definition. |
| Winsorization | none | Discovery reported a rank statistic; adding a trim here would be a new choice invisible in the comparison. |
| Weighting | episode-weighted | One vote per episode, consistent with the unit of independence. |
| Direction | one-sided, signs from discovery | #1227 body. |
| p-value | wild cluster bootstrap on arrival clusters | The engine already used by the neighbouring look. |

### 2.3 What the preflight may read

An allowlist, not a ban on label columns:

- **Held-out (> 2026-07-05): structure only** — arrival session, whether the
  horizon resolved, whether the brief carried the name. Not the label, and not
  the signal columns either: reading held-out `technical_atr_pct` to learn how
  the three statistics co-move would put the held-out dependence structure into
  a Holm power number without touching one label value.
- **Burnt (≤ 2026-07-05): outcome values are fair game.** Discovery is frozen
  and spent under ledger rule 3; `preflight_power_sim` in
  `2026_09_experts_last_look.py` already reads burnt-panel scale and ICC on the
  same footing, and the ledger's 2026-09-03 row records a burnt-panel read as
  no charge.

`scripts/ml/a20_power.py` enforces the first half in code and
`tests/test_a20_power_preflight.py` asserts the exact column set, with a control
that fails when anything is added to it.

### 2.4 If the computed date lands after 2026-10-14

#1227's `Wake: 2026-10-14` was derived under `car_10`, which matures 10 sessions
after arrival; `sel_ar_20` takes 20. Measured 2026-09-21: the matured frontier
sits exactly 20 sessions back, so that date reaches about **51** matured arrival
clusters rather than the ~60 the issue projects.

If §4 puts the gate later than 2026-10-14, the Wake line moves to the computed
date. That is not a slip: it becomes a date derived by arithmetic from a
measurement, which is what the wake convention requires, in place of one derived
under the wrong maturity.

> **Written after the run, and against the direction this clause anticipated.**
> There is no computed date at all: the gate is already met, so the
> cluster-count search never ran. This paragraph provided only for "later", and
> the answer turned out to be "now". The Wake line is therefore not replaced by
> a better date — it stops having a job, and whether it and the `waiting:data`
> label come off #1227 is an owner decision this memo does not make. Said
> plainly because "the deadline is gone, we can go early" is the self-serving
> reading, and it is worth recording that nothing here was chosen: the
> population was fixed in §2.1 before any power number existed, and the curve
> was computed without reading one held-out outcome.

## 3. Method

Held-out **structure** (episodes per arrival session, filtered to the population
being simulated) sets the cluster shape. Burnt-panel rows supply the outcome
scale, the ICC, and the **real signal matrix** — resampled whole into that
cluster shape, so the cross-signal correlation is the one the data has. Effects
are injected at 25 / 50 / 75% of the recomputed burnt A20 estimate, p-values come
from the wild cluster bootstrap, Holm is applied across the three, and power is
the share of simulations rejecting the ATR hypothesis.

Simulating independent normal signals instead — as the earlier template does —
would overstate power for three technical signals on the same names.

The recomputed burnt effect carries a cluster-bootstrap CI and power is reported
across it, so a single in-sample point estimate does not silently carry the gate.

**The simulated outcome keeps the panel's variance.** The injected signal takes
its share out of `sd_y²` rather than being added on top of it, so
`var_resid = sd_y² − var(Xβ)`, split into a cluster term and a within term by
the ICC. Return volatility is set by the market, not by how much of it we can
explain, so holding the total fixed is the physically right constraint as well
as the conservative one. §4.3 records that this was a correction, what it moved,
and the alternative that was rejected.

**A power number whose error bar crosses the bar is not a verdict.** The run
reports a 95% Wilson interval around the simulated power and refuses to call the
gate when that interval contains 80%, saying to re-run with more simulations
instead. The Wilson form is used rather than `p ± z·SE` because the plain form
collapses at the boundary: 4 rejections out of 4 gives a standard error of
exactly zero and would claim certainty.

## 4. Results

One run, 2026-09-23, `scripts/ml/a20_power.py`, 4 000 simulations at the gate.
The numbers in §4.2 and §4.3 come from earlier runs of the same script and are
kept because they record how the answer moved.

**Burnt-panel A20 effects** (standardised, jointly fitted, 197 episodes; 90%
cluster-bootstrap interval). These are the same in both population arms, and
that is forced rather than chosen — see the last bullet of §5:

| signal | effect | 90% interval |
|---|---|---|
| ATR | **−0.378** | −0.451 … −0.275 |
| MA50 extension | −0.303 | −0.433 … −0.128 |
| press gate | +0.026 | −0.054 … +0.116 |

**The cost D5 warned about did not materialise.** July reported `rho = -0.35` for
ATR on `car_10`; on `sel_ar_20` it is **−0.378**, slightly stronger. The delay
below comes entirely from A20 taking twice as long to mature, not from a weaker
signal.

**Power**, held-out panel as it stands: 35 matured clusters, accrual 1.00
cluster/session, sd(y) 0.165, ICC 0.06.

| shrinkage | signal | briefed (419 ep) | all (610 ep) |
|---|---|---|---|
| 25% | ATR | 22% | 34% |
| **50% — the gate** | **ATR** | **83.9% ±0.6** | **95.7% ±0.3** |
| 50% | MA50 | 69% | 85% |
| 50% | press gate | 5% | 5% |
| 75% | ATR | 100% | 100% |

**Verdict: the gate is MET on the pre-committed briefed population.** ATR power
at 50% shrinkage is 83.9%, its 95% interval is 82.8–85.0%, and the bar is 80%.
The interval lies entirely above the bar, so this is a resolved verdict and not
a number that happens to round the right way.

**There is no gate date, because there is nothing to wait for.** The
cluster-count search never ran: it stops as soon as power clears the bar, and
power cleared at the panel's current size. The `Wake: 2026-10-14` line on #1227
was derived under `car_10` and is now simply wrong in both directions — the
horizon doubled, and the gate is met anyway. Removing it is an owner decision,
not a consequence of this memo.

**The wider population is reported, not used.** §2.1 pre-committed the briefed
arm before any of these numbers existed, and it clears on its own. Reaching for
the wider arm after seeing both would be the choose-the-easier-test-afterwards
move that ledger rule 6 exists to stop. It also rests on an assumption the
briefed arm does not — last bullet of §5.

### 4.1 Two findings that are not about the gate

**The press gate cannot be confirmed or refuted at this sample size, and waiting
will not fix it.** Its burnt-panel effect is +0.026 with an interval straddling
zero, and power is 5% — exactly what a test of a null effect returns at α=0.05.
Running #1227 spends its one look on all three; for the press gate that look
will return nothing either way. That is worth knowing before the run, not after.

**MA50 at 69% is below the gate too**, and #1227's gate condition names only
ATR. Running today therefore reports one adequately powered test and two
underpowered ones. Whether that is acceptable is a registration decision. This
memo does not say when MA50 would clear 80% — the search targets ATR and never
ran, so that date is computable but not computed.

### 4.2 A correction made during the run

The first version of `gate_date` projected time for the missing arrivals to
*happen* and then mature, giving 2026-10-26. That double-counts: 55 arrivals
already exist in the held-out window and only 34 have matured, so what is
missing is maturity, not arrivals. The unit test asserted the same wrong model,
so it passed and confirmed the error. Both are fixed, and the test now says why
it exists. The five-week difference between 2026-10-26 and 2026-09-25 is the
size of that mistake.

Both dates come from the earlier, smaller panel and neither survives as an
answer — the gate is met and no date is emitted at all. They are kept because
the size of the mistake is the point.

The same shape appeared in the power number itself: 200 simulations gave 76%,
400 gave 79.75%, and both were reported to two significant figures while the
Monte Carlo error was ±2 points. Only the 4 000-simulation run (±0.7) could tell
"clears" from "does not".

### 4.3 The DGP was corrected AFTER the first number was seen

This is the part of the memo that most deserves suspicion, so it is stated
plainly. The first run reported 77.3% and a gate date of 2026-09-25. A review of
the code then found that the simulation drew its noise with variance `sd_y**2` —
the panel's TOTAL outcome variance — and added the injected signal on top. Every
simulated panel therefore had more spread than the panel it was calibrated to,
the test statistic divided by too large a residual, and power came out too low.

**The correction moves the number UP, toward the bar.** That direction was
knowable before re-running, which is exactly why the reasoning has to stand on
its own rather than on the answer it produced:

- **The original is not a defensible alternative; it is wrong.** A DGP whose
  simulated outcome has a spread the panel does not have is not a modelling
  choice. `TestTheSimulatedOutcomeKeepsThePanelsVariance` now asserts
  `std(y_sim)` against `sd_y`, which is the check that would have refused it.
- **The size was measured, not guessed.** R² on the burnt panel is 0.28
  (measured 2026-09-22, both populations). At the 50% gate the injected signal
  accounts for 0.25 × 0.28 = 7% of the variance, so the noise was about 3.7%
  too wide.
- **Of the two coherent repairs, this memo takes the MORE conservative one.**
  Holding the panel's TOTAL variance fixed and letting the shrunken signal take
  its share gives `var_resid = sd_y² (1 − 0.25 R²)`. Holding the panel's
  RESIDUAL variance fixed instead — `sd_y² (1 − R²)`, which is what the reviewer
  proposed — removes 18% of the noise rather than 3.7% and reports far more
  power. That version assumes the discovery R² is the true explained share,
  which is an in-sample quantity inflated by selecting these three signals out
  of a larger set. The conservative form is used.

**The correction did NOT by itself flip the verdict.** Two things changed
between the 77.3% run and the 83.9% run: the DGP was fixed, and a day passed, so
the panel grew from 34 matured clusters to 35. They were separated by re-running
the corrected DGP at the old cluster count (4 000 simulations, seed 20260922):

| | ATR power | 95% interval |
|---|---|---|
| original DGP, 34 clusters | 77.3% | — |
| corrected DGP, 34 clusters | **80.5%** | 79.2 – 81.7% |
| corrected DGP, 35 clusters | **83.9%** | 82.8 – 85.0% |

The 6.6-point move splits almost evenly: **+3.2 from the correction, +3.4 from
one more matured cluster.** And at 34 clusters the interval straddles the 80%
bar, so by this memo's own verdict rule (§3) the corrected DGP alone returns NO
VERDICT, not a pass. What resolves the gate is the extra data, on a statistic
that had already been repaired. A reader who rejects the correction should read
the answer as "unresolved, re-run with more simulations", not as "failed".

## 5. Limitations, stated before the numbers

- **The shrunken effect is still an in-sample quantity.** The three signals were
  selected on this panel; a different label does not undo that. The 25/50/75
  grid is #1227's own pre-registered discount, not a calibration — if the true
  out-of-sample shrinkage is worse than 50%, the gate passes while the test is
  underpowered, and nothing here would detect it.
- **The July ATR figure is row-level and early-weighted.** The published
  `rho = -0.35` is over rows, not episodes (231 of 523 rows repeat a ticker
  within 3 days), and `selection_score_v2_ext_tilt_decision_2026_07_06.md`
  records that pure ATR rank collapses late in the window. The recompute here is
  episode-level on a different label, so it is a fresh estimate rather than a
  restatement — but it inherits the same regime-dependence.
- **The wider population arm is calibrated on names it does not contain.** The
  burnt panel is briefed-only in BOTH arms, and not by choice: the label store's
  first non-briefed row is dated 2026-07-12, a week after the 2026-07-05
  discovery cutoff, so every one of the 501 burnt label rows is briefed
  (measured 2026-09-23, counts only). The `all` arm therefore takes a held-out
  cluster shape that includes 349 shadow names and fills it with effect sizes,
  an outcome scale and a signal matrix estimated entirely on briefed names. Its
  95.7% is "what power would be at 610 episodes **if** the extra names behaved
  like the briefed ones" — a bigger-N calculation, not a second estimate. The
  briefed arm carries no such assumption, which is a further reason the
  pre-committed choice is the one that decides.
- **Romano-Wolf would dominate Holm** under dependence, and
  `alphalens_research/backtest/romano_wolf.py` already exists and is tested.
  #1227 pre-registered Holm, so swapping it is a registration change and belongs
  in that decision, not here.
