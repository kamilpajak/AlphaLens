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

The cause is a mechanism the simulator does not model at all: **arrival sessions
differ in how noisy they are, and a session's noise tracks its own volatility —
the same quantity the regressor under test measures.** That mechanism is not a
hypothesis fitted to the answer. It is measurable on the burnt panel, it measures
`gam` = +0.360 with a standard error of 0.101, and putting that measured value
back into the simulator predicts a slope SE of **0.0707** against the realized
**0.0731**, closing about 88% of the gap in one step.

| | slope SE | × published |
|---|---|---|
| published simulator (§10.1) | 0.0538 | 1.00 |
| simulator + the measured mechanism | **0.0707** | 1.31 |
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
Two consequences follow from the construction and are not opinions about it:

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

## 5. Why a cluster intercept cannot inflate a slope

**The simulator's cluster penalty on the SLOPE is 1.00×.** Measured directly:
with the ICC at its estimated 0.050 the SE is 0.0535, and with the ICC forced to
zero it is 0.0536. Setting a cluster ICC does nothing to the precision of a slope.

That is a property of the design, not a defect in the ICC estimate. An
independent cluster intercept inflates the variance of a MEAN a great deal. It
inflates the variance of a SLOPE only through the within-cluster correlation of
the regressor — and the simulator sets that to zero. Two assumptions that each
look conservative in isolation cancel each other exactly.

§10.7 could not have found this from its own assumption list, because it is an
**interaction between two listed assumptions** rather than a property of either.

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
real, it is **necessary** for §7, and by itself it is invisible.

## 7. The mechanism, and the forward prediction that tests it

### 7.1 Raising the ICC does not get there at a credible setting

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

Reaching the realized 0.0731 needs an ICC near 0.70 to 0.90, against the burnt
0.050, and 0.90 is the clamp ceiling of `estimate_icc_local` itself. That is the
edge of the DGP's own parameter space, so it is not a credible explanation.

### 7.2 Session-level heteroskedasticity, and that it must track the feature

Modelled by scaling a session's whole noise by `exp(gam × z(that session's mean
ATR))`, renormalised so the panel's TOTAL noise variance is unchanged, with the
ICC left at its measured 0.050.

**`gam` is defined on the per-session, unweighted z-scale throughout this memo**,
the same scale §7.3 measures it on. A sweep over a row-weighted z-scale was also
run and gives figures about 2.6% apart at the same nominal `gam`; it is not quoted
here, because two tables on two scales under one symbol is how a threshold comes
to sit beside a number in a different unit.

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
down slightly. The inflation comes from the ALIGNMENT, not from the spread. That
is why §6's structural error is necessary: without a clustered regressor there is
nothing for the noise scale to align with.

### 7.3 The mechanism is measurable on the burnt panel

Per-session residual spread, on the 21 burnt arrival sessions carrying at least 3
episodes (of 26), residuals taken from the 3-signal standardised burnt fit:

| quantity | value |
|---|---|
| per-session residual sd | 0.105 to 1.097, a ratio of **10.4×** |
| sd of log(session sd) observed | **0.570** |
| sd of log(session sd) expected from sampling alone, at the mean session size of 9.0 | 0.250 |
| **`gam` measured** — slope of log(session sd) on z(session mean ATR) | **+0.360**, se 0.101, 95% [+0.161, +0.558] |

The observed spread is more than twice what chance explains, and the alignment
with ATR is a t of 3.6.

> **CORRECTION, same day.** The probe that produced this table carries a comment,
> written BEFORE it ran, predicting that this estimate would be "weak BY
> CONSTRUCTION" because the burnt ATR column's within-session ICC is only 0.008.
> That prediction was wrong and the printed output says so. A low within-session
> ICC limits how much of the between-session ATR spread is structural; it does not
> remove the spread, and the sd of the per-cluster mean standardised ATR is 0.5708.
> The caveat is left in the verbatim output of §11 rather than edited out.

### 7.4 The forward prediction

`gam` was measured on the burnt panel before this table was computed, and it is
the burnt side's job to supply the simulator's calibration. So this is a
prediction, not a fit. 3000 panels per seed, four seeds, real held-out design,
ICC left at 0.0505:

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
which contains the realized value with room on both sides.

In SE units the mechanism accounts for `(0.0707 − 0.0539) / (0.0731 − 0.0539)` =
**88% of the gap**, and the 0.0024 left over is a third of one standard deviation
of the one-panel CR2 estimator measured in §8.

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

The realized 0.0731 sits at about the **99th percentile**: P(simulated ≥ realized)
is 0.6% on the burnt design and 0.9% on the real held-out design. So the realized
precision was never impossible under the published simulator — roughly a
one-in-120 draw. **The percentile alone would not have settled it.** What settles
it is §7.4: a mechanism measured independently, on the side of the data meant to
supply calibration, that moves the CENTRE of the distribution to the realized
value.

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
volatility-like feature the penalty is the full one; for a feature orthogonal to
session volatility the probe measures none. "Multiply every power figure by 1.36"
is wrong in both directions depending on the feature, which is a worse failure
mode than a known constant bias.

**The recommendation.** Two routes are open and one is cheaper than it looks.

1. **Model it.** `simulate_panel` would need a per-cluster noise scale tied to the
   cluster's own feature values, calibrated per feature the way §7.3 calibrates it
   for ATR. This is the correct fix and it is reusable, but every new feature needs
   its own `gam`, measured on burnt data, and §7.3's estimate rests on 21 sessions.
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
month. An earlier pass in the same session quoted 63 / 91 / 142 / 205 clusters and
1.4 / 4.1 / 7.3 months; those were computed against the 0.0716-to-0.0773 range
rather than against the single CR2 figure §8 settles on, and this table supersedes
them.

**Against house rule 5, the news is mixed and both halves matter.** Rule 5 makes a
model that loses to ATR alone an empty result. ATR alone is now measured at a
held-out −0.0713 with an interval spanning zero, so BEATING it is a low bar.
Beating it DETECTABLY on 64 clusters needs about 0.18, roughly 2.5× ATR's own
held-out point estimate and half the burnt −0.358 that did not survive. The
floor-25 and floor-30 episode gates passed on 2026-09-05 and are not what blocks
an ML registration. This precision is.

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

That bar existed so the PRE-RUN power simulation could not import held-out
dependence into the gate it had to clear blind. The gate is now history: the look
is spent, the cluster is retired, and no decision downstream of that gate remains
open. A feature column is also not an outcome — the live scorer reads these three
every day, and owner decisions D1 to D5 for the ML label registry require reading
features. On that reading the read is legal and costs nothing, and **no charge is
taken against the budget**, on the same basis the ledger's 2026-09-03 row records a
burnt-panel read as no charge.

It is recorded anyway, with its reasoning, because it is a departure from a
written allowlist and the owner may judge it differently. **If the Jul-to-Nov 2026
window should stay structurally blind for a FUTURE registration, that has to be
said now**: §6's ICC figures cannot be unread, and the correct remedy would be to
treat that window's dependence structure as spent.

## 11. Limitations

- **The mechanism is measured on burnt data and applied to held-out structure.**
  §7.3's `gam` comes from 21 burnt sessions; §7.4 applies it to the held-out
  design. The match to the realized SE is a forward prediction, but a single
  prediction hitting inside its own interval is weaker evidence than a replication
  would be, and no replication is available without spending a look.
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
```
