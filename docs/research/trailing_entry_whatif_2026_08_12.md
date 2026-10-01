# Trailing (bounce-confirmed) entries vs hardcoded limit entries — what-if replay

**Date:** 2026-08-12
**Status:** COMPLETE — direction-level diagnostic (a re-cut of already-used data, NOT a pre-registered strategy test; no Bonferroni claim is made). **Read the 2026-09-30 AND 2026-10-01 corrections below before quoting any number: the trigger this study used is not the one the broker runs, and the published day-1 magnitude does not reproduce.**
**Question (operator):** on our historical data, would trailing entries (enter only after price bounces d% off its running low) beat the current limit-at-touch entries?
**Method:** what-if replay over the population-ladder parquets (85 days, 769 plannable candidates) on cached Polygon minute bars; both variants share ONE exit function (the repo `ladder_replay` engine); slippage stressed both ways; independent verifier re-derived 4 cases by hand (6-decimal match, cases picked by deterministic rule, never by outcome) and reproduced the recorded parquet outcomes with Pearson r = 1.0000. Script: `apps/alphalens-research/scripts` authoring copy of `whatif_trailing_entry.py` (run from `/tmp` on the VPS); records parquet `/tmp/whatif_trailing_entry_records.parquet` (11,292 rows).

## Correction 2026-09-30 — the trigger form, and what it does and does not touch

**The study priced the trailing trigger as `run_low * (1 + d)`. The broker prices it as
`trough + distance`, where `distance = reference * d` is an absolute price frozen once at the touch.**

The distance is a price distance to the market, not a fraction of the running low, so it does not
shrink as the low falls. The authority is our own code, not vendor prose:
`alphalens_pipeline/brokers/automanager/entry_trail_geometry.py` computes `distance = reference *
d_frac` once and `order_price = reference + distance`, and that function builds the order that is
actually sent. A second, independent sign: the broker rejects a distance that is not a multiple of
the instrument tick (`PriceNotInTickSizeIncrements`), and a quantity rounded to a tick is a price
rather than a percentage.

The two forms agree at the touch, where the trough IS the reference, and separate as the trough
falls. Because `trough <= reference`, the study's level is the LOWER of the two, so its variant B
fires earlier and at a better price than the order would. The difference is `(reference - trough) *
d`.

**So the grid below describes an order type no venue we use offers.** That is a fault in the study,
not in the feature that shipped.

### What this does NOT say

- **No number here is withdrawn.** The size of the difference is a product of two small factors and
  need not move anything. **It has now been measured — see the addendum below, and read that before
  quoting any figure from the grid.**
- **The reproduction claim in the Method line is intact and the arithmetic claim is not, and the line
  does not distinguish them.** The independent verifier re-derived four cases by hand and matched the
  recorded parquet to `r = 1.0000`. It reproduced the study faithfully, including this trigger. A
  verifier that agrees with the code cannot catch a premise the code and the verifier share.
- **The fill-rate and concession claims are not automatically affected either**, because they depend
  on whether a trigger is reached at all rather than on where it sits, and that is a separate
  measurement.

### Two further divergences from the live order, neither of them this one

Named here so a later reader does not discover them as new:

- The server ratchets the trigger in COARSE steps of `distance * 0.10`
  (`entry_trail_geometry.TRAILING_STEP_FRACTION`, floored to whole ticks at placement), so the live
  trigger lags the falling low. This study models a continuous trail. Whether that helps or hurts
  variant B is not obvious and is not asserted here.
- The live reference is a BID and this study's bars are trade prices, so a spread sits between them.

### The same arithmetic also reached production, and was fixed there

The terminal measurement stamp in `control_loop` carried the proportional form too, in the field
that feeds the offline join this memo's evidence line asks for ("live rollout must MEASURE
realized-vs-replay before widening"). Fixed on 2026-09-30 under #1635, by a different route than the
study's: the arm now journals the ABSOLUTE `distance` it sent on the `trail_armed` line, and the
stamp adds it to the trough. Inverting the recorded trigger would not have worked, because the arm
prices the order with the AMBIENT `ALPHALENS_BROKER_ENTRY_TRAIL_BPS` while the `watch_open` record's
`d_bps` is frozen at drain time, so neither `d` reproduces the distance once the flag moves.

