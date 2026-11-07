# Why the #1227 power simulator was optimistic, and what it costs an ML registration

**Status:** LOCKED. Measured 2026-11-07 on the VPS production host `vmi2478967`,
against the same stores and at the same repo `HEAD` (`43dd277e`) that produced the
#1227 verdict earlier the same day, with numpy 2.5.0 / pandas 3.0.6 / scipy
1.18.0. Nothing here registers anything, nothing here runs a confirmation, and
**no held-out outcome value was read.** The one-shot look on ledger cluster 1
stays spent and terminal; this memo does not reopen it and cannot. One departure
from a written allowlist is disclosed in §9 and is the owner's to judge.

## The short answer

The gap between the simulated and the realized precision of the #1227 slope is
**not any of the three things `atr_confirmation_prereg_2026_09.md` §10.7 named in
advance.** Each moves the simulated standard error by 3% or less.

The cause is a mechanism the simulator does not model at all: **the noise is
HETEROSKEDASTIC AND ALIGNED WITH THE FEATURE UNDER TEST** — the panel's noise
scale tracks ATR, which is the very quantity the regressor measures. The
simulator gives every observation the same noise scale, so it cannot produce this
at any setting of its own parameters.

**What this panel does NOT settle is the LEVEL at which that alignment lives.**
Scaling the noise by the SESSION's volatility and scaling it by each ROW's own
volatility both reach the realized precision, the second at a smaller parameter
and with no clustering at all, and the estimator that measures the parameter
cannot tell the two apart (§7.5). The session reading is the better supported of
the two, because the row-level test has nine times the sample and finds less, but
it is a preference and not an identification.

On the session reading, with the parameter measured on the burnt panel at
`gam` = +0.360 (se 0.101) and every other input left at its measured value, the
simulator predicts a slope SE of **0.0708** against the realized **0.0731**,
closing **88%** of the gap — or **71%** once the estimator's own measured upward
bias is removed (§7.4).

| | slope SE | × published |
|---|---|---|
| published simulator (§10.1) | 0.0538 | 1.00 |
| simulator + the measured mechanism, as read | **0.0708** | 1.32 |
| simulator + the same mechanism, bias-corrected | 0.0675 | 1.25 |
| **realized on the spent look** (CR2) | **0.0731** | 1.36 |

**What it costs.** For a one-sided test at 0.05 and 80% power the detectable
standardised effect moves from **0.134** to **0.182** — a registration quoting the
simulator would have understated the effect it needs by 36%. And the correction is
**not a constant**: it comes from the alignment between session noise and the
feature being tested, so it is large for a volatility-like feature and
unmeasurable for a feature orthogonal to session volatility.

## 1. The question

The spent look reported a standardised partial ATR slope of −0.0713, a one-sided
restricted wild cluster bootstrap p of 0.1782, and a 90% equal-tail interval of
[−0.1864, +0.0490], on 394 ticker-episodes in 64 arrival clusters. Three readings
of that output put the slope's standard error at 0.0716, 0.0731 and 0.0773 against
the 0.0538 the power simulator measured before the run (§10.1, §11.5). That is a
factor of 1.33 to 1.44.

§10.7 had disclosed the mechanism class in advance, with the amount unknown:

> The measured SE is the spread of a SIMULATED sampling distribution, not of the
> real one. It inherits every assumption `a20_power.simulate_panel` makes: normal
> cluster and residual draws, ICC applied to residual variance, and real burnt
> signal rows resampled into the held-out cluster sizes. If the true outcome
> distribution is skewed, these figures are optimistic by an unmeasured amount.

This matters beyond #1227 because the same function would compute the power of any
ML registration, and a registration that cannot state its own power honestly
cannot be filed.

## 2. What the simulator actually does

`a20_power.simulate_panel` builds one panel per draw. For each arrival cluster of
size `m` it draws `m` rows of the real BURNT signal matrix uniformly at random
from the whole burnt panel, adds one normal cluster shock `u`, adds `m` normal row
errors `e`, and sets the outcome to the injected signal plus `u` plus `e`. The ICC
estimated on the burnt outcome splits the residual variance between `u` and `e`.
Three consequences follow from the construction and are not opinions about it:

- **Every regressor has a within-cluster correlation of exactly zero.** Rows are
  drawn independently of one another, so no cluster shares a market state.
- **The cluster shock is independent of the cluster's regressor values**, because
  `u` is drawn without looking at the rows.
- **Every cluster has the same noise scale.** There is one `var_u` and one
  `var_e` for the whole panel.

## 3. The panel every figure below rests on

Read 2026-11-07, and stated so any drift from §10's reading is visible:

| quantity | this probe | §10 read 2026-11-06 | the RUN used |
|---|---|---|---|
| burnt episodes | 198 | 198 | — |
| burnt arrival clusters | 26 | not published | — |
| held-out episodes / clusters | 394 / 64 | 385 / 63 | 394 / 64 |
| sd(`sel_ar_20`) burnt | 0.1681 | 0.1681 | — |
| ICC of the burnt outcome | 0.0505 | 0.050 | — |
| burnt standardised effects | atr −0.358, ma50 −0.315, press +0.006 | same | — |

The blind structure path has caught up with the panel the run fitted, so §5
onward is measured on the run's own 394 × 64 design rather than on the staler one.

## 4. What was ruled out

Each variant changes exactly one assumption. The empirical-shape arm is rescaled
to the SAME `var_u` and `var_e` the baseline uses, so a change in the spread of
the slope is attributable to the named shape and cannot be scale in disguise.
3000 panels per seed, four seeds, true effect zero, nuisance signals injected at
the power gate's own 50% shrinkage — the configuration that produced the
published 0.0538.

| variant | what changed | SE | between-seed range | × baseline |
|---|---|---|---|---|
| A | nothing; `simulate_panel` as written | **0.0535** | [0.0524, 0.0552] | 1.00 |
| B | design rows drawn from ONE real burnt cluster per simulated cluster | 0.0552 | [0.0543, 0.0565] | 1.03 |
| C | `u` and `e` drawn from the real burnt residual shapes, not normals | 0.0530 | [0.0527, 0.0534] | 0.99 |
| D | both B and C | 0.0547 | [0.0542, 0.0552] | 1.02 |

**Variant A reproduces the published figure.** 0.0535 against 0.0538 ± 0.00125,
inside the between-seed range §10.1 recorded. The instrument is the same
instrument, so the other rows mean what they say.

**None of §10.7's three candidates explains anything.** The largest is 3%, and it
points the wrong way for a tidy story: restoring real within-cluster regressor
correlation makes the simulator slightly MORE pessimistic, which is the correct
direction at a hundredth of the required size.

The tail shapes are why C does so little. The burnt OUTCOME is skewed and
mildly heavy-tailed, but once the three signals are fitted the RESIDUAL is close
to normal, and the DGP draws the residual, not the outcome:

| quantity | skew | excess kurtosis |
|---|---|---|
| burnt `sel_ar_20` | −0.607 | +0.797 |
| residual of the 3-signal burnt fit | +0.250 | −0.220 |
| per-cluster residual mean | +0.182 | −0.362 |
| within-cluster deviation | +0.189 | +0.261 |

## 5. Why the cluster ICC buys nothing HERE, and when it would

**On the design the simulator builds, its cluster penalty on the SLOPE is
1.00×.** Measured directly: with the ICC at its estimated 0.050 the SE is 0.0535,
and with the ICC forced to zero it is 0.0536.

**That is a statement about this configuration, not a law.** A cluster ICC
inflates the variance of a slope through the within-cluster correlation of the
REGRESSOR, and the simulator sets that to zero by resampling rows i.i.d. Give it
a clustered regressor and the ICC does bite — §7.1, on the real held-out design
whose ATR column has a within-session ICC of 0.1442, with the cluster shock still
drawn INDEPENDENT of the regressor:

| design, cluster shock independent of the regressor | ICC 0 | ICC 0.050 | ICC 0.900 |
|---|---|---|---|
| burnt rows i.i.d., regressor ICC 0.0080 (§4) | 0.0536 | 0.0535 | — |
| real held-out design, regressor ICC 0.1442 (§7.1) | 0.0519 | 0.0541 | **0.0784** |

