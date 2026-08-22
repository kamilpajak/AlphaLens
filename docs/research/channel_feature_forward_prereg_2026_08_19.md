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
`channel_vote_dispersion = max − min` over valid draws. Draws that fail, come back
off-vocabulary, or arrive truncated (`finish_reason` `MAX_TOKENS`) are excluded from the
median; a candidate with zero valid draws is recorded `channel_status="unverified"` with the
failing `channel_assessment_outcome` and is **excluded from both legs of the primary test**
(§4), pre-committed here.

**Even vote sets (pre-committed).** `valid_n` is not `k`: one lost draw leaves two valid
draws, which is routine. The even case is decided here rather than by an implicit lower or
upper median, because the primary's two legs are literally `verified` and `unverified` and
a silent tie-break would move rows between them. **When the two central ordinals disagree,
the result is `partial`**; when they agree, that value stands. `partial` is excluded from
both legs (§4.1), so a tied candidate is reported as tied and enters neither leg. Pinned by
`tests/thematic/mapping/test_channel_assessor.py::TestEvenVoteTieBreak`.

**Instrument failures and the shadow denominator.** `shadow_strict_assessed_n` counts only
candidates the model ANSWERED. A failed assessment carries `channel_status == "unverified"`
by construction, so counting it would make an outage read as a channel-less day; those rows
are reported as `shadow_strict_failed_n` on the same row. Any offline re-cut of the shadow
rule (§5.6) must filter on `channel_assessment_outcome == "success"` before recomputing —
the same filter §4.1 already applies to the candidate-level legs.

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
3. **Crowd-out metric, at BOTH levels.** (a) *Proposal level* — share of stage-A proposals
   that land in the 500M-10B bracket, and the distribution of proposed tickers by mcap
   decile, compared against the retro's 96.0% `KEPT_TICKER_ABSENT` and the mega-cap-headed
   kept-theme proposal sets (XOM/CVX/COP/GOOGL/PSX/NVDA/RTX). (b) *Ship level* — the same
   two readouts over the rows that actually reach a brief: share of brief rows whose ticker
   was NOT named in the source event, and the mcap-decile distribution of shipped rows.
   Level (b) exists because the repair is only real if it survives the ship path: the
   verify stage attempts the top `_MAX_VERIFY_ATTEMPTS_PER_THEME` = 5 in-bracket candidates
   by stage-A confidence and ships at most `_MAX_CANDIDATES_PER_THEME` = 3, and stage A is
   now asked to order most-direct-first — so the less-visible names this increment exists
   to surface sit at the tail. The acceptance probe measured 2.1 in-bracket per theme, at
   which the cap does not bind; §11 projects 5-7 per theme if Move 1 works, at which it
   does. **Watch `n_in_bracket` and `n_over_assess_cap` in the theme-decisions sidecar
   against the cap of 5 on the first days after deploy.** Selection is deliberately NOT
   changed in this increment; the cap is made visible, not moved. Both levels use
   **proposal / brief** data, not outcomes, so they can be read within days of deploy.
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

---

## Amendment 1 — cohort restart, new taxonomy, grounding condition (2026-08-20)

**Status of this amendment:** LOCKED, committed **before** the change it describes is
implemented and **before** it deploys, as §3 requires of any mid-window instrument change.
**Nothing above this line is rewritten.** §1-§10 stand as the record of cohort 1; this
amendment closes that cohort, opens cohort 2, and restates every rule that names a value of
the old vocabulary. Where the two disagree, this amendment governs from boundary 2 onward
and §1-§10 governs the closed cohort 1.

**Design memo for the change:** `docs/research/grounding_and_prose_honesty_design_2026_08_20.md`.

### A1.1 What cohort 1 accrued, and what is being discarded

Cohort 1 opened at the first VPS `alphalens thematic map-themes` run on
`mapper-freeze-v3` + `channel-assess-v1`, **2026-08-19 22:46 UTC**, and accrued exactly one
run before this amendment:

| Quantity | Cohort 1, total |
|---|---|
| Themes with a resolved catalyst | 10 |
| Stage-A proposals | 102 |
| In the 500M-10B bracket | 13 |
| Assessed (stage B answered) | 13 |
| `verified` / `partial` / `unverified` | 1 / 8 / 4 |
| `channel_assessment_outcome != "success"` | 0 |
| Brief rows shipped | 12 |
| **Matured `market_excess_return` outcomes** | **0** |