Two consequences for anyone joining on that field. The key is now `trigger_at_final_trough`; rows
written before 2026-09-30 carry `would_be_trigger` and the proportional arithmetic. And the new
field is `null` for a tier armed before the distance was journaled, and for one that never armed at
all — no verdict, rather than a re-derived number.

## Addendum 2026-09-30 — what the trigger correction is worth, measured

Read 2026-09-30 14:05 UTC, by running the corrected script (`cc512e34`) over the local
population-ladder store. Both trigger forms ran in ONE pass over the SAME touches and bars, paired on
`(date, ticker, tier, slip, d)` — a key verified unique, so the join is one-to-one and no figure is
inflated by fan-out.

`fill bps` is `(additive fill - proportional fill) / proportional fill`, across all cohorts. Positive
means the superseded form filled CHEAPER and so flattered variant B. `dR` is in the fixed
denominator, per paired fill.

| `d` | paired | fill bps med | fill bps mean | dR mean | dR mean ex-worst | fills add / prop | additive cheaper |
|---|---|---|---|---|---|---|---|
| **0.5%** | 574 | **+0.024** | +0.060 | **-0.0000** | -0.0001 | 574 / 574 | 3 |
| 1.0% | 569 | +0.393 | +0.469 | -0.0003 | -0.0004 | 569 / 569 | 3 |
| 1.5% | 557 | +0.831 | +1.197 | -0.0002 | -0.0006 | 557 / 557 | 9 |
| 2.0% | 552 | +1.642 | +2.871 | -0.0015 | -0.0017 | 552 / 552 | 4 |
| 3.0% | 519 | +3.328 | +3.931 | -0.0021 | -0.0024 | 519 / 522 | 6 |

(The slippage-adverse view differs in the fourth decimal and is omitted; it is in the script's own
output.)

**The correction is immaterial at the setting production runs.** At `d` = 0.5%, the live flag value,
it is worth four hundred-thousandths of an R against a grid whose spread is two orders larger, and
the fill counts are IDENTICAL. Fill counts stay identical through `d` = 2.0%; only at 3.0% does the
additive form miss three fills the proportional one caught. So this defect moves no sign, no ranking
and no boundary, and in particular the `ENTRY_TRAIL_BPS_MAX = 150` argument in
`entry_trailing_design_2026_08_12.md` §6 does not rest on it.

`dR mean ex-worst` drops the single largest-magnitude row and agrees in SIGN with `dR mean` in every
row, so no figure here is one row's opinion. The difference is nonetheless TWO-SIDED: three to nine
rows per configuration fill cheaper under the additive form, because a higher trigger can fail to
fire on a bar the lower one fired on, the trough keeps falling, and the eventual fill lands far lower
many bars later. A one-way statement of the direction would be wrong.

### The construction, because the number is a function of it

| | 2026-08-12 run | this run (an INCOMPLETE local copy — see the Correction below) |
|---|---|---|
| store dates iterated | 85 | 111 |
| plannable candidates | 769 | 936 |
| candidates with a cached bar path | — | 493 |
| **tier touches** | **946** | **576** |
| bar-cache span | — | 2026-05-27 to 2026-08-31 |
| trigger | proportional | additive, with a proportional control |

The two populations OVERLAP but neither contains the other: this store spans 2026-04-14 to
2026-09-04 while its bar cache starts on 2026-05-27, so this run misses the original's early dates
and adds three weeks the original never saw.

**Superseded 2026-10-01:** this paragraph explains 576 against 946 as a different WINDOW. That is not
the explanation. The copy was an incomplete mirror of the same store, which yields 1334 touches — see
the Correction below. The window difference is real and is not what produced the gap.

Also carried, because it bounds how much the comparison can show: **164 of the 493 paths with bars
end before the entry window closes.** That truncation removes later bars, which is exactly where the
two forms diverge, and it removes variant B fills more readily than variant A ones. So the measured
delta is compressed toward zero rather than inflated by this.

### The per-arm drop breakdown this memo deferred

Item 3 of the open list asked for "the per-variant implausible-drop breakdown ... one-line script
change if the study is ever re-run". Splitting the arms supplied it:

