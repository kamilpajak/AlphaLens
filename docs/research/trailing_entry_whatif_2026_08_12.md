# Trailing (bounce-confirmed) entries vs hardcoded limit entries — what-if replay

**Date:** 2026-08-12
**Status:** COMPLETE — direction-level diagnostic (a re-cut of already-used data, NOT a pre-registered strategy test; no Bonferroni claim is made). **Read the 2026-09-30 correction below before quoting any number: the trigger this study used is not the one the broker runs.**
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

### Correction 2026-10-01 — the open question this addendum raised does not exist

**It was my error.** The 2026-09-30 run was made against a local copy of the population-ladder store,
not against the store. That copy holds 576 of the 1334 tier touches the same script finds in the real
store. From that fragment I published two statements — that `d` = 1.0% does not beat variant A, and
that the edge crosses zero well below the `d` approximately 2% this study published — and opened
#1652 to settle them. Both were artifacts of the missing data. The store was reachable the whole
time and the run takes minutes.

**What was run.** `whatif_trailing_entry.py` with the corrected trigger, on the VPS against
`~/.alphalens/population_ladders`, twice:

- **run 1, the full store**: 134 dates, 1138 plannable candidates, 1334 tier touches (ALL-cohort
  universe 1331).
- **run 2, the published date set**: the same store restricted to the 85 ladder dates before
  2026-08-12, through a directory view that links `bars/` and `grouped/` so forward bars are
  complete. It iterates 85 dates and 769 plannable candidates, the same counts as the Method line at
  the top of this study, and finds 932 tier touches against the published 946.

#### ALL cohort: direction and magnitude both reproduce

Policy view (`meanR_policy_fix`), slippage `none`:

| panel | dates | ALL N | A | d=0.5% | d=1.0% | d=1.5% | d=2.0% | d=3.0% |
|---|---|---|---|---|---|---|---|---|
| published 2026-08-12 | 85 | 946 | 0.215 | **0.233** | 0.225 | (in log) | (in log) | 0.198 |
| run 2 (the published 85 dates) | 85 | 932 | 0.192 | **0.206** | 0.200 | 0.201 | 0.193 | 0.186 |
| run 1 (full store) | 134 | 1331 | 0.117 | **0.128** | 0.122 | 0.126 | 0.118 | 0.117 |
| the 2026-09-30 local copy | 111 | 576 | 0.171 | 0.177 | 0.169 | 0.171 | 0.161 | 0.156 |

Against each panel's own A: published +0.018 at `d` = 0.5% and +0.010 at `d` = 1%; run 2 gives
+0.014 and +0.008; run 1 gives +0.011 and +0.005. The local copy gives +0.006 and **-0.002**, and
that last figure is the artifact the correction above is about.

`d` = 0.5% is the maximum of the grid on both real panels, and the ALL-cohort magnitude reproduces at
both settings this study published.

#### day-1 cohort: the direction reproduces, the magnitude does not

| panel | day-1 N | A | d=0.5% | d=1.0% | d=1.5% | d=3.0% |
|---|---|---|---|---|---|---|
| published 2026-08-12 | 357 | 0.177 | **0.202** | 0.194 | (in log) | 0.140 |
| run 2 (the published 85 dates) | 356 | 0.156 | **0.163** | 0.162 | 0.160 | 0.137 |
| run 1 (full store) | 537 | 0.085 | **0.097** | 0.093 | 0.095 | 0.078 |

At `d` = 0.5%: published +0.025, run 2 **+0.007**, run 1 +0.012. At `d` = 1%: published +0.017,
run 2 +0.006, run 1 +0.008. The published day-1 gain is between two and four times what either
re-run finds.

**The cohort did not change size.** Run 2 finds 356 day-1 touches against the published 357 on the
same 85 dates. So this is not dilution by newly reachable touches — it is nearly the same rows with a
different mean.