The ladder endpoint is ~42 sessions away, so **no row of cohort 1 has an outcome, and no leg
of the primary has a single cell.** What is discarded by restarting is 13 label rows and one
day of descriptive volume — not one unit of the accrual the floor in §6.2 counts. The
instrument was never brought into contact with an outcome, so the slot registered in §4.3 is
**not consumed** by cohort 1 and is carried into cohort 2 unspent (§4.3 returns the slot only
when a cohort ends "with no label→outcome contact ever having occurred", which is exactly
this case).

Cohort 1 rows are **never pooled** with cohort 2, exactly as §2 forbids pooling
`mapper-freeze-v2` rows with cohort 1. They remain readable on disk under their own tokens.

### A1.2 What changes, and why it moves the tokens

Two changes to the stage-B instrument, both in one increment:

1. **The status vocabulary is renamed to a causal-support taxonomy.** `verified` /
   `partial` / `unverified` become **`established` / `suggestive` / `not_established`**,
   order-preserving and 1:1, ordinal codes 0/1/2 unchanged. The reason is that `verified`
   overclaims: the verdict comes from a second LLM call over the same rendered event text,
   not from independent verification. The three levels also gain written operational
   definitions, and the prompt gains one fixed sentence stating that causal support is a
   statement about evidence and **not** a forecast of the share price.
