# Transmission channel as a scored feature, not a selection filter — design memo

**Status:** LOCKED
**Date:** 2026-08-19
**Author:** Kamil Pajak
**Branch:** `feature/channel-as-feature`
**Motivating evidence:** `docs/research/stage1_retro_gate_increment_results_2026_08_19.md` (PR #1065)
**Forward pre-registration:** `docs/research/channel_feature_forward_prereg_2026_08_19.md`
**Supersedes-as-verdict-window:** §13.4 item 3 of `theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md` (ISO weeks 40-42, 2026-09-28..10-18) — see §8.

**Figure:** [`assets/channel_feature_selection_flow.svg`](assets/channel_feature_selection_flow.svg) — the whole selection flow on one page, with both prompts verbatim and real acceptance-probe outputs ([`assets/README.md`](assets/README.md)).

---

## 1. What happened, in numbers

Stage 1 (#975 / #980, deployed 2026-08-03) made the theme mapper event-conditioned and
made a **transmission channel mandatory**: `theme_mapper._normalize` drops any candidate
whose `transmission_channel` field is empty, and the prompt tells the model to refuse a
theme it cannot attach a channel to. Proposal volume fell from ~99/day to ~19/day and
brief rows from ~10/day to ~1-6/day.

The pre-registered retrospective (PR #1065) replayed the **frozen** Stage-1 gate over the
pre-gate cohort, whose forward returns are already known. Three findings drive this memo:

1. **The pre-registered hypothesis is not supported, and the point estimate is inverted.**
   On the clean design-holdout window, themes the gate would KEEP underperformed themes it
   would REFUSE by **7.15 pp** of matured `market_excess_return`
   (pair-cluster Δ = **−0.0715**, one-sided bootstrap p = **0.945**,
   95% CI **[−0.159, +0.017]**, **38 KEPT vs 49 REFUSED** theme-event pairs,
   142 vs 145 matured rows). The Bonferroni slot
   `stage1_retro_gate_increment_clean_kept_minus_refused_2026_08_19` is spent; that exact
   test can never be re-run with a tweaked rubric or re-cut windows.

2. **Crowd-out 96.0%.** When the gate keeps a theme, the mapper's proposals go to the firms
   literally named in the article, which are large: **334 KEPT_TICKER_ABSENT vs 14
   KEPT_TICKER_PROPOSED** rows. The kept-theme proposal sets are headed by XOM (14 pairs),
   CVX (11), COP (7), GOOGL (6), PSX (6), NVDA (6), RTX (6) — almost all above the
   500M-10B bracket the pipeline can actually ship. Event-conditioning did not just filter
   the candidate list, it **moved it out of the universe**.

3. **The refusals are mostly channel refusals.** Pair-majority taxonomy over 189 pairs with
   at least one decline: `no_channel` **68.8%**, `non_event` 15.9%, `other` 14.3%
   (mostly no-channel / non-event blends), `direction_filter` 1.1%. Spot notes also show
   the opposite failure — an **invented** channel rather than a refusal
   (`AI ethics` → VERI, `AI_safety` → BAH, `Artificial Intelligence` → NDAQ).

A fourth finding is an instrument fact, not a return fact: with a single pinned OpenRouter
provider, majority-of-5 voting is stable on ~95% of events, but **91 of 238 pairs had mixed
votes**. A single draw of a channel judgment is not a measurement.

Literature agrees with the direction of the retro estimate rather than with the gate.
Less-visible economic links drift **more**, not less (Hoberg-Phillips text-based peers:
momentum spillover 2-3x larger on low-visibility peers; Menzly-Ozbas: supplier/customer
predictability strongest where analyst coverage and institutional ownership are low;
Cohen-Frazzini: ~1.55%/month customer-supplier drift, converging **faster** for large
caps). Attention-driven small-cap drift runs ~1-20 trading days (Da-Engelberg-Gao
+30 bp over two weeks; Tetlock: news-backed moves drift ~20 days while no-news moves
reverse). A hard channel gate therefore buys precision by throwing away exactly the
population where the effect is supposed to live — and, worse, it destroys the outcome
labels needed to ever check that (the reject-inference problem from credit scoring, except
here the refused candidate's forward return is freely observable, so shadow labeling is
cheap).

## 2. The decision

**The transmission channel stays as a scored feature and an annotation. It never selects.**

Three moves:

* **Move 1 — split candidate GENERATION from channel ASSESSMENT.** The proposal stage runs
  first and permissively over the small/mid-cap universe; only then is each proposed
  (event, ticker) pair assessed for its channel. This is what repairs the 96% crowd-out:
  the proposer is no longer rewarded for naming the firms the article names.
* **Move 2 — the assessment emits STRUCTURED fields per candidate**, with "no verified
  channel" as a **first-class legal outcome**, stamped on the candidate row for later
  display and validation. It never drops a candidate.
* **Move 3 — a SHADOW verdict column** records what a strict channel gate **would** have
  done, so the forward KEPT-vs-REFUSED contrast becomes computable on live data. Today a
  refusal leaves no candidate row, no brief row and no ladder row at all — which is exactly
  why the ISO 40-42 window could not have answered the question it was pointed at (§8).

## 3. Two-stage call flow (real functions)

Unchanged entry path: `alphalens_cli/commands/thematic.py::map_themes_cmd` →
`thematic/mapping/orchestrator.py::map_themes` → per theme `_rows_for_theme` →
`_resolve_catalyst` (a theme with no catalyst still returns early, no LLM call).

### Stage A — permissive proposal (`theme_mapper.propose_candidates`)

`_PROMPT_TEMPLATE` STEP 2 stops being a gate and becomes a recall instruction:

* **Removed:** "A company qualifies only if you can name a TRANSMISSION CHANNEL", the
  "chain of at least two links" form requirement, drop-test (a) *Materiality*, drop-test
  (b) *Direction*, and the "These are NOT channels. Reject them" list.
  Drop-test (b) was a direction filter and is out of doctrine on its own.
* **Kept from that block:** the theme-slug lexical trap, restated as one line —
  *do not propose a company only because its industry shares a word with `theme_tag`;
  where the tag and the event disagree, the event wins.* That guard is about the routing
  label, not about requiring a channel (it is the `supreme_court` → firearms-maker defect).
* **Added — the crowd-out repair, with no market-cap token anywhere:** the companies named
  in the article are the obvious answers and usually the least useful ones; for every
  obvious name, add at least one **less-visible** company — a specialist whose entire
  business is this line rather than a conglomerate with a token segment, a supplier one or
  two steps down, a regional or single-product operator, a company serving the category
  without being famous for it.
* **Decline licence narrowed** to exactly two legal reasons, renamed
  `no_candidates_reason` → `decline_reason` with an enum:
  `no_event` (cannot tell what happened / not a business development) and
  `not_business_development` (real development, touches no line of business of any
  US-listed company). **Never decline because a link is weak, indirect or speculative** —
  weak links are wanted here and are graded in stage B.
* `_MAPPER_RESPONSE_SCHEMA` loses `transmission_channel` from properties and `required`;
  `_normalize` loses the channel-less `continue` and its WARNING counter.
* `_MAX_CANDIDATES = 15` is deliberately **unchanged** — moving the ceiling in the same
  commit as the prompt would make any volume change unattributable.
* `_MAPPER_FREEZE_SCHEMA` moves `mapper-freeze-v2` → `mapper-freeze-v3`. That tag is the
  human-legible cohort marker; the code-level change (`_normalize` losing its drop) is
  invisible to `prompt_sha` and `schema_sha`, which is exactly what the tag exists for.

### mcap bracket — unchanged, still deterministic and post-LLM

`orchestrator.DEFAULT_MCAP_RANGE = (500_000_000, 10_000_000_000)` →
`mcap_filter.classify_by_mcap`, applied **after** the model answers. No size number ever
enters a prompt. `_propose_and_filter_candidates` is split into `_propose_and_bracket`
returning a `ThemeProposal` (pre-bracket `proposed`, positional `verdicts`, in-bracket
`candidates`, `in_bracket`, `keywords`, `outcome`); the existing funnel INFO/WARNING log
lines keep their exact wording, because the operator greps them.

### Stage B — channel assessment (new `thematic/mapping/channel_assessor.py`)

`_assess_channels_for_theme` runs **after** the bracket, on in-bracket candidates only, and
writes its result into the same candidate dicts. One assessment per candidate, `k = 3`
independent draws at temperature 0.0, aggregated by **ordinal median** over
`unverified=0 / partial=1 / verified=2` (deterministic for odd k, no tie-break), with
`dispersion = max − min` over the valid draws. Draws that come back malformed or failed are
excluded from the median; if no valid draw survives, the result is
`status="unverified", outcome=CALL_FAILED` — a failure is recorded as unverified with a
failure outcome, **never as a drop and never as verified**.

Why post-bracket: cost (the bracket is the largest sink) and relevance (only in-bracket
names can ever produce a ladder outcome, so only they can carry a forward contrast).
Off-bracket proposals still get a funnel row, stamped `channel_status="not_assessed"`, so
nothing becomes invisible.

The assessment prompt reuses the stage-A untrusted-data discipline verbatim (same fence
tag, same sanitised rendering) and adds a second fenced block for the candidate. It asks
for the chain in one form — *fact stated in the event → what changes and for whom → which
line of this company's economics moves, and roughly when* — and it says plainly that
`unverified` is a normal, expected, unpenalised answer, that a fabricated chain is worse
than none, and that **nothing is dropped on the answer**. It contains no market-cap, P/E or
volume token; that is pinned by the same test as the stage-A prompt.

### Downstream — pure pass-through

`screening/scorer.py::score_candidates` merges enrichment onto the candidates frame as the
LEFT side, so every channel column rides into `thematic_scored`;
`argumentation/orchestrator.py::generate_briefs` merges again into
`~/.alphalens/thematic_briefs/`. Django's parquet ingest reads only enumerated model
fields, so the channel columns stay **parquet-only**, exactly like `transmission_channel`
today. No migration, no serializer change, no API surface in this increment.

## 4. Field contract

Stamped by `orchestrator._build_row` and listed in `_MAP_THEMES_COLUMNS` (the typed-empty
schema, so a zero-candidate day writes the same column set). One exception, marked in the
table: **`channel_config_version` is stamped frame-wide by the driver, not by
`_build_row`** — the token depends on the run's model, which only the driver knows under
`map_themes(model=...)`, and a per-row builder reading `DEFAULT_MODEL` would contradict the
freeze token stamped beside it on the same rows.

| Column | Type | Vocabulary / meaning |
|---|---|---|
| `channel_status` | str | `verified` / `partial` / `unverified` / `not_assessed`. `not_assessed` is a Python-only sentinel for a proposal dropped by the bracket before assessment — it is never in the LLM schema. |
| `channel_type` | str | `customer_demand`, `supplier_input`, `input_cost`, `regulatory`, `substitution`, `capacity_supply`, `financing_ma`, `category_attention`, `none`. Forced to `none` when status is `unverified`/`not_assessed`; an off-vocabulary value is coerced to `none` and logged at WARNING. |
| `channel_text` | str | The chain the model wrote. `""` when there is no chain. |
| `channel_evidence` | str | The fact **in the event** the chain rests on. This is what makes a fabricated channel checkable after the fact. |
| `channel_falsifier` | str | The single observable that would show the chain is not real. |
| `channel_confidence` | float | `[0,1]`, the assessor's own confidence. NaN when not assessed. Distinct from `llm_confidence`, which stays the stage-A proposer's number. |
| `channel_vote_k` | Int64 | Draws requested (`_ASSESS_VOTES`). Persisted so a later change of k is visible in the data, not only in the config token. |
| `channel_vote_valid_n` | Int64 | Draws that parsed and entered the median. |
| `channel_vote_dispersion` | Int64 | Ordinal spread over valid draws; 0 = unanimous. The per-row instrument-noise readout the retro's instrument qualification makes mandatory. |
| `channel_assessment_outcome` | str | `success` / `empty_payload` / `malformed_payload` / `truncated` / `call_failed` / `not_assessed` / `over_assess_cap`. Mirrors `MapperOutcome` so "the assessor said unverified" is never conflated with "the assessor call died". `truncated` is a generation the provider cut at the output budget (`finish_reason` `MAX_TOKENS`), read from the response rather than inferred from the body — a clipped draw arrives either empty or as partial JSON, and both would otherwise masquerade as a different failure. `over_assess_cap` is an in-bracket candidate ranked below the per-theme assessment cap; it shares the `not_assessed` STATUS with a bracket drop but keeps its own outcome so the two stay separable in the funnel parquet. |
| `channel_assessed_at` | str | ISO-8601 UTC, or None. |
| `channel_config_version` | str | Canonical-JSON poolability token for the assessment stage. **Stamped frame-wide by the driver**, not by `_build_row`. |
| `shadow_strict_verdict` | str | Theme-level, `keep` / `refuse` (§5). |
| `shadow_strict_verified_n` | Int64 | Theme's ANSWERED candidates with `channel_status == "verified"`. |
| `shadow_strict_assessed_n` | Int64 | Theme's candidates the model actually ANSWERED (in-bracket, assessed, `channel_assessment_outcome == "success"`). Makes the verdict's denominator explicit. |
| `shadow_strict_failed_n` | Int64 | Theme's assessed candidates whose call FAILED. Kept out of `shadow_strict_assessed_n` on purpose: an instrument failure carries `channel_status == "unverified"` by construction, so counting it in the denominator would turn a 429 storm into a healthy-looking "no theme had a channel today". Stamped on the row rather than only in the sidecar, which is written best-effort and can be absent for exactly the run that needs it. |
| `shadow_strict_rule_version` | str | `shadow-strict-any-verified-v1`. |

**Removed:** `transmission_channel` (free text, stamped today at `_build_row` and carried in
`_MAP_THEMES_COLUMNS` / `_PROPOSAL_FUNNEL_COLUMNS`). Its content moves to `channel_text`
with a real status beside it. No alias, no shim.

**New sidecar:** `~/.alphalens/thematic_candidates/theme_decisions/{asof}.parquet`, one row
per theme the driver touched — `asof, theme, catalyst_url, catalyst_event_type,
mapper_outcome, decline_reason, n_proposed, n_in_bracket, n_verified, n_partial,
n_unverified, n_assess_failed, n_over_assess_cap, shadow_strict_verdict, shadow_strict_rule_version,
mapper_config_version, channel_config_version`. Written best-effort, exactly like the
proposal-funnel writer. Without it, a stage-A decline and a no-catalyst skip still leave
zero trace on disk.

**Not widened:** `proposal_shadow/{date}.parquet`. It feeds a pre-registered head-to-head
whose rows are post-mcap by definition; no `channel_*` column may leak into it, and a
regression test pins that.

## 5. The shadow verdict

`shadow_strict_verdict = "refuse"` iff **no** ANSWERED candidate of the theme reached
`channel_status == "verified"`, else `"keep"`. Rule version
`shadow-strict-any-verified-v1`, deliberately **not** part of `channel_config_version`: the
rule can be re-cut offline from `shadow_strict_verified_n` / `shadow_strict_assessed_n`
(e.g. "verified or partial", or `n_verified >= 2`) without invalidating a day's frozen
parquet. It is a poolability key, not a freeze input.

It is stamped on **every row of the theme** and on the theme-decisions sidecar row, so a
"refused" theme now leaves rows that exist, ship, and mature — which is the whole point.

**Read this as a measurement substitution, not a continuation.** `shadow_strict_verdict` is
a *different estimand* from the frozen Stage-1 gate: derived per-candidate, **after** the
mcap bracket, from a differently-worded prompt, over a candidate set produced by a
permissive proposer. The frozen gate judged **themes**, pre-bracket, by majority-of-5 on
the strict prompt. A forward result under the new rule cannot be pooled with the retro and
must never be presented as its continuation.

## 6. Version wiring and what resets

`theme_mapper.mapper_config_version` gains a **required** keyword `channel_config_version`,
placed in the payload under key `"channel"`. `theme_mapper` does not import
`channel_assessor` (the frozen-instrument snapshot in §9 must stay clean); the orchestrator
composes the token at both call sites — `map_themes` and, importantly, also
`write_empty_candidates`. Without the composition, an assessment-prompt edit would leave
the freeze token unchanged and the 6x/day rerun would serve stale channel fields from a
parquet produced under different rules.

What resets, stated plainly rather than discovered later:

* **The frozen candidate cohort.** Every past date's parquet stops matching the new token,
  so a rerun recomputes instead of reusing. Existing rows are never restamped (ADR 0013 R3).
* **The ISO 40-42 forward window is superseded** (§8).
* **The proposal-shadow head-to-head restarts BOTH arms, for the second time** after the
  2026-08-03 reset. `mapper_config_version` is stamped on the mechanical rows too, and the
  mechanical arm's theme population is a function of which themes the LLM proposed for.
* **Both golden map recordings miss by design** (cassette key = sha256 over the full
  request descriptor). Each needs a new version directory, a live re-record and a written
  diff memo. Note for that memo: with k=3 the three identical stage-B requests collapse to
  one cassette key, so the replayed dispersion is always 0 — **the golden proves nothing
  about vote stability**.
* **The retro replay script** (`stage1_retro_label_pairs.py`) stops matching the live
  mapper and is repointed at the frozen snapshot module (§9).

## 7. What is explicitly NOT changed

* **No sentiment filter and no bearish-event-type filter, anywhere.** Deleting stage-A
  drop-test (b) removes the only direction filter that existed; nothing replaces it. A
  `channel_direction` annotation in {favourable, adverse, ambiguous} was considered and
  **left out on purpose**: long-only geometry is out of scope, and an adverse/favourable
  column is the single field most likely to be quietly turned into a filter later. Recorded
  here as a decision, not an oversight.
* **The mcap bracket stays a deterministic post-hoc Python filter** with the same
  500M-10B default. No size, P/E or volume token enters any prompt, stage A or stage B.
* **Verification (press gates) is untouched** — `_verify_candidates_for_theme`,
  `_MAX_CANDIDATES_PER_THEME = 3`, `_MAX_VERIFY_ATTEMPTS_PER_THEME = 5` all keep their
  current behaviour and cap. The shadow verdict deliberately does not read them.
* **Selection, ordering and the brief sort are untouched.** No `channel_*` column may
  appear in `orchestrator` sort keys, `scorer.compose_weighted_score` /
  `selection_score`, or `_BRIEF_SORT_KEYS`. A structural anti-rot test (modelled on
  `tests/test_no_market_state_in_selection.py`, planted positive control included) is the
  defence; weakening it is a defect.
* **Display is unchanged in this increment.** The channel columns stay parquet-only; the
  card gains nothing. That is deliberate: under the unvalidated-display doctrine, "no
  verified channel" may later be shown only as a neutral descriptive annotation — never a
  verdict word, never an authority colour, never a sort key. A display PR is a separate
  decision.
* **The argumentation-layer prompt defect is a separate known issue.** The brief generator
  still assumes a direction for its subject and does not read
  `transmission_channel`/`channel_*`; that is epic #974 / the Stage-1 deploy observation
  window's own follow-up, and it is not fixed here.

## 8. The ISO 40-42 window, and why superseding it is the right trade

The verdict window "ISO week 40 to 42, i.e. 2026-09-28 to 2026-10-18" is defined in
`theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md` §13.4 item 3 as
deploy (2026-08-03) + 8-10 fresh ISO weeks. Bumping `mapper_config_version` restarts that
counter again, so the window as dated no longer exists.

That trade is worth taking because the window could not have delivered the thing it was
being held as collateral for:

* **It was underpowered.** Post-deploy volume ran ~3-4 candidates/day ≈ 1.0-1.2 theme-event
  pairs/day; the retro's own power memo puts an ISO 42 read at **~0.53 power** for the full
  observed effect magnitude and **~0.22** for half of it, and puts an 80%-power read no
  earlier than ≈ ISO 2027-W07.
* **It could not compute a KEPT-vs-REFUSED contrast at all.** A refused theme post-deploy
  produces no brief row and no ladder entry, so the refused leg does not exist in live
  data. The retro memo says this in its own instrument caveat: without refusal
  shadow-tracking, "the ISO 40-42 read is not a KEPT-vs-REFUSED test at all".
* **The retro removed the reason to spend on it.** Its recommendation was explicitly *do
  not pre-register a forward-window extension on this study's account*, and to treat any
  future KEPT-vs-REFUSED forward test as a new design **requiring refusal shadow-tracking
  infrastructure first**. Move 3 is that infrastructure.

The replacement forward experiment is pre-registered in
`docs/research/channel_feature_forward_prereg_2026_08_19.md` before any post-boundary row
matures.

## 9. Frozen Stage-1 instrument snapshot (required)

`apps/alphalens-research/scripts/stage1_retro_label_pairs.py` hard-asserts
`theme_mapper.mapper_config_version(...) == FROZEN_MCV` and calls the live
`propose_candidates`. The stage-A rewrite breaks it at its first line of work.

`apps/alphalens-research/alphalens_research/retrospective_audit/stage1_frozen_v2.py` holds
a **byte copy** of the v2 prompt template, response schema and sampling constants, plus
`frozen_mapper_config_version(...)` reproducing `FROZEN_MCV` exactly and
`propose_candidates_frozen(...)` with the v2 normaliser (channel required, channel-less
dropped). The retro script points there instead. Two payoffs: the pre-registered instrument
stays replayable after the live prompt moves, and the module is the ready-made champion arm
if a champion/challenger is ever wanted (§10).

## 10. Rejected alternatives

* **Keep the hard gate.** Rejected. The one pre-registered test of it came back inverted
  (Δ = −0.0715, p = 0.945) and the slot is spent; the crowd-out measurement shows the gate
  does not merely filter, it relocates the candidate set out of the shippable universe
  (96.0% absent); the literature says the low-visibility links a channel test refuses are
  where the drift is larger. Keeping it also keeps the reject-inference hole: refusals
  leave no labels, so the gate can never be evaluated on live data.
* **Tune the gate threshold** (accept `partial` as a pass, require k≥2 verified, per-theme
  quotas). Rejected as the same instrument with a knob. It re-runs the estimand whose slot
  is already spent, invites threshold-shopping against the same cohort, and still destroys
  the labels of everything below the line. The threshold question is fully preserved
  **offline** instead: `shadow_strict_verified_n` / `shadow_strict_assessed_n` /
  `channel_status` per row let any threshold be re-derived without a new LLM call, under a
  new rule version.
* **Run the frozen strict prompt as a second per-theme shadow call (true champion arm).**
  Deferred, not rejected. It would put the retro's exact keep/refuse label beside the
  derived one on live data, at ~10 themes/day × ~$0.014 ≈ **$0.14/day (~$4/mo)** at one
  draw, or ~**$0.70/day (~$21/mo)** to reproduce majority-of-5 honestly. Build it only if
  (a) the derived shadow verdict and the frozen gate are later found to disagree materially
  on the same themes, or (b) someone wants to claim continuity with the retro cohort. Its
  prerequisite — the snapshot module in §9 — is being built anyway, so deferring costs
  nothing.
* **Assess before the bracket.** Rejected on cost and relevance: the bracket is the largest
  sink (17 of 19 dropped on 2026-08-05) and off-bracket names can never produce a ladder
  outcome. They keep a funnel row marked `not_assessed`, so nothing disappears.
* **Assess only the top-N candidates per theme by stage-A confidence.** Rejected for v1
  (kept in the back pocket if in-bracket volume overshoots). It would make the shadow
  verdict a function of the verification loop's cap, coupling two layers ADR 0013 keeps
  apart.
* **Batch all of a theme's candidates into one assessment call.** Rejected for v1. It is
  ~10x cheaper but puts candidates in one context (cross-contamination) and risks the model
  silently returning fewer entries than it was given, which would break the
  one-assessment-per-candidate invariant.

## 11. Cost

DeepSeek v4-pro via OpenRouter, post-promo ($1.74/M in, $3.48/M out, reasoning charged as
output). Only the **first** of the 6 daily slots pays; the other five reuse the frozen
parquet.

| Stage | Unit cost | Volume/day | Cost/day | Cost/month |
|---|---|---|---|---|
| A — proposal (~4k in + ~2k out) | ~$0.014/call | ~10 themes (unchanged) | ~$0.14 | ~$4.2 |
| B — assessment, k=1 (~1.5k in + ~600 out) | ~$0.0047/call | ~30-50 in-bracket | ~$0.18 | ~$5.5 |
| B — assessment, k=3 (**chosen**) | ~$0.014/candidate | ~30-50 in-bracket | ~$0.55 | ~$17 |

Chosen total: **~$21/mo added**, about **+35%** on the ~$58/mo project run rate. k=3 is not
a comfort choice: the retro's instrument qualification measured mixed votes on 91 of 238
pairs, so a single draw is not a measurement, and `channel_vote_dispersion` is the only
per-row readout of that noise. If the cost must be cut, the order is k=3 → k=1 (saves
~$11/mo, loses the dispersion readout), then per-theme batching (~$1.5/mo, at the quality
cost above).

The in-bracket share is the real uncertainty: 2/19 (~10%) was observed on 2026-08-05 under
the mega-cap-skewed Stage-1 prompt, but the crowd-out repair targets smaller names on
purpose. If it works and volume lands at 50-70/day, stage B is nearer ~$25/mo.

## 12. Risks to watch after deploy

* **Silent re-introduction of the gate** — the largest long-term risk. A later change that
  reads `channel_status` in a filter, sort key or score recreates exactly what the retro
  rejected, without a new pre-registration. The structural test is the only defence.
* **Everything drifts to `partial`** because a legal middle answer is comfortable.
  `channel_evidence` + `channel_falsifier` make it auditable, `channel_vote_dispersion`
  makes it measurable. A status mix with almost no `unverified` is a prompt defect, not a
  good news day. Plan a manual read of ~30 `partial` rows in the first two weeks before
  trusting the shadow verdict.
* **Stage A overshoots into thin names** and moves the bottleneck to yfinance: the bracket
  does one mcap lookup per proposal, and the 2026-07-25 rate-limit incident cost a whole
  day's briefs. Watch the `NO_MCAP` share in the funnel parquet and the named-dropped log
  line on the first days.
* **Latency.** REVISED — see §15. The original estimate ("a few seconds each, ~5-15
  minutes") was contradicted by the acceptance probe's own measurement (median 899
  completion tokens, 787 of them reasoning), and the token-cap fix makes each call slower
  still. The stage is now capped per theme AND fanned out 3-wide, and
  `TimeoutStartSec` went 75 → 110min.
* **An unverified row is now shippable and the UI says nothing about it.** Accepted for
  this increment, stated here so it is a decision rather than a leak; a display PR must not
  render it as an unlabelled quality claim.
* **Golden re-record is a reviewed operation**, needs a live key and a diff memo, and is its
  own step in the PR — not a CI-green fix.

## 13. Build order (TDD, red first at every step)

1. `channel_assessor` — vocabulary constants, prompt renders the untrusted fence, prompt
   carries no mcap token, schema shape, `unverified` parses as SUCCESS, an off-vocabulary
   status invalidates only that draw, ordinal median + dispersion arithmetic, CALL_FAILED →
   unverified + CALL_FAILED, `assess_candidates` length/order invariant,
   `channel_config_version` stability plus a positive control that a prompt edit moves it.
2. `theme_mapper` — prompt no longer requires a channel; prompt does not decline for weak
   linkage; prompt invites less-visible companies; `decline_reason` enum; a channel-less
   candidate **survives** `_normalize`; freeze tag v3; `mapper_config_version` carries the
   channel token.
3. `orchestrator` — assessment never changes row count (including an all-unverified theme
   and a total assessor outage); `_build_row` stamps the 16 fields; off-bracket funnel rows
   read `not_assessed`; shadow verdict = refuse iff zero verified; theme-decisions sidecar;
   the new per-theme channel log line, with the existing funnel line unchanged; the
   structural guard that no `channel_*` column reaches any sort key or score input.
4. Observability — `channel_{verified,partial,unverified,assess_failed}` and
   `shadow_{refused,kept}` gauges, emitted unconditionally (0 on a quiet day and on a
   frozen-set reuse), folded into the single `_emit_stage_volume` textfile; alert rules read
   a window, never an instant vector.
5. Golden re-record for both map fixtures + provenance memo; retro script repointed at the
   frozen snapshot module.

All new and edited research tests subclass `unittest.TestCase` — the runner is
`unittest discover`, and pytest-style tests are silently skipped.

## 14. Acceptance probe (2026-08-19) — PASS

An engineering gate run on this branch before deploy, **not** a hypothesis test and **not**
evidence about returns. The clean design-holdout was consumed by PR #1065 and this design
was written in reaction to that result, so no number below is joined to
`market_excess_return`, `population_ladders` or any outcome store, and no such join was
computed. The only questions asked: does the machine propose what it was built to propose,
does the assessment annotate instead of delete, do the two anchors behave.

Setup: worktree build at `1a29fe95` (`mapper-freeze-v3`), the retro's own provider pin
(`deepseek/deepseek-v4-pro`, order `Alibaba`, `fp8`, `allow_fallbacks: false`) — purity
186/186 calls — 3 threads, exponential backoff, an empty or malformed body retried as a
failure. Inputs: the frozen `stage1_retro/inputs.parquet` (238 theme-event pairs). Sample:
40 themes drawn uniformly without replacement (`random.Random(20260819)`), one pair per
theme ⇒ **40 pairs / 127 brief rows**, DEV 26 / CLEAN 14. k=1 on the sample, k=3 on the
anchors. Real production path throughout: `propose_candidates` →
`mcap_filter.classify_by_mcap` (PIT, `asof` = the pair's `brief_date`) →
`channel_assessor.assess_candidate` / `shadow_strict_verdict`.

**Crowd-out repair (the primary number).** Brief rows whose ORIGINAL small/mid-cap ticker
appears in the new proposal set: **27 / 94 = 28.7%** on the denominator comparable to the
retro's kept-theme rows, against the frozen gate's **14 / 348 = 4.0%** — a 7.2× recovery
(21.3% on all 127 sampled rows; 10 of 40 themes recover at least one). **All 22 recovered
pair×ticker names were also in bracket**, so the recovery is not an artifact of naming
companies the mcap filter then discards. None of the 22 is `verified`: under a strict
any-verified gate every one would be deleted again, which is the design argument restated
as data. Comparability caveat: the retro labelled by majority-of-5, this probe used a
single draw; single-draw noise cannot move 4% to 29%, but the figure is not a point
estimate of a production rate.

**Volume.** 295 stage-A proposals = 7.4 per sampled theme (9.5 per proposing theme, median
10, cap 15). Bracket: `in_bracket` 84 (28.5%), `too_big` 162, `no_mcap` 27, `too_small` 22
⇒ 2.1 in-bracket per theme. Stage-A outcomes 31 `success` / 9 `declined` / 0 unresolved;
every decline used the narrowed enum (`no_event` 9/9) and eight of the nine are `EX-99.1`
filler, listicles, an "SA Asks" Q&A or a law-firm solicitation. The ninth (`press_freedom`)
is a real event whose first draw truncated — one manual look after deploy.

**Channel distribution (84 in-bracket candidates, k=1).** `verified` 5 (6.0%), `partial` 42
(50.0%), `unverified` as an answer 34 (40.5%), `unverified` carrying a call failure 3
(3.6%). Types: `none` 37, `category_attention` 16, `customer_demand` 16, `substitution` 5,
`supplier_input` 5, `financing_ma` 3, `regulatory` 1, `input_cost` 1. Theme-level shadow
verdict **3 keep / 22 refuse** over the 25 themes with an in-bracket candidate. Against the
retro's 68.8% `no_channel` refusal share this is the healthy reading: the assessment still
discriminates at least as hard as the gate did — it simply no longer deletes. 84 candidates
in, 84 annotated rows out, order preserved.

**Anchors (k=3).** `harassment_ebay_settlement`: stage A gave one
`declined:not_business_development` and two proposal draws (7 and 10 names, all marketplace
or litigation-services companies — no ride-hailing name in any draw, so the theme-tag guard
held); stage B returned 0 verified / 3 partial / 4 unverified, shadow `refuse` (0 of 7),
every candidate kept. `us_ukraine_relations_patriot`: three proposal draws, all `success`
(9 / 10 / 5 names); stage B returned 0 verified / 7 partial / 2 unverified, shadow `refuse`
(0 of 9), every candidate kept. Both anchors PASS — no deletion, and no fabricated
`verified` channel on either.

**Defect found — the stage-B token cap is below this model's reasoning burn.**
`_ASSESS_MAX_OUTPUT_TOKENS = 1500` is charged against reasoning tokens as well. Over 89
stage-B calls: median completion 899, median reasoning 787, and **8 calls (9.0%) returned
an EMPTY body at exactly `completion_tokens = 1501` / `reasoning_tokens = 1500`**; two
anchor calls truncated mid-JSON. After the single re-roll, 3 of 84 candidates (3.6%) still
ended `unverified` + `empty_payload`. Because such a row is a non-verified row inside
`shadow_strict_assessed_n`, an assessor that is merely thinking too long biases the shadow
verdict toward `refuse`. Fix before the first frozen day (both are
`channel_config_version` inputs, so retrofitting later is far more expensive): raise the
cap above the observed reasoning tail (≥4000) and classify a `length` finish reason as its
own outcome instead of letting it arrive as `empty_payload`.

Cost actually spent: **$1.07** over 186 calls (stage A $0.41 / stage B $0.40 / anchors
$0.26), summed from the OpenRouter `usage.cost` field rather than estimated.

Full report and per-call provenance:
`scratchpad/channel_feature/acceptance.md` (session scratch, not versioned).


## 15. Review-fix increment (2026-08-19, post-probe)

Three reviewers read the branch after the acceptance probe. Everything below landed as
separate commits on the same PR, **before** the first frozen day, because each item is
either a `channel_config_version` input (retrofitting after the cohort opens ends the
accrual window a second time) or a production-stall risk.

| Finding | Fix |
|---|---|
| The 1500-token cap sits below the model's reasoning burn (probe: 9.0% of stage-B calls returned an empty body at exactly completion 1501 / reasoning 1500) | `_ASSESS_MAX_OUTPUT_TOKENS` 1500 → 4000. |
| A clipped generation was invisible to the parser: it arrived either empty (`EMPTY_PAYLOAD`, re-rolled at the same budget) or as partial JSON (`MALFORMED_PAYLOAD`, not re-rolled at all) | `finish_reason` is threaded from the response into `_parse_draw`; `MAX_TOKENS` becomes `AssessmentOutcome.TRUNCATED`, which outranks the body and is re-rolled once. |
| A 4000-token reasoning draw exceeds the client's 60s read timeout, which would convert truncation failures into socket timeouts | `openrouter_client._DEFAULT_TIMEOUT` read 60 → 180s. NOT a config-version input, so it stays tunable later. |
| `_aggregate` resolved an EVEN vote set to the LOWER median silently, and `valid_n` is not `k` whenever a draw is lost — so a `{verified, unverified}` disagreement was placed in the primary's leg U by an untested tie-break | Pre-committed rule: two disagreeing central ordinals resolve to `partial`, which the pre-registration excludes from both legs. Written into the forward pre-reg §3 and pinned by `TestEvenVoteTieBreak`. |
| `shadow_strict_verdict` counted instrument failures in its denominator, so an outage read as "no theme had a channel today" with a healthy-looking non-zero `shadow_strict_assessed_n` | The denominator counts ANSWERED candidates only; failures are reported as the new `shadow_strict_failed_n` column, stamped on every row (the sidecar that used to hold the count is best-effort and can be absent for exactly the run that needs it). |
| Stage B was sequential, unbounded and unthrottled — worst case 10 themes × 15 in-bracket × 3 votes = 450 calls against `TimeoutStartSec`, and `map_themes` writes its parquet ONCE after the theme loop, so a SIGTERM leaves no file and the next slot restarts from zero | Capped at `_MAX_ASSESS_PER_THEME = _MAX_VERIFY_ATTEMPTS_PER_THEME` (5) — the candidates that can still ship — and fanned out 3-wide per theme via `ThreadPoolExecutor` (`Executor.map` preserves input order, so the positional zip contract is untouched). Candidates past the cap are annotated `over_assess_cap`, never dropped, and the truncation is logged at WARNING plus recorded per theme as `n_over_assess_cap`. `TimeoutStartSec` 75 → 110min. |
| The six new Prometheus gauges had no alert rule anywhere | `AlphalensThematicChannelAssessFailureHigh` (failure share > 1/3 over ≥5 assessed, `for: 6h`) plus an `AlphalensThematicChannelGaugesMissing` companion. Live rules are hand-synced on the VPS — the repo edit alone does not arm them. |
| `_PROPOSAL_FUNNEL_COLUMNS` carried `channel_status` without the outcome, so a row whose assessment DIED was byte-identical to a row the model answered `unverified` — in the one file a later recall audit reads | Added `channel_assessment_outcome` and `channel_vote_valid_n` to the funnel. |
| The crowd-out repair was measured only at PROPOSAL level, while the ship path still truncates at 5 attempts / 3 rows in stage-A confidence order | The pre-reg's descriptive 3 now has a ship-level arm (share of brief rows whose ticker was not named in the event; mcap-decile distribution of shipped rows) and an instruction to watch `n_in_bracket` / `n_over_assess_cap` against the cap. Selection itself is deliberately unchanged. |
| A missing `decline_reason` fired the same prompt-drift WARNING as a genuine off-enum value | Absent coerces to `no_event` quietly; only a non-empty off-enum value warns. Both tests now assert the coercion TARGET, not merely enum membership. |

Test-side fixes in the same pass: the `valid_n == 2` cases, an `over_assess_cap` case, the
`not_assessed` shadow-filter case, `status_counts` value assertions, the frozen-v2 error
ladder (empty → re-roll, malformed not re-rolled, raising call, client-init failure), the
six gauge counters asserted against a REAL theme loop and against the REAL frozen-reuse
branch (the CLI test patches `map_themes` wholesale and can only exercise its own default),
and the empty-day token test rewritten to pin the wiring rather than a column's presence.
