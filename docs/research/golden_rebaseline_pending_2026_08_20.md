# Golden re-baseline REQUIRED — causal-support taxonomy + grounding + prose contract (2026-08-20)

**Status:** PENDING — the re-record has NOT been done. This note exists so the red
tests are read as a required reviewed operation, not as a defect to be worked around.
**Scope:** three fixture sets —
`apps/alphalens-research/tests/golden/fixtures/map_day/` (v3 → v4),
`apps/alphalens-research/tests/golden/fixtures/map_day_nvda_ising/` (v2 → v3),
`apps/alphalens-research/tests/golden/fixtures/brief_day/`.
**Change under test:** `feature/grounding-and-prose-honesty`. Design:
`docs/research/grounding_and_prose_honesty_design_2026_08_20.md`.

## Read this first

**A self-rebaselining oracle is the anti-pattern this note exists to prevent.** These
cassettes are not fixtures the implementer may regenerate to turn CI green. They are
recordings of what a specific model said to a specific prompt, and re-recording them is
the ONLY honest response to a deliberate prompt change — but it costs real money, it
needs a human to approve it, and the resulting projection must be READ, not merely
committed. Nothing in this branch may edit `golden/projection.json` by hand, and nothing
may weaken `replay_client` to fall back to a live call.

## Why every set misses, and why that is correct

`tests/golden/replay_client.py` keys each cassette on a sha256 over the FULL request
descriptor (model + contents + config) and fails loud on a miss, by design. Three
deliberate changes move those keys:

1. **The stage-B assessment prompt and response schema** — the status vocabulary became
   `established` / `suggestive` / `not_established`, and three required grounding fields
   were added, asked and emitted before the causal grade.
2. **`_MAPPER_FREEZE_SCHEMA` v3 → v4** — a cohort marker only; stage A's prompt, schema
   and sampling are unchanged in this increment.
3. **Both brief prompt templates** — the `tldr` instruction stopped presupposing a
   benefit, the channel record is projected into `<facts>` as its own
   `<channel_record>` block, the bear-case risk list gained the channel record, and the
   `catalyst_failure_exit` rule became per-level.

The replay harness says so itself, verbatim, on every miss:

> no cassette for model='deepseek/deepseek-v4-pro' key=f6df92cc72b2… — re-record with
> record_golden_brief.py (a changed prompt / model / param is a behaviour change, not a
> live-call fallback)

A second, subtler miss is worth naming because it looks like a bug and is not.
`map_provenance.split_cassette_records` classifies a stage-B cassette by looking for
`channel_support_status` in the recorded request. The recorded v3 / v2 cassettes carry
the OLD key name, so EVERY record now falls into the stage-A bucket and the classifier
raises, verbatim:

> expected exactly 1 stage-A LLM cassette in .../map_day/cassettes_llm/v3, found 2 (of 2
> total) — one recording holds one proposal call, so this is two recordings mixed, or the
> recordings pre-date the current stage-B schema

Note the count: **2 stage-A of 2**, not "0 of 2". The classifier cannot distinguish its two
possible causes, so the message names both; here the cause is the second one, and the
recordings are pre-boundary. It must NOT be relaxed to accept either spelling: doing so would
let a future reader believe the golden exercised the new contract when it replayed the old one.

## What the re-record must do

* `scripts/record_golden_map.py --fixture NAME --llm-only`, once per map fixture, into
  `map_day/{cassettes_llm,golden}/v4` and `map_day_nvda_ising/.../v3`. Leave v3 / v2 in
  place — `map_fixtures.py::current_recording` selects which one the test loads.
* `scripts/record_golden_brief.py` for the brief set, regenerating
  `fixtures/brief_day/golden/projection.json` in the SAME commit (the projection pins
  `sorted(columns)`, so the six new `brief_*` guard columns land there).
* **Re-cut `fixtures/brief_day/scored.parquet` from a post-boundary day.** The recorded
  fixture predates PR #1066 and carries ZERO `channel_*` columns, so replaying it as-is
  would render an EMPTY channel block on every row and the golden would never exercise
  the new contract at all. The re-cut set needs at least one row of each of
  `established` / `suggestive` / `not_established` and at least one non-grounded row.
  Without that this whole exercise buys a green tick and no coverage.
* Write a `RECORDED` provenance note in the style of
  `docs/research/golden_map_rebaseline_2026_08_19.md`, naming what was held constant.

## Standing caveat, unchanged

With k = 3 the three identical stage-B requests collapse to ONE cassette key, so the
replayed `channel_vote_valid_n`, `channel_support_dispersion` and
`channel_grounding_agree_n` are always the unanimous case. **The golden proves nothing
about vote stability or about grounding agreement**, and a reader must not treat a green
replay as evidence on either.

## Also on the deploy checklist, not in this branch

* The repo rules file has been renamed to the new gauges in this branch. Live rules on the
  VPS are hand-synced and NOT repo-mounted, so the copy to the VPS and the reload must still
  happen in the SAME operation as the image deploy — until then the live
  `AlphalensThematicChannelAssessFailureHigh` sums absent series and never fires, and the live
  `absent()` rule alerts forever.
* A new theme-misroute rule should be worded as a PIPELINE DEFECT page — misroute share
  above one third of answered, volume guard of 5 answered — and never as a trading signal.
  It MUST read a WINDOW, not an instant vector:
  `max_over_time(alphalens_thematic_channel_theme_misroute_total[6h])` against the
  `max_over_time` of the answered sum, with a short `for:`. `map_themes` calls
  `_channel_counts([], [])` on a frozen-set reuse, so five of the six daily slots publish
  zeros and the textfile is overwritten a few hours later — an instant-vector ratio with
  `for: 12h` can never stay true long enough to fire, and the volume guard is false for most
  of the day. Same rule the `_map_themes_outcome_metrics` docstring already states.
* The withheld-prose path reuses `brief_status="unavailable"`, which feeds
  `AlphalensThematicBriefUnavailableHigh`. A couple of withheld rows a day raises that
  ratio and can page; re-tune it by hand on the same deploy rather than inventing a
  `brief_status` value the SPA cannot render.
* Amendment 2 on `docs/research/channel_feature_forward_prereg_2026_08_19.md` records
  the exact `mapper_config_version` / `channel_config_version` and the first cohort-2
  `asof` at DEPLOY time, not merge time.