2. **A second, orthogonal column is added: `channel_grounding_status`** in
   `grounded` / `theme_misroute` / `candidate_misfit` (model-emitted), plus the Python-only
   `unknown` (asked, no valid draw) and `not_assessed` (never asked). It separates two
   conditions that today both render as `unverified`: honest uncertainty ("the event is
   about the theme, this company is plausibly in scope, no company-specific mechanism was
   established") from a pipeline defect ("this company was attached to a story it has
   nothing to do with"). It is **not** a fourth support level and is never folded into the
   support column.

Both move `prompt_sha` and `schema_sha` inside `channel_config_version`, whose payload tag
also moves `channel-assess-v1` → **`channel-assess-v2`**. `channel_config_version` rides
inside `mapper_config_version` under key `"channel"`, so `mapper_config_version` moves too;
`_MAPPER_FREEZE_SCHEMA` is additionally bumped `mapper-freeze-v3` → **`mapper-freeze-v4`**
purely as a legible cohort marker, so that a future reader filtering on the human-readable
tag alone cannot silently pool the discarded 2026-08-19 day with cohort 2. Stage A's prompt,
schema, sampling constants and mcap range are **unchanged** in this increment.

`SHADOW_STRICT_RULE_VERSION` moves `shadow-strict-any-verified-v1` →
`shadow-strict-any-established-v1`. It stays a poolability key and **not** a freeze input, as
§3 already records; it moves only because it names a vocabulary that no longer exists.

Per §3, this ends the accrual window and starts a new cohort.

### A1.3 Why now rather than later

One day of data is the cheapest possible reset this programme will ever be offered. The same
two changes made in a week cost a week of accrual; in a month, a month — and §6.3 already
puts the floor between roughly four and nine months away, so a later reset would push the
read past the 2027-09-30 sunset in the slower accrual scenarios. The defects are known now,
the instrument has zero outcomes attached now, and the correction is cheap now. Deferring
would also mean knowingly accruing a cohort under a vocabulary the programme has already
decided is indefensible, which is worse than losing the day.

**Corollary, pre-committed here: a third restart is not acceptable.** Every vocabulary,
prompt and schema change this programme already knows it wants — including whatever the
argumentation / thesis layer needs to read from the assessor — lands in this one increment.
A further mid-window instrument change before the floor is met is an amendment that must
argue why it could not have landed here.

### A1.4 Cohort 2 boundary

**Cohort 2 opens at the first VPS `alphalens thematic map-themes` run on the image carrying
`mapper-freeze-v4` + `channel-assess-v2` — deploy time, not merge time.**

Membership is identified in the data, not by hand: a row belongs to cohort 2 iff its
`mapper_config_version` payload carries `"schema": "mapper-freeze-v4"` **and** a `"channel"`
key equal to the deployed `channel_config_version` (whose payload carries
`"schema": "channel-assess-v2"`).

**Amendment 2 is owed on deploy day**, before any post-boundary row matures, recording the
exact `mapper_config_version` and `channel_config_version` token strings and the first
cohort-2 `asof`. It replaces deliverable §10.2, which cohort 1 never reached.

Boundary history, kept rather than rewritten:

| Cohort | Opened | Tokens | Closed | Accrued |
|---|---|---|---|---|
| 1 | 2026-08-19 22:46 UTC | `mapper-freeze-v3` + `channel-assess-v1` | 2026-08-20, by this amendment | 13 assessed candidates, 12 briefs, 0 matured outcomes |
| 2 | first run on the new image (Amendment 2) | `mapper-freeze-v4` + `channel-assess-v2` | — | — |

### A1.5 Frozen instrument identity, restated (supersedes the §3 table)

| Token | Pins |
|---|---|
| `mapper_config_version` (`mapper-freeze-v4`) | stage-A proposal prompt, response schema, sampling constants, mcap range, and (nested, key `"channel"`) the stage-B token |
| `channel_config_version` (`channel-assess-v2`) | stage-B prompt sha, response-schema sha, temperature 0.0, max output tokens, `votes = 3`, the **support** and **grounding** vocabularies, the channel-type vocabulary, render caps |
| `shadow_strict_rule_version` (`shadow-strict-any-established-v1`) | the theme-level shadow rule only; still **not** a freeze input |

Aggregation per candidate, k = 3 draws at temperature 0.0 under the pinned OpenRouter
provider:

* **Support level** — ordinal median over `not_established=0 / suggestive=1 / established=2`
  (the codes are unchanged, so the §3 arithmetic and the even-vote rule survive verbatim).
  `channel_support_dispersion = max − min` over valid draws (renamed from
  `channel_vote_dispersion`; there are now two aggregated answers and the bare word
  "dispersion" would be ambiguous).
* **Even vote sets** — unchanged and still pre-committed: when the two central ordinals
  disagree the result is the middle value, now named **`suggestive`**, which is excluded from
  both legs. Pinned by
  `tests/thematic/mapping/test_channel_assessor.py::TestEvenVoteTieBreak`.
* **Grounding status** — categorical, so no median. **Plurality over the valid draws, with
  tie precedence `grounded` > `theme_misroute` > `candidate_misfit`**, pre-committed here.
  A split vote therefore never manufactures a defect, and when every draw claims a defect but
  they disagree, the candidate-independent value wins because an operator can verify it once
  per theme instead of once per row. `channel_grounding_agree_n` (valid draws equal to the
  aggregate) is the per-row noise readout, the categorical counterpart of
  `channel_support_dispersion`.
* **Draw validity is all-or-nothing.** An off-vocabulary grounding value invalidates that
  draw, exactly as an off-vocabulary support value does; it is never coerced. A valid draw
  therefore always carries both answers, so `grounding_unknown` equals `assess_failed` by
  construction, and that identity is pinned by a test.
* **Zero valid draws** → `channel_support_status = "not_established"` (the least-claiming
  answer, unchanged convention) + `channel_grounding_status = "unknown"` + the failing
  `channel_assessment_outcome`, and the row is excluded from both legs, as §3 already
  pre-commits.

**No cross-normalisation between the two columns.** A `theme_misroute` row is **not** forced
to `not_established`: the (`established` × `theme_misroute`) cell is the fabrication readout
— a fluent chain built on an event that is not about the theme — and it is the single most
informative cell for the later stratified audit. Both answers are recorded as given; the
exclusion happens in the analysis by the pre-committed rule in A1.6, not by overwriting a
field.

### A1.6 Primary hypothesis, restated in the new taxonomy (supersedes §4 and §4.1)

**H1 (TWO-SIDED, unchanged in substance):** among post-boundary candidates with a matured
outcome **and `channel_grounding_status == "grounded"`**, the mean `market_excess_return` of
`channel_support_status == "established"` candidates differs from that of
`channel_support_status == "not_established"` candidates. α = 0.05, two-sided, single look.
Two-sided for the reason already given in §4 and not revisited.

* **Leg E** (was leg V): `channel_support_status == "established"`.
* **Leg N** (was leg U): `channel_support_status == "not_established"`.
* **`suggestive` is EXCLUDED from the primary** and reported descriptively; the pre-specified
  sensitivity pooling it into leg E is carried over unchanged and still consumes no slot.
* **`not_assessed` rows** (off-bracket, or over the per-theme assessment cap) never enter
  either leg — they have no outcome by construction, and both columns read `not_assessed` on
  such a row.
* **Instrument-failure rows** (`channel_assessment_outcome != "success"`) are excluded from
  both legs and counted in the attrition table, unchanged.

**NEW pre-committed exclusion — grounding-failed rows.** Rows with
`channel_grounding_status != "grounded"` are **excluded from both legs** and counted in the
attrition table on the same footing as instrument failures. The reason is definitional, not
statistical: a row whose event is not about the theme, or whose company has no relationship
to the event's subject matter, is not a measurement of that theme's transmission channel at
all. Grounding is a **validity condition on the measurement**, not a level of the thing being
measured, and it is therefore reported separately and never contrasted against the support
levels as if it were one of them.

The attrition table gains four counted rows, reported with the leg counts and never merged
into them:

| Attrition reason | Reported as |
|---|---|
| `channel_grounding_status == "theme_misroute"` | `n_excluded_theme_misroute` |
| `channel_grounding_status == "candidate_misfit"` | `n_excluded_candidate_misfit` |
| `channel_grounding_status == "unknown"` | `n_excluded_grounding_unknown` (equals the instrument-failure count by construction; reported anyway so the identity is visible) |
| `channel_support_status == "suggestive"` (grounded) | `n_excluded_suggestive`, unchanged |

**Third pre-specified sensitivity (no slot, cannot replace the verdict):** the identical
estimator re-run with the grounding exclusion lifted, i.e. over all matured rows regardless
of `channel_grounding_status`. It exists so the cost of the exclusion is visible rather than
assumed, and it is pre-committed here, before any cohort-2 row exists. Like the other two
sensitivities it may qualify the reading and may never replace the verdict.

**Unit of inference, outcome, winsorization, estimator, machinery and the two-stage commit
protocol are unchanged** (§4.1 last paragraph, §4.2). The grounding exclusion is applied
**before** cells are formed, so a pair-cell mean never mixes grounded and non-grounded rows.

**Ledger entry.** The registered id `channel_status_forward_verified_minus_unverified_2026_08_19`
is **not renamed** — a registered identifier is immutable, and renaming it would look like a
second registration. Its wording refers to the superseded vocabulary; the mapping is 1:1
(`verified` → `established`, `unverified` → `not_established`) and is recorded here. Looks
budgeted remains **1**, still unspent (A1.1).

### A1.7 Descriptives, restated (supersedes the affected items of §5)

Unchanged in intent; renamed and extended:

* **§5.2 `channel_type` breakdown** — unchanged.
* **§5.3 crowd-out, both levels** — unchanged.
* **§5.4 volume per day** — the `channel_status` mix becomes the **`channel_support_status`
  mix**, and a **`channel_grounding_status` mix** is reported beside it.
* **§5.5 instrument stability** — `channel_vote_dispersion` is renamed
  **`channel_support_dispersion`**; **`channel_grounding_agree_n`** joins the same
  descriptive. A support-dispersion distribution concentrated at 2 remains a HALT condition
  (A1.8). A grounding-agreement distribution concentrated at 1-of-3 means the grounding
  question is not being answered stably and invalidates any theme-level reading of it.
* **§5.6 shadow descriptives** — `shadow_strict_verified_n` becomes
  `shadow_strict_established_n`; the shadow rule's meaning is unchanged ("keep iff at least
  one ANSWERED candidate reached the top level"). **Grounding is deliberately NOT folded into
  the shadow**: the shadow replays the OLD gate, which had no grounding concept, and coupling
  them would change the estimand being shadowed. The per-theme grounding counts
  (`n_grounded`, `n_theme_misroute`, `n_candidate_misfit`, `n_grounding_unknown`) are stamped
  beside it in the theme-decisions sidecar so any offline re-cut is possible without new LLM
  calls.
* **§5.7 status-mix audit** — extended into a **stratified** manual read of ~30 rows in the
  first two weeks, stratified across grounded and non-grounded rows, checking
  `channel_evidence` and `channel_grounding_quote` against the source event. Qualitative, no
  rate claimed. This read is **not** the audit that A1.9 requires before any gating.
* **NEW descriptive — the 3×3 cross-tab** of `channel_support_status` ×
  `channel_grounding_status`, reported every time either column is reported. The cells that
  carry the diagnostic: (`grounded` × `not_established`) is the honest-uncertainty population
  this whole design exists to keep; (`grounded` × `established`) is the working case;
  (`theme_misroute` × anything) is an upstream pipeline defect; (`candidate_misfit` ×
  `not_established`) is a stage-A defect; (`established` × `theme_misroute`) is the
  fabrication readout.

**Reporting rule, pre-committed:** because the plurality tie-break resolves toward
`grounded`, the measured `theme_misroute` rate is a **lower bound** on the pipeline defect
rate and must always be reported next to the `channel_grounding_agree_n` distribution, never
as a point estimate.

### A1.8 Integrity conditions and HALT, restated over the new names (supersedes §7)

The experiment is void, and the slot returned, if any of these is found before the look:

1. **`channel_*` reached selection.** Unchanged. The new columns keep the `channel_` prefix
   precisely so the existing structural anti-rot test covers them. **One consumer is
   explicitly permitted and is not a breach: the argumentation prompt builder may READ the
   channel columns to write the brief prose.** Prose is not selection — it changes no row's
   presence, rank or score. `_BRIEF_SORT_KEYS`, `compose_weighted_score` and `selection_score`
   remain forbidden.
2. **Assessment changed row counts.** Unchanged and now doubly binding: the never-shrink
   invariant (`len(rows_out) == len(rows_in)` for every assessment outcome, including a total
   assessor outage) must hold **including for every value of `channel_grounding_status`**. A
   `theme_misroute` row that fails to ship is a breach.
3. **Instrument collapse.** `channel_support_dispersion == 2` on a large share of rows, or a
   support mix with almost no `not_established`, remains a HALT. **Added:** a grounding mix
   with essentially no `theme_misroute` on days when a round-up-shaped catalyst is visible in
   the funnel is a prompt defect of the same kind — the comfortable-middle failure pointing
   the other way — and fixing it ends the cohort and restarts accrual, as any prompt fix does.
   It is a prompt defect, not a finding.
4. **Provider drift.** Unchanged.
5. **Peeking.** Unchanged. The grounding mix, the 3×3 cross-tab, the agreement distribution
   and the crowd-out readouts are all readable during accrual precisely because none of them
   touches `market_excess_return` by leg.

### A1.9 The grounding detector does not gate anything

Pre-committed, and binding on this experiment and on the pipeline:

**DETECT, STAMP, KEEP, MEASURE.** `channel_grounding_status` never removes a candidate, never
enters a filter, a sort key, a score, the verify loop or the assessment cap. `assess_candidates`
keeps its one-result-per-input contract. A row detected as `theme_misroute` or
`candidate_misfit` ships exactly as it would have shipped, in the same position, with the
status recorded, counted in the per-theme log line and the sidecar, and exported as a
Prometheus gauge on every run including zero.

The reason is the record above this line: §1 of this memo exists because a gate that deleted
candidates on an LLM judgement was measured and found inverted. Turning a fresh LLM judgement
into a fresh deletion gate on day one would repeat that error with a new vocabulary.

**Any future gating on `channel_grounding_status` requires, in this order:** (a) an
independent stratified audit of detector accuracy against operator-labelled ground truth,
with its own design memo; (b) a separate pre-registration with its own slot, drawn against
the same program-lifetime hypothesis budget; (c) a deploy that ends whatever cohort is then
accruing. No part of that is authorised by this amendment.

### A1.10 Accrual consequence of the new exclusion

The floor in §6.2 (≥ 69 matured pair-cells per leg, recomputed upward if the realised sd
exceeds 0.149) is **unchanged in value and now counts grounded cells only**. Because
non-grounded rows are excluded before cells are formed, the effective accrual rate is the
§6.3 rate multiplied by the grounded share, which is unknown today and measurable within the
first month of cohort 2 (A1.7). The §6.3 calendar estimate is therefore **optimistic by
exactly that factor** and is refreshed as a descriptive note once the grounded share is
observed — without moving the floor, and without touching the **2027-09-30 sunset**, which
stands.

### A1.11 Deliverables added by this amendment

1. This amendment, committed before the implementation lands and before it deploys.
2. **Amendment 2 on deploy day** — exact `mapper_config_version`, `channel_config_version`,
   first cohort-2 `asof`.
3. The results memo (§10.4) additionally reports: the 3×3 cross-tab, the four new attrition
   rows, the `channel_grounding_agree_n` distribution, and the third sensitivity (grounding
   exclusion lifted).

## Amendment 2 — deploy-day record for cohort 2 (2026-08-21)

**Status of this amendment:** LOCKED. It records facts about a deploy that has already
happened; it changes no rule, moves no token, and opens no cohort.

**It is one day late.** A1.4 owed it "on deploy day, before any post-boundary row matures".
Cohort 2 opened 2026-08-20 13:51 UTC and this is written 2026-08-21. No cohort-2 row has
matured in the interval (maturation needs the ~42-session ladder window), so nothing is
compromised — but the obligation was to write it before, not merely before harm, and that
was missed. Recorded rather than quietly corrected.

### A2.1 Exact tokens (discharges A1.4 and deliverable A1.11.2)

`channel_config_version`, verbatim from the first cohort-2 candidates parquet:

```
{"field_constants":{"block_tag":"untrusted_event","candidate_field_max_chars":120,"candidate_rationale_max_chars":300,"field_max_chars":80,"headline_max_chars":200,"implication_max_chars":240,"implications_max":5,"text_max_chars":600,"unavailable":"(none)"},"grounding_statuses":["grounded","theme_misroute","candidate_misfit"],"max_output_tokens":4000,"model":"deepseek/deepseek-v4-pro","prompt_sha":"0af6af9132f8","schema":"channel-assess-v2","schema_sha":"72effbe5c286","support_levels":["established","suggestive","not_established"],"temperature":0.0,"types":["customer_demand","supplier_input","input_cost","regulatory","substitution","capacity_supply","financing_ma","category_attention","none"],"votes":3}
```

`mapper_config_version` carries `"schema":"mapper-freeze-v4"`, `"prompt_sha":"fdcbf59d0720"`,
`"schema_sha":"30118172b8b8"`, `"mcap_range":[500000000,10000000000]`, `"max_candidates":15`,
`"model":"deepseek/deepseek-v4-pro"`, `"temperature":0.0`, and the nested `"channel"` key
holding the `channel_config_version` string above — exactly the nesting A1.4 uses to define
membership.

Both tokens are single-valued across every cohort-2 row inspected (`nunique() == 1` per
column), so no row straddles a token change.

### A2.2 First cohort-2 `asof` — and a wrinkle that must be stated

**First cohort-2 `asof`: 2026-08-19**, written 2026-08-20 14:39 UTC by the first
`map-themes` run on the new image (boundary 13:51 UTC).

The wrinkle: **that `asof` predates the cohort boundary timestamp.** It is nonetheless a
cohort-2 row, and correctly so — A1.4 pre-committed that membership is "identified in the
data, not by hand", by the token pair. The brief parquets are regenerated on every one of the
six daily slots, so a run after the boundary rewrites `asof` dates from before it under the
new instrument.

Two consequences, recorded now rather than discovered during the read-out:

1. **Cohort membership is not a function of `asof`.** Any analysis that partitions cohorts by
   date rather than by token will mis-assign rows. The token rule governs, as written.
2. **A day's rows are a snapshot, not a fixed set.** The same `asof` can carry different rows,
   and a different count, depending on which slot last wrote it. Counts quoted from a single
   read are provisional; the results memo must state the read timestamp beside any count.

Boundary history, restated in full (supersedes the A1.4 table, which left cohort 2 open):

| Cohort | Opened | Tokens | Closed | Accrued (asof dates) |
|---|---|---|---|---|
| 1 | 2026-08-19 22:46 UTC | `mapper-freeze-v3` + `channel-assess-v1` | 2026-08-20 by Amendment 1 | `asof` 2026-08-18 only — 13 candidates, 0 matured |
| 2 | 2026-08-20 13:51 UTC (first write 14:39 UTC) | `mapper-freeze-v4` + `channel-assess-v2` | — | `asof` 2026-08-19 (8) and 2026-08-20 (11) at the 2026-08-21 read |

### A2.3 A change that is deliberately NOT an amendment

Issue #1070 (a `suggestive` + `grounded` row whose channel describes HARM while the prose may
still assert benefit) is being closed by extending the deterministic prose guard so that it
also refuses a benefit claim against a harm-direction channel.