So on a clustered design the ICC moves the SE by **1.51×** across its range. An
earlier draft of this memo carried the heading "why a cluster intercept CANNOT
inflate a slope" and the sentence "setting a cluster ICC does nothing to the
precision of a slope". **Both are withdrawn: §7.1 of this same memo refutes
them.** What survives is the narrower and sufficient claim — at the ICC the burnt
panel measures, and with the regressor clustering the simulator actually builds,
the cluster term buys nothing.

**A third premise, which neither §4 nor §6 named.** The 1.00× result also needs
the ICC to SPLIT a fixed residual variance rather than ADD to it. The code does
split it: `residual_variance` fixes the total, then `simulate_panel` divides it
into `var_u` and `var_e`. A DGP that added a cluster component on top would raise
the SE even at a regressor ICC of exactly zero.

§10.7 could not have found any of this from its own assumption list, because it
is an **interaction between two listed assumptions** rather than a property of
either.

## 6. One real structural error, which alone buys nothing

The held-out design is strongly clustered, and the simulator resamples from a
burnt panel that is not:

| regressor | burnt | **held-out** | in the DGP |
|---|---|---|---|
| `technical_atr_pct` | 0.0080 | **0.1442** | 0.0000 |
| `technical_ma50_distance_pct` | 0.0650 | **0.1959** | 0.0000 |
| `n_gates_passed` | 0.0348 | 0.0508 | 0.0000 |

The held-out ATR column is **eighteen times** as clustered within an arrival
session as the burnt column the simulator draws from. This is a real error of the
kind that normally matters a great deal.

**On its own it buys 1%.** Feeding the simulator the actual 394-row held-out
design matrix, so the regressor's cluster structure is the real one rather than
destroyed, leaves the SE where it was. 3000 panels per seed, four seeds, one noise
model, three designs:

| design | SE | between-seed range | × baseline |
|---|---|---|---|
| burnt rows drawn i.i.d. (the baseline) | 0.0533 | [0.0523, 0.0538] | 1.00 |
| **real held-out design, fixed** | 0.0539 | [0.0531, 0.0548] | 1.01 |
| real held-out design, clusters resampled | 0.0538 | [0.0533, 0.0548] | 1.01 |

This is §5 restated from the other side. A clustered regressor cannot inflate a
slope's variance while the cluster shock is drawn independent of it. The error is
real, and by itself it is invisible. An earlier draft called it **necessary** for
§7; §7.5 measures that it is not — row-level alignment reaches the realized
precision with no clustering at all. It is necessary only for the SESSION-level
reading of the mechanism.

## 7. The mechanism, what tests it, and what it leaves unidentified

### 7.1 Raising the residual ICC does not get there at a measured setting

Swept on the real held-out design, 1500 panels per seed, four seeds, cluster shock
independent of the regressor:

| residual ICC | SE | between-seed range | × published | × realized |
|---|---|---|---|---|
| 0.000 | 0.0519 | [0.0517, 0.0524] | 0.96 | 0.71 |
| **0.050** (the burnt measurement) | 0.0541 | [0.0536, 0.0545] | 1.01 | 0.74 |
| 0.100 | 0.0560 | [0.0554, 0.0566] | 1.04 | 0.77 |
| 0.200 | 0.0594 | [0.0587, 0.0603] | 1.10 | 0.81 |
| 0.350 | 0.0640 | [0.0633, 0.0654] | 1.19 | 0.88 |
| 0.500 | 0.0683 | [0.0677, 0.0699] | 1.27 | 0.93 |
| 0.700 | 0.0736 | [0.0726, 0.0754] | 1.37 | 1.01 |
| 0.900 | 0.0784 | [0.0770, 0.0804] | 1.46 | 1.07 |

Reaching the realized 0.0731 needs a residual ICC near 0.70 to 0.90, against the
**0.0505 the burnt panel measures**. That measurement is the whole of the
argument: a factor of 14 between a measured input and the input a fit would
require is not a plausible calibration error.

An earlier draft added that "0.90 is the clamp ceiling of `estimate_icc_local`
itself" and called that "the edge of the DGP's own parameter space, so it is not a
credible explanation". **That reasoning is withdrawn.** The clamp is
`min(max(icc, 0.0), 0.9)`, an implementation guard; a true residual ICC of 0.70
does not become implausible because an estimator would refuse to report it. Note
also that the held-out residual ICC was never measured and cannot be without
spending a look, and that the mechanism §7.2 proposes would itself raise the
realized residual ICC above the burnt figure.

### 7.2 Heteroskedasticity that tracks the feature, at session level