| arm | implausible drops |
|---|---|
| B / additive (published) | 2 |
| B / proportional (control) | 10 |

The control arm drops five times as many rows. That is itself a small sign that the superseded
trigger produces more implausible fills, and it is one more reason the control is not a policy.

## Correction 2026-10-01 — the open question was my artifact; the day-1 magnitude does not reproduce

**It was my error.** The 2026-09-30 run was made against a local copy of the population-ladder store,
not against the store. That copy yields 576 tier touches where the same script finds 1334 in the
store. From that fragment I published two statements — that `d` = 1.0% does not beat variant A, and
that the edge crosses zero well below the `d` approximately 2% this study published — and opened
#1652 to settle them. Both were artifacts of the missing data.

**The failure mode was already written down.** A 2026-09-24 note records that a local `~/.alphalens`
mirror is a DIFFERENT store after a rebuild and that a fresh mtime does not say otherwise. #1652's own
body cites that note while recommending the VPS run. So this was not a missing fact; it was publishing
before taking the step the note names.

**What was run.** Read 2026-10-01, 09:50-11:20 UTC, from the VPS checkout of `main` at `04877f02`:

- **run 1, the full store**: `./.venv/bin/python apps/alphalens-research/scripts/whatif_trailing_entry.py
  --out /tmp/whatif_full.parquet`, log `/tmp/whatif_full.log`. 134 dates, 1138 candidates
  (`not_plannable: 4`), 1334 tier touches, ALL-cohort universe 1331.
- **run 2, the published DATES but NOT the published data conditions**: the same command with
  `--store /tmp/ladders_pre_20260812`, log `/tmp/whatif_cut.log`. That view is the same store seen
  through symlinks — every `<date>.parquet` with `date < 2026-08-12`, plus `bars/`, `grouped/` and
  `corporate_actions_cache.json` linked whole, so forward bars are COMPLETE. It iterates 85 dates and
  769 candidates, the Method line's own counts, and finds 932 tier touches against the published 946.
  The original run had 197 entry windows cut short by its data horizon; run 2 reports
  `entry_window_truncated: 0`. That difference is candidate number one for the day-1 shortfall below,
  so run 2 is not an apples-to-apples panel.

Both logs are on the VPS and are the only place these figures can be re-read. **Every delta below is
the difference of the three-decimal levels printed in these tables**, so it reproduces from the table
and can differ from the unrounded delta by 0.001.

### The cohorts, measured not derived

Policy view (`meanR_policy_fix`), slippage `none`. The adverse-slippage arm was NOT restated.

| panel | cohort | N | A | d=0.5% | d=1.0% | d=1.5% | d=2.0% | d=3.0% |
|---|---|---|---|---|---|---|---|---|
| published | ALL | 946 | 0.215 | **0.233** | 0.225 | (in log) | (in log) | 0.198 |
| run 2 | ALL | 932 | 0.192 | **0.206** | 0.200 | 0.201 | 0.193 | 0.186 |
| run 1 | ALL | 1331 | 0.117 | **0.128** | 0.122 | 0.126 | 0.118 | 0.117 |
| the 2026-09-30 fragment | ALL | 576 | 0.171 | 0.177 | 0.169 | 0.171 | 0.161 | 0.156 |
| published | day-1 | 357 | 0.177 | **0.202** | 0.194 | (in log) | (in log) | 0.140 |
| run 2 | day-1 | 356 | 0.156 | **0.163** | 0.162 | 0.160 | 0.151 | 0.137 |
| run 1 | day-1 | 537 | 0.085 | **0.097** | 0.093 | 0.095 | 0.085 | 0.078 |
| published | day-2+ | 589 | 0.238 | **0.251** | 0.244 | (in log) | (in log) | 0.233 |
| run 2 | day-2+ | 576 | 0.214 | **0.232** | 0.224 | 0.227 | 0.219 | 0.215 |
| run 1 | day-2+ | 794 | 0.139 | **0.149** | 0.141 | 0.146 | 0.140 | 0.143 |

As deltas at `d` = 0.5%: ALL published +0.018, run 2 +0.014, run 1 +0.011. day-1 published +0.025,
run 2 **+0.007**, run 1 +0.012. day-2+ published +0.013, run 2 **+0.018**, run 1 +0.010.