**This is not an instrument change under A1.3 and opens no cohort**, for two independent
reasons:

1. The guard lives in the argumentation stage and is fingerprinted by
   `brief_support_guard_version`, which A1.5 does not list as a freeze input. The cohort key
   is `mapper_config_version` + `channel_config_version` and neither moves.
2. H1 tests `market_excess_return` conditioned on `channel_grounding_status`. The guard
   touches neither. It governs what the written brief may CLAIM, not what is measured.

The alternative considered and **rejected**: adding a `channel_direction` field to the
assessor schema. It is the better instrument — a label on the model's own chain rather than a
lexical test over it — but it would move `channel_config_version` and open cohort 3. A1.3
pre-commits that "a third restart is not acceptable" and names "whatever the argumentation /
thesis layer needs to read from the assessor" as belonging to increment 2. The honest answer
to A1.3's required question — why could this not have landed there — is that **it could
have**: #1070 was filed as a known gap of the same increment and shipped without it. That is
not an argument for an exception; it is an argument for accepting the weaker instrument now
and revisiting the schema at the next legitimate boundary.

The lexical guard's known residual — a miss is indistinguishable from compliance — is the one
already documented for the benefit lexicon, and the same suppressed-match telemetry applies.

---

## Amendment 3 — a population change at the theme selector (2026-08-22)

