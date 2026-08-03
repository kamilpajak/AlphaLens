# Frozen catalyst-role case set

`cases.json` is a fixed set of (catalyst event, candidate ticker) pairs used to measure the
role instrument in `apps/alphalens-research/scripts/classify_catalyst_roles.py`.

## Why it exists

The golden replay cassette for the `map-themes` stage is keyed on the prompt SHA, so it
re-records whenever the prompt changes. That makes it useless as a yardstick for a prompt
change: the oracle moves with the subject under test. This case set does not move. The event
payloads are copied out of recorded runs and stay valid across the mapper prompt change
(issue #975) and the larger article-to-event-set rework (issue #976), so a before/after
comparison measures the change instead of re-recording it.

## What a case holds

Each case carries the event payload the instrument reads (`ticker`, `brief_date`, `theme`,
company / sector / industry, headline, URL, event type, sentiment, named entities, extracted
second-order implications) plus `expected_role`.

It deliberately does NOT carry the pipeline's own verdict fields (`layer4_weighted_score`,
`rank_in_day`, `llm_confidence`, `rationale`, `gates_passed_str`). The instrument is blind to
those on purpose - feeding them back would measure whether the pipeline agrees with itself,
not whether a transmission channel exists.

## `anchor` vs `contested`

- **`"anchor": true`** - a known-answer case with an unambiguous `expected_role`. These
  cases GATE: a single mismatch means the run's aggregate numbers are not to be trusted.
  The six anchors mirror the `ANCHORS` tuple in `classify_catalyst_roles.py`.
- **`"contested": true`** - `expected_role` is `null` and the case does NOT gate. It is
  tracked because the strict and permissive rubrics disagree on it by construction (a
  sector-level tailwind is a channel under the permissive rubric and is not one under the
  strict rubric). `contested_reason` on the case says why. Watching how a contested case
  moves is informative; asserting a role for it would be inventing an answer.

## The one rule

**Cases are appended, never edited to match a new model's output.** A case whose expected
role is rewritten after seeing what the model said has stopped being a measurement. If a new
model disagrees with an anchor, that is the result - either the model is wrong or the anchor
was never as unambiguous as claimed, and both are worth a written decision rather than a
quiet edit. Add new cases (including new contested ones) freely; changing an existing
`expected_role` needs its own justification.

Every case names its `provenance`, and the top-level `sources` block records where each file
came from. Nothing here is hand-written: all payloads were extracted from real recorded runs.