### What reproduces

- **The sign, the ordering and the grid maximum.** `d` = 0.5% is the maximum of the grid on the ALL,
  day-1, day-2+, E1 and E2 cohorts of both panels. It is NOT the maximum on E3, where `d` = 1.5% wins
  on both.
- **The ALL-cohort gain, in sign and ordering, at 78% (run 2) and 61% (run 1) of the published
  magnitude.** That is a 20-40% shortfall, not an exact reproduction.
- **"day-1 cohort flips hardest"**, the parenthetical in the Verdict, which is about `d` = 3%. day-1
  sits below its own A by -0.019 (run 2) and -0.007 (run 1), against ALL's -0.006 and 0.000 and
  day-2+'s +0.001 and +0.004. day-1 is the only cohort that goes clearly negative. Only the published
  magnitude (-0.037) is larger than either re-run finds.

### What does not reproduce: the day-1 MAGNITUDE

At `d` = 0.5% the published day-1 gain is +0.025 against +0.007 (run 2) and +0.012 (run 1), so between
two and four times smaller. At `d` = 1% it is +0.017 against +0.006 and +0.008.

**The cohort is the same SIZE.** Run 2 finds 356 day-1 touches against the published 357 on the same
85 dates, so the shrinkage is not dilution by a larger cohort. **Whether it is the same ROWS was not
checked.** The store has been rebuilt three times since, which can replace members one for one; a join
on `(date, ticker, tier)` against the published records parquet would settle it and was not run.

#### Why it shrank is UNRESOLVED

Ruled out by inspection, which is weaker than measurement:

