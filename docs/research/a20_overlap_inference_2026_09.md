# A20 overlap inference — does the #1227 power gate survive the 20-session window?

**Status:** COMPLETE 2026-09-23, **re-run 2026-09-24 on a corrected panel — the assumption still
holds, but the power figures are withdrawn.** **The size verdict itself is UNDER MEASUREMENT since
2026-11-07**: it was measured under homoskedastic noise only, and the instrument to test it under
feature-aligned noise now exists but has not been run on the real stores. See the 2026-11-07 note
below. The arrival-session wild cluster bootstrap keeps its
level on an overlap-aware panel in every cell of the grid, and that verdict survives the correction
intact. The power column does not: it was computed on a panel counted in the wrong unit. See the
correction box; §5 holds the original run and §6 the re-run.
**Power figures qualified again 2026-11-07:** §6's corrected **64.6-66.2%** also comes from
`a20_power.simulate_panel`, which the spent #1227 look measured to be optimistic on the slope
standard error by **1.36x** for a reason the registration never disclosed — noise that is
heteroskedastic and ALIGNED WITH THE FEATURE under test, where the simulator gives every
observation one noise scale. Those figures stand as SIMULATOR readings and are not realized
power. See
[`a20_simulator_precision_gap_2026_11_07.md`](a20_simulator_precision_gap_2026_11_07.md). The
bootstrap LEVEL verdict, which is this memo's actual subject, is unaffected: it is a size check,
not a power figure.

**That last sentence is UNDER MEASUREMENT as of 2026-11-07 and was asserted, never measured.** A
wild cluster bootstrap's SIZE under heteroskedasticity correlated with the regressor is not
obviously its size under homoskedasticity, especially at few clusters, where leverage concentrates
in the high-|x| clusters. This module chooses between four inference methods BY SIZE (7.5%,
Bradley's liberal criterion) and refuses a power figure to any method that exceeds it — so if
alignment moves the sizes, it can move WHICH METHOD WINS. `a20_overlap_power.py --alignment-size`
now runs every cell of the grid at three noise corners (homoskedastic, session-aligned at gam 0.400,
row-aligned at 0.205), paired draw for draw on one seed, and `size_verdict` reports a three-state
answer: the claim survives, it does not, or the cells it depends on are unresolved. The sentence
above may only be edited — restricted to the `kappa = 0` rows, or withdrawn — after the
publishable 2000-panel x 399-bootstrap run on the REAL stores, with the per-arm table, its
construction and its read date published beside it. It must not be edited on a low-count Mac run or
on a fixture.

**The instrument was repaired on 2026-11-08 and now reports BOTH readings (2026-11-08).** Nine
defects in the three-arm size experiment were found by mutation and fixed; none of them moved the
default homoskedastic path, whose twelve bit-identity digests still match. Two of the nine change
what this memo will be able to quote:

- `size_verdict` computes a SECOND verdict on the `kappa = 0` rows alone, reports
  `refutation_is_composed`, and its verdict line states which reading refuted. A cell at
  `kappa = fitted` applies the feature-dependence TWICE — a linear loading of the shared shock and
  an exponential rescaling of the whole residual — and the two magnitudes do not add, so a
  refutation arriving only from such a cell is a different claim from one the single-mechanism rows
  share. `--single-mechanism-only` runs the `kappa = 0` grid on its own, which is what makes the
  restriction this section pre-authorises reachable rather than hypothetical.
- The ROW-level closed form for what alignment does to the two correlation forms is WITHDRAWN. It
  was published as `E[exp(gam*x)]**2 / E[exp(2*gam*x)]` on the feature column, on the grounds that
  the panel-wide normaliser cancels. It does not: that normaliser is `sqrt(mean(s**2))` over the
  panel's OWN rows, so it is random and correlated with the rows whose covariance the form
  describes. Read 2026-11-08 over 40 000 panels at panel seed 5, gam 0.6, eight clusters of two
  rows, icc 0.4, phi 0, on the seeded column draw the suite's own fixture uses (`_z` of a
  400-point draw from `default_rng(31)`), the form understated the simulated within-cluster
  correlation by 1.04× on a gaussian column, 1.43× on a lognormal one at sigma 0.2, 5.06× at sigma
  0.4 and 38.65× at sigma 0.6; the same four columns on a differently SHAPED panel, one cluster of
  two rows, read 1.13× / 1.66× / 6.18× / 48.78×, so the quantity is not a function of the column
  at all. The simulated side of that comparison carries a Monte Carlo error: at 40 000 panels its
  standard deviation over ten independent panel seeds is 0.0051, which is about plus or minus
  0.8× on the 38.65×, so a recomputation landing on 38.2× has reproduced this and not
  contradicted it. The error is a function of the DRAW as well as of the shape: over 200
  independent sigma 0.6 draws, seeds 0..199, 4 000 panels each, the ratio spans 1.01× to 132.60×
  with a median 2.37×. Seed 31 reads 36.61× in that spread and ranks 195th of its 200 draws, the
  97.5th percentile with five draws worse, so the headline is a bad draw and not a central
  estimate; this is why the column seed is named here. Every figure in this paragraph is derived
  from the recipe by `TestThereIsNoClosedFormForTheRowLevel`, in
  `apps/alphalens-research/tests/test_a20_overlap_power.py`, which carries the three tables, their
  seeds and the spread; that class also checks this paragraph against them.
  These figures AGREE with the adversarial finding that prompted the deletion (1.45× / 5.10× /
  38.8× on the lognormal columns). An earlier version of this paragraph said "2.00× on a lognormal
  one" from a table whose lognormal column named no seed and did not reproduce; it understated the
  error by about 22× at the worst column. Nothing in this memo quoted the form itself. Both
  correlation statements under alignment are now MEASURED DIRECTIONS, and the exact form that
  survives is the session level leaving the within-cluster correlation at the `icc`.