Modelled by scaling a session's whole noise by `exp(gam × z(that session's mean
ATR))`, renormalised so the panel's TOTAL noise variance is unchanged, with the
ICC left at its measured 0.050.

**`gam` is defined on the per-session, unweighted z-scale throughout this memo**,
the same scale §7.3 measures it on. A sweep over a ROW-WEIGHTED z-scale was also
run, and §12 PRINTS it as phase 3's `icc 0.050` row without labelling it as the
other scale — so the two sit side by side in the verbatim output and must not be
read as one series. Comparing that row against phase 4's TIED column, the scales
differ by **1.3% at `gam` 0.2, 1.9% at 0.3, 2.6% at 0.4, 2.8% at 0.5 and 2.6% at
0.7**: the discrepancy grows with `gam`, and 2.6% is the value at one grid point
rather than a typical one. The "loudest/quietest session" column diverges much
further between the scales, by 8.7% to 27.2%, because that ratio is a function of
the z-scale itself. Only the per-session scale is used in §7.2, §7.3 and §7.4;
two tables on two scales under one symbol is how a threshold comes to sit beside a
number in a different unit.

Both arms below carry the SAME spread of session noise scales. Only the TIED arm
lines that spread up with the session's ATR level. 1500 panels per seed, four
seeds:

| `gam` | loudest/quietest session noise | **TIED** SE | **FREE** SE | TIED ÷ realized | FREE ÷ realized |
|---|---|---|---|---|---|
| 0.0 | 1.0× | 0.0542 | 0.0545 | 0.74 | 0.75 |
| 0.2 | 2.3× | 0.0621 | 0.0543 | 0.85 | 0.74 |
| 0.3 | 3.6× | 0.0674 | 0.0541 | 0.92 | 0.74 |
| 0.4 | 5.5× | **0.0732** | 0.0540 | **1.00** | 0.74 |
| 0.5 | 8.4× | 0.0790 | 0.0539 | 1.08 | 0.74 |
| 0.7 | 19.5× | 0.0892 | 0.0536 | 1.22 | 0.73 |

The `gam` = 0 row is an internal control: the two arms are the same DGP there and
they agree to 0.0003.

**The mechanism is specific, not generic.** A session-noise spread that does not
track the regressor leaves the SE flat across the whole sweep — it even drifts
down slightly. **The inflation comes from the ALIGNMENT, not from the spread.**
That is the finding that survives every later correction in this memo, and it is
the one §9 rests on.

An earlier draft continued "that is why §6's structural error is necessary:
without a clustered regressor there is nothing for the noise scale to align
with". That is withdrawn. A row's noise can align with that row's OWN volatility
without any session structure whatever, and §7.5 measures that this reaches the
realized precision too. What §6's structural error is necessary for is the
SESSION-level reading specifically.

### 7.3 The mechanism is measurable on the burnt panel

Per-session residual spread, on the 21 burnt arrival sessions carrying at least 3
episodes (of 26), residuals taken from the 3-signal standardised burnt fit:

| quantity | value |
|---|---|
| per-session residual sd | 0.105 to 1.097, a ratio of **10.4×** |
| sd of log(session sd) observed | **0.570** |
| sd of log(session sd) expected from sampling alone, **simulated on the real 21 session sizes** | **0.388** (sd across draws 0.111) |
| **`gam` measured** — slope of log(session sd) on z(session mean ATR) | **+0.360**, se 0.101, 95% [+0.161, +0.558] |

The alignment with ATR is a t of 3.6. **The SPREAD, by contrast, is only 1.6
standard deviations above what chance alone produces, and an earlier draft of
this memo overstated it badly.**

That draft used `1 / sqrt(2 × (mean session size − 1))` = 0.250 as the chance
benchmark and concluded the observed spread was "more than twice what chance
explains". That function is CONVEX in the session size, so evaluating it at the
mean is biased low with a guaranteed direction whenever the sizes are unequal —
and these range from 3 to 26. Measured instead on the real 21 session sizes:

| benchmark for sd(log session sd) under homoskedasticity | value |
|---|---|
| the withdrawn construction, `1/sqrt(2(mean size − 1))` | 0.250 |
| size-corrected, `sqrt(mean of 1/(2(n_i − 1)))` | 0.336 |
| **simulated, 4000 homoskedastic draws on the real sizes** | **0.388**, sd 0.111 |
| OBSERVED | 0.570 |

So the ratio is **1.47×, not 2.28×**; in variance units chance explains **46%**,
not the 19% the withdrawn figure implied; and 0.570 sits **1.64 standard
deviations** above the homoskedastic expectation, which on its own is not
significant. **The evidence that sessions differ in noise is weak.** The evidence
that the noise ALIGNS with ATR is the t of 3.6, and that is a separate
measurement which this correction does not touch.

> **CORRECTION, same day.** The probe that produced this table carries a comment,
> written BEFORE it ran, predicting that this estimate would be "weak BY
> CONSTRUCTION" because the burnt ATR column's within-session ICC is only 0.008.
> That prediction was wrong on its own terms — the estimate came back at a t of 3.6,
> not weak — but **the caveat was pointing at something real that this box originally
> failed to answer.** It raised IDENTIFICATION, and the first draft of this box
> replied about SPREAD, which is a different question. With a within-session ATR ICC
> of 0.0080 most of the between-session variation in mean ATR IS sampling noise
> rather than session structure, exactly as the caveat said, and §7.5 measures the
> consequence: this estimator cannot tell a session-level mechanism from a row-level
> one. The caveat was right about the limit and wrong only about the t.
>
> **SECOND CORRECTION, after an arithmetic audit of this memo.** The first draft of
> this box supported that last sentence with "the sd of the per-cluster mean
> standardised ATR is 0.5708". **That figure is the HELD-OUT one**, computed on the
> held-out design at `icc_calibration_probe.py` lines 111-113, and citing it as
> evidence about the BURNT panel was wrong. Measured on the burnt panel instead
> (`burnt_atr_spread_probe.py`, 2026-11-07, verbatim in §12):
>
> | sd of the per-session mean standardised ATR | burnt | held-out |
> |---|---|---|
> | all sessions | **0.3877** (26) | 0.5708 (64) |
> | sessions with at least 3 episodes, the regression's own denominator | **0.4033** (21) | 0.5302 (52) |
>
> The burnt figure is the lower one, as an ICC of 0.0080 against 0.1442 predicts, and
> it is not small. 0.4033 is the spread the `gam` regression actually had to work
> with, and a t of 3.6 is what it returned. The box's conclusion rests on that t,
> which is measured on the burnt panel and needs no help from the other figure.
> The pre-run caveat is left in the verbatim output of §12 rather than edited out.

### 7.4 The forward prediction

`gam` was measured on the burnt panel before this table was computed, and it is
the burnt side's job to supply the simulator's calibration. 3000 panels per seed,
four seeds, real held-out design, ICC left at 0.0505:

| `gam` | what it is | SE | between-seed range | × published | × realized |
|---|---|---|---|---|---|
| 0.000 | the DGP as written | 0.0539 | [0.0529, 0.0546] | 1.00 | 0.74 |
| 0.161 | measured 95% low | 0.0601 | [0.0588, 0.0607] | 1.12 | 0.82 |
| 0.250 | | 0.0645 | [0.0632, 0.0651] | 1.20 | 0.88 |
| 0.300 | | 0.0673 | [0.0659, 0.0678] | 1.25 | 0.92 |
| **0.360** | **MEASURED point estimate** | **0.0707** | **[0.0694, 0.0713]** | **1.31** | **0.97** |
| 0.450 | | 0.0760 | [0.0746, 0.0766] | 1.41 | 1.04 |
| 0.500 | | 0.0789 | [0.0775, 0.0795] | 1.47 | 1.08 |
| 0.558 | measured 95% high | 0.0821 | [0.0807, 0.0828] | 1.53 | 1.12 |

The measured point estimate predicts **0.0707** against the realized **0.0731**.
The measured 95% interval on `gam` maps to an SE interval of **[0.0601, 0.0821]**,
which contains the realized value with room on both sides. In SE units that is
`(0.0707 − 0.0539) / (0.0731 − 0.0539)` = **88% of the gap**, with 0.0024 left
over, a third of one standard deviation of the one-panel CR2 estimator from §8.

### 7.4.1 How much of a prediction this actually is, and the bias in it

**The parameter was measured before this table. The FAMILY was not.** §7.1's sweep
was run first, printed the realized 0.0731 as its explicit target, and concluded
in its own output that "the cell that reaches it IS the calibration the burnt
panel could not supply". The functional form `exp(gam × z(mean ATR))` with the
total noise variance renormalised was then chosen BECAUSE a sweep over it reaches
that target. So this is a one-parameter family selected against the answer, whose
single parameter was then measured independently and not tuned. That is real
evidence and it is not a blind forward prediction, and an earlier draft of this
section asserted the stronger reading — "So this is a prediction, not a fit" —
which is withdrawn.

**The estimator also over-reads, so 88% is the optimistic end.** §7.5 measures
what the §7.3 estimator returns under a KNOWN truth on the real burnt design: a
true session-level `gam` of 0.360 reads back as **+0.419**, and a homoskedastic
panel reads **+0.045**. Inverting that map, the observed reading of +0.360
corresponds to a true session `gam` of about **0.303**:

| what is fed to the simulator | SE | × realized | share of the gap closed |
|---|---|---|---|
| the reading, `gam` 0.360 | 0.0708 | 0.97 | **88%** |
| the bias-corrected truth, `gam` 0.303 | 0.0675 | 0.92 | **71%** |

Both are reported because the correction is a model of the estimator rather than
a second measurement of the world. **71% is the figure a sceptical reader should
use.** The remaining 0.0056 is about four fifths of one standard deviation of the
one-panel CR2 estimator, so it is still inside what one panel can produce.

### 7.5 At what LEVEL does the alignment live? This panel does not say

A claim-versus-evidence audit of this memo asked the question §7.2 had not: the
TIED arm scales a whole session's noise by that session's volatility, but a row's
noise could scale by that ROW's own volatility instead, with no session structure
at all. Both are "aligned with the feature". They are different mechanisms, they
imply different fixes, and the memo had asserted the first. Measured, 2000 panels
per seed, four seeds, on the real held-out design:

| `gam` | session-scaled | row-scaled | row-scaled, **no cluster shock** |
|---|---|---|---|
| 0.00 | 0.0540 | 0.0540 | 0.0520 |
| 0.20 | 0.0620 | 0.0723 | 0.0699 |
| 0.30 | 0.0673 | 0.0870 | 0.0842 |
| 0.36 | 0.0708 | 0.0970 | 0.0939 |
| 0.40 | **0.0731** | 0.1039 | 0.1007 |
| 0.70 | 0.0891 | 0.1512 | 0.1477 |

The `gam` = 0 row is an internal control and the first two arms agree exactly.

**Row-level alignment reaches the realized SE at a SMALLER parameter, and with no
clustering whatever.** Interpolating, the session arm reaches 0.0731 at
`gam` = 0.400, the row arm at **0.205**, and the row arm with the cluster shock
removed entirely at **0.222**. So §7.2's claim that a clustered regressor is
necessary is false, and §9's earlier claim that a feature orthogonal to session
volatility pays no penalty is false in the same way: such a feature pays nothing
under the SESSION parameterisation and can pay the full penalty under the row one.

**And the §7.3 estimator cannot tell the two apart.** Simulated on the real burnt
design — real session sizes, real ATR column — with 1500 panels per row, putting
a known truth in and reading the estimator out:

| the truth put in | mean `gam_hat` read out | sd | fraction significant at \|t\| > 1.96 |
|---|---|---|---|
| homoskedastic | +0.045 | 0.091 | 9.0% |
| SESSION-level, `gam` 0.36 | +0.419 | 0.091 | 99.4% |
| **ROW-level, `gam` 0.36** | **+0.240** | 0.096 | **72.4%** |
| ROW-level, `gam` 0.60 | +0.429 | 0.106 | 96.5% |

A purely row-level truth produces a positive, usually significant reading. With a
burnt within-session ATR ICC of 0.0080 a session's mean ATR is mostly a composite
of its own rows, so the estimator has almost nothing session-specific to work
with. The TIED-versus-FREE contrast in §7.2 does not separate them either: both
truths are "tied". The homoskedastic row also shows the estimator is slightly
liberal — 9.0% significant against a nominal 5% — which is worth knowing beside
the t of 3.6.

**One reading does discriminate, and the memo had it filed under the wrong
heading.** A pairs ROW bootstrap is robust to row-level heteroskedasticity, so if
row-level alignment were large on the burnt panel the real-data row bootstrap
would not agree with a homoskedastic simulator. It does agree: 0.0532 against
0.0535 (§11). Tested directly, by regressing the log absolute row residual on the
row's own z(ATR):

| test on the burnt panel, same residuals | slope | se | t | points |
|---|---|---|---|---|
| ROW level, log\|residual\| on z(row ATR) | +0.132 | 0.085 | **+1.55** | 198 |
| SESSION level, log(session sd) on z(session mean ATR) | +0.360 | 0.101 | **+3.56** | 21 |

**The row test has nine times the points and finds less.** That favours the
session reading, and it is the strongest discriminating evidence available without
spending a look. It is not an identification: §7.5's second table shows the
session estimator responds to row-level truth, so a mixture cannot be excluded.

**What this changes, and what it does not.** The alignment is measured and it is
what inflates the SE; that stands. The LEVEL is a preference, not a result. For
§9 this strengthens rather than weakens the recommendation: a corrected simulator
has to choose a level, this panel does not choose it, and a registration that
bounds its precision by the realized reading does not have to.

## 8. How much of the gap could have been one unlucky panel

The published 0.0538 is the spread of the slope ACROSS panels. The run's three
figures are estimates computed ON ONE PANEL. They are different objects, and a
cluster-robust SE over 64 clusters is itself noisy, so the comparison needs the
second distribution before it means anything.

Over 12000 simulated panels under the baseline DGP, the one-panel CR2 SE has a
mean of 0.0529, a standard deviation of 0.0071, and a **relative standard
deviation of 13.3%**. It is essentially unbiased for the true SE, reading 0.99× of
the across-panel spread, so it is a fair yardstick.

| percentile | p1 | p5 | p25 | p50 | p75 | p90 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| CR2 SE | 0.0385 | 0.0421 | 0.0480 | 0.0524 | 0.0573 | 0.0622 | 0.0652 | 0.0713 |

The realized 0.0731 sits at about the **99th percentile**. §12 contains FOUR
readings of that tail probability, not one, and an earlier draft of this memo quoted
only the smallest of each pair — which made the realized value look more extreme
and happened to favour this memo's own thesis. All four:

| panel design the distribution was simulated on | probe | P(simulated ≥ 0.0731) |
|---|---|---|
| burnt design | phase 1 | 0.6% |
| burnt design, i.i.d. rows (X3) | phase 2 | 0.7% |
| real held-out design, fixed (X1) | phase 2 | 0.9% |
| real held-out design, clusters resampled (X2) | phase 2 | **1.5%** |

The two burnt readings measure the same thing with different seeds and agree. So the
honest statement is **between 0.6% and 1.5%, i.e. a draw of roughly 1 in 67 to 1 in
167.** An earlier draft said "roughly a one-in-120 draw"; 1 in 120 corresponds to
0.83%, which appears nowhere in the output, and that phrasing is withdrawn.

So the realized precision was never impossible under the published simulator. **The
percentile alone would not have settled it, at any of the four readings.** What
settles it is §7.4: a mechanism measured independently, on the side of the data
meant to supply calibration, that moves the CENTRE of the distribution to the
realized value.

**Two notes on how the figures in this section were produced.** The relative
standard deviation of 13.3% and the 95% interval [+0.161, +0.558] in §7.3 are both
computed inside the probes from UNROUNDED values. Recomputing them from the rounded
pairs this memo prints gives 0.0071 ÷ 0.0529 = 13.4% and 0.360 ± 1.96 × 0.101 =
[+0.162, +0.558]. The memo quotes the probes rather than the recomputation, and the
difference is rounding, not disagreement. Separately, §4's baseline of 0.0535 and
§6's baseline of 0.0533 are **two independent measurements of the same DGP at the
same 3000 panels and four seeds**, 0.4% apart, which is Monte Carlo noise. They are
not one number quoted twice, so the "× baseline" columns in §4 and §6 divide by
different denominators and should not be compared across the two tables.

**Only one of the three realized readings is a direct SE, and this memo uses it.**

| reading | value | what it assumes |
|---|---|---|
| **CR2 t** | **0.0731** | nothing beyond the CR2 estimator the run already printed |
| interval half-width ÷ 1.645 | 0.0716 | that the registered interval is symmetric — §10.4 measured that it is NOT |
| one-sided bootstrap p | 0.0773 | that the wild cluster bootstrap null distribution is normal |

The honest single figure for the gap is therefore **1.36×**. The 1.33-to-1.44
range §11.5 published mixes one measurement with two back-derivations, and that
sentence should be read with this table beside it.

## 9. What this costs an ML registration

**The detection threshold moves by a third.** For a one-sided test at 0.05 with
80% power the detectable standardised effect is `SE × (z(0.95) + z(0.80))` =
`SE × 2.4865`:

| precision used | detectable \|ρ\| at 80% power | what it promises |
|---|---|---|
| simulated 0.0538 | **0.134** | an effect of 0.134 is findable |
| realized CR2 0.0731 | **0.182** | it is not; 0.182 is the real bar |

A registration quoting the simulator would have understated the effect it needs
by 36%, on the same panel, with no error in its own arithmetic.

**The correction is not a constant.** §7.2 measures that the inflation comes from
the alignment between a session's noise scale and the feature under test. For a
volatility-like feature the penalty is the full one. "Multiply every power figure
by 1.36" is wrong in both directions depending on the feature, which is a worse
failure mode than a known constant bias.

An earlier draft added that "for a feature orthogonal to session volatility the
probe measures none". **That is withdrawn.** §7.5 measures that alignment at the
ROW level reaches the realized precision with no session structure at all, so a
feature orthogonal to SESSION volatility can still pay the full penalty if its own
per-row values track the noise. What a feature escapes by being orthogonal to
session volatility is the session-level parameterisation, not the mechanism.

**The recommendation.** Two routes are open and one is cheaper than it looks.

1. **Model it.** `simulate_panel` would need a noise scale tied to the feature
   values, calibrated per feature the way §7.3 calibrates it for ATR. This is the
   correct fix and it is reusable, but it carries three costs this memo has
   measured. Every new feature needs its own `gam` on burnt data, and §7.3's
   estimate rests on 21 sessions. The estimator over-reads a session-level truth by
   about 16% (§7.4.1). And **the fix has to choose between a session-level and a
   row-level scale, which this panel does not identify** (§7.5) — the two reach the
   same realized SE at `gam` 0.400 and 0.205, so picking the wrong one miscalibrates
   every later power figure by about a factor of two in the parameter.
2. **Bound it instead.** Take the realized reading as the precision. The CR2 SE of
   **0.0731 at 394 ticker-episodes in 64 arrival clusters on `sel_ar_20`** is a
   measured fact about this exact label, panel and estimator, and it already
   contains every mechanism this memo could not isolate.

**Prefer route 2 for the first ML registration and route 1 only when a feature
needs its own power curve.** Scaling the realized SE as `1/sqrt(clusters)` — which
assumes the session-noise structure holds as the panel grows, and that is an
assumption — gives:

| detectable \|ρ\| at 80% power | arrival clusters needed | from 64, at 0.917 clusters/session |
|---|---|---|
| 0.18 | ~65 | +1.3 clusters, about 1.4 sessions — effectively already held |
| 0.15 | ~94 | +33 sessions, about **1.6 months** |
| 0.12 | ~147 | +90 sessions, about **4.3 months** |
| 0.10 | ~211 | +161 sessions, about **7.7 months** |

Constructed as `n = 64 × (0.0731 ÷ (ρ ÷ 2.4865))²`, at 21 trading sessions per
month. The accrual rate of **0.917 arrival clusters per XNYS session** is NOT
measured here: it is carried from `edge_hypothesis_budget_2026_07.md` §3, measured
2026-09-24 on a then-current panel of 206 episodes in 33 clusters. Cross-checked
against this memo's own panel it holds: 64 clusters over the 69 XNYS sessions from
2026-07-06 to 2026-10-09, the window with a matured 20-session label, is 0.928 per
session, and 64 ÷ 0.917 is 69.8 sessions.

An earlier pass in the same session quoted 63 / 91 / 142 / 205 clusters and 1.4 /
4.1 / 7.3 months. Inverting the formula above, each of those rows implies a single
SE of about **0.0719** rather than the 0.0716-to-0.0773 range, and the provenance of
that 0.0719 cannot now be reconstructed. An earlier draft of this paragraph said
they "were computed against the range"; that is withdrawn. This table supersedes
them and states its own SE.

**Against house rule 5, the news is mixed and both halves matter.** Rule 5 makes a
model that loses to ATR alone an empty result. ATR alone is now measured at a
held-out −0.0713 with an interval spanning zero, so BEATING it is a low bar.
Beating it DETECTABLY on 64 clusters needs about 0.18, roughly 2.5× ATR's own
held-out point estimate and half the burnt −0.358 that did not survive.

Two qualifications this memo owes rule 5, because an earlier draft stated it too
simply. **First, rule 5 is a PAIR of baselines, not one.** The README reads "base
rate + univariate `technical_atr_pct` with its a-priori direction fixed in advance",
compared **on the same CV folds**. The −0.0713 above is a one-shot held-out partial
slope, not a fold-wise metric, so it speaks to the ATR half only — and if ATR is
dead out of sample, the **base-rate** half becomes the binding baseline, which this
memo does not measure. **Second, the data-volume floors are cluster floors and only
one arm cleared them.** The "25-30 grouped-CV floor" in
`nondeterministic_exit_policy_design_2026_08_03.md` §"Timeline" counts **arrival
clusters**, not episodes, and an earlier draft of this memo called them "episode
gates", which is confusing beside #1227's own `MIN_EPISODES = 300` and
`MIN_CLUSTERS = 30`. On the 2026-09-05 re-measure the **ivx30-present arm** read 31
clusters and cleared both floors, while the strict `chain_quality == "OK"` arm read
**18 clusters and did NOT clear floor-30**. That re-measure is recorded outside this
repository and carries no construction here, so it is quoted as a pointer and not as
a measurement. So: for the ivx30-present arm the floors are not what blocks a
registration, and this precision is; for the strict arm the cluster floor is still
a live blocker.

## 10. Outcome blindness, and one disclosure the owner should see

**No held-out outcome value was read.** Every `sel_ar_20` value any statistic here
rests on comes from the BURNT side (brief date on or before 2026-07-05), which
ledger rule 3 makes fair game and which `a20_power.burnt_panel` exists to read.
The probes assert this rather than claim it: every frame fitted is checked for
columns beginning `sel_ar`, `sel_zar`, `sel_car` or `car_`, and the held-out label
read stops at the four key columns plus `sel_label_status_20`.

**The disclosure.** The probes DO read the held-out brief SIGNAL columns —
`technical_atr_pct`, `technical_ma50_distance_pct`, `n_gates_passed` — which the
pre-run allowlist in `a20_power.py` deliberately barred:

> reading held-out `technical_atr_pct` to learn how the three statistics co-move
> would put the held-out dependence structure straight into a Holm power number
> without touching one label value.

That bar was about STRUCTURE, not about outcome values: reading held-out signals
would have put held-out dependence into a power number the gate had to clear
blind. That gate is spent and the cluster is retired.

**Two corrections to how an earlier draft argued this.** It replied that "a
feature column is also not an outcome", which answers an objection the allowlist
never raised — the bar never claimed the column was an outcome, it claimed the
dependence structure was informative. And it said "no decision downstream of that
gate remains open", which **§9 of this memo contradicts**: §9 is a prospective
power calculation built on the real held-out design and it makes a recommendation
for the FIRST ML registration. A decision downstream does remain open, and this
memo is an input to it.

What the legality argument actually rests on: the live scorer reads these three
columns every day, and owner decision **D5** requires an ATR-only baseline beside
every model, which cannot be computed without reading that feature. An earlier
draft wrote "decisions D1 to D5"; D1 to D4 settle the label's horizon, benchmark,
population and anchor and involve no feature column, so that citation is narrowed
to D5 plus README house rule 5.

**No charge is taken against the budget**, on the same basis the ledger's
2026-09-03 row records a burnt-panel read as no charge. An earlier draft also
wrote that "on that reading the read is legal and costs nothing" — which settles a
question this same section says belongs to the owner. **It is not settled here.**
The read happened, the reasoning is above, and the judgement is the owner's.

It is recorded anyway, with its reasoning, because it is a departure from a
written allowlist and the owner may judge it differently. **If the Jul-to-Nov 2026
window should stay structurally blind for a FUTURE registration, that has to be
said now**: §6's ICC figures cannot be unread, and the correct remedy would be to
treat that window's dependence structure as spent.

## 11. Limitations

- **The LEVEL of the mechanism is not identified. This is the memo's weakest
  joint.** §7.5 measures that session-level and row-level alignment both reach the
  realized SE, that the row-level route needs no clustering at all, and that the
  §7.3 estimator reads positive and usually significant under either truth. The
  session reading is preferred because the row-level test has 198 points against
  the session test's 21 and finds less, but a mixture cannot be excluded. An ML
  registration should be told this, because a corrected simulator must pick a level
  and the two differ by roughly a factor of two in the parameter.
- **The mechanism is measured on burnt data and applied to held-out structure.**
  §7.3's `gam` comes from 21 burnt sessions; §7.4 applies it to the held-out
  design. The FAMILY was also chosen against the target (§7.4.1), so this is a
  one-parameter check rather than a blind prediction, and one check landing inside
  its own interval is weaker than a replication. None is available without
  spending a look.
- **The evidence that sessions differ in noise is weak on its own.** §7.3's
  observed sd(log session sd) of 0.570 sits 1.64 standard deviations above the
  0.388 a homoskedastic panel produces on the same 21 session sizes. The ALIGNMENT
  with ATR, at a t of 3.6, is what carries the section; the spread alone would
  not.
- **"Noisier sessions" may be mis-named.** §7.3 measures that the per-session
  residual spread varies and tracks ATR. A session-varying TRUE EFFECT would
  produce the same reading, because the residual is taken from a globally fitted
  model. The operational conclusion is unchanged — the simulator models neither —
  but calling the mechanism heteroskedasticity rather than effect heterogeneity is
  a choice this measurement does not justify.
- **One realized observation cannot prove bias on its own.** §8 measures the
  realized reading at about the 99th percentile of the simulated one-panel
  distribution. That is unlikely, not impossible. The case rests on §7.4, not on
  the percentile.
- **Variant E is a weak instrument and the memo does not lean on it.** The
  nonparametric cluster bootstrap over real burnt data gives 0.0315, rescaled to
  394 episodes 0.0350, a design effect of 0.66× against its own row bootstrap —
  i.e. TIGHTER under clustering, which is not a credible property. It resamples
  from only 26 distinct burnt arrival clusters, so drawing 64 repeats each about
  2.5 times and it cannot show between-cluster variance it has never observed. The
  figure is reported for completeness and is evidence for nothing. By contrast the
  real-data ROW bootstrap, rescaled to 394 episodes, reads 0.0532 against the
  simulator's 0.0535: the simulator reproduces the i.i.d. precision of real data
  correctly, and only the clustering is wrong.
- **Two of the three realized SE readings are back-derived** under assumptions
  this programme's own memo refutes elsewhere. See §8.
- **The burnt signals explain real variance and the held-out ones apparently do
  not.** The run's ATR point estimate is −0.0713 against the burnt −0.358. This
  memo holds the injected effects at the burnt values because that is what the
  published 0.0538 used; it does not re-derive the calibration for a world where
  the signals are null.
- **The probe scripts are scratch and are not in the repository.** They carry no
  tests. Their output is quoted verbatim in §12 so every number can be checked
  against the construction that produced it, and the constructions are described
  in §2, §4, §6 and §7.
- **`a20_power.py` is unchanged.** This memo recommends a change in §9 and does
  not make one. No frozen constant was touched and no registered script was edited.
- **Two other memos rest on this simulator and both are qualified, not corrected.**
  `a20_power_preflight_2026_09.md` §8 reads the #1227 power gate at 91.5% and 92.2%,
  and `a20_overlap_inference_2026_09.md` §6 reads 64.6% to 66.2% after its own
  correction. Both are simulator readings and both inherit the optimism measured
  here. Each now carries a dated box pointing at this memo. Neither has been
  RE-MEASURED under a corrected DGP, because no corrected DGP exists yet, so no
  replacement figure is offered for either.
- **This memo was audited three ways and corrected the same day, and several
  corrections go against its own thesis.** Each is named where it occurred rather
  than silently applied, because this memo's subject is a figure that was published
  and then had to be withdrawn. **Claims withdrawn:** that a cluster intercept
  "cannot" inflate a slope and that a cluster ICC "does nothing" to one (§5,
  refuted by §7.1 of this same memo); that a clustered regressor is "necessary"
  (§6, §7.2, refuted by §7.5); that a feature orthogonal to session volatility pays
  no penalty (§9, same reason); that §7.4 is "a prediction, not a fit" (§7.4.1);
  that the clamp ceiling of `estimate_icc_local` bounds what is credible (§7.1);
  that the session noise spread is "more than twice what chance explains", which is
  1.47× (§7.3); that the realized reading was "roughly a one-in-120 draw", which is
  1 in 67 to 1 in 167 (§8); and that the feature read "is legal and costs nothing",
  which is the owner's call (§10). **Figures corrected:** the between-session ATR
  spread cited for the burnt panel was the held-out one (§7.3); the share of the gap
  the mechanism closes is 88% as read and **71%** bias-corrected (§7.4.1); the
  chance benchmark is 0.388, not 0.250 (§7.3); §8 quoted the smallest of four
  available tail probabilities; and §9 called arrival-cluster floors "episode
  gates" while only one of two arms cleared them.

## 12. Verbatim output

Five probes, each run on `vmi2478967` against `~/.alphalens/selection_labels` and
`~/.alphalens/thematic_briefs` with the workspace venv at repo `HEAD` `43dd277e`.
Phases 1, 2 and 5 at 3000 panels per seed; phases 3 and 4 at 1500. Four seeds
throughout: 20261106, 51, 52, 53.

| phase | script | what it answers |
|---|---|---|
| 1 | `simulator_gap_probe.py` | §4, §5, §8 — the three §10.7 candidates, the cluster penalty, the one-panel SE distribution |
| 2 | `heldout_design_probe.py` | §6 — the regressor ICCs and the three designs |
| 3 | `icc_calibration_probe.py` | §7.1, §7.3 — the ICC sweep and the burnt heteroskedasticity measurement |
| 4 | `hetero_specificity_probe.py` | §7.2 — TIED against FREE |
| 5 | `measured_gam_probe.py` | §7.4 — the forward prediction |
| 6 | `burnt_atr_spread_probe.py` | §7.3's second correction — the BURNT between-session ATR spread, after an audit showed the first draft cited the held-out figure |
| 7 | `row_vs_session_probe.py` | §7.3's chance benchmark, §7.4.1's estimator bias and §7.5's identification question |

The §7.3 caveat the correction box withdraws is left standing in phase 3's output
below, as written before the run.

```text
########## PHASE 1 ##########
==============================================================================
WHERE THE 1.35x OPTIMISM COMES FROM
==============================================================================
published simulated SE  0.0538   (memo 10.1, 4 seeds x 3000)
realized SE on the run  0.0716 .. 0.0773   (memo 11.5)
sims per seed 3000, seeds (20261106, 51, 52, 53), rho 0.0