- **a changed cohort PREDICATE.** `git log` shows two commits on the script since 2026-08-01,
  `7727dee1` (#1037) and `cc512e34` (the trigger fix), and the `day1` predicate is untouched. This
  says nothing about whether the rows the predicate selects changed.

Not ruled out, not measured, and not to be quoted as the explanation:

- **the trigger correction this addendum measures, POOLED.** Its within-run control reads `dR` mean
  and median of -0.0000 at `d` = 0.5% on run 2's panel. But that control is computed ACROSS ALL
  COHORTS, as the table's own note above says, and it was not cut to day-1. The error term is
  `(reference - trough) x d`, so it is largest where post-touch drawdowns are deepest, and bullet 3
  below says that is the day-1 cohort. A pooled mean over 932 touches cannot bound a subgroup effect
  in 356 of them.
- **forward bars are now complete.** Run 2 reports `entry_window_truncated: 0`; the original run's
  horizon cut 197 entry windows short. A truncated window removes later bars, and a day-1 touch has
  far less room inside the horizon than a day-2+ touch, so truncation and the day-1 cohort can
  interact. Capping run 2's forward bars at the original horizon would test this with the same tooling
  and needs no store snapshot. It was not run.
- **the ladder store has been rebuilt.** Three archived directories sit beside it on the VPS:
  `population_ladders.pre1416`, `population_ladders.pre1442-20260913-164940` and
  `population_ladders.pre1444-20260914-115836`.

#### What else in this addendum was measured on the fragment

Every row of the delta table above, its paired counts, its fill counts, its `ex-worst` column, the
"fill counts are IDENTICAL" conclusion and the 2-against-10 implausible-drop split all come from the
same incomplete copy (576 of 1334 touches). Exactly ONE cell was re-measured on run 2: `dR` at
`d` = 0.5%. The rest is neither withdrawn nor re-measured — including the "leaves the fill counts
identical" claim that the design memo's §6 note leans on.

#### A faithful reproduction of the STORE is impossible

All three archived snapshots begin 2026-05-19 and run into September. No snapshot of the store as it
stood on 2026-08-12 exists, so the published absolute levels cannot be re-derived from the store by
anyone. This bounds what the STORE can give back. It does not bound the truncation question above,
which is a flag on the same run.

### Three further published claims this reproduction contradicts

- **"The edge decays monotonically with `d`"** (Verdict). It does not: `d` = 1.5% sits above
  `d` = 1.0% on the ALL cohort of both panels, by +0.001 (run 2) and +0.004 (run 1), and on day-2+
  as well (0.227 against 0.224, and 0.146 against 0.141). Caveat 4 below
  already says the optimum inside the grid is not resolvable at this N, so read this as "the grid is
  FLAT between 1% and 1.5%" rather than as a measured reversal.
- **"grid support is {50,100} with 150 marginal"** (`entry_trailing_design_2026_08_12.md` §6 and the
  comment on `ENTRY_TRAIL_BPS_MAX`). On the ALL cohort of both panels 150 bps ranks ABOVE 100 bps,
  which argues for INCLUDING 150 rather than against the bound. It does not hold on run 2's day-1
  cohort, where 1.5% (+0.004) is below 1.0% (+0.006).
- **"the replay's edge is negative by d≈2%"** (same two places). On the ALL cohort `d` = 2.0% is
  +0.001 on BOTH panels — flat, not negative — and only `d` = 3% reaches zero or below. The claim
  holds for day-1 (run 2 -0.005 at 2%) and for E2, not for the grid.

### The region beyond the bound is MIXED, not uniformly bad

This is the sentence an earlier draft of this correction got wrong in my own favour. Deltas against
each cohort's own A, at the two settings the bound of 150 excludes:

| cohort | run 2: d=2% / 3% | run 1: d=2% / 3% |
|---|---|---|
| ALL | +0.001 / -0.006 | +0.001 / 0.000 |
| day-1 | **-0.005 / -0.019** | 0.000 / **-0.007** |
| day-2+ | +0.005 / +0.001 | +0.001 / +0.004 |
| E1 | -0.002 / -0.003 | 0.000 / +0.002 |
| E2 | -0.004 / **-0.018** | -0.006 / -0.008 |
| E3 | **+0.035** / +0.005 | **+0.032** / +0.009 |

So §6's "a bound must exclude the measured-bad region" describes a property of day-1 and E2, not of
the grid: ALL is flat there, E1 is flat, day-2+ is positive, and **E3 — the deepest tier — prefers a
WIDE trail on both panels** (+0.035 and +0.032 at `d` = 2%). The bound still points the right way,
because 50 bps is the maximum on ALL, day-1 and day-2+ and nothing beyond 150 improves them. Its
published justification is cohort-specific and should be read that way.

### Claims left UNRESOLVED rather than settled

- **"day-1 touches benefit MOST"** at `d` = 0.5%, read against day-2+ rather than against ALL, which
  contains day-1. Run 2 contradicts it: +0.007 against day-2+'s +0.018. Run 1 supports it by a hair:
  +0.012 against +0.010. One panel each way. The real shape of the change is a reallocation between
  the two day cohorts, which neither panel settles.
- **"beats limit-at-touch in EVERY cohort"**. ALL, day-1 and day-2+ are restated here; E1, E2 and E3
  are not. Read the breadth as unverified rather than reconfirmed.
- **"under BOTH slippage assumptions"**. Both re-runs report the slippage-`none` view only.
- **"it enters CHEAPER, not dearer"** — the negative-concession finding. Neither run restated it.

### What does not change

`ENTRY_TRAIL_BPS_MAX = 150` and the first LIVE value of 50 both stand. The margin behind the bound is
thinner than published and that is worth stating in numbers rather than in the word "clearly": day-1
at `d` = 3% sits below A by -0.019 on run 2 and -0.007 on run 1, against the published -0.037.

The payoff estimate in Caveat 4 below reads about +0.02R per entry. On these re-runs it is +0.014R on
the published dates and +0.011R on the full store — about 30% smaller and about 45% smaller
respectively, not "about a quarter". The tick slack it buys shrinks with it.

Three divergences from the live order remain unmodelled and are named in
`alphalens_research/diagnostics/trailing_entry_trigger.py`: the server's coarse ratchet of a tenth of
the distance, the bid-versus-trade-price reference, and the day-1 gap gate production applies and this
study does not. The trough also still seeds from the touch bar's low where the daily-bar replay seeds
from the reference — a median 13.2 bps apart on these touches, which is larger than the term the
trigger correction above measures.

## Verdict

**A tight trail (d = 0.5–1%) beats limit-at-touch in EVERY cohort, under BOTH slippage assumptions — and it enters CHEAPER, not dearer.** The edge decays monotonically with d and crosses to negative around d ≈ 2% (day-1 cohort flips hardest: d = 3% is clearly worse than A).

> **Reproduction 2026-10-01 — read this line with the Correction above.** On both re-runs the ALL-cohort
> gain reproduces and `d` = 0.5% is still the grid maximum. Three parts of this sentence do not hold:
> the decay is NOT monotone (`d` = 1.5% sits above `d` = 1.0% on both panels), the day-1 MAGNITUDE at
> `d` = 0.5% is two to four times smaller, and "under BOTH slippage assumptions" plus "enters CHEAPER"
> were not restated at all. **"flips hardest" REPRODUCES** on both panels. The word EVERY now rests on
> ALL, day-1 and day-2+; E1, E2 and E3 still rest on this run alone.

Policy view (missed tier = 0R, fixed risk denominator = A's risk unit — the view that cannot flatter B by re-denominating):

| Cohort | N | A (none/adv) | B d=0.5% (none/adv) | B d=1% (none/adv) | B d=3% (none/adv) |
|---|---|---|---|---|---|
| ALL | 946 | 0.215 / 0.209 | **0.233 / 0.230** | 0.225 / 0.223 | 0.198 / 0.196 |
| day-1 touch | 357 | 0.177 / 0.175 | **0.202 / 0.200** | 0.194 / 0.192 | 0.140 / 0.139 |
| day-2+ touch | 589 | 0.238 / 0.229 | **0.251 / 0.249** | 0.244 / 0.241 | 0.233 / 0.230 | *restated 2026-10-01* |
| E1 | 628 | 0.211 / 0.209 | **0.223 / 0.221** | 0.218 / 0.215 | 0.183 / 0.181 | *not restated 2026-10-01* |
| E2 | 243 | 0.153 / 0.135 | **0.174 / 0.172** | — | — | *not restated 2026-10-01* |
| E3 | 75 | (in log) | (in log) | — | — | *not restated; `d` = 1.5% is the grid max here* |

Full grids incl. d = 1.5/2%, medians, win rates, own-denominator view: rerun log `/tmp/whatif_rerun.log` on the VPS (regenerate any time from the records parquet).

## Why the trail wins (mechanics, from the data)

1. **The concession is NEGATIVE at small d** (ALL cohort: −0.3% average entry price vs the limit). After price touches the limit it usually keeps sliding; the trail follows the falling price down and triggers off a LOWER low — so "waiting for confirmation" gets paid instead of paying. The intuition "trailing always buys dearer" is wrong at small d on our paths.
2. **Fill-rate cost is negligible at small d**: 99.5% at 0.5%, 98.5% at 1% (vs 100% for A). By d = 3% it drops to 93.8% and the missed winners eat the edge. **Revised 2026-10-01:** on the re-runs the fill rate is 99.4% / 98.6% at d = 0.5% / 1% on the published 85 dates and 99.2% / 97.1% on the full store; at d = 3% it is 94.3% and **89.1%** against the published 93.8%, so the full-store cost at 3% is nearly double.
3. **Day-1 touches benefit MOST** (+0.025R at d = 0.5%, win rate 68.1% → 70.0%) — **the win-rate half REVERSES on both re-runs** (day-1 at d = 0.5% goes 0.646 → 0.635 on run 2 and 0.575 → 0.564 on run 1; the ALL cohort falls too), and the R half is **UNRESOLVED as of 2026-10-01:** the re-run on the same 85 dates puts day-1 at +0.007R against the ALL cohort's +0.014R, which contradicts this bullet, while the full store puts it at +0.012R against +0.011R, which supports it by a hair. One panel each way. The mechanism below was not measured by either run — consistent with the day-1 adverse-selection finding (`reference_day1_gap_gate_and_adverse_selection_2026_08_11`): day-1 dips are the most likely to be falling knives, so bounce confirmation filters exactly where filtering pays. Note the day-1 gap GATE only covers the open-below-E1 subclass; the trail helps the remaining day-1 touches too.
4. Effect size honesty: +0.018R mean on ALL is ~8% relative — real but modest; N = 946 tier-touches from 85 days, third-order cut of the same data. Direction, not calibration: {0.5%, 1%} beat A robustly, the exact optimum inside that range is not resolvable at this N.

## Caveats

- **Execution realism**: variant A assumed touch-fill (adverse variant demands trade-through — changed almost nothing: 938/946 touches traded through on the same bar); variant B assumed stop-buy at trigger +1 tick adverse. Real stop-buy slippage on thin small-caps can exceed 1 tick; the ~+0.02R edge could absorb ~2-3 extra ticks on a $3 stock before flipping, less on dearer names. **Revised 2026-10-01:** on the re-runs the edge is +0.014R / +0.011R, so the slack is nearer 1-2 ticks on that $3 stock.
- **60 "implausible" B rows dropped** (0.6%, mirror of the monitor's split guard) without a per-variant bias quantification — flagged by the verifier, direction unknown, small.
- 197 candidates had entry windows truncated by the data horizon; 4,937 tier-entries were still horizon-open and marked at last close — identical treatment for A and B, so comparisons stand, but absolute R levels are conservative. **2026-10-01:** "comparisons stand" is in doubt BETWEEN panels, not within this one — run 2 reports `entry_window_truncated: 0`, and the Correction above names a truncation-by-day-1 interaction as the leading unmeasured candidate for the day-1 shortfall.
- Implementing trailing ENTRIES live was previously REJECTED (INC-4) on execution-complexity grounds (bot-managed stop-buys trailed per tick, restart-safety, off-tick amend limits — see `trailing_execution_design_2026_08_07.md`). This result is evidence to REOPEN that decision with a concrete payoff estimate (~+0.02R/entry), not a green light to build. **Revised 2026-10-01:** the re-runs put that estimate at +0.014R per entry on the published dates and +0.011R on the full store, so the slack it buys against stop-buy slippage is about a quarter smaller.

## Addendum (same day): ATR-normalized trail distance + volatility-heterogeneity diagnostic

**2026-10-01:** the absolute levels quoted below are the 2026-08-12 run's and do not reproduce (see the
Correction above). The +0.002R comparison is unaffected in SIGN, but it is now about a sixth of the
grid's measured spread rather than a hundredth of the level.

Operator follow-up question: should d be volatility-normalized (d_i = k × ATR/price), and is there per-condition structure an ML model could learn? Extended replay (`/tmp/whatif_trailing_entry_atr.py` on the VPS; fixed-d rows reproduced digit-for-digit vs the base run, 11,292 records aligned; 0 touches excluded for bad ATR, 1 capped case at the 5% guard):

- **ATR normalization adds nothing**: best k (0.05; median effective d_i ≈ 0.25%) → ALL-cohort policy meanR 0.235/0.232 (none/adverse) vs fixed d=0.5% 0.233/0.230. A +0.002R non-difference, same near-tie in both day cohorts. Not worth the added atr dependency + cap guard.
- **No heterogeneity for ML to learn**: ATR/price terciles (~315 touches each; boundaries 4.33% / 6.02%) ALL monotonically prefer the tightest trail. Mid-vol earns more at EVERY setting (a level effect on outcomes, not a slope crossover in optimal d) — there is no low-vol-wants-tight / high-vol-wants-wide pattern, so a learned per-touch d has no conditional signal to exploit at this N.
- Caveat: k=0.05's median effective d (~0.25%) hints the optimum may sit tighter than 0.5%, but at these distances the replay's intra-bar sequencing assumptions start to dominate (+ multiplicity) — not pursued.

**ML verdict recorded for the ~2026-08-21/28 ML data-readiness re-run:** training a per-entry trail-distance model is premature at N≈946 correlated touches AND currently unmotivated — the counterfactual grid shows near-zero conditional structure. Revisit only if future data (N in the thousands) shows tercile slope crossovers.

## Follow-ups (not scheduled)

1. Reopen the trailing-entry execution design (V-variant selection) with this payoff estimate; weigh against the amend-rate limits and restart-safety cost.
2. If built: ship behind a per-env flag, validate on SIM with the same replay as the acceptance oracle.
3. The per-variant implausible-drop breakdown (verifier issue 3) — one-line script change if the study is ever re-run.