#### Why the day-1 gain shrank is UNRESOLVED

Ruled out by measurement:

- **the trigger correction this addendum measures.** On run 2's panel its within-run control reads
  `dR` mean -0.0000 and `dR` median -0.0000 at `d` = 0.5%, the same answer the local copy gave.
- **a changed cohort definition.** `git log` shows two commits on the script since 2026-08-01,
  `7727dee1` (#1037, the original study) and `cc512e34` (the trigger fix); the `day1` predicate is
  untouched, and the cohort size is 356 against 357.

Not ruled out, not measured, and not to be quoted as the explanation:

- **forward bars are now complete.** Run 2 reports `entry_window_truncated: 0`. The original run had
  a data horizon one day past its newest ladder date, and the Caveats below record 197 candidates
  whose entry windows that horizon cut short. A truncated window removes later bars, and a day-1
  touch has far less room inside the horizon than a day-2+ touch, so truncation and the day-1 cohort
  can interact. Nobody has checked whether they do.
- **the ladder store has been rebuilt.** Three archived directories sit beside it on the VPS
  (`population_ladders.pre1416`, `.pre1442-20260913`, `.pre1444-20260914`). That is consistent with
  932 ALL touches against 946, and says nothing about which rows moved.

#### A faithful reproduction is impossible

All three archived snapshots begin 2026-05-19 and run into September. No snapshot of the store as it
stood on 2026-08-12 exists, so the published absolute levels cannot be re-derived from the store by
anyone. The Verdict table records one run. What a later run can check is the deltas, and only the
ALL-cohort delta checks out well.

#### Three further claims this reproduction contradicts

- **"The edge decays monotonically with `d`"** (Verdict). It does not, on either real panel: run 2
  reads 0.206, 0.200, 0.201, 0.193, 0.186 and run 1 reads 0.128, 0.122, 0.126, 0.118, 0.117, so
  `d` = 1.5% sits above `d` = 1.0% in both.
- **"grid support is {50,100} with 150 marginal"**
  (`entry_trailing_design_2026_08_12.md` §6). Both panels rank 150 bps ABOVE 100 bps. Nothing in
  production moves, because 50 is still the maximum, but 150 is not the marginal member of that set.
- **"<=1.5% fill-rate cost"** (same memo, Evidence base). It holds on the published 85 dates and not
  on the full store. Fill rates, with A at 1.000 on every panel:

| panel | d=0.5% | d=1.0% | d=1.5% | d=2.0% | d=3.0% |
|---|---|---|---|---|---|
| run 2 (the published 85 dates) | 0.994 | 0.986 | 0.982 | 0.976 | 0.943 |
| run 1 (full store) | 0.992 | 0.971 | 0.953 | 0.944 | 0.891 |

At `d` = 1% the cost is 1.4% on the published dates and 2.9% on the full store.

#### Two claims that stay UNRESOLVED rather than settled

- **"day-1 cohort flips hardest" / day-1 benefits most.** Run 2 contradicts it: day-1 gains +0.007
  against the ALL cohort's +0.014. Run 1 supports it by a hair: +0.012 against +0.011. One panel each
  way, so unresolved.
- **"beats limit-at-touch in EVERY cohort".** Only ALL and day-1 are restated here. The day-2+, E1,
  E2 and E3 rows are not, so read the breadth as unverified rather than reconfirmed.

#### What does not change

`ENTRY_TRAIL_BPS_MAX = 150` and the first LIVE value of 50 both stand. `d` = 0.5% is the maximum of
the grid on both real panels, and the region the bound excludes is never better than baseline on
either. The margin behind the bound is thinner than published, and that is worth stating in numbers
rather than in the word "clearly": day-1 at `d` = 3% sits below A by -0.019 on run 2 and -0.007 on
run 1, against the published -0.037.

The payoff estimate in the Caveats below reads about +0.02R per entry. On these re-runs it is
+0.014R on the published dates and +0.011R on the full store, so the tick slack that estimate buys is
about a quarter smaller and the stop-buy slippage caveat gets stronger, not weaker.

Three divergences from the live order remain unmodelled and are named in
`alphalens_research/diagnostics/trailing_entry_trigger.py`: the server's coarse ratchet of a tenth of
the distance, the bid-versus-trade-price reference, and the day-1 gap gate production applies and this
study does not. The trough also still seeds from the touch bar's low where the daily-bar replay seeds
from the reference — a median 13.2 bps apart on these touches, which is larger than the term this
addendum measures.

## Verdict

**A tight trail (d = 0.5–1%) beats limit-at-touch in EVERY cohort, under BOTH slippage assumptions — and it enters CHEAPER, not dearer.** The edge decays monotonically with d and crosses to negative around d ≈ 2% (day-1 cohort flips hardest: d = 3% is clearly worse than A).

> **Reproduction 2026-10-01 — read this line with the Correction above.** On both re-runs the ALL-cohort
> gain reproduces and `d` = 0.5% is still the grid maximum. Three parts of this sentence do not hold:
> the decay is NOT monotone (`d` = 1.5% sits above `d` = 1.0% on both panels), the day-1 MAGNITUDE is
> two to four times smaller, and "flips hardest" is contradicted on one panel and supported on the
> other. The word EVERY still rests on this run alone — only ALL and day-1 were restated.

Policy view (missed tier = 0R, fixed risk denominator = A's risk unit — the view that cannot flatter B by re-denominating):

| Cohort | N | A (none/adv) | B d=0.5% (none/adv) | B d=1% (none/adv) | B d=3% (none/adv) |
|---|---|---|---|---|---|
| ALL | 946 | 0.215 / 0.209 | **0.233 / 0.230** | 0.225 / 0.223 | 0.198 / 0.196 |
| day-1 touch | 357 | 0.177 / 0.175 | **0.202 / 0.200** | 0.194 / 0.192 | 0.140 / 0.139 |
| day-2+ touch | 589 | 0.238 / 0.229 | **0.251 / 0.249** | 0.244 / 0.241 | 0.233 / 0.230 |
| E1 | 628 | 0.211 / 0.209 | **0.223 / 0.221** | 0.218 / 0.215 | 0.183 / 0.181 |
| E2 | 243 | 0.153 / 0.135 | **0.174 / 0.172** | — | — |
| E3 | 75 | (in log) | (in log) | — | — |

Full grids incl. d = 1.5/2%, medians, win rates, own-denominator view: rerun log `/tmp/whatif_rerun.log` on the VPS (regenerate any time from the records parquet).

## Why the trail wins (mechanics, from the data)

1. **The concession is NEGATIVE at small d** (ALL cohort: −0.3% average entry price vs the limit). After price touches the limit it usually keeps sliding; the trail follows the falling price down and triggers off a LOWER low — so "waiting for confirmation" gets paid instead of paying. The intuition "trailing always buys dearer" is wrong at small d on our paths.
2. **Fill-rate cost is negligible at small d**: 99.5% at 0.5%, 98.5% at 1% (vs 100% for A). By d = 3% it drops to 93.8% and the missed winners eat the edge.
3. **Day-1 touches benefit MOST** (+0.025R at d = 0.5%, win rate 68.1% → 70.0%) — **UNRESOLVED as of 2026-10-01:** the re-run on the same 85 dates puts day-1 at +0.007R against the ALL cohort's +0.014R, which contradicts this bullet, while the full store puts it at +0.012R against +0.011R, which supports it by a hair. One panel each way. The mechanism below was not measured by either run — consistent with the day-1 adverse-selection finding (`reference_day1_gap_gate_and_adverse_selection_2026_08_11`): day-1 dips are the most likely to be falling knives, so bounce confirmation filters exactly where filtering pays. Note the day-1 gap GATE only covers the open-below-E1 subclass; the trail helps the remaining day-1 touches too.
4. Effect size honesty: +0.018R mean on ALL is ~8% relative — real but modest; N = 946 tier-touches from 85 days, third-order cut of the same data. Direction, not calibration: {0.5%, 1%} beat A robustly, the exact optimum inside that range is not resolvable at this N.

## Caveats

- **Execution realism**: variant A assumed touch-fill (adverse variant demands trade-through — changed almost nothing: 938/946 touches traded through on the same bar); variant B assumed stop-buy at trigger +1 tick adverse. Real stop-buy slippage on thin small-caps can exceed 1 tick; the ~+0.02R edge could absorb ~2-3 extra ticks on a $3 stock before flipping, less on dearer names. **Revised 2026-10-01:** on the re-runs the edge is +0.014R / +0.011R, so the slack is nearer 1-2 ticks on that $3 stock.
- **60 "implausible" B rows dropped** (0.6%, mirror of the monitor's split guard) without a per-variant bias quantification — flagged by the verifier, direction unknown, small.
- 197 candidates had entry windows truncated by the data horizon; 4,937 tier-entries were still horizon-open and marked at last close — identical treatment for A and B, so comparisons stand, but absolute R levels are conservative.
- Implementing trailing ENTRIES live was previously REJECTED (INC-4) on execution-complexity grounds (bot-managed stop-buys trailed per tick, restart-safety, off-tick amend limits — see `trailing_execution_design_2026_08_07.md`). This result is evidence to REOPEN that decision with a concrete payoff estimate (~+0.02R/entry), not a green light to build. **Revised 2026-10-01:** the re-runs put that estimate at +0.014R per entry on the published dates and +0.011R on the full store, so the slack it buys against stop-buy slippage is about a quarter smaller.

## Addendum (same day): ATR-normalized trail distance + volatility-heterogeneity diagnostic

Operator follow-up question: should d be volatility-normalized (d_i = k × ATR/price), and is there per-condition structure an ML model could learn? Extended replay (`/tmp/whatif_trailing_entry_atr.py` on the VPS; fixed-d rows reproduced digit-for-digit vs the base run, 11,292 records aligned; 0 touches excluded for bad ATR, 1 capped case at the 5% guard):

- **ATR normalization adds nothing**: best k (0.05; median effective d_i ≈ 0.25%) → ALL-cohort policy meanR 0.235/0.232 (none/adverse) vs fixed d=0.5% 0.233/0.230. A +0.002R non-difference, same near-tie in both day cohorts. Not worth the added atr dependency + cap guard.
- **No heterogeneity for ML to learn**: ATR/price terciles (~315 touches each; boundaries 4.33% / 6.02%) ALL monotonically prefer the tightest trail. Mid-vol earns more at EVERY setting (a level effect on outcomes, not a slope crossover in optimal d) — there is no low-vol-wants-tight / high-vol-wants-wide pattern, so a learned per-touch d has no conditional signal to exploit at this N.
- Caveat: k=0.05's median effective d (~0.25%) hints the optimum may sit tighter than 0.5%, but at these distances the replay's intra-bar sequencing assumptions start to dominate (+ multiplicity) — not pursued.

**ML verdict recorded for the ~2026-08-21/28 ML data-readiness re-run:** training a per-entry trail-distance model is premature at N≈946 correlated touches AND currently unmotivated — the counterfactual grid shows near-zero conditional structure. Revisit only if future data (N in the thousands) shows tercile slope crossovers.

## Follow-ups (not scheduled)

1. Reopen the trailing-entry execution design (V-variant selection) with this payoff estimate; weigh against the amend-rate limits and restart-safety cost.
2. If built: ship behind a per-env flag, validate on SIM with the same replay as the acceptance oracle.
3. The per-variant implausible-drop breakdown (verifier issue 3) — one-line script change if the study is ever re-run.