**CORRECTION, 2026-11-09 — the defect count above, and what the pairing claim rests on.**
The sentence above reads "Nine defects in the three-arm size experiment were found by mutation and
fixed". That is the FIRST repair round's count. Three rounds ran in total, on 9, 11 and 18 findings;
5 of the first 9 and 2 of the next 11 came back only half fixed and were re-opened, and two of the
defects were introduced by an earlier repair round. The count is superseded, not the claim it was
attached to.

The pairing claim in this section — "paired draw for draw on one seed", which is what makes a
corner-to-corner difference the MECHANISM rather than three independent Monte Carlo draws — was
checked more narrowly than it was stated, and is now checked along both axes it was missing.
Measured 2026-11-09, each mutation applied alone to `rejection_rate` against the `test_a20*.py`
glob at 508 tests:

| mutation | before | after |
|---|---|---|
| de-pair every method except `arrival` | 508 OK | RED, `TestThePairingHoldsForEveryPublishedMethod` |
| de-pair only when `n_sims` exceeds the fixture's 5 | 508 OK | RED, `TestThePairingDoesNotDependOnHowManyPanelsRun` |
| per-arm `clusters`, passed positionally | 508 OK | RED, the inference-invariant case |
| per-arm `coef_idx`, passed positionally | RED by an `IndexError` elsewhere | RED, named as a pairing break |

The first of those flips a published decision: at `block5` the row corner's size moves from 0.075
to 0.085, from holding the 7.5% bar to missing it, which deletes that corner's power figure and
withholds the span. Three of the four published methods were unpinned for pairing, because the
pairing fixture's calendar is six sessions, where `block10` and `block20` leave ONE cluster and
`rejection_rate` refuses them — so the method axis needed its own 40-session fixture rather than
an extra case on the old one.

The panel axis cannot be matched in a unit suite: every published figure comes from 2000 panels.
What is checked instead is the invariant that makes a five-panel fixture representative —
`rejection_rate` seeds its generator ONCE and then loops, so panels 1..K are the same draw whether
`n_sims` is K or 2K. A break conditioned on `n_sims`, or on a panel index below 2K, diverges that
prefix. A de-pairing inert on every fixture and active on every real run is the worst shape there
is, because the suite reports it as absent.

One further correction in the same area, to a cross-reference rather than to a number.
`TestTheMarginalSpreadSurvivesAlignment` was named as what pins the noise SCALE and did not: an
exact no-op of `a20_power._alignment_scale` left both its cases green, because a scale of ones also
holds the spread at `sd_y` and its normaliser case compared the function's output against an
`np.exp` the test recomputed itself. Its fixture was also degenerate at the session level — four
identical clusters, so that branch returns exactly 1.0 in every row by arithmetic, sd 0.0000,
whatever it computes. The no-op direction is now a case of its own, on distinct cluster slices, at
a threshold of half the MEASURED spread (0.4137 at the session level, 0.3803 at the row level).