**Status of this amendment:** LOCKED, committed **before** the change it describes deploys.
Nothing above this line is rewritten. **Cohort 2 continues; the accrual counter does not
restart.**

### A3.1 What changes

The theme selector's tie-break. The daily pipeline ranks ~11,000 candidate themes and takes
the top 10 by `novelty_rank`. That score is coarse and count-based, so the cut is frequently a
tie: on 12 of 17 measured days the boundary was tied, the tied pool had median size 7 (max
18), and 82 themes were dropped by the tie-break alone. The tie was being resolved
**alphabetically** — pandas sort stability over the groupby output — verified on 9 of 9 days
where a fully-tied pool was split.

It becomes a permutation seeded from the `asof` date, and each theme's marginal inclusion
probability is recorded on the theme rollup. PR #1086.

### A3.2 Why this is NOT an instrument change under A1.3

Four independent reasons, in descending order of how much they settle:

1. **The cohort key does not move.** A1.5 names exactly two freeze inputs,
   `mapper_config_version` and `channel_config_version`. Both were computed on this branch and
   on `origin/main` and are **byte-identical**. The selector is fingerprinted by
   `novelty_config_version`, which A1.5 does not list — the same argument A2.3 made for
   `brief_support_guard_version`.
2. **The restart rule does not reach it.** §3 ends the accrual window on "any change to either
   prompt, to `votes`, or to the status/type vocabulary". A tie-break at theme selection is
   none of those.