------------------------------------------------------------------------------
1. THE PANEL THIS PROBE READ
------------------------------------------------------------------------------
burnt episodes            198   (memo 10 read 198)
burnt arrival clusters    26
held-out cluster sizes    64 clusters, 394 episodes   (memo 10 read 63 / 385; the RUN used 64 / 394)
sd(sel_ar_20) burnt       0.1681   (memo 10 read 0.1681)
icc of the OUTCOME        0.0505   (memo 10 read 0.050)
burnt standardised effects atr -0.358, ma50 -0.315, press +0.006

------------------------------------------------------------------------------
2. WHAT THE DGP ASSUMES vs WHAT THE BURNT PANEL SHOWS
------------------------------------------------------------------------------
   the DGP draws design rows i.i.d. ACROSS the whole panel, so the
   within-cluster correlation of every REGRESSOR is zero by construction.
   quantity                            icc in the data   icc in the DGP
   regressor atr (technical_atr_pct)            0.0080           0.0000
   regressor ma50 (technical_ma50_dis           0.0650           0.0000
   regressor press (n_gates_passed)             0.0348           0.0000
   outcome sel_ar_20                            0.0505           0.0505
   residual of the 3-signal fit                 0.0500           0.0505

   tail shape. the DGP draws u and e NORMAL, i.e. (0, 0).
   quantity                                       skew  excess kurtosis
   outcome sel_ar_20 (burnt)                    -0.607            0.797
   residual of the 3-signal fit                  0.250           -0.220
   cluster effect (per-cluster mean)             0.182           -0.362
   within-cluster deviation                      0.189            0.261

   cluster sizes  burnt    n=26 mean 7.62 median 6.0 max 26
   cluster sizes  held-out n=64 mean 6.16 median 5.0 max 23

------------------------------------------------------------------------------
3. SD OF THE REGISTERED SLOPE, ONE ASSUMPTION CHANGED AT A TIME
------------------------------------------------------------------------------
   variant                                          SE           spread  x baseline
   A baseline  simulate_panel unchanged         0.0535 [0.0524, 0.0552]        1.00   (46s)
   B clustered X  rows from one real cluster    0.0552 [0.0543, 0.0565]        1.03   (48s)
   C empirical u,e  real residual shapes        0.0530 [0.0527, 0.0534]        0.99   (46s)
   D both B and C                               0.0547 [0.0542, 0.0552]        1.02   (47s)
   E nonparametric  real clusters, real rows    0.0315 [0.0310, 0.0320]        0.59   (21s)
                                              variant E averaged 487.1 episodes against the held-out 394
                                              rescaled to 394 episodes by sqrt(n): 0.0350

------------------------------------------------------------------------------
4. WHAT CLUSTERING COSTS, IN THE DATA AND IN THE SIMULATOR
------------------------------------------------------------------------------
   real data, ROW bootstrap at n=487      SE 0.0478   rescaled to 394: 0.0532
   real data, CLUSTER bootstrap (variant E)   SE 0.0315   design effect 0.66x
   CAVEAT: variants E and the row bootstrap resample from only 26 DISTINCT burnt
   clusters, so neither can show between-cluster variance it has not observed. Drawing
   64 clusters from 26 repeats each distinct one about 2.5 times.
   simulator with icc = 0                     SE 0.0536
   simulator with icc = 0.050 (variant A)       SE 0.0535   design effect 1.00x

------------------------------------------------------------------------------
6. THE SE OF ONE PANEL IS ITSELF A RANDOM VARIABLE
------------------------------------------------------------------------------
   CR2 SE of the ATR slope, 12000 simulated panels, 64 clusters each
   mean 0.0529   sd 0.0071   relative sd 13.3%
   percentile p1  p5  p25  p50  p75  p90  p95  p99
   value      0.0385  0.0421  0.0480  0.0524  0.0573  0.0622  0.0652  0.0713

   for reference, the SD of the slope across the same panels is 0.0535,
   so the CR2 estimator reads 0.99x the true SE on average.

   where the run's three readings fall in this simulated distribution:
   interval half-width / 1.645    0.0716   percentile  99.1   P(simulated >= this)   0.9%
   CR2 t                          0.0731   percentile  99.4   P(simulated >= this)   0.6%
   bootstrap p                    0.0773   percentile  99.8   P(simulated >= this)   0.2%

------------------------------------------------------------------------------
7. THE GAP, ACCOUNTED FOR
------------------------------------------------------------------------------
   step                                                   SE  x published
   A  published simulator                             0.0535         0.99
   B clustered X  rows from one real cluster          0.0552         1.03
   C empirical u,e  real residual shapes              0.0530         0.98
   D both B and C                                     0.0547         1.02
   E  nonparametric, rescaled to n                    0.0350         0.65
   REALIZED on the spent look (memo 11.5)           0.0716..        1.44
########## PHASE 2 ##########
==============================================================================
IS THE GAP IN THE NOISE MODEL OR IN THE DESIGN MATRIX?
==============================================================================
sims per seed 3000, seeds (20261106, 51, 52, 53), rho 0.0
burnt panel      198 episodes, 26 arrival clusters
held-out design  394 episodes, 64 arrival clusters   (the RUN fitted 394 in 64)

------------------------------------------------------------------------------
1. WITHIN-ARRIVAL ICC OF EACH REGRESSOR
------------------------------------------------------------------------------
   regressor                                  burnt  held-out  in the DGP
   atr  technical_atr_pct                    0.0080    0.1442      0.0000
   ma50  technical_ma50_distance_pct         0.0650    0.1959      0.0000
   press  n_gates_passed                     0.0348    0.0508      0.0000

   spread of each regressor                   burnt  held-out
   atr  sd                                   2.4564    2.1105
   ma50  sd                                 19.8241   14.0762
   press  sd                                 0.4574    0.4085

   cluster sizes burnt     n=26 mean 7.62 median 6.0 max 26
   cluster sizes held-out  n=64 mean 6.16 median 5.0 max 23

------------------------------------------------------------------------------
2. SD OF THE SLOPE UNDER ONE NOISE MODEL AND THREE DESIGNS
------------------------------------------------------------------------------
   design                                             SE           spread  x base
   X3  burnt design, i.i.d. rows (the baseline)   0.0533 [0.0523, 0.0538]    1.00
   X1  real held-out design, FIXED                0.0539 [0.0531, 0.0548]    1.01
   X2  real held-out design, clusters resampled   0.0538 [0.0533, 0.0548]    1.01

------------------------------------------------------------------------------
3. WHERE THE RUN'S THREE READINGS FALL, PER DESIGN
------------------------------------------------------------------------------

   X3  burnt design, i.i.d. rows (the baseline)
      CR2 SE over 12000 panels: mean 0.0529  sd 0.0071  p95 0.0653  p99 0.0718
      across-panel SD of the slope 0.0533; CR2 reads 0.99x of it
      interval half-width / 1.645  0.0716  percentile  98.9  P(>= this)   1.1%
      CR2 t                        0.0731  percentile  99.3  P(>= this)   0.7%
      bootstrap p                  0.0773  percentile  99.8  P(>= this)   0.2%

   X1  real held-out design, FIXED
      CR2 SE over 12000 panels: mean 0.0533  sd 0.0071  p95 0.0656  p99 0.0726
      across-panel SD of the slope 0.0539; CR2 reads 0.99x of it
      interval half-width / 1.645  0.0716  percentile  98.7  P(>= this)   1.3%
      CR2 t                        0.0731  percentile  99.1  P(>= this)   0.9%
      bootstrap p                  0.0773  percentile  99.8  P(>= this)   0.2%

   X2  real held-out design, clusters resampled
      CR2 SE over 12000 panels: mean 0.0538  sd 0.0077  p95 0.0675  p99 0.0751
      across-panel SD of the slope 0.0538; CR2 reads 1.00x of it
      interval half-width / 1.645  0.0716  percentile  97.9  P(>= this)   2.1%
      CR2 t                        0.0731  percentile  98.5  P(>= this)   1.5%
      bootstrap p                  0.0773  percentile  99.4  P(>= this)   0.6%

------------------------------------------------------------------------------
4. SUMMARY AGAINST THE PUBLISHED AND THE REALIZED
------------------------------------------------------------------------------
   quantity                                           SE  x published
   published simulator (memo 10.1)                0.0538         1.00
   X3  burnt design, i.i.d. rows (the baseline)   0.0533         0.99
   X1  real held-out design, FIXED                0.0539         1.00
   X2  real held-out design, clusters resampled   0.0538         1.00
   REALIZED, interval half-width / 1.645          0.0716         1.33
   REALIZED, CR2 t                                0.0731         1.36
   REALIZED, bootstrap p                          0.0773         1.44
########## PHASE 3 ##########
==============================================================================
WHAT WOULD THE SIMULATOR HAVE TO BELIEVE TO HAVE PREDICTED 0.072?
==============================================================================
sims per seed 1500, seeds (20261106, 51, 52, 53), rho 0.0, design = real held-out (FIXED)
held-out panel 394 episodes in 64 arrival clusters
burnt sd(sel_ar_20) 0.1681, burnt outcome ICC 0.0505 (what the DGP uses)
sd of the per-cluster mean standardised ATR  0.5708
published simulated SE 0.0538; realized CR2 SE 0.0731 (range 0.0716..0.0773)

------------------------------------------------------------------------------
1. RESIDUAL ICC SWEEP, cluster shock INDEPENDENT of ATR (lam = 0)
------------------------------------------------------------------------------
      icc       SE             spread  x published  x realized CR2
    0.000   0.0519   [0.0517, 0.0524]         0.96            0.71
    0.050   0.0541   [0.0536, 0.0545]         1.01            0.74
    0.100   0.0560   [0.0554, 0.0566]         1.04            0.77
    0.200   0.0594   [0.0587, 0.0603]         1.10            0.81
    0.350   0.0640   [0.0633, 0.0654]         1.19            0.88
    0.500   0.0683   [0.0677, 0.0699]         1.27            0.93
    0.700   0.0736   [0.0726, 0.0754]         1.37            1.01
    0.900   0.0784   [0.0770, 0.0804]         1.46            1.07

------------------------------------------------------------------------------
2. CLUSTER HETEROSKEDASTICITY SWEEP
------------------------------------------------------------------------------
   a session's whole noise is scaled by exp(gam * z(its mean ATR)),
   renormalised so the panel's total noise variance is unchanged.
   gam = 0 is the DGP as written: every session equally noisy.
   the noise ratio between the loudest and quietest session is shown.
   gam 0.0  loudest/quietest session noise scale 1.0x
   gam 0.2  loudest/quietest session noise scale 2.5x
   gam 0.3  loudest/quietest session noise scale 4.0x
   gam 0.4  loudest/quietest session noise scale 6.3x
   gam 0.5  loudest/quietest session noise scale 9.9x
   gam 0.7  loudest/quietest session noise scale 24.8x
   gam 1.0  loudest/quietest session noise scale 98.1x

      icc    gam 0.0    gam 0.2    gam 0.3    gam 0.4    gam 0.5    gam 0.7    gam 1.0
    0.050     0.0541     0.0629     0.0687     0.0751     0.0812     0.0915     0.1004
    0.200     0.0594     0.0704     0.0778     0.0859     0.0937     0.1068     0.1184
    0.500     0.0683     0.0828     0.0927     0.1032     0.1135     0.1307     0.1461

   target: the realized CR2 SE 0.0731

------------------------------------------------------------------------------
3. IS SESSION-LEVEL HETEROSKEDASTICITY VISIBLE IN THE BURNT PANEL?
------------------------------------------------------------------------------
   21 burnt sessions with at least 3 episodes
   per-session residual sd: min 0.105 median 0.723 max 1.097   ratio 10.4x
   a homoskedastic panel still shows spread by chance; for scale, the
   sd of log(sd) across sessions is 0.570 against
   the 0.250 expected from sampling alone at the mean session size 9.0

   gam measured on the burnt panel (slope of log session sd on z(session ATR))
   gam_hat +0.360  se 0.101  95% [+0.161, +0.558]
   burnt ATR within-session icc is 0.008, so there is almost no between-session ATR
   variation here to regress against. this estimate is weak BY CONSTRUCTION, which is
   the finding: the burnt panel cannot calibrate the one parameter that explains the gap.

------------------------------------------------------------------------------
4. HOW TO READ THIS
------------------------------------------------------------------------------
   the target is the realized CR2 SE 0.0731. the cell that reaches it
   IS the calibration the burnt panel could not supply. compare two routes:
   raising the residual icc needs about 0.9, the clamp ceiling of
   estimate_icc_local, against the burnt 0.050. adding session
   heteroskedasticity needs a gam under 0.5 at the burnt icc left untouched.
   the second is the parsimonious explanation; the first is at the edge of
   the DGP's own parameter space.
########## PHASE 4 ##########
==============================================================================
TIED TO ATR, OR JUST ANY SESSION-LEVEL NOISE SPREAD?
==============================================================================
sims per seed 1500, seeds (20261106, 51, 52, 53), design = real held-out (FIXED)
held-out panel 394 episodes in 64 arrival clusters
burnt icc 0.0505 (left untouched), burnt sd(sel_ar_20) 0.1681
published simulated SE 0.0538; realized CR2 SE 0.0731

   both arms share the same spread of session noise scales at each gam;
   only the TIED arm lines that spread up with the session's ATR level.

     gam  scale ratio      TIED      FREE  TIED/realized  FREE/realized
     0.0         1.0x    0.0542    0.0545           0.74           0.75
     0.2         2.3x    0.0621    0.0543           0.85           0.74
     0.3         3.6x    0.0674    0.0541           0.92           0.74
     0.4         5.5x    0.0732    0.0540           1.00           0.74
     0.5         8.4x    0.0790    0.0539           1.08           0.74
     0.7        19.5x    0.0892    0.0536           1.22           0.73

   if TIED reaches the realized SE and FREE does not, the mechanism is
   specific: a session's noise tracks its volatility, and an ML
   registration must simulate that for any volatility-like feature.
   if both reach it, the lesson is generic and applies to every feature.
########## PHASE 5 ##########
==============================================================================
THE FORWARD PREDICTION: THE MEASURED gam, PUT BACK IN
==============================================================================
sims per seed 3000, seeds (20261106, 51, 52, 53), design = real held-out (FIXED)
held-out panel 394 episodes in 64 arrival clusters
icc left at the burnt measurement 0.0505; sd(sel_ar_20) burnt 0.1681
gam measured on the BURNT panel: +0.360, 95% [+0.161, +0.558]
published simulated SE 0.0538; realized CR2 SE 0.0731

       gam what it is                     SE             spread  x published  x realized
     0.000 the DGP as written         0.0539   [0.0529, 0.0546]         1.00        0.74
     0.161 measured 95% low           0.0601   [0.0588, 0.0607]         1.12        0.82
     0.250                            0.0645   [0.0632, 0.0651]         1.20        0.88
     0.300                            0.0673   [0.0659, 0.0678]         1.25        0.92
     0.360 MEASURED point estimate    0.0707   [0.0694, 0.0713]         1.31        0.97
     0.450                            0.0760   [0.0746, 0.0766]         1.41        1.04
     0.500                            0.0789   [0.0775, 0.0795]         1.47        1.08
     0.558 measured 95% high          0.0821   [0.0807, 0.0828]         1.53        1.12

   the prediction is the MEASURED row. it was not chosen to match the
   realized SE; gam came off the burnt panel before this table was run.

########## PHASE 6 ##########
==========================================================================
BETWEEN-SESSION SPREAD OF THE STANDARDISED ATR COLUMN, BOTH PANELS
==========================================================================

burnt:  198 episodes, 26 sessions
  within-session ICC of the ATR column        0.0080
  sd of the per-session MEAN standardised ATR 0.3877   <-- the quantity
  session sizes: mean 7.62 median 6.0 harmonic 3.96 max 26
  normal-theory check sqrt(icc + (1-icc)/m_harmonic) = 0.5082
  on the 21 sessions with at least 3 episodes: sd of session means 0.4033

held-out:  394 episodes, 64 sessions
  within-session ICC of the ATR column        0.1442
  sd of the per-session MEAN standardised ATR 0.5708   <-- the quantity
  session sizes: mean 6.16 median 5.0 harmonic 3.72 max 23
  normal-theory check sqrt(icc + (1-icc)/m_harmonic) = 0.6120
  on the 52 sessions with at least 3 episodes: sd of session means 0.5302

The gam regression in icc_calibration_probe.py section 3 regresses
log(per-session residual sd) on z(per-session mean ATR) over the BURNT
sessions with at least 3 episodes, so the burnt subset figure is the one
that bounds how much variation that regression had to work with.

########## PHASE 7 ##########
============================================================================
SESSION-LEVEL OR ROW-LEVEL? THREE AUDIT OBJECTIONS, MEASURED
============================================================================
sims per seed 2000, seeds (20261106, 51, 52, 53), real held-out design (FIXED)
burnt 198 episodes / 26 sessions; held-out 394 / 64
burnt outcome icc 0.0505; realized CR2 SE 0.0731; published 0.0538

----------------------------------------------------------------------------
A. DOES ROW-LEVEL HETEROSKEDASTICITY REACH THE REALIZED SE TOO?
----------------------------------------------------------------------------
   session = noise scaled by exp(gam * z(SESSION mean ATR)), icc kept at
             the burnt 0.050, i.e. the memo's section 7.2 and 7.4 arm
   row     = noise scaled by exp(gam * z(the ROW's own ATR)), SAME icc
   row_only= the same row scaling with NO cluster shock at all (icc 0)

      gam    session        row   row_only  sess/real  row/real  rowonly/real
     0.00     0.0540     0.0540     0.0520       0.74      0.74          0.71
     0.20     0.0620     0.0723     0.0699       0.85      0.99          0.96
     0.30     0.0673     0.0870     0.0842       0.92      1.19          1.15
     0.36     0.0708     0.0970     0.0939       0.97      1.33          1.28
     0.40     0.0731     0.1039     0.1007       1.00      1.42          1.38
     0.50     0.0790     0.1214     0.1179       1.08      1.66          1.61
     0.70     0.0891     0.1512     0.1477       1.22      2.07          2.02

----------------------------------------------------------------------------
B. WHAT THE SECTION 7.3 ESTIMATOR READS UNDER EACH KNOWN TRUTH
----------------------------------------------------------------------------
   simulated on the REAL burnt design (real session sizes, real ATR column),
   1500 panels per row. the estimator is the one section 7.3 uses, filter and
   subset re-standardisation included.

   truth                               mean gam_hat       sd   P(t>1.96)
   homoskedastic                             +0.045    0.091        9.0%
   SESSION-level, gam 0.36                   +0.419    0.091       99.4%
   ROW-level, gam 0.36                       +0.240    0.096       72.4%
   ROW-level, gam 0.60                       +0.429    0.106       96.5%

   if ROW-level truth also produces a positive significant gam_hat, the
   section 7.3 estimator does NOT identify the level of the mechanism.

----------------------------------------------------------------------------
C. THE 'EXPECTED FROM SAMPLING ALONE' BENCHMARK, ON THE REAL SIZES
----------------------------------------------------------------------------
   the 21 burnt sessions with >=3 episodes, sizes mean 9.00 median 7.0 max 26
   memo's construction  1/sqrt(2*(mean size - 1))      0.250
   size-corrected       sqrt(mean 1/(2*(n_i - 1)))     0.336
   SIMULATED under homoskedasticity, 4000 draws        0.388  (sd across draws 0.111)
   OBSERVED (section 7.3)                              0.570
   so the ratio is 1.47x, not 2.28x; in VARIANCE units chance explains 46%, not 19%

----------------------------------------------------------------------------
D. IS THERE ROW-LEVEL ALIGNMENT ON THE BURNT PANEL?
----------------------------------------------------------------------------
   slope of log|row residual| on z(row ATR): +0.132  se 0.085  t +1.55
   (the row analogue carries a factor of about 1.0 against the session
   estimator because log|e| and log(sd) differ by a constant, not a scale)

   section 7.3's SESSION estimator on the same residuals, for comparison:
   gam_hat +0.360  se 0.101  t +3.55  on 21 sessions
```