Neither correction edits the size claim above.

Neither item edits the size claim above. The claim still waits for its real-store run.

**Amends:** [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md) §2.2 (the
inference row) and §3 (the method). That memo's 83.9% is not withdrawn; this one asks whether the
assumption under which it was computed holds, and §5 says.
**Registers nothing. Runs no held-out outcome.** Burnt-panel reads and held-out STRUCTURE only,
under the same allowlist the preflight fixed in its §2.3.

> ### CORRECTION — 2026-09-24
>
> This study consumed `held_out_structure` from `a20_power.py`, which counted distinct
> `(brief_date, ticker)` pairs where ledger rule 5 counts collapsed ticker-episodes. The panel was
> 205 episodes in 32 arrival clusters, not 419 in 35. Full account in
> [`a20_power_preflight_2026_09.md`](a20_power_preflight_2026_09.md); fix in `bf2f7b9a`.
>
> **What survives.** The size result, which is this memo's actual subject. Re-run on the corrected
> panel the arrival-session bootstrap holds its level in every cell, 4.2–6.1% against a nominal 5%
> and a pre-specified 7.5% bar — better behaved than before, since the conservative corner fell from
> 7.1% to 6.1%. The ranking of methods is unchanged: 5-session blocks 47%, 10-session 18%,
> 20-session still breaks size at 13–14% and still gets no power figure.
>
> **What is withdrawn.** Every power number in §5, including the "~93%". Corrected: **64.6–66.2%**
> across the grid, against a bar of 80%. §6 holds the re-run.
>
> **What this says about the review that passed it.** Nothing in this memo could have caught the
> error, because the panel arrived from a function this study called rather than from a claim it
> examined. Two adversarial review passes read the argument and not the input. The agreement between
> this memo and the preflight was never corroboration — both read the same line of code.

---

## 1. The hole

The merged preflight simulates one **independent** random effect per arrival session and then
computes its p-value with a wild cluster bootstrap clustered on arrival session. It generates and
tests under the same assumption. That is internally consistent, and it is exactly why it cannot
detect that the assumption is wrong.

The panel is not built that way. `sel_ar_20` accumulates 20 trading sessions from the arrival open,
and arrival clusters land at about one per session, so two adjacent clusters share up to 19 of their
20 outcome sessions. #1227 anticipated this in its own body — "session clusters alone are not
sufficient" — and wrote that when the primary was `car_10`, half as long.

The direction matters. Understated standard errors inflate the statistic and make rejection
**easier**. For a one-sided test whose rejection means "ATR is real", the exposure is CONFIRMING a
hand-made penalty that does not work, and keeping it for good, because ledger rule 4 ends testing
whichever way the single look lands.

## 2. A withdrawn justification

An earlier attempt to clear the concern measured two things on the burnt panel: the autocorrelation
of **session-mean residuals** (lag 1 `+0.060`, lags 2–10 inside `[−0.24, +0.27]` on 16–25 pairs), and
the CR1 cluster-robust standard error of the ATR coefficient under blocks of 1 / 2 / 5 / 10
consecutive sessions (`×1.00 / ×0.93 / ×0.90 / ×0.86`). It concluded that the overlap does not bite.

Two adversarial reviewers rejected that independently, and both were right:

- the quantity that transmits dependence into a **coefficient** is the autocovariance of the score
  contributions `X_t'û_t`, not of mean residuals — a mean-residual series can look clean while the
  score does not;
- a CR1 standard error that **falls** as the cluster coarsens to 13, 5 and 3 groups is more readily
  small-G instability than evidence of independence, and at 3 groups the estimate means very little.

That justification is withdrawn. It is recorded here rather than deleted because the memo it would
have gone into is the one this amends, and a reader comparing the two should see why the second
answer is not the first one restated.

## 3. What replaces it

### 3.1 A simulator in which the overlap is structural

`scripts/ml/a20_overlap_power.py` shares a per-day shock across the **real** arrival calendar, so
adjacent clusters share their windows by construction rather than by assumption. Calendar session
offsets are used, not positions in the arrival list, so a gap in the calendar produces a real gap in
the sharing.

The calibration does not move. The same burnt-panel `sd_y` and `icc` are used, split so that:

