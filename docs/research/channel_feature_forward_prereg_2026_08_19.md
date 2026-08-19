# Channel-as-feature — forward experiment pre-registration

**Status:** LOCKED
**Date:** 2026-08-19
**Author:** Kamil Pajak
**Branch:** `feature/channel-as-feature`
**Design:** `docs/research/channel_as_feature_design_2026_08_19.md`
**Motivating study:** `docs/research/stage1_retro_gate_increment_results_2026_08_19.md` (PR #1065)
**Evidence tier:** forward, confirmatory-eligible — the cohort is generated **after** this
memo is locked, and no outcome in it exists yet at lock time.
**Supersedes:** the ISO week 40-42 verdict window (2026-09-28..10-18) defined in
`theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md` §13.4 item 3 — see §9.

---

## 1. Question

Stage 1 asserted that a candidate with a nameable transmission channel from the event to
the company is a better candidate. The retrospective replay of the frozen gate found the
opposite sign (pair-cluster Δ = **−0.0715**, one-sided p = **0.945**, CI95
**[−0.159, +0.017]**, 38 vs 49 pairs) and spent the only slot that question had. The
channel judgment is therefore being demoted from a **filter** to a **scored feature**
(design memo §2), and the open question becomes measurable for the first time:

> Among candidates the pipeline actually ships, does the assessed `channel_status` predict
> matured `market_excess_return`?

This is answerable forward **only because** the demotion keeps the would-be-refused
candidates in the data. Under the hard gate they left no candidate row, no brief row and no
ladder row, so no refused leg existed to measure (§9).

## 2. Cohort boundary

**The cohort opens at the first pipeline run executed on the new
`mapper_config_version`** — i.e. the first `alphalens thematic map-themes` run on the VPS
after the image carrying `mapper-freeze-v3` + `channel_config_version` goes live.
**Deploy time, not merge time.**

The boundary is identified in the data, not by hand: a row belongs to the cohort iff its
`mapper_config_version` payload carries `"schema": "mapper-freeze-v3"` **and** the
`"channel"` key equal to the deployed `channel_config_version`. The exact token strings and
the first cohort `asof` date are appended to this memo as an amendment on deploy day,
before any post-boundary row matures.

Rows written under `mapper-freeze-v2` or earlier are a different treatment and are **never
pooled** with the cohort. Existing rows are never restamped (ADR 0013 R3).

## 3. Frozen instrument identity

The instrument for this experiment is the pair of prompts plus the aggregation rule, pinned
by three tokens that must all be constant across the accrual window:

| Token | Pins |
|---|---|
| `mapper_config_version` (`mapper-freeze-v3`) | stage-A proposal prompt, response schema, sampling constants, mcap range, and (nested, key `"channel"`) the stage-B token |
| `channel_config_version` (`channel-assess-v1`) | stage-B prompt sha, response-schema sha, temperature 0.0, max output tokens, `votes = 3`, the status and type vocabularies, render caps |
| `shadow_strict_rule_version` (`shadow-strict-any-verified-v1`) | the theme-level shadow rule only; **not** a freeze input, because it is re-derivable offline |

Any change to either prompt, to `votes`, or to the status/type vocabulary **ends the
accrual window** and starts a new cohort. A mid-window change is an amendment that must be
committed before the change deploys, and the accrual counter restarts; partially accrued
legs are reported descriptively and are not pooled across the token change.

Aggregation per candidate: k = 3 independent draws at temperature 0.0 under the pinned
OpenRouter provider, ordinal median over `unverified=0 / partial=1 / verified=2`,
`channel_vote_dispersion = max − min` over valid draws. Draws that fail or come back
off-vocabulary are excluded from the median; a candidate with zero valid draws is recorded
`channel_status="unverified", channel_assessment_outcome="call_failed"` and is **excluded
from both legs of the primary test** (§4), pre-committed here.

## 4. Primary hypothesis — one test, one slot

**H1 (TWO-SIDED):** among post-boundary candidates with a matured outcome, the mean
`market_excess_return` of `channel_status == "verified"` candidates differs from that of
`channel_status == "unverified"` candidates.

Two-sided is deliberate. The retro point estimate runs the **other** way (kept themes
underperformed refused ones by 7.15 pp), so pre-committing to a sign would either re-run a
disproven one-sided claim or launder the retro's inverted estimate into a new directional
hypothesis. Neither is honest. α = 0.05, two-sided, single look.

### 4.1 Legs and unit of inference

* **Leg V:** candidates with `channel_status == "verified"`.
* **Leg U:** candidates with `channel_status == "unverified"`.
* **`partial` is EXCLUDED from the primary** and reported descriptively (§5). A
  pre-specified **sensitivity** re-runs the identical estimator with `partial` pooled into
  leg V; it is reported alongside the primary and consumes **no additional slot** — it can
  qualify the reading, it can never replace the verdict.
* **`not_assessed` rows (off-bracket) never enter either leg** — they have no outcome by
  construction.
* **Instrument-failure rows** (`channel_assessment_outcome != "success"`) are excluded from
  both legs and counted in the attrition table.

**Unit of inference = the (theme, source_event) pair × leg cell**, matching the retro's
unit. Within one pair, the matured candidates of each leg are averaged into a single cell
mean; a pair may contribute a cell to both legs (this is normal and is precisely what the
per-candidate assessment makes possible). Errors are clustered **two ways: by pair and by
brief day**. Row-level p-values never appear in the decision.

### 4.2 Outcome and estimator

* **Outcome:** stored `market_excess_return` from the population-ladder store
  (`~/.alphalens/population_ladders/<brief_date>.parquet`), matured (`terminal`) rows only,
  joined 1:1 on `(brief_date, ticker)`. Rows that never matured for structural reasons
  (`plannable=False` / `NO_STRUCTURE`) never enter the population, exactly as in the retro;
  open positions are excluded and counted.
* **Winsorization:** 1% per leg, each leg winsorized separately; the unwinsorized contrast
  is reported as a sensitivity.
* **Estimator:** Δ = mean(cells in leg V) − mean(cells in leg U); inference by **two-way
  (pair × brief-day) cluster bootstrap**, 10,000 resamples, seed fixed and recorded in the
  results memo; report the point estimate, the two-sided bootstrap p, the percentile 95%
  CI, both leg means with sds, and both cluster counts.
* **Machinery:** reuse `apps/alphalens-research/scripts/stage1_retro_outcome_inference.py`
  (`winsorize`, `pair_cluster_delta`, `two_way_cluster_bootstrap`, `power_one_sided`,
  `required_n_per_leg`), whose pure-stats functions are pinned by
  `apps/alphalens-research/tests/test_stage1_retro_outcome_inference.py`. The two-sided
  p-value is taken from the same bootstrap distribution; any new helper ships with its own
  `unittest.TestCase` before the look.
* **Two-stage commit protocol** (as in the retro): the label/cohort extract is written and
  its sha256 committed **before** the outcome join runs, and the analysis script refuses to
  run against a mismatching hash.

### 4.3 Ledger entry

* Entry: `channel_status_forward_verified_minus_unverified_2026_08_19`.
* Family: the program-lifetime EDGE hypothesis budget,
  `docs/research/edge_hypothesis_budget_2026_07.md`. `channel_*` is a **new stampable
  signal**, so it takes a new §3 cluster row (**next free id = 23**) which raises the
  program denominator for everyone. The row is appended in the implementation PR, before
  any look; no look happens off-ledger.
* Looks budgeted: **1**. Unit: pair-cell, per rule 5 of that ledger. Primary horizon: the
  matured ladder endpoint (~42 sessions), per rule 6 — the multi-horizon reads in §5 are
  descriptive and are **not** separate tests.
* The slot is registered at this memo's commit, consumed when the outcome join runs, and
  returned only if the experiment halts with no label→outcome contact ever having occurred.
* A null, or an inverted, or an underpowered result is logged and **cannot** be re-tried
  with a re-cut window, a different status threshold, or a different horizon.

## 5. Secondary and descriptive — no slots, no verdict vocabulary

None of the following may carry a verdict word, and none may be used to promote
`channel_*` into selection or ordering:

1. **Multi-horizon excess** at 1 / 2 / 5 / 10 / 20 / 42 trading days, computed from the
   grouped-daily store minus SPY (same recipe as
   `apps/alphalens-research/scripts/ml/2026_07_signal_car10_continuous.py`). Motivation:
   the literature puts attention-driven drift at ~1-20 trading days (Da-Engelberg-Gao;
   Tetlock's ~20-day news-backed drift vs no-news reversal), so the ~42-session ladder
   endpoint may straddle a reversal. This is **shape description**; the primary horizon
   stays the matured endpoint and is not re-picked after seeing these.
2. **`channel_type` breakdown** — counts and cell means by the nine-value vocabulary.
   Interest concentrates on `category_attention` (an honest "attention only" answer) versus
   the mechanical types, but no per-type test is registered; n per type will be small.
3. **Crowd-out metric** — share of stage-A proposals that land in the 500M-10B bracket, and
   the distribution of proposed tickers by mcap decile, compared against the retro's 96.0%
   `KEPT_TICKER_ABSENT` and the mega-cap-headed kept-theme proposal sets (XOM/CVX/COP/
   GOOGL/PSX/NVDA/RTX). This is the acceptance readout for Move 1; it uses **proposal**
   data, not outcomes, so it can be read within days of deploy.
4. **Volume per day** — proposals/day, in-bracket/day, assessed/day, brief rows/day, and
   the `channel_status` mix; against the pre-Stage-1 baseline (~99 proposals/day,
   ~10 brief rows/day) and the Stage-1 baseline (~19/day, ~1-6/day).
5. **Instrument stability** — distribution of `channel_vote_dispersion` and of
   `channel_vote_valid_n`, i.e. the live counterpart of the retro's 91/238 mixed-vote
   finding. A dispersion distribution concentrated at 2 invalidates the primary read and is
   a HALT condition (§7), not a finding.
6. **Shadow verdict descriptives** — `shadow_strict_verdict` keep/refuse rates per day and
   the theme-decisions sidecar counts (declines by reason, no-catalyst skips), which are the
   first on-disk trace these have ever had.
7. **Status-mix audit** — a manual read of ~30 `partial` rows in the first two weeks
   checking `channel_evidence` against the source event, for fabricated chains of the
   AVAV/KTOS kind. Qualitative, no rate claimed.

## 6. Read-out timing

The read is **accrual-triggered, not calendar-triggered.** No look happens before the floor
in §6.2 is met.

### 6.1 Detectable effect vs required accrual

Planning sd = the retro's pooled pair-level sd **0.149**. Two-sample, two-sided, α = 0.05,
power 0.80, balanced legs: n per leg = 2·(z₀.₉₇₅ + z₀.₈₀)²·(sd/Δ)².

| True |Δ| | n per leg |
|---|---|
| 0.15 | 16 |
| 0.10 | 35 |
| **0.0715** (retro magnitude) | **69** |
| 0.05 | 140 |
| 0.0358 (half the retro magnitude) | 272 |

The retro's own one-sided figure was 54 per leg at the same magnitude; the two-sided design
costs the extra ~15 cells per leg, and that cost is accepted for the reason in §4.

**The planning sd is an assumption, not a measurement of this cohort.** It was measured on
theme-level pair cells under the old proposal mix. If the crowd-out repair works, the new
mix is smaller-cap and dispersion will likely be **higher**, which raises the required n.
The realised sd is reported in the results memo, and if it exceeds 0.149 the floor is
recomputed **before** the look, from the pre-committed formula above — not after seeing Δ.

### 6.2 Floor rule (binding)

* **No look before both legs carry ≥ 69 matured pair-cells** (recomputed upward if the
  realised sd exceeds 0.149, per §6.1).
* Exactly **one** look. If the floor is met, the look happens on the next scheduled
  research window and the results memo is written whether the answer is positive, null or
  inverted.
* **Sunset: 2027-09-30.** If the floor is unmet by then, the slot is closed without a
  verdict, the accrual is reported descriptively, and any future test of the same question
  is a new design with a new slot. Half the effect (272 cells/leg) is treated as infeasible
  now and is not chased.

### 6.3 Calendar estimate — an estimate, not a promise

Accrual per leg ≈ pairs/day × P(pair has ≥1 matured candidate in that leg). The pairs/day
rate after the change is genuinely unknown: it is bounded below by the current post-Stage-1
rate (~1.0-1.2 pairs/day) and bounded above by the pre-Stage-1 rate (~10 brief rows/day at
the cohort's 3.43 rows/pair ≈ 2.9 pairs/day). Assume leg coverage 0.5 (a pair contributes a
cell to a given leg half the time) and add the ~42-session maturity lag on the last cell:

| pairs/day | cells/day/leg | trading days to 69 cells | + maturity | ≈ from boundary |
|---|---|---|---|---|
| 1.0 | 0.5 | 138 | +42 | ~180 sessions ≈ 8.5 months |
| 2.0 | 1.0 | 69 | +42 | ~111 sessions ≈ 5.3 months |
| 3.0 | 1.5 | 46 | +42 | ~88 sessions ≈ 4.2 months |

So a deploy in early autumn 2026 implies a plausible read somewhere between **early 2027
and late 2027**, and this memo deliberately names **no date**. The accrual rate and the leg
coverage are both measurable within the first month (§5.4, §5.6) and the estimate is
refreshed then, as a descriptive note, without moving the floor.

## 7. Integrity conditions and HALT

The experiment is void, and the slot returned, if any of these is found before the look:

1. **`channel_*` reached selection.** Any column with the `channel_` prefix, or
   `shadow_strict_*`, appearing in a sort key, in `compose_weighted_score` /
   `selection_score`, or in `_BRIEF_SORT_KEYS`. The forward cohort would then be a treated
   population and the contrast would be a selection artifact rather than a measurement. The
   structural anti-rot test (design memo §7) exists to make this loud.
2. **Assessment changed row counts.** The never-drops invariant (`len(rows_out) ==
   len(rows_in)` for every assessment outcome, including a total assessor outage) must hold
   over the whole window; a breach makes the unverified leg conditionally sampled.
3. **Instrument collapse.** `channel_vote_dispersion == 2` on a large share of rows, or a
   status mix with almost no `unverified`, means the assessor is not measuring what the
   prompt asks. Both are prompt defects; fixing either ends the cohort and restarts accrual
   under a new `channel_config_version`.
4. **Provider drift.** The pinned OpenRouter provider changing mid-window is an amendment,
   not a footnote — record it and restart accrual if the status mix shifts with it.
5. **Peeking.** No partial Δ, no interim leg means, no dashboard of the contrast before the
   floor. Volume, status mix, dispersion and crowd-out (§5.3-§5.6) are the only quantities
   readable during accrual, and none of them touches `market_excess_return` by leg.

## 8. Explicitly not tested here

* **Not a continuation of the retro.** `channel_status` is a different estimand from the
  frozen Stage-1 gate — per-candidate instead of per-theme, post-bracket instead of
  pre-bracket, a differently-worded prompt, over a candidate set from a permissive
  proposer. Results from this experiment may not be pooled with, or read as an extension
  of, `stage1_retro_gate_increment_*`.
* **Not a test of the shadow rule.** `shadow_strict_verdict` is recorded so a theme-level
  KEPT-vs-REFUSED contrast becomes *computable*; testing it is a separate design with a
  separate slot, and the per-row `shadow_strict_verified_n` / `shadow_strict_assessed_n`
  columns exist so that design can re-cut the threshold offline without new LLM calls.
* **Not a test of the mcap bracket**, of the press-verification gates, or of the brief
  prose. None of them moves in this increment.
* **No promotion path is registered.** Even a clean positive result promotes `channel_*`
  only to "eligible for a pre-registered promotion test into ordering" under rule 3 of the
  hypothesis budget. Until then the columns stay display-only and parquet-only, under the
  unvalidated-display doctrine: no verdict word, no authority colour, no sort key.

## 9. Supersession of the ISO 40-42 window

`theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md` §13.4 item 3 dates the
earliest verdict window at **ISO weeks 40-42 (2026-09-28..10-18)**, being the 2026-08-03
deploy plus 8-10 fresh ISO weeks. Bumping `mapper_config_version` to `mapper-freeze-v3`
restarts that counter, for both arms of that head-to-head (the mechanical arm's theme
population is gated by the LLM arm's output, and the token is stamped on mechanical rows
too). **This is the second reset of that accrual after the 2026-08-03 one, and it is
intended.**

Why the trade is worth taking:

* **The old window was underpowered.** ~3-4 candidates/day ≈ 1.0-1.2 pairs/day; the retro's
  power memo puts the ISO 42 read at ~0.53 power for the full observed magnitude and ~0.22
  for half of it, with 80% power no earlier than ≈ ISO 2027-W07.
* **The old window could not compute the contrast at all.** Post-deploy, a refused theme
  produced no brief row and no ladder entry, so the refused leg did not exist in live data.
  The retro states this directly: without refusal shadow-tracking, "the ISO 40-42 read is
  not a KEPT-vs-REFUSED test at all". Move 3 of the design memo builds that tracking.
* **The retro withdrew the justification for spending on it**, recommending explicitly
  against a forward-window extension on its account and requiring any future
  KEPT-vs-REFUSED forward test to be a new design with shadow-tracking first. This memo is
  that new design's first half; the theme-level contrast itself remains unregistered.

What is *not* superseded: the retro study's own verdict and spent slot (they stand as
written), and the head-to-head's frozen mechanical rule, mcap band, shadow schema and kill
rules — only the **dating** of its verdict window moves, exactly as §13.4 item 3 already
contemplated for a `mapper_config_version` change.

## 10. Deliverables

1. This memo, locked before the implementation PR merges and before any post-boundary row
   matures.
2. A deploy-day amendment appended here recording the exact `mapper_config_version`,
   `channel_config_version` and first cohort `asof`.
3. A §3 cluster row (id 23) appended to `docs/research/edge_hypothesis_budget_2026_07.md`
   in the implementation PR, before any look.
4. A cohort extract with a committed sha256, then the outcome join, then a results memo
   `docs/research/channel_feature_forward_results_<date>.md` reporting the primary, the two
   pre-specified sensitivities (unwinsorized; `partial` pooled into V), the attrition table,
   and every descriptive in §5 — written whatever the answer is.
5. A one-line status update on this memo when the slot is consumed or sunset.