3. **No §7 HALT condition is touched.** No `channel_*` or `shadow_strict_*` column reaches
   selection; the never-drops invariant is untouched; the assessor prompt, vote count and
   vocabularies are unchanged; the pinned provider is unchanged.
4. **H1 is a within-cohort contrast.** It tests `market_excess_return` conditioned on
   `channel_grounding_status`. Both legs are drawn from whatever population the selector
   produces, so a change in that population shifts both legs together rather than one.

### A3.3 What it nevertheless does, stated plainly

It changes **which themes enter the cohort**, and therefore which candidates are assessed. That
is a population change, and it is recorded here rather than left implicit, because §7.4 sets
the precedent that a population-level shift is amendment-worthy even when no token moves: it
requires provider drift to be recorded "and restart accrual if the status mix shifts with it".

The same test applies here. The status mix (§5.4) is readable during accrual, and it is the
quantity to watch. **Pre-committed now, before the change deploys:** if the support or
grounding status mix shifts materially after this lands, that is grounds to treat the boundary
as a cohort break and say so — not grounds to reinterpret the mix afterwards. The direction of
any observed shift is not predicted here, deliberately.

The expected magnitude is small: the change only reorders themes the score already treats as
equivalent, and it binds on the ~12-of-17 days where the boundary is tied, affecting the
marginal slot rather than the top of the ranking.

### A3.4 Why now rather than at the next boundary

A1.3 requires a change made mid-window to argue why it could not have landed in increment 2.
The honest answer here is that **it is not an instrument change at all**, so A1.3's question is
addressed to a different class of change. But the substantive reason to do it now is that the
propensities are only useful going forward: they cannot be reconstructed for days already
accrued, because a propensity needs the deciding slot's event counts and the events parquet has
grown since. Every day of delay is a day that can never enter an off-policy evaluation. The
alphabetical bias it removes is, separately, a defect with no defender.

### A3.5 Transition-day exclusion

`mapper_config_version` does not fingerprint `novelty_config_version`, so on the deploy day an
`asof` whose candidates are already frozen keeps its old slate while its rollup carries the new
token. **That date is excluded from any propensity-weighted analysis**, recorded here so the
exclusion is a rule rather than a later judgement call. It has no effect on H1, which does not
read propensities.