- within-cluster correlation is `icc` for **every** sharing fraction — the marginals are untouched;
- correlation at a lag of `k` sessions is `φ · icc · (horizon − k) / horizon`, zero at `k ≥ horizon`;
- **`φ = 0` reproduces the merged preflight exactly.**

That last property is what makes this a measurement of the old assumption rather than a replacement
of it. `φ = 1` is the conservative end, attributing *all* same-session correlation to the shared
window; any part that is genuinely arrival-specific — one catalyst, one theme, one day's news — does
not travel to the neighbour. The grid is reported rather than one value chosen.

### 3.2 The channel that decides it

A shared shock that moves every name arriving on a date by the **same** amount is a level shift, and
a level shift is orthogonal to a mean-zero regressor. It therefore never reaches the coefficient, no
matter how much the windows overlap. Measured on simulated panels: with full sharing and no loading,
the lag-1 score autocorrelation is `+0.005 ± 0.014`, against `−0.029` for the small-sample bias line
and against `+0.475` for the correlation of the **outcomes** themselves.

That holds only while the shock hits every name equally. ATR measures volatility, so high-ATR names
plausibly take more of a common move. The simulator scales each episode's share of the shared shock
by `1 + κ · z(atr)`, renormalised so total variance is unchanged and only the dependence structure
moves. This is the one channel through which overlapping windows can bias the ATR coefficient.

The diagnostic responds to it — that is the refutation control, and without it the near-zero result
above would be a check that could not fail:

| κ | φ = 0 | φ = 1 |
|---|---|---|
| 0.0 | −0.035 | −0.002 |
| 0.3 | −0.035 | **+0.067** |
| 0.6 | −0.035 | **+0.166** |
| 1.0 | −0.035 | **+0.245** |

> **The magnitudes in this table are PENDING RE-MEASUREMENT (2026-11-07).**
> `score_autocovariance` sorted sessions LEXICOGRAPHICALLY, so with ten or more clusters "lag 1"
> paired session 1 with session 10. Measured 2026-11-07 on a 35-session fixture whose per-session
> score is a monotone ramp, so the true value is known: calendar order reads **+0.914**,
> lexicographic order **+0.527**. The sign of the error is **not** predictable — three AR(1) panels
> at ρ = 0.6 gave gaps of +0.236, +0.019 and −0.142, so one read the scrambled panel as *more*
> dependent than it is. The function now sorts numerically and refuses a label that is not an
> integer string. The **conclusion** of this section survives both orderings (a level shift barely
> reaches the coefficient, and the loaded channel does reach it), because the comparison that
> carries it is between κ arms measured the same way. The **magnitudes** in the table do not
> survive, and they are re-measured on the next real-store run rather than edited here from a
> fixture.

### 3.3 Size before power

Power for a method whose level is wrong is not a power figure, so it is not computed. The bar is
pre-specified at **0.075** for a nominal 0.05 (Bradley's liberal robustness criterion; the upper end
is the one that matters, because the over-rejecting method is the one that hands back a confident
wrong confirmation). A method above it gets no power number at all, so there is nothing to quote.

## 4. The two calibration numbers, measured on the burnt panel

| Quantity | Value | Why it matters |
|---|---|---|
| ICC over arrival sessions | **0.0605** | only 6% of residual variance is common to a session, so there is little to share in the first place |
| κ, fitted ATR loading | **0.142** | the shared shock is close to a pure level shift, which the coefficient barely sees |
| `sd_y` | 0.1653 | unchanged from the preflight |
| burnt episodes, briefed | 197 | unchanged from the preflight |

κ is fitted as `|residual| = a + b·z(atr)` on the burnt panel and reported as `b / a`: the
proportional widening of an episode's response per standard deviation of the signal. It is clamped
below at zero — a negative loading would mean high-ATR names react *less* to a common move, which is
not a direction worth simulating and would make the overlap look harmless for the wrong reason.

Both channels are narrow. That is a prediction, not the answer; §5 is the answer.

## 5. Results — power figures SUPERSEDED 2026-09-24

> The size verdict below stands and was re-confirmed on the corrected panel. Every POWER figure in
> this section is superseded; see §6.

Run 2026-09-23, 2000 simulations x 399 bootstrap draws per cell, seed 20260923. Panel: 197 burnt
episodes, 419 held-out episodes in 35 arrival clusters over 35 sessions.

### 5.1 The arrival-session bootstrap holds its level

