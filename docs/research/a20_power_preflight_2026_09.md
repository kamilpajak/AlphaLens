# A20 power preflight for #1227 — is the ATR test powered, and if not, when?

**Status:** COMPLETE 2026-09-22. The pre-commitments in §2 were written and
committed in `1621be4b`, **before** the preflight ran; §4 is the single recorded
run. Nothing here registers #1227 and nothing here runs its confirmation.

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
> The computed date is 2026-09-25 — *earlier* than the Wake line, not later.
> This paragraph only provided for the "later" case. The same rule applies:
> a date computed from observed arrivals replaces one estimated under the wrong
> maturity, in whichever direction it falls. Said plainly because moving a
> deadline *closer* is the self-serving direction, and it is worth stating that
> nothing here was chosen — the date follows from arrivals already on record,
> the fixed 20-session maturity, and a power curve computed without reading one
> held-out outcome.

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

## 4. Results

One run, 2026-09-22, on the pre-committed **briefed** population.
`scripts/ml/a20_power.py`; 4 000 simulations at the gate, 2 000 per cluster count.

**Burnt-panel A20 effects** (standardised, jointly fitted, 197 episodes; 90%
cluster-bootstrap interval):

| signal | effect | 90% interval |
|---|---|---|
| ATR | **−0.378** | −0.451 … −0.275 |
| MA50 extension | −0.303 | −0.433 … −0.128 |
| press gate | +0.026 | −0.054 … +0.116 |

**The cost D5 warned about did not materialise.** July reported `rho = -0.35` for
ATR on `car_10`; on `sel_ar_20` it is **−0.378**, slightly stronger. The delay
below comes entirely from A20 taking twice as long to mature, not from a weaker
signal.

**Power at the gate** (50% of the discovery effect), held-out panel as it stands:
34 matured clusters, 396 episodes, accrual 1.00 cluster/session, sd(y) 0.165,
ICC 0.06.

| signal | power | Monte Carlo SE |
|---|---|---|
| ATR | **77.3%** | ±0.7 |
| MA50 | 61.8% | ±0.8 |
| press gate | **4.1%** | ±0.3 |

**Verdict: not yet.** The gate is ATR ≥ 80%; it stands at 77.3%.

**When.** Power by cluster count, and the date each is reached — from arrival
sessions **already on record** (55 exist, 34 have matured) plus the fixed
20-session maturity:

| clusters | ATR power | reached |
|---|---|---|
| 34 (now) | 77.3% ±0.7 | — |
| 36 | 79.5% ±0.9 | 2026-09-23 |
| **38** | **83.7% ±0.8** | **2026-09-25** |
| 40 | 87.2% ±0.7 | 2026-09-29 |

38 is the first count whose interval lies entirely above the gate; 36 straddles
it. **So the preflight clears on 2026-09-25**, three sessions out — and the Wake
line should move there from 2026-10-14.

### 4.1 Two findings that are not about the gate

**The press gate cannot be confirmed or refuted at this sample size, and waiting
will not fix it.** Its burnt-panel effect is +0.026 with an interval straddling
zero, and power is 4% — indistinguishable from the 5% a coin would give. Running
#1227 spends its one look on all three; for the press gate that look will return
nothing either way. That is worth knowing before the run, not after.

**MA50 at 61.8% is below the gate too**, and #1227's gate condition names only
ATR. A run on 2026-09-25 would therefore report one adequately powered test and
two underpowered ones. Whether that is acceptable is a registration decision.

### 4.2 A correction made during the run

The first version of `gate_date` projected time for the missing arrivals to
*happen* and then mature, giving 2026-10-26. That double-counts: 55 arrivals
already exist in the held-out window and only 34 have matured, so what is
missing is maturity, not arrivals. The unit test asserted the same wrong model,
so it passed and confirmed the error. Both are fixed, and the test now says why
it exists. The five-week difference between 2026-10-26 and 2026-09-25 is the
size of that mistake.

The same shape appeared in the power number itself: 200 simulations gave 76%,
400 gave 79.75%, and both were reported to two significant figures while the
Monte Carlo error was ±2 points. Only the 4 000-simulation run (±0.7) could tell
"clears" from "does not".

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
- **Romano-Wolf would dominate Holm** under dependence, and
  `alphalens_research/backtest/romano_wolf.py` already exists and is tested.
  #1227 pre-registered Holm, so swapping it is a registration change and belongs
  in that decision, not here.
