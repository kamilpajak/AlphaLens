# Grounding detection and prose honesty — design memo

**Status:** LOCKED
**Date:** 2026-08-20
**Author:** Kamil Pajak
**Branch:** `feature/grounding-and-prose-honesty`
**Builds on:** `docs/research/channel_as_feature_design_2026_08_19.md` (LOCKED, PR #1066,
merged `e3952306`), `docs/research/stage1_retro_gate_increment_results_2026_08_19.md` (PR #1065)
**Amends:** `docs/research/channel_feature_forward_prereg_2026_08_19.md` — Amendment 1,
committed with this memo
**Related:** EPIC #974 (event grounding), issue #976 (extractor `ARTICLE -> EVENT_SET`),
ADR 0013 (trade-side layer separation)

---

## 1. Where this starts

PR #1066 replaced the hard transmission-channel gate with a two-stage design. Stage A
(`theme_mapper.propose_candidates`) proposes permissively; a deterministic market-cap bracket
runs in Python; stage B (`thematic/mapping/channel_assessor.py`) assesses each surviving
candidate and returns a channel status plus type, evidence and falsifier — and **never removes
a candidate**. A derived per-theme shadow verdict records what the old strict gate would have
done.

The gate was removed because a pre-registered retrospective measured it and found it
**inverted**: pair-cluster delta **-0.0715** against realised benchmark-excess returns,
one-sided **p = 0.945**, CI95 **[-0.159, +0.017]**, 38 vs 49 pairs, with **96.0%**
`KEPT_TICKER_ABSENT` crowd-out of the original small/mid-cap tickers on kept themes. The
operational reading is the one that governs everything below:

> "The model cannot articulate a transmission channel" is **not** evidence that the candidate
> is bad. If anything the sign runs the other way, which is consistent with the literature
> that price adjustment is slowest for links that are least visible.

The first live run of the new design, 2026-08-19 22:46 UTC: 10 themes, 102 proposals, 13 in
bracket, 13 assessed (1 `verified` / 8 `partial` / 4 `unverified`), 12 briefs, 0 assessment
failures.

That run also exposed three defects. This memo fixes them.

## 2. The three defects

### 2.1 Two different epistemic conditions are conflated

Today `unverified` carries both of:

* **Honest uncertainty** — "we found a real event about this theme, this company is plausibly
  in scope, but no company-specific cash-flow mechanism was established." This is a normal,
  expected, informative answer and is precisely the population the whole channel-as-feature
  redesign exists to keep in the data.
* **A pipeline defect** — "this company was attached to a story it has nothing to do with."
  This is a bug ticket against a named upstream layer, and it is worth exactly nothing as a
  measurement of anything.

**Evidence from the first live run.** Theme `consumer_sentiment` was driven by a Yahoo Finance
daily market round-up (`event_type=macro`, no company named) and produced only semiconductor
and networking names: NVDA, AMD, INTC, CSCO, JNPR. All 10 proposals happened to fall out on
the market-cap bracket, so nothing shipped that day — but nothing in the design prevented them
from shipping. The mechanism is live; only the bracket happened to be in the way.

This is the same defect class as EPIC #974 stage 2 (issue #976, `ARTICLE -> EVENT_SET`): one
article carries several unrelated events, and entities from one story get attached to a theme
from another. Today it renders as `unverified`, which files a bug inside an uncertainty
category and makes both unreadable.

### 2.2 The vocabulary overclaims

`verified` is not defensible. The verdict comes from a second LLM call over the same rendered
event text — not from an independent source, not from a document fetch, not from a
cross-check. Calling that "verified" imports an authority the instrument does not have, and it
is the word most likely to be read as a recommendation by the one person the tool serves.

The same objection applies to `partial` and `unverified`: they describe a *degree of
verification*, which is not what is being measured. What is being measured is **how well the
event text supports a causal mechanism**.

### 2.3 The prose contradicts the record

The argumentation layer
(`apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/`) still prompts for
"1 sentence thesis why this ticker benefits from the theme", and for "1-2 short paragraphs
explaining the second-order benefit mechanism". The benefit is **presupposed by the
instruction**: the model is not asked *whether*, only *why*. The channel status is attached
afterwards, in a parquet column that the Django ingest drops by design.

So the operator's actual experience today is: **the prose is the only channel-related thing
they ever see, and it is the one artefact that never saw the channel record.** A row whose
assessor answered `unverified` still ships a fluent sentence explaining why the company
benefits.

**The human-factors evidence** (Perplexity literature review, 2026-08-20; treated as
motivation, not as an oracle — project doctrine):

* **Bansal et al., CHI 2021** — "Does the whole exceed its parts? The effect of AI explanations
  on complementary team performance." Adding explanations raised the rate at which people
  accepted the system's recommendation **whether or not the recommendation was correct**. A
  fluent explanation increases belief roughly independently of accuracy.
* **Steyvers et al., Nature Machine Intelligence, 2025** — "What large language models know and
  what people think they know." There is a measurable calibration gap between a model's internal
  uncertainty and users' perceived accuracy, and the intervention that narrows it is **aligning
  the explanation's own hedging with the model's uncertainty** — not attaching a label or a
  number beside otherwise-confident text.
* **Hazard-control hierarchy** (the standard occupational-safety ordering: elimination,
  substitution, engineering controls, administrative controls/warnings, PPE). Warnings sit near
  the bottom because they depend on the person reading and heeding them. Designing the defect
  out ranks above warning about it.

Applied here, all three say the same thing: a badge next to confident prose is an
administrative control. The engineering control is to **generate the prose from the structured
record** and to make the unsupported shape mechanically impossible to render.

## 3. The decision

Two contracts, one increment, one cohort restart.

* **Contract A — the assessor.** Rename the status vocabulary to a causal-support taxonomy with
  written operational definitions, and add an orthogonal grounding condition answered in the
  same stage-B call, before the causal grade.
* **Contract B — the prose.** Project the channel record into the brief facts block, replace
  the benefit-presupposing instructions with one prose shape per support level, and add a
  machine-enforced guard that a `not_established` or non-grounded record cannot render an
  unqualified benefit verb.

Both land together. The reason they cannot be split is in §6: each moves
`channel_config_version`, and each split costs another cohort restart.

## 4. Contract A — the assessor

### 4.1 Causal support: an ordinal scale about the world

`CHANNEL_STATUSES = ("verified", "partial", "unverified")` becomes
`CHANNEL_SUPPORT_LEVELS = ("established", "suggestive", "not_established")`. Order-preserving,
1:1, ordinal codes 0/1/2 unchanged, so the median rule and the pre-committed even-vote
tie-break survive verbatim.

Operational definitions — the prompt text and the docstring must match word for word, because
the prompt **is** the definition:

* **`established`** — a named mechanism **plus company-specific evidence present in the rendered
  event**: the event states a fact about this company, a named counterparty of it, its product
  line or its market, and every link of the chain (event fact -> what changes and for whom ->
  which line of this company's economics moves, and roughly when) rests on something the event
  states or directly implies. No link is supplied from the model's own background knowledge.
* **`suggestive`** — a mechanism is named and plausible, but at least one link rests on a fact
  the event does not state (the model believes this company supplies a named party and the
  event never says so), **or** the link is category-level rather than company-specific (the
  event moves the category this company sells into, without naming a buyer, payer, contract,
  regulation, input price or competitor).
* **`not_established`** — no concrete company-specific cash-flow path from this event to this
  company was found. **A normal, expected, unpenalised answer.** Explicitly not a claim that the
  company is a bad candidate, not a claim that no path exists, and not a return forecast.

One sentence is single-sourced as `CAUSAL_SUPPORT_NOT_A_FORECAST` and used verbatim in the
prompt and exported for the prose layer and the card:

> "Causal support describes how well the event text supports a mechanism; it is not a forecast
> of the share price."

The existing "nothing is dropped on your answer" paragraph stays in the prompt.

`not_assessed` is unchanged in name and meaning — a Python-only sentinel never offered to the
model.

**Old to new map**, order-preserving and 1:1: `verified -> established`,
`partial -> suggestive`, `unverified -> not_established`. **This is a reading aid for old
parquets, not a pooling licence.** The definitions are reworded, the not-a-forecast line is
new, and the grounding questions now precede the grade — the instrument changed, and the
2026-08-19 rows must never be merged with the new cohort.

### 4.2 Grounding: an orthogonal validity condition

`CHANNEL_GROUNDING_STATUSES = ("grounded", "theme_misroute", "candidate_misfit")`
(model-emittable), plus `GROUNDING_UNKNOWN = "unknown"` (asked, no valid draw) and the reused
`NOT_ASSESSED` (never asked).

* **`grounded`** — the event text concerns the theme it was routed under, **and** it places this
  company, its product line or its market inside the event's scope. A quotable span exists.
* **`theme_misroute`** — the event text does not concern the theme. A pipeline defect,
  attributable to extraction (`normalize_extraction`'s `themes`) or to `catalyst_resolver`
  picking one event out of a multi-event article. **Candidate-independent**: every candidate of
  that theme should answer the same, and disagreement is itself a readout.
* **`candidate_misfit`** — the event concerns the theme, but this company's business has no
  relationship to the event's subject matter at all. A stage-A defect. **Not** to be used merely
  because no mechanism could be established — the prompt must say so in those words, because
  that exact confusion is the defect being fixed.
* **`unknown` / `not_assessed`** — instrument failure / never asked. Out of every grounding
  numerator **and** denominator, exactly as instrument failures are excluded from
  `shadow_strict_assessed_n`.

**Why its own column rather than a fourth status value:**

1. **An ordinal scale's levels must be mutually exclusive and ordered.** A misrouted event can
   still elicit a confident chain, so `theme_misroute` is not below `not_established` on the same
   axis — it is a statement about whether the axis applies at all. Splicing it in would corrupt
   the ordinal median and would silently redefine the pre-registered legs.
2. **Folding it in destroys the fabrication readout.** The most informative cell is
   (`established` x `theme_misroute`) — a fluent chain built on an event that is not about the
   theme. As one enum that row renders as a single value and the fabrication becomes invisible.
3. **Attribution.** Support level is a measurement about the world, owned by the assessor. A
   grounding failure is a defect ticket against a named upstream layer. ADR 0013 keeps layers
   separable; one column carrying both makes the failure unattributable.
4. **Measurement.** Two columns give the 3x3 cross-tab that is the actual diagnostic:
   (`grounded` x `not_established`) = honest uncertainty, the population this design exists to
   keep; (`grounded` x `established`) = the working case; (`theme_misroute` x anything) = an
   upstream bug; (`candidate_misfit` x `not_established`) = a stage-A bug. One column collapses
   all four into the bucket that is today's defect.
5. **Independence of the pre-registered test.** Keeping grounding out of the support column
   means the primary's legs are defined on the same axis as before; the amendment only adds a
   pre-committed exclusion of non-grounded rows, mirroring the existing exclusion of instrument
   failures.

### 4.3 Where grounding is computed, and the three new fields

Three additional **required** fields on the **existing** stage-B call
(`_ASSESS_RESPONSE_SCHEMA` + `_ASSESS_PROMPT_TEMPLATE`), answered **before** the causal grade
and emitted first in the output object. No new LLM call, no new module, no new version token
beyond `channel_config_version`.

Path: `assess_candidate -> _draw_once -> _parse_draw` (parses and validates the new fields per
draw) `-> _aggregate` (plurality over valid draws) `-> row_fields ->`
`orchestrator._build_row` (candidates parquet) and `orchestrator._funnel_row` (proposal
funnel). The per-theme tally is `channel_assessor.status_counts`, consumed by
`_assess_channels_for_theme` (log lines), `_decision_for` (theme-decisions sidecar),
`_channel_counts` (`df.attrs`) and `alphalens_cli/commands/thematic.py::_map_themes_outcome_metrics`
(Prometheus).

1. **`grounding_status`** — the enum above. The two failure values are what separate the two
   conditions of §2.1.
2. **`grounding_quote`** — the **verbatim** span of the **rendered event** that places this
   company (or its product line, or its market) in scope. Empty string unless `grounded`.
   Verbatim rather than paraphrase, so a deterministic Python substring check is meaningful.
3. **`grounding_reason`** — one clause naming what the event **is** about versus what the theme
   claims, or why the company is unrelated. Empty string when `grounded`. Without it a misroute
   leaves no readable why: `channel_text` and `channel_evidence` are already forced empty on a
   non-established row, so the record would otherwise be a bare enum.

**Deterministic post-check, Python not LLM:** `channel_grounding_quote_verbatim` (bool) is a
whitespace-normalised, casefolded substring test of the quote against the rendered untrusted
block. It **never overwrites `grounding_status`** — detect, stamp, keep, measure applies at the
field level too. It is the only mechanical defence against a fabricated citation.

**Aggregation, pre-committed** (and mirrored into the pre-registration amendment):
`grounding_status` is categorical, so no median. **Plurality over valid draws, tie precedence
`grounded` > `theme_misroute` > `candidate_misfit`.** A split vote therefore never manufactures
a defect; when every draw claims a defect but they disagree, the candidate-independent value
wins because an operator can verify it once per theme instead of once per row.
`channel_grounding_agree_n` (draws equal to the aggregate) is the per-row noise readout, the
categorical counterpart of `channel_support_dispersion`. The quote and reason are taken from
the **first valid draw whose `grounding_status` equals the aggregate** — deterministic given
draw order, the same rule `_aggregate` already uses for `chosen`.

**Draw validity is all-or-nothing.** An off-vocabulary `grounding_status` invalidates **that
draw**, exactly like an off-vocabulary support status; it is **not** coerced. `channel_type` is
coerced to `none` because it is telemetry; grounding is a measurement, and coercion would
manufacture either "the pipeline is fine" or "the pipeline is broken" out of noise.
Consequence, pinned by a test: a valid draw always carries both answers, so
`grounding_unknown == assess_failed`.

**Failure and never-asked semantics.** Zero valid draws -> `channel_support_status =
not_established` (the least-claiming answer, unchanged convention) + `channel_grounding_status =
unknown` + the failing `channel_assessment_outcome`. `unknown` exists because grounding has no
least-claiming value: `grounded` would hide a bug and `theme_misroute` would invent one. Never
asked (bracket drop, over the per-theme cap) -> **both** columns read `not_assessed` via the
existing `unassessed()` / `over_assess_cap()` sentinels.

**No cross-normalisation between the two columns.** The existing intra-column rule stays
(`_parse_draw` blanks type/text/evidence/falsifier when the support status is the bottom
level). But a `theme_misroute` row must **not** be forced to `not_established` — see §4.2
point 2. Recording both answers as given, and excluding non-grounded rows from the
pre-registered legs by a pre-committed rule, is the honest handling; overwriting one column
with the other destroys the evidence.

**Known limit, stated as a design fact.** The assessor sees only `theme_tag`, `event_type`,
`published_at`, the headline, `companies_named_in_event` and `extracted_implications` — never
the article body. `grounding_quote` is therefore a span of the **rendered block**, and the
prompt says so. The detector will **under-detect `candidate_misfit`** (a headline rarely
disproves a business relationship) and can **over-call `theme_misroute`** on terse headlines.
The live example works because "Stock market today: ..." is visibly not a `consumer_sentiment`
event from the headline alone.

### 4.4 Field and column changes

`ChannelAssessment`: `status -> support_status`; `dispersion -> support_dispersion`; new
`grounding_status`, `grounding_quote`, `grounding_reason`, `grounding_agree_n`,
`grounding_quote_verbatim`. `_Draw`: `status -> support_status`, plus `grounding_status`,
`grounding_quote`, `grounding_reason`.

Candidates parquet: `channel_status -> channel_support_status`;
`channel_vote_dispersion -> channel_support_dispersion`; new `channel_grounding_status`,
`channel_grounding_quote`, `channel_grounding_reason`, `channel_grounding_agree_n` (Int64),
`channel_grounding_quote_verbatim` (bool). Unchanged: `channel_type`, `channel_text`,
`channel_evidence`, `channel_falsifier`, `channel_confidence`, `channel_vote_k`,
`channel_vote_valid_n`, `channel_assessment_outcome`, `channel_assessed_at`,
`channel_config_version` (still stamped frame-wide by the driver).

`CHANNEL_ROW_COLUMNS` grows 11 -> 16, ordered: `channel_support_status`,
`channel_grounding_status`, `channel_grounding_quote`, `channel_grounding_reason`,
`channel_grounding_agree_n`, `channel_grounding_quote_verbatim`, `channel_type`,
`channel_text`, `channel_evidence`, `channel_falsifier`, `channel_confidence`,
`channel_vote_k`, `channel_vote_valid_n`, `channel_support_dispersion`,
`channel_assessment_outcome`, `channel_assessed_at`.

Shadow: `shadow_strict_verified_n -> shadow_strict_established_n`;
`ShadowVerdict.verified_n -> established_n`; `SHADOW_STRICT_RULE_VERSION`
`shadow-strict-any-verified-v1 -> shadow-strict-any-established-v1`.
`shadow_strict_verdict` / `_assessed_n` / `_failed_n` unchanged in name and meaning.
**Grounding is deliberately not folded into the shadow** — the shadow replays the OLD gate,
which had no grounding concept, and coupling them would change the estimand being shadowed. The
per-theme grounding counts are stamped beside it in the sidecar so any offline re-cut is
possible without new LLM calls.

Proposal funnel: `channel_status -> channel_support_status`, plus `channel_grounding_status`.
Quote, reason and agree_n stay **out** of the funnel — off-bracket rows are never assessed, and
the in-bracket detail lives in the candidates parquet.

Theme-decisions sidecar and `ThemeDecision`: `n_verified -> n_established`,
`n_partial -> n_suggestive`, `n_unverified -> n_not_established`; new `n_grounded`,
`n_theme_misroute`, `n_candidate_misfit`, `n_grounding_unknown`. `n_assess_failed` /
`n_over_assess_cap` unchanged. **No `theme_grounding_verdict` column and no second rule token**
— it is fully re-derivable from these counts, and a theme-level verdict field is the shape most
likely to be turned into a gate later.

`status_counts` returns 8 keys (`established`, `suggestive`, `not_established`,
`assess_failed`, `grounded`, `theme_misroute`, `candidate_misfit`, `grounding_unknown`);
`orchestrator._EMPTY_COUNTS` grows to match. `df.attrs`:
`channel_verified/partial/unverified -> channel_established/suggestive/not_established`, plus
`channel_grounded`, `channel_theme_misroute`, `channel_candidate_misfit`,
`channel_grounding_unknown`, `themes_misrouted` (themes whose answered majority is
`theme_misroute`) — all emitted unconditionally, including on frozen reuse.

Prometheus: `alphalens_thematic_channel_verified_total -> _established_total`,
`_partial_total -> _suggestive_total`, `_unverified_total -> _not_established_total`; new
`_grounded_total`, `_theme_misroute_total`, `_candidate_misfit_total`,
`_grounding_unknown_total`, `alphalens_thematic_themes_misrouted_total`. `_assess_failed_total`
and the two shadow gauges unchanged.

**Removed with no alias and no shim** (project doctrine): `channel_status`,
`channel_vote_dispersion`, `shadow_strict_verified_n`, `CHANNEL_STATUSES`, and the literals
`verified` / `partial` / `unverified` anywhere in live pipeline code. They survive **only**
inside `apps/alphalens-research/alphalens_research/retrospective_audit/stage1_frozen_v2.py`,
which is a byte copy of the frozen Stage-1 instrument and **must not be touched** by the
rename. A dedicated test asserts the frozen module still carries the old vocabulary and still
reproduces its `FROZEN_MCV`, so a repo-wide find-and-replace cannot silently corrupt the
retro's replayability.

## 5. Contract B — the prose

### 5.1 What is wrong with the current flow, precisely

`mapping/orchestrator._build_row` stamps the channel columns; `screening/scorer` merges
enrichment onto a copy of the candidate frame, so **every `channel_*` column already survives
into the scored frame the brief stage reads**. Nothing new has to be plumbed to make the record
reachable. And yet `argumentation/orchestrator._row_to_facts` — the whole fact projection —
carries no `channel_*` key. **The record is computed, persisted, carried through scoring, and
then dropped one line before the prompt.**

The prompt then presupposes the conclusion: "1 sentence thesis **why this ticker benefits**",
"the second-order **benefit mechanism**". And the bear-case source list is closed and does not
include the channel record, while the prompt itself warns that adding a fact category without
updating that list silently suppresses that risk — which is exactly what is happening.

### 5.2 What the facts block must carry

A new `_format_channel_block(facts)` in `argumentation/prompts.py`, modelled line for line on
the existing `_format_template_facts_block`: its own `<channel_record>` delimiter,
`_xml_escape` on every value, rendered inside `<facts>` so the existing anti-injection clause
scopes it — and that clause is widened to name the third block.

Rendered keys:

```
causal_support:    established | suggestive | not_established | no_record
channel_type:      <one of the nine>
mechanism:         <channel_text>          (omitted when empty)
evidence_in_event: <channel_evidence>      (omitted when empty)
falsifier:         <channel_falsifier>     (omitted when empty)
grounding:         grounded | theme_misroute | candidate_misfit | unknown
event_type:        <catalyst_event_type>   (already on the scored frame)
```

Two deliberate exclusions, stated in the docstring so a later reader does not "fix" them:

* **`channel_confidence`, `channel_vote_k`, `channel_vote_valid_n`,
  `channel_support_dispersion` are NOT injected.** They are instrument telemetry; a
  self-reported float in the prompt invites "with 80% confidence" prose, and the calibration
  evidence in §2.3 says the hedging must track the **level**, not a spurious number.
* **No market-cap, P/E or volume token is added.** The bracket stays deterministic Python,
  pinned by `tests/thematic/test_theme_mapping.py`. `market_cap` continues to be rendered as a
  pre-computed fact exactly as today; nothing new.

**`no_record` is a fourth facts-level value, not a fourth taxonomy level.** It renders when
`channel_assessment_outcome != "success"` (or the row predates the columns). Without it, an
assessor outage — which by construction carries the lowest support level — would make the model
write "no company-specific path was established", asserting a judgement no model ever made.
That is the same refusal the assessor already makes, pushed one stage downstream.

### 5.3 One prose shape per level

The `tldr` instruction becomes, in both the Pro and the Flash template: **one sentence stating
what causal support exists between the event and this company, at the level given in
`causal_support`** — explicitly **not** "why it benefits".

* **`established`** — name the mechanism and the evidence fact it rests on: "<event fact> ->
  <what changes> -> <which line of this company's economics moves>; the event states
  <evidence_in_event>." The bear case may cite the falsifier. The exit line is derived from
  `falsifier`, which is by construction "the single observable that would show this chain is not
  real" — an exit trigger already.
* **`suggestive`** — name the possible channel **and, in the same sentence, the missing or
  indirect link**: "a plausible <channel_type> channel runs ..., but the event does not state
  <the missing link>." The missing link must be **named**, not gestured at ("some uncertainty" is
  a defect). Any forward statement must be conditional on that link. The exit line names the
  missing link's resolution against the position.
* **`not_established`** — plainly: "<TICKER> surfaced from <event, cited factually>; no
  company-specific cash-flow path from that event to this company was established." It must not
  assert a benefit, and it must not manufacture a mechanism from the theme word, the industry
  name, or the stage-A `rationale` (which the model can see and will otherwise launder). It may
  state the null case, and it may state that the pairing rests on the theme tag alone.
* **grounding is not `grounded`** (overlays any level) — one extra clause: "the event names no
  link to this company (it is a <event_type> item about the category), so treat the pairing
  itself as unreliable." The guard treats a non-grounded row exactly like `not_established`.
* **`no_record`** — "the channel assessment did not complete for this row; no causal-support
  statement is available." No benefit verb, no invented level.

### 5.4 Direction neutrality, and why it is not a sentiment filter

Both templates state explicitly that the described effect **may be positive, neutral, or
adverse for this company**. The channel vocabulary is already direction-ambiguous by
construction: `input_cost` is a price this company **pays**, `capacity_supply` is capacity added
to **its** market, `substitution` may move demand **away**. The prose must describe the
direction the record actually supports, including "the plausible effect on this company is
neutral" and "the plausible effect is adverse".

**This is description, not selection.** Nothing is dropped, nothing is re-ordered, no event type
is screened, no sentiment classifier exists anywhere in the path, and the trade-setup geometry
is untouched — the long-only ladder is built exactly as before for every row, whatever the prose
says. The operator cherry-picks; the tool's job is to state honestly what the record supports.
Adding a "drop adverse-direction rows" rule at any later point is the same deletion mistake the
retrospective just cost us and would need its own pre-registration.

### 5.5 Bear case and catalyst-failure exit

The closed bear-case source lists gain exactly **one** admissible source: *the channel record —
a missing or indirect link named in the record, the record's own falsifier, an unestablished
causal path, or a grounding failure*. The surrounding discipline is kept verbatim ("never
manufacture one to reach the count", no confidence-score padding), and its mirror is added:
**never list `not_established` as if it were a company defect** — the honest bear-case sentence
is about the **evidence**, not about the business.

The `catalyst_failure_exit` exemplar is replaced by a per-level rule:

* `established` / `suggestive` — the exit trigger is the record's `falsifier`, or the resolution
  of the named missing link, rendered as an observable. That is the field's actual purpose and
  the record already computes it.
* `not_established` / non-grounded / `no_record` — the exit line must **not** be thesis-specific,
  because there is no thesis. It states the event-level condition instead ("exit if no further
  event ties this company to the theme by <the setup's own horizon>") and may not name a
  mechanism, a competitor product, or a contract.

### 5.6 The guard

New module
`apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/support_guard.py` —
**pipeline side, not `alphalens_research/eval/`**. The eval modules (`faithfulness.py`,
`financing_claims.py`) are the right *shape* but the wrong *tier*: they are research-side
telemetry, the workspace DAG forbids `alphalens_pipeline` importing `alphalens_research`, and
this check must run inside `generate_brief` before a row ships. Research may later import the
pipeline guard to compute corpus rates; that direction is allowed.

Call site: `generator.generate_brief`, immediately after the CJK `_contains_cjk` check — the
same position and the same shape (parsed cleanly, but the prose violates a hard contract).

**Scope, deliberately narrow and pinned by a test:** the guard is inert — returns no violations
without scanning — unless `causal_support` is `not_established` or `no_record`, or grounding is
not `grounded`. For `established` and `suggestive` it must **never** fire. It is a
support-contract check, not a style police; a guard that also policed `established` rows would
start rewriting well-grounded prose and would drift into an editorial filter.

**Matching:** hyphen-normalised, lower-cased, whole-word or whole-phrase (`re.escape` plus word
boundaries) against a Tier-1 affirmative benefit lexicon:

```
benefits, benefit, benefiting, benefited, will gain, gains, gaining, stands to gain,
is positioned to, positioned to win, wins, will win, captures, will capture, capture share,
profits from, profiting from, boosts, will boost, lifts, will lift, drives revenue,
drives growth, will drive revenue, will drive growth, translates into revenue,
translates to revenue, flows through to earnings, accrues to, is a beneficiary,
direct beneficiary, second-order beneficiary, primary beneficiary, upside from,
tailwind for, poised to, set to gain, should see demand, will see demand,
expands margins, will expand margins
```

Overlapping matches collapse to one per field (a field asserting a benefit twice is one
violation). Tier-2 polysemous tokens (`drive`, `capture`, `lift`) require a same-clause economic
anchor (`revenue`, `margin`, `earnings`, `demand`, `share`) before firing, so "drive train" and
"capture rate" do not over-fire.

**Suppressors** — a match must survive all of them to count as fired:

1. **negation** — a negation cue in the clause before the phrase ("does not benefit", "no
   evidence that it benefits").
2. **conditional** — an explicit conditional or hedge marker in the **same clause**: `if`,
   `were`, `would`, `could`, `should`, `only if`, `conditional on`, `the event does not state`,
   `unless`. This is the explicit conditional qualification the contract requires: "if the
   reported contract is confirmed, XYZ would gain a customer" passes; "XYZ benefits from the
   theme" fires.
3. **quoted** — the phrase sits inside quotation marks (a cited headline).

The clause, negation and quote primitives already exist in
`alphalens_research/eval/faithfulness.py` and **cannot be imported** (wrong DAG direction). v1
re-implements roughly forty lines in the pipeline module and says so in the docstring; the
extract-on-second-use rule then points at moving those primitives **down** into the pipeline and
having research import them, as a separate refactor — never at weakening the DAG.

**When it trips — loud, recorded, never a silent rewrite, never a dropped row:**

1. **Log at WARNING** with ticker, field, matched phrase and span (the existing
   `LANGUAGE_DRIFT` precedent).
2. **One regeneration, not a rewrite.** New `BriefErrorKind.UNSUPPORTED_BENEFIT_CLAIM`, added to
   the single-re-roll retryable set — same cap, `temperature=0`, exactly like `LANGUAGE_DRIFT` /
   `EMPTY_CONTENT`. The retry uses the same prompt (the contract is already in it); the model
   gets a clean greedy draw rather than a post-hoc edit. **We never edit the model's text**: a
   Python-inserted "may" would fabricate hedging the model never reasoned about and destroy the
   audit trail.
3. **If the retry also violates: keep the row, withhold the prose.** Terminal kind
   `unsupported_benefit_claim`, so `_enriched_row` stamps `brief_status = "unavailable"` and the
   four prose columns stay null — the **existing** graceful-degradation path the SPA already
   renders. The row keeps its place, its rank, its `also_in_themes`, its deterministic signals
   and its full trade setup.

**This is not a deletion gate.** The candidate ships; only four prose strings are withheld, and
the withholding is stamped, logged, gauged and visible on the card. The alternative — shipping
the confident prose with a badge beside it — is precisely what the persuasion evidence in §2.3
says does not work.

**Stamped columns** (parquet-only, in `_enriched_row` and in the empty-day schema mirror):
`brief_support_guard_status` (`clean` / `repaired` / `withheld` / `fired_unrecovered` /
`no_prose` / `not_applicable`),
`brief_support_guard_violations` (Int64, the FIRED count on the LAST draw the guard scanned —
the withheld text on `withheld`, the first draw on `fired_unrecovered`),
`brief_support_guard_suppressed` (Int64, the near-miss count on that same draw, so a suppressor
that misfires is readable rather than trusted),
`brief_support_guard_spans_json` (at most 3 spans, for the audit worksheet),
`brief_support_guard_version`, `brief_causal_support`, `brief_channel_grounding`.
`not_applicable` is a real value, so "the guard did not fire" and "the guard did not run" never
merge — the same discipline as `not_assessed` versus a real level in the assessor. `clean` is
held to the same rule: `fired_unrecovered` (the guard fired, the re-roll then died for an
unrelated reason) and `no_prose` (no draw ever reached the guard) are separate values, because
folding either into `clean` would bias the compliance rate optimistically — the direction that
would wrongly argue the detector is accurate.

**What the guard can and cannot claim.** It is a LEXICAL detector over a bounded, English-only
phrase list, so the `clean` rate measures that list's RECALL, not the prose's honesty. Verified
misses at the time of writing include "is a key supplier of" and "demand for its products
rises". The suppressors are scoped to the phrase, not the sentence, because the prompt itself
mandates a negation at `not_established` and hedged risk prose in every `bear_summary`; a
sentence-wide suppressor made the guard anti-correlated with the risk it exists to catch. A
match counts only when its own segment names the candidate, so competitor-benefit sentences in
the bear case — the shape that field's instruction asks for — are not violations.

**Gauges**, emitted every run including zeros:
`alphalens_thematic_brief_support_guard_fired_total`, `..._repaired_total`,
`..._withheld_total`, `..._fired_unrecovered_total`, `..._no_prose_total`. A series that
vanishes on a healthy day is indistinguishable from a stopped exporter.
`fired_total = repaired + withheld + fired_unrecovered`: every draw on which the prose contract
was actually broken, independent of the recovery.

A withheld row raises `alphalens_thematic_brief_unavailable_count` too, because the withhold
reuses the graceful-degradation path. The paired alert's description names both causes so a
withhold day is not read as an LLM outage; splitting them is `..._withheld_total`.

## 6. Detect, stamp, keep, measure

**The binding rule of this increment.** `channel_grounding_status` never removes a candidate,
never enters a filter, a sort key, a score, the verify loop or the assessment cap.
`assess_candidates` keeps its one-result-per-input contract. A row detected as `theme_misroute`
or `candidate_misfit` ships exactly as it would have shipped, in the same position, with the
status recorded, counted in the per-theme log line and the sidecar, and exported as a gauge.

Named places that would have to change for it to gate, listed so a future reader can see the
gate is **absent by design** and not merely missing:

1. `assess_candidates` returning fewer results than inputs.
2. A filter on the candidate's channel record inside `orchestrator._assess_channels_for_theme`
   after the positional zip.
3. A new `continue` in `orchestrator._verify_candidates_for_theme` — the only loop in the
   pipeline that legitimately drops a candidate today, and only on the gate verdict plus
   `keep_unverified`.
4. A `channel_` column entering `_CANDIDATE_SORT_KEYS` / `_CANDIDATE_SORT_ASCENDING`.
5. `scorer.score_candidates` or `selection_score.compose_weighted_score` reading a `channel_`
   column.
6. `argumentation._BRIEF_SORT_KEYS`.
7. Django promoting the columns from parquet-only to model fields plus an API filter.

Items 4-6 are covered by the existing structural regex because the new columns keep the
`channel_` prefix. Items 1-3 need behavioural never-shrink tests. Item 7 is why the parquet-only
status is restated here: these columns need no migration, no serializer change and no OpenAPI
regeneration in this increment, and that is deliberate.

**Gating requires a future audit and a separate pre-registration.** Any future use of
`channel_grounding_status` as a filter requires, in order: (a) an independent **stratified audit
of detector accuracy** against operator-labelled ground truth, with its own design memo; (b) a
separate pre-registration with its own slot against the program-lifetime hypothesis budget;
(c) a deploy that ends whatever cohort is then accruing. None of that is authorised here.

**One permitted consumer, stated so it is not mistaken for a breach:** the argumentation prompt
builder may **read** the channel columns to write prose. Prose is not selection — it changes no
row's presence, rank or score.

## 7. Version wiring and what resets

Three tokens move:

1. **`channel_config_version`** — payload `"schema": "channel-assess-v1" -> "channel-assess-v2"`;
   key `"statuses"` renamed to `"support_levels"` with the new vocabulary; new key
   `"grounding_statuses"`; `prompt_sha` and `schema_sha` both move because the prompt template
   and the response schema change. `_ASSESS_TEMPERATURE`, `_ASSESS_MAX_OUTPUT_TOKENS` (4000),
   `_ASSESS_VOTES` (3) and the render caps are unchanged.
2. **`_MAPPER_FREEZE_SCHEMA`** — `mapper-freeze-v3 -> mapper-freeze-v4`, so
   `mapper_config_version` moves twice over (also via the nested `"channel"` key). **Stage A's
   prompt, schema and sampling do not change in this increment; the tag is bumped purely as a
   cohort marker.** The reason is concrete: 2026-08-19 already produced rows under
   (`mapper-freeze-v3`, `channel-assess-v1`), so a future reader filtering on the legible tag
   alone would silently pool the discarded day with the new cohort. The freeze reset it causes
   would have happened anyway through the nested channel token, so it costs nothing extra.
3. **`SHADOW_STRICT_RULE_VERSION`** — `shadow-strict-any-verified-v1` ->
   `shadow-strict-any-established-v1`. Not a freeze input and not a `channel_config_version`
   input; the rule stays re-derivable offline from `shadow_strict_established_n` /
   `shadow_strict_assessed_n`. It moves only because it names a vocabulary that no longer exists.

**Does not move:** `novelty_config_version`; the OpenRouter client timeout (deliberately not a
config input); the pinned OpenRouter provider (a provider change mid-window is a separate
amendment); `_MAX_ASSESS_PER_THEME`, `_MAX_CANDIDATES_PER_THEME`,
`_MAX_VERIFY_ATTEMPTS_PER_THEME` — selection is not touched.

**What that resets:**

* **Every date's frozen candidates parquet.** `_load_frozen_candidates` sees a
  `mapper_config_version` mismatch and recomputes; existing rows are never restamped (ADR 0013
  R3). Real LLM cost across the 6x/day slots.
* **The forward pre-registration cohort, for the second time in two days.** Boundary 1
  (2026-08-19 22:46 UTC, `mapper-freeze-v3` + `channel-assess-v1`, 13 assessed candidates) is
  **closed and kept in the amendment's history, not rewritten**. Boundary 2 opens at the first
  VPS `map-themes` run on the image carrying `mapper-freeze-v4` + `channel-assess-v2` — **deploy
  time, not merge time** — and the exact token strings plus the first cohort `asof` are appended
  as Amendment 2 before any post-boundary row matures. Thirteen label rows are discarded; zero
  matured outcomes are discarded, and the registered slot stays unspent.
* **The proposal-shadow head-to-head restarts both arms a third time** (the token is stamped on
  mechanical rows too). The ISO 40-42 window stays superseded.
* **Both golden map cassette sets miss** (cassette key = sha256 over the full request
  descriptor): `map_day` v3 -> v4 and `map_day_nvda_ising` v2 -> v3, each needing a live
  re-record and a provenance memo in the style of `docs/research/golden_map_rebaseline_2026_08_19.md`.
  Same caveat as before: with k=3 the three identical stage-B requests collapse to one cassette
  key, so the replayed dispersion and `grounding_agree_n` are always the unanimous case — the
  golden proves nothing about vote stability.
* **The golden brief cassettes miss too**, because the brief prompt changes. Re-record with the
  existing recorder and regenerate the projection in the same commit. The recorded `scored.parquet`
  fixture predates PR #1066 and carries **no** `channel_*` column, so it must be re-cut from a
  post-#1066 day with at least one row of each of `established` / `suggestive` /
  `not_established` / non-grounded — otherwise the golden renders an empty channel block on every
  row and never exercises the new contract.
* **`docs/research/channel_as_feature_design_2026_08_19.md`** needs a dated revision block on its
  field-contract, shadow and version-wiring sections rather than an edit in place; the memo is
  LOCKED, so the honest form is a new dated section recording what moved and why, with the old
  table left readable.

**Not reset and not renamed:**
`apps/alphalens-research/alphalens_research/retrospective_audit/stage1_frozen_v2.py` and
`apps/alphalens-research/scripts/stage1_retro_label_pairs.py`. They pin the frozen Stage-1
instrument and its `FROZEN_MCV`; a repo-wide rename touching them would silently break the
retro's replayability.

## 8. Rejected alternatives

### 8.1 Drop grounding-failed rows now

**Rejected, and this is the strongest rejection in the memo.** The programme has exactly one
measured result on deleting candidates because a model said the link was not there: the
retrospective, whose point estimate ran **the wrong way** (-7.15 pp, one-sided p 0.945) and
which crowded out 96.0% of the small/mid-cap tickers it touched. Replacing "no channel -> drop"
with "misroute -> drop" is the same move with a new vocabulary and no new evidence.

A grounding-failure detector is itself an unvalidated LLM judgement over a **headline-only**
rendering. Its own design (§4.3) says it will under-detect one arm and over-call the other; its
tie-break resolves toward `grounded`, so the measured misroute rate is a lower bound; and
nothing has audited its accuracy against operator ground truth. Gating on it now would delete
rows on an instrument with no measured error rate, and would make the error rate permanently
unmeasurable, because the deleted rows would leave no forward outcome to check against.

Detect, stamp, keep, measure is the strictly more informative choice and costs nothing but a
column. §6 names what a future gate would require.

### 8.2 Keep the `verified` wording and only add grounding

**Rejected.** It leaves defect 2 in place, and it does so at the moment of maximum leverage:
the cohort is restarting anyway for the grounding field, so the rename is free right now and
costs a full second restart at any later date. Beyond the accounting, `verified` is the word on
which a reader most plausibly acts, and the pipeline cannot stand behind it: nothing was
verified, a second LLM call agreed with a first over the same text. Shipping a word the design
knows to be indefensible, in the same increment that fixes a different honesty defect, would be
incoherent.

### 8.3 Annotate without touching the prose

**Rejected**, and this is where the human-factors evidence is load-bearing. The "cheap" version
of this work is to add the grounding column, put a status chip on the card, and leave the brief
generator asking why the ticker benefits. That is an **administrative control** in the
hazard-control hierarchy — a warning label attached beside the hazard — and it sits near the
bottom of that ordering for the reason Bansal et al. measured: a fluent explanation raises
acceptance largely independently of correctness. Steyvers et al. point at the intervention that
does work: make the **explanation's own hedging track the model's uncertainty**. A chip beside
the paragraph does not do that; generating the paragraph from the record does.

There is also a plainer argument. Today the prose is the only channel-related artefact the
operator ever sees (the columns are parquet-only and the Django ingest drops them). Annotating a
surface nobody reads while leaving the surface everybody reads unchanged fixes nothing.

### 8.4 Rebuild event grounding first (EPIC #974 stage 2 / issue #976)

**This is the correct root fix, and it is not what this task does.** The `consumer_sentiment`
failure is an **extraction** failure: one article carried several unrelated events and the
entities of one story were attached to the theme of another. `ARTICLE -> EVENT_SET` (#976) makes
the event, not the article, the unit — which removes the defect at its source rather than
detecting it downstream.

It is not the move for this increment for three reasons. It does not exist yet, so choosing it
means shipping nothing now while a known live defect renders as honest uncertainty. It is a
larger change to the most upstream stage of the pipeline, with its own cohort consequences.
And, decisively, **the detector is what will tell us whether #976 worked**: once the extractor
ships, a fall in the `theme_misroute` rate across the #976 boundary is the readout. Designing
the field now, before #976, is what makes that comparison possible at all.

So this increment is the **containment layer** ahead of #976: it makes the defect visible,
attributable and counted, and it stops the prose from laundering it — while the root fix is
built.

### 8.5 A separate LLM call for grounding

**Rejected.** It duplicates context the stage-B call already holds (theme tag, event type,
headline, named companies, extracted implications), and it buys a second instrument with its own
prompt sha, its own failure ladder, its own outage semantics and its own version token to keep
in step with `channel_config_version`. A per-**event** call is cheap (~10-25/day) but answers
only the `theme_misroute` arm; `candidate_misfit` is per-candidate by definition, so a per-event
call cannot replace the field, only add a second one. Its one real advantage — isolating the two
questions so the grounding answer cannot drag the causal grade — is bought instead by **ordering**
(grounding asked and emitted first) and **measured** by the 3x3 cross-tab rather than assumed.

Decisive argument: the retrospective's lesson is that a judgement you cannot join to a row's
forward outcome cannot be evaluated. A field on the existing per-candidate call inherits the row,
the k=3 replicate structure, the valid-draw and dispersion instrument-noise readouts, the
assessment-outcome ladder and the never-shrink invariant for free. Anything else re-implements
all five.

### 8.6 A deterministic check on named entities versus theme

**Rejected as vacuous on one arm and actively harmful on the other.** Vacuous:
`catalyst_resolver.find_trigger_event` selects the event by walking the window for events tagged
with the theme, so "is the event tagged with this theme" is **true by construction** — the
mis-tag is the defect, and no check on the same field can see it. Harmful: "the candidate ticker
must appear in the event's named entities" is precisely the crowd-out gate the retrospective
measured at 96.0% `KEPT_TICKER_ABSENT`; the whole crowd-out repair proposes companies the article
does **not** name, so that check would flag the design's intended output as a defect.

What deterministic code **can** contribute is a cheap covariate, not a verdict: `event_type ==
"macro"` with an empty named-entity list is the round-up shape, and it is already carried in the
funnel as `catalyst_event_type`. No new column, and it must not be a gate.

### 8.7 A `channel_direction` (favourable / adverse) annotation

**Rejected again**, as in the #1066 memo, for the same reason: long-only geometry is out of
scope, and a direction column is the single field most likely to be quietly turned into a filter
later. §5.4 gets direction honesty into the **prose** without creating the column.

## 9. Risks

1. **Prompt halo.** One call now answers two questions, so the grounding answer can drag the
   causal grade or the reverse. Mitigations: grounding is asked and emitted **first**, the two
   columns are never cross-normalised, and the 3x3 cross-tab plus `channel_support_dispersion` /
   `channel_grounding_agree_n` make contamination measurable. Stated honestly: the k=3 draws
   share one prompt, so dispersion measures instrument noise, **not** independence between the
   two questions.
2. **Headline-only evidence.** The detector never sees the article body. It will under-detect
   `candidate_misfit` and may over-call `theme_misroute` on terse headlines. The structural cure
   is #976; this column then becomes the instrument that measures whether #976 worked.
3. **Cohort restart number two, one day apart.** Thirteen assessed candidates are discarded. A
   **third** restart would make the forward experiment effectively un-accruable, so every
   vocabulary, prompt and schema change this programme already knows it wants — including
   whatever the prose layer needs from the assessor — lands in **this** increment, not in a
   follow-up next week.
4. **Metric renames break the hand-synced VPS Prometheus rules.** The channel-failure rule sums
   the three status gauges by name and the gauges-missing rule uses `absent()` on the old
   `..._verified_total` — an `absent()` rule on a renamed metric alerts forever. The rules-file
   edit plus the copy-to-VPS and reload must happen in the **same operation** as the image
   deploy, and any dashboard on the old names goes with it. A new theme-misroute rule (misroute
   share above one third of answered, volume guard of 5 answered, `for: 12h`) must be worded as a
   **pipeline defect** page, never as a trading signal.
5. **Silent gate creep** — still the largest long-term risk, and now sharper: `theme_misroute` is
   the field most likely to be turned into a filter later, because it reads as "obviously a bug,
   why ship it". §6 lists the seven named places that would have to change, and which of them
   are covered by structural tests versus behavioural never-shrink tests.
6. **The misroute rate is a lower bound.** The plurality tie-break deliberately resolves toward
   `grounded`, so a split vote never manufactures a defect. The measured rate must always be
   reported next to the `channel_grounding_agree_n` distribution, never as a point estimate of
   pipeline defect rate.
7. **Candidate-independent, measured per candidate.** Within-theme disagreement on
   `theme_misroute` means the instrument is noisy or the theme is only partly on-topic; low
   within-theme agreement invalidates a theme-level warning, not the individual rows. Report the
   within-theme agreement before anyone acts on a misroute count.
8. **No backward-compatibility shims** (project doctrine), so every reader of the old column
   names breaks at the boundary: ad-hoc analysis scripts, golden projections, and any join that
   crosses 2026-08-19. Anything reading across the boundary must map the old vocabulary to the
   new one explicitly at read time, and must not pool the two cohorts.
9. **A repo-wide find-and-replace would corrupt the frozen retro instrument.** Guarded by an
   explicit test rather than a reviewer's memory.
10. **Cost and truncation.** Three extra required output fields add roughly 150-250 output tokens
    per draw, times k=3, times ~30-50 in-bracket candidates: about 2 to 4 dollars a month on top
    of the existing ~21. More important than the money is the reasoning tail — the acceptance
    probe measured 9% empty bodies at the old 1500-token cap, and the current cap is 4000. Watch
    the truncation rate on the first days; a rising truncation rate biases the shadow verdict
    toward refuse exactly as it did before.
11. **Drift to the comfortable middle, pointing the other way.** A three-value grounding
    vocabulary invites answering `grounded` for everything, because `grounded` is the
    unremarkable answer. A grounding mix with almost no `theme_misroute` on a day when the
    operator can see a round-up in the funnel is a **prompt defect**, not a healthy day, and it
    deserves the same first-two-weeks manual read of about 30 rows, stratified across grounded
    and non-grounded.
12. **The withheld-prose path reuses `brief_status = "unavailable"`**, which feeds the existing
    unavailable-ratio alert. A guard that withholds a couple of rows a day raises that ratio and
    can page. The alert is re-tuned by hand as a recorded step in the deploy, rather than
    introducing a new `brief_status` value that would break the SPA's two-state rendering.
13. **Prompt-injection surface widens.** `channel_text`, `channel_evidence`,
    `channel_falsifier` and now `channel_grounding_quote` are model output over third-party news
    text that has already passed one untrusted fence. They go through `_xml_escape` inside their
    own delimited block, and the existing anti-injection clause is widened to name the third
    block, or a crafted closing delimiter plus an injected instruction escapes the data scope.

## 10. What is explicitly NOT changed

* **No sentiment filter and no bearish-event-type filter, anywhere.** Nothing in this increment
  screens on direction; §5.4 makes direction a matter of prose honesty, not selection.
* **Long-only geometry is out of scope.** The trade-setup builder is untouched; the ladder is
  built exactly as before for every row.
* **The mcap bracket stays deterministic Python.** No market-cap, P/E or volume token enters any
  prompt, pinned by the standing test.
* **Selection and ordering are untouched** — no sort key, no score, no cap, no verify-loop
  change.
* **The brief response schema keeps exactly its four string fields.** No fifth output field, so
  the substantive-field check, the JSON-repair recovery bar, the eval layer's column maps, the
  Django model and the SPA wire format are untouched by the prose contract itself.
* **No Django surface.** The new columns are parquet-only; the ingest drops unknown columns by
  design. Putting the causal-support level on the card is a separate PR with a migration, an
  OpenAPI regeneration in the same commit, a Storybook state, and the unvalidated-display
  doctrine (no verdict word, no authority colour).

## 11. Build order (TDD, red first at every step)

1. **Assessor vocabulary + constants**, red on a test asserting the new tuples and that the old
   three literals appear nowhere in the module; plus the frozen-instrument test that
   `stage1_frozen_v2.py` still carries the old vocabulary and still reproduces its `FROZEN_MCV`.
2. **Prompt and schema**, red on: grounding asked before the support grade, the quote scoped to
   the rendered block, `CAUSAL_SUPPORT_NOT_A_FORECAST` present verbatim, the "nothing is dropped"
   paragraph surviving, and the standing no-mcap / no-sentiment pins.
3. **Per-draw parse + validity**, red on: each grounding value round-trips; an off-vocabulary
   grounding value invalidates only that draw; `grounded` blanks the reason and non-grounded
   blanks the quote; a fabricated quote sets `channel_grounding_quote_verbatim` false **without**
   changing the status.
4. **Aggregation**, red on: plurality; tie precedence; `grounding_agree_n` arithmetic; quote and
   reason taken from the first valid draw matching the aggregate; the even-vote support rule
   re-expressed in the new vocabulary with ordinals unchanged; the failure ladder yielding
   `not_established` + `unknown` + the failing outcome; the pinned identity
   `grounding_unknown == assess_failed`.
5. **`row_fields` / 16-column contract / `status_counts` (8 keys) / shadow rename**, red on the
   column contract, the funnel columns, the sidecar columns, the `df.attrs` keys and the gauges —
   including the never-shrink cases with an all-misroute theme and a mixed theme.
6. **Version tokens**, red on the new schema tags plus positive controls that a prompt edit, a
   support-vocabulary edit and a grounding-vocabulary edit **each** move
   `channel_config_version`, and that `mapper_config_version` reads `mapper-freeze-v4`. The two
   vocabulary controls patch the tuples directly: before this increment the grounding vocabulary
   reached the token only through `schema_sha` (the response schema embeds the tuple at import),
   which is an implementation detail a refactor could remove without any document noticing.
7. **Facts projection + channel block**, red on: delimiters, XML escaping of a hostile payload,
   no block rendered when the facts dict has no channel keys, no mcap token in the new text, and
   each `causal_support` value rendering its own instruction. Watch the standing
   "flash prompt is shorter than pro" pin.
8. **The guard**, red on the two positive controls first: benefit prose at `not_established`
   fires; the identical text at `established` is clean. Then the suppressor table and the Tier-2
   anchor cases.
9. **Guard wiring**, red on: a violating first draw regenerates once and ships `repaired`; a
   twice-violating row **still ships** with the same rank and a full trade setup, prose withheld,
   status `withheld`, gauge 1; row counts identical across every guard outcome; no guard column
   is a brief sort key.
10. **Golden re-records** — map cassettes v4 / v3, brief cassettes, the re-cut `scored.parquet`
    fixture with one row of each level, new projections, provenance memos. A reviewed operation
    with real LLM cost, not a CI-green fix.
11. **Prometheus rules** — rename in the repo rules file, hand-sync to the VPS and reload in the
    same operation as the image deploy; add the misroute rule worded as a pipeline defect. The
    misroute rule MUST read a window, e.g.
    `max_over_time(alphalens_thematic_channel_theme_misroute_total[6h])` against the
    `max_over_time` of the answered sum, with `for:` shortened to match. Five of every six daily
    slots reuse the day's frozen parquet and publish zeros, so an instant-vector ratio with a
    long `for:` can never stay true long enough to fire.

Zen pre-merge review with `deepseek/deepseek-v4-pro` at high thinking is mandatory for this PR
(shared assessor / generator / prompt / schema surface).

## 12. Deliverables

1. This memo, LOCKED.
2. Amendment 1 on `docs/research/channel_feature_forward_prereg_2026_08_19.md`, committed with
   this memo, **before** the implementation lands and before it deploys.
3. Amendment 2 on the same pre-registration on deploy day: exact `mapper_config_version`,
   `channel_config_version`, first cohort-2 `asof`.
4. A dated revision block on `docs/research/channel_as_feature_design_2026_08_19.md` recording
   what moved, with the old tables left readable.
5. Golden re-record provenance memos for the map and brief cassette sets.
6. The first-two-weeks stratified manual read of ~30 rows across grounded and non-grounded,
   reported qualitatively with no rate claimed — and explicitly **not** the audit that §6
   requires before any gating.