| sharing φ | loading κ | size | power at the 50% gate |
|---|---|---|---|
| 0.00 | 0 *(the merged preflight's assumption)* | 6.1% | 92.9% |
| 0.50 | 0 | 4.0% | 93.5% |
| 0.50 | fitted 0.142 | 4.8% | 92.2% |
| 1.00 | 0 | 5.5% | 93.3% |
| 1.00 | fitted 0.142 *(the conservative corner)* | **7.1%** | 91.3% |

Nominal level 5%, pre-specified bar 7.5%. Every cell sits under the bar, including the corner where
**all** same-session correlation is attributed to the shared window.

At 2000 simulations the Monte Carlo error on 7.1% is about ±1.1 points, so that one interval —
[6.0, 8.2] — crosses the bar. By this project's own rule a number whose interval crosses its bar is
not a verdict, so that cell was re-run alone at **40,000 simulations**: size **7.125%**, 95% interval
**[6.87%, 7.38%]**. The interval lies entirely below the 7.5% bar, so the corner is resolved rather
than rounded the right way.

**It is resolved, not clean.** 7.1% against a nominal 5% is about 1.4x the intended rejection rate:
in the conservative corner a test run at α 0.05 behaves closer to α 0.07. It clears the
pre-specified bar, and the bar was written down before the number existed, but the registration
should quote the measured level rather than the nominal one. At `φ = 0.5` with the fitted loading the
size is 4.8%, so this overshoot belongs to the corner where **all** same-session correlation is
assumed to travel, not to the likely case.

### 5.2 Coarser blocks are worse, not better

| method | groups on this calendar | size | power |
|---|---|---|---|
| arrival session | 35 | 4.0 – 7.1% ✓ | ~93% |
| 5-session block | 7 | 4.5 – 6.0% ✓ | ~76% |
| 10-session block | 4 | 3.4 – 4.0% ✓ | ~24% |
| **20-session block** | **2** | **12.4 – 14.3% ✗** | not computed |

The 20-session block is the remedy the overlapping-returns literature prescribes and the one both
adversarial reviewers recommended: a block at least as long as the horizon. On this calendar it
leaves two groups, and it **over-rejects at 12–14% in every cell — including `φ = 0`, where there is
no cross-cluster dependence at all to protect against**. That is not the overlap; it is the
cluster-robust estimator failing at two groups. Its size is wrong for a reason unrelated to the
problem it was brought in to solve, so it gets no power figure.

The 10-session block does hold its level, and costs two thirds of the power to protect against
something the measurement does not find.

### 5.3 Why the overlap does not reach the coefficient

Three measured numbers compose into one account:

1. **ICC 0.061** — only 6% of residual variance is common to an arrival session, so there is little
   to share. `sel_ar_20` is already beta-adjusted against IWM, which removes the largest common
   component before any of this.
2. **κ 0.142** — what remains is close to a pure per-date level shift, and a level shift is
   orthogonal to a mean-zero regressor.
3. Consequently adjacent clusters' **outcomes** correlate at `φ · icc · 19/20`, while their **score
   contributions** do not measurably correlate at all.

### 5.4 A by-product: the power of a one-hypothesis family

The `φ = 0, κ = 0, arrival` cell is the merged preflight's exact assumption with no Holm correction,
and it returns **92.9%** against that memo's **83.9%**. The whole difference is the bar: 0.05 instead
of 0.05/3. That is the family-of-one power figure #1227 needs if the look narrows to ATR alone, and
it is recorded here rather than re-derived later.

## 6. Limitations, stated before the numbers

- **κ is fitted on 197 burnt episodes over 26 arrival sessions.** It is a small-sample estimate of a
  second-moment relationship, which is the noisiest kind. The grid is reported partly for that
  reason.
- **The simulated shared shock is a single common factor.** Real co-movement among briefed names has
  sector and theme structure that one factor does not reproduce. A name-level factor structure would
  be a different simulator and is not built here.
- **`sel_ar_20` is already beta-adjusted against IWM**, so the largest common component is removed
  before any of this. That is a reason the measured ICC is small, and it is also why this memo cannot
  be carried over to an unadjusted outcome without re-measuring.
- **This memo does not re-derive the 83.9%.** It asks whether the assumption under which that number
  was computed holds. If it does, the number stands as computed; if it does not, the number needs a
  new inference method and a new power run, and §5 says which.
- **The registered test is one-sided; every rejection rate here is two-sided**, at the same bar the
  merged preflight used, so the two memos are comparable. That is a level shift applied equally to
  every row and does not change which method wins.

## 7. What this settles, and what it does not

**Settled.** The inference method #1227 should register is the one already in use: the wild cluster
bootstrap on arrival-session clusters. #1227's body says "session clusters alone are not
sufficient"; measured on this panel, for this coefficient, they are. That sentence was written
against `car_10` and against no measurement, and the registration should supersede it with the
numbers in §5 rather than quietly ignore it.

**Settled.** The merged 83.9% stands as computed. At a family of one it is 92.9%.

**Settled, and against expectation.** A block at least as long as the horizon — the standard remedy,
and the one both adversarial reviewers recommended — is unusable here. It leaves two groups and
over-rejects at 12–14% even with no dependence present. Recording this because the reasoning that
produced the recommendation was sound; it was the sample size that made the remedy worse than the
problem.

**Not settled here.** Whether the look runs at all, on what family, against what smallest actionable
effect, and what the three-way ATR outcome maps to. Those belong in the registration, which this
memo does not write.

**Settled since, elsewhere.** The mixed price-adjustment gate ran on 2026-09-23 and **passes** —
[`atr_split_adjustment_gate_2026_09.md`](atr_split_adjustment_gate_2026_09.md). Its premise is
withdrawn: `auto_adjust=False` does not return split-unadjusted bars, so ATR and the label are both
split-adjusted, and no corporate action reaches a held-out label window. The one real artefact in
the stores is a patchwork step on MQ that lands in no window. The guard's blind band, and its three
firings all being false positives, are recorded there as open items.

**Not settled here.** The yfinance stale-cache census was run for this memo's own calibration
(three logged fallback events in the whole journal, all in the burnt window, none of the three rows
present in the panel, zero in the held-out window), and the journal does not reach back before
2026-05-25.

## 6. Re-run on the corrected panel — 2026-09-24

Same script, same seed (20260923), same 2000 simulations x 399 bootstrap draws. Panel: 197 burnt
episodes, **205 held-out episodes in 32 arrival clusters** over 35 sessions. sd(y) 0.1653, ICC 0.061,
fitted signal loading kappa 0.142 — all three unchanged, because they come from the burnt side.

| sharing phi | loading kappa | size | power at the 50% gate |
|---|---|---|---|
| 0.00 | 0 *(the merged preflight's assumption)* | 4.2% | 66.1% |
| 0.50 | 0 | 4.4% | 65.5% |
| 0.50 | fitted 0.142 | 5.1% | 64.6% |
| 1.00 | 0 | 5.2% | 66.2% |
| 1.00 | fitted 0.142 *(the conservative corner)* | **6.1%** | 64.8% |

**Size: the conclusion is unchanged and slightly stronger.** Every cell sits under the 7.5% bar, and
the conservative corner improved from 7.1% to 6.1% — far enough under the bar that the 40 000-run
re-check §5.1 needed is not required here. Smaller clusters mean less within-session correlation for
the bootstrap to mishandle.

**Power: 64.6–66.2%, against a bar of 80%.** At 2000 simulations the Monte Carlo error is about
±1.1 points, so the widest reading is [64.0, 68.1]. The bar is not in that interval.

**Those are HOMOSKEDASTIC-CORNER readings, and the bracket over them is withheld (2026-11-07).**
Every figure in the table above comes from a DGP that gives each observation one noise scale, the
assumption measured 1.36× optimistic on the slope standard error. `--alignment-size` now computes
size AND power per corner, as fields on each row, and the module's two-stage refusal WINS over the
bracket: a corner whose size missed the 7.5% bar contributes no power figure, and a span missing a
corner is not a span, so `power_span` is withheld and `power_span_withheld_reason` names the arm
responsible. That shape is deliberate. If alignment pushes the arrival-session bootstrap over the
bar on the real panel, then no method is entitled to carry 64.6–66.2% there, and the driver must
show that rather than fill the column. No bracketed replacement is published here until the
2000-panel run on the real stores exists.

**A corroboration that is not an echo.** `a20_power.py` reports 66.7% at the family of one on the
2026-09-24 panel; this module reports 66.1% on the 2026-09-23 one. The two use DIFFERENT data
generating processes and different code paths for the simulation, so their agreement is real evidence
about the simulator. They share the panel read, so it is no evidence at all about the panel — which
is exactly the distinction the original error turned on.
