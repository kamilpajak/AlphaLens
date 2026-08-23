# Analysis contract — does theme SELECTION cause the mega-cap skew?

Status: LOCKED
Date: 2026-08-23
Template: `docs/research/experiment_analysis_contract_2026_08_07.md`
Related: #1002 (75.8% of proposals are above the bracket), `docs/research/proposal_funnel_first_read_2026_08_21.md`

Committed BEFORE the collector exists and therefore before any observation. The
commit timestamp preceding the first data commit is the evidence. Nothing here
may be edited after the first read; a change of mind becomes a numbered
amendment appended at the bottom.

## What was known before writing this

* the mapper's proposals are 75.8% above the $10B ceiling (#1002, 524 proposals,
  15 days, exact 95% 71.8-79.6);
* theme dependence in that read was overwhelming — `box_office` 0/15 above the
  ceiling versus `pentagon` 28/28 — and three themes sit structurally outside
  the bracket;
* the selector takes the top 10 by novelty rank; on 2026-08-21 the eligible pool
  was 3099 themes, of which 505 had >=3 recent articles and 226 had >=5;
* the tie-break at the cut became a seeded draw on 2026-08-22 (#1086), and the
  rollup now records per-theme inclusion propensities;
* base rates from the same read: **0.93 useful names per theme-day**, and
  **47.9% of theme-days yield at least one**;
* nothing has ever been measured about themes the selector did NOT pick — the
  mapper was never asked about them.

No shadow observation exists. No comparison between selected and unselected
themes has been computed.

## 1. QUESTION

The mapper proposes mostly mega-caps. Two mechanisms could produce that, and
they call for opposite fixes:

* **selection** — the themes we pick simply contain no small companies;
* **within-theme behaviour** — even where small companies exist, the model
  reaches for familiar names.

This contract addresses the FIRST only. Does asking the mapper about themes the
selector did not pick produce a materially higher rate of in-bracket names?

The second mechanism needs a prompt-variant arm on the SAME themes and is out of
scope here.

## 2. UNIT

One **theme-day**: a (asof, theme) pair the mapper was asked about.

Not a proposal. A per-proposal rate weights a theme by how many names the model
happened to emit, which is a property of the model's verbosity, not of the
theme. The proposal count is reported as a secondary.

## 3. SAMPLING

Each production day, after `map_themes` has run:

* **Arm S (selected)** — the themes the pipeline actually mapped that day, read
  from the proposal funnel. No extra cost; these already exist.
* **Arm U (unselected)** — `SHADOW_THEMES_PER_DAY = 20` themes drawn at random,
  without replacement, from the eligible pool BELOW the cut, using a seed
  derived from the asof date so the draw is reproducible.

Eligibility for arm U is the selector's own threshold (`novelty_score >= 3`)
PLUS `count_recent >= 5`. The second condition is not cosmetic: the unrestricted
eligible pool is ~3099 themes, most of which are single-article noise the
selector would never reach under any ranking. Comparing against those would
answer a question nobody asked. 226 themes met both conditions on 2026-08-21.

**Stratified, and reported separately:**

* **U-near** — 10 themes from ranks 11-30, the band a small change to the
  selector could actually reach;
* **U-far** — 10 themes from rank 31 and below, within the eligible pool.

## 4. THE SHADOW ARM NEVER SHIPS

Arm U's proposals are written to their own store and are NEVER fed to the brief,
the scorer, the candidate parquet, or any card. This is a measurement, not a
change to what the tool recommends.

Enforced by test, not by intent: a test asserts the shadow writer's output path
is outside `~/.alphalens/thematic_candidates/` and that the daily pipeline's
brief stage cannot read it.

## 5. PRIMARY

**Proportion of theme-days yielding at least one IN-BRACKET proposal, arm U
minus arm S.**

In-bracket means the same `bracket_verdict == "in_bracket"` the production
funnel already stamps, computed the same way for both arms.

Interval: cluster bootstrap resampling **DAYS**, 10 000 draws, percentile 95%.
Days are the cluster because themes within a day share a news corpus, a market
session and a prompt version.

## 6. SECONDARY

Each with its own N, none able to overturn the primary:

* the same primary split U-near versus U-far;
* mean in-bracket proposals per theme-day;
* the above-ceiling rate per proposal, for comparability with #1002's 75.8%;
* proposals per theme-day, both arms (the verbosity term the unit choice avoids);
* the share of arm-U themes that produced no proposal at all, and why
  (no catalyst resolved / model declined / error).

## 7. POWER AND FLOORS

The #1002 base rate is 47.9% of theme-days yielding at least one useful name.
Detecting 48% -> 70% needs roughly 74 theme-days per arm at conventional power,
which arm U reaches in 4 days at 20/day and arm S in ~8 days at 10/day.

The binding floor is therefore NOT the theme-day count. It is the number of
DAYS, because the estimator resamples days:

* **>= 20 days** with observations in both arms, and
* **>= 74 theme-days** in each arm.

Below either, the verdict is INCONCLUSIVE and the numbers are published with
their N. This mirrors the bracket-cost contract's Amendment 3, whose lesson was
that a row-count floor cannot see the cluster count the estimator depends on.

## 8. FAILURE MODES ADMITTED IN ADVANCE

* **Rank is confounded with everything.** Arm U themes are lower-ranked BY
  CONSTRUCTION, and rank correlates with article volume, recency and probably
  sector. The contrast is "themes the selector did not pick" as a bundle, not
  "novelty rank" in isolation. No claim may separate them.
* **Not a policy evaluation.** A random draw from below the cut cannot tell us
  what a SPECIFIC alternative selector would produce; hitting one named policy
  by chance is a 1-in-226 event. It answers whether the unpicked population
  differs, which is the precondition for any selector change being worth making.
* **Same prompt, same day, same corpus** for both arms — this is the design's
  main protection, and it does not neutralise DIFFERENTIAL exposure: selected
  themes are more newsworthy, so they may be better represented in whatever the
  model absorbed. Measured on our own corpus at 1.25x median forward coverage
  (2026-08-22), which vanishes after normalising by input volume.
* **Market caps are as-of the read**, not the proposal date. Both arms share the
  convention, so it biases the level more than the difference.
* **Arm S is observational.** It is what the pipeline did, not a randomised
  control.

## 9. CONTROLS

* **Positive control** — arm S's above-ceiling rate computed here must land near
  #1002's 75.8% (71.8-79.6). A large departure means this collector disagrees
  with the funnel and invalidates the run.
* **Attrition** — every drawn arm-U theme lands in exactly one of: proposals
  written / no catalyst / model declined / error. The four counts must sum to
  the number drawn.
* **Cost** — the per-day LLM spend is recorded on every read. The estimate before
  measuring is ~$0.15/day at 20 themes; if the realised figure exceeds $1/day
  the collector stops and the contract is amended rather than quietly overrun.

## 10. STOPPING

Read weekly, each read stating its own N. Collection stops when the floors in §7
are met and the primary is read, or when a pre-committed watch condition fires:
if the daily proposal rate in arm S departs from its 51.5/day post-2026-08-18
level by more than half, the population has changed and the pooled read is
suspended pending an amendment.

## 11. VERDICTS, FIXED NOW

* **SELECTION IS A LEVER** — arm U's in-bracket theme-day rate exceeds arm S's
  and the 95% interval on the difference excludes zero.
* **SELECTION IS NOT THE LEVER BY THIS DATA** — the interval includes zero, or
  arm S is higher. This does not prove selection is irrelevant; it says we have
  no evidence for it, which is a weaker statement and the report must use those
  words.
* **INCONCLUSIVE** — either floor in §7 unmet.

## 12. FORBIDDEN

The report may not use "defect", "bug", or "broken" about the selector or the
mapper. It may not claim causation. It may not present a secondary as the answer
when the primary is INCONCLUSIVE. It may not report the U-near/U-far split as
the primary if the pooled result is inconclusive and the split is not.

## 13. ARCHIVE

Reads: `docs/research/theme_shadow_arm_read_<DATE>.md`, one per read.
Collector: `alphalens thematic shadow-map`.
Store: `~/.alphalens/theme_shadow/<YYYY-MM-DD>.parquet` — outside
`thematic_candidates/`, per §4.

---

## Amendment 1 — §3's pool size was wrong, and the real one is small

Written 2026-08-23, after a four-theme live smoke of the collector and BEFORE
any comparison between the arms exists.

**The error.** §3 states "226 themes met both conditions on 2026-08-21". They
did not. 226 is the count of themes with `count_recent >= 5` **regardless of
novelty**; 3099 met the novelty threshold regardless of article count. I read
the two conditions separately and reported one as though it were the
intersection. Measured:

| date | novelty >= 3 | count_recent >= 5 | **both** |
|---|---:|---:|---:|
| 2026-08-19 | 3011 | 224 | **44** |
| 2026-08-20 | 3060 | 229 | **56** |
| 2026-08-21 | 3099 | 226 | **56** |

**What it changes.** The eligible pool is roughly 50, not 226. After removing
the ~10 the selector took, arm U draws 20 from about 40. Two consequences the
original text did not admit:

* **themes recur heavily across days.** The unit is the theme-DAY and a theme
  carries different news on a different day, so recurrence is not invalid — but
  the day-cluster bootstrap in §5 absorbs day-level dependence only. It does
  nothing about the same theme appearing on many days.
* **the far band is thin.** With ~50 eligible, ranks 31+ hold roughly 20
  themes, so `far` will often draw most of what exists rather than sampling it.

**What it does NOT change.** The eligibility rule stays as written. Loosening it
to `count_recent >= 3` would widen the pool to 169, but arm S is drawn by the
production selector from the SAME narrow set, and widening one arm only would
make the arms less comparable — which is the one property this design has.

**Added reporting obligation.** Every read states the **distinct-theme count**
beside the theme-day count for each arm, and the mean number of days each theme
appears. A theme-day total that comes from twelve themes is a different sample
from one that comes from forty, and the reader must be able to see which.
