# Golden re-baseline RECORDED — causal-support taxonomy + grounding + prose contract (2026-08-20)

**Status:** RECORDED
**Scope:** three fixture sets —
`apps/alphalens-research/tests/golden/fixtures/map_day/` (v3 → v4),
`apps/alphalens-research/tests/golden/fixtures/map_day_nvda_ising/` (v2 → v3),
`apps/alphalens-research/tests/golden/fixtures/brief_day/` (re-cut, not versioned).
**Approved by:** Kamil Pajak
**Change under test:** `feature/grounding-and-prose-honesty`. Design:
`docs/research/grounding_and_prose_honesty_design_2026_08_20.md` (§13 carries three findings
from this recording, appended as a dated addendum since the memo is LOCKED). Supersedes the
PENDING note `docs/research/golden_rebaseline_pending_2026_08_20.md`, now flipped to DONE with
a pointer here.

## What this is

Three deliberate changes moved every cassette key in the golden suite: the stage-B assessment
prompt/schema renamed its vocabulary (`verified/partial/unverified` →
`established/suggestive/not_established`) and added three grounding fields asked before the
causal grade; `_MAPPER_FREEZE_SCHEMA` bumped `v3 → v4` as a pure cohort marker (stage A's prompt
itself did not move); both brief prompt templates stopped presupposing a benefit. All three golden
fixture sets missed loudly, by design (`replay_client.cassette_key` is a sha256 over the full
request descriptor). This note records the re-baseline: what was re-recorded, what stayed
constant, the exact request/response diffs, the money spent, and — the part that makes the
exercise worth anything — a reading of what the new contract actually produced.

## 1. Method

**Map fixtures** — `scripts/record_golden_map.py --fixture NAME --llm-only`, once per fixture.
The five non-LLM surfaces (Polygon press, SEC 10-K cache, yfinance mcap, Form-4, catalyst
events/news) were served from the already-frozen fixture files via
`tests/golden/map_fixtures.py::frozen_surfaces`, byte-identical to the previous recording — only
the LLM cassette moved. `map_fixtures.py`'s `current_recording` was bumped `v3 → v4`
(`quantum_2026_05_24`) and `v2 → v3` (`nvda_ising_2026_04_14`); the old recordings were left in
place untouched.

**Brief fixture** — re-cut from scratch, not versioned (unlike the map fixtures, `brief_day` has
no side-by-side version scheme; the design memo calls for re-cutting `scored.parquet`, not
extending it). The recorded `2026-05-24` slice (DFIN/QLYS/QUBT/MANH) predates PR #1066 by three
months and carries **zero** `channel_*` columns — replaying it as-is would render an empty channel
block on every row and prove nothing about the new contract. The trap the pending note named was
real and had to be defeated with a genuine post-boundary run, not a fixture edit.

### 1.1 How the post-boundary scored slice was produced

The input stores (`thematic_events`, `thematic_news`) live on the VPS. Per the brief's suggested
approach: rsynced the real 2026-07-16→2026-08-19 window (35 daily parquets each, matching
`catalyst_resolver.DEFAULT_LOOKBACK_DAYS = 30`) from the VPS into the local Mac
`~/.alphalens/{thematic_events,thematic_news}/` — non-destructive additions, no VPS write, no
Mac file overwritten (the two stores only held the pre-existing golden-fixture dates locally).
Then ran THIS branch's `alphalens thematic map-themes --date <asof> --max-themes 10` locally
against real OpenRouter/Polygon/SEC/yfinance/Form4 — a genuine live run of the new
`channel-assess-v2` code, not a replay — for **two** independent days (2026-08-18, 2026-08-19,
11 verified candidates each) while searching for compositional diversity (§2). A third day
(2026-08-17) was started and killed mid-run once the operator's diversity budget (~3 days) was
spent without an `established` row appearing (§4.2). Candidates output went to a scratch
`--output-dir`, never touching the real `~/.alphalens/thematic_candidates/` store.

The scored slice itself came from running THIS branch's `screening_scorer.score_candidates`
(Layer 4 core signals — `verified`, `layer4_weighted_score`, gates, catalyst strength; NO LLM
call) directly over the 2026-08-19 candidates (11 rows). The optional Buffett/O'Neil
quant-enrichment and options-telemetry passes (`alphalens thematic score`'s CLI wrapper) were
**not** run — they are display-only additions the brief's `_row_to_facts` never reads, and
skipping them avoided an unrelated Alpha Vantage 25-req/day budget risk. The resulting scored
frame (15 rows — `ZG`/`UWMC` each appear twice, once per theme, and `score_candidates`'s
ticker-only merge Cartesian-multiplies a repeated ticker; a real finding, not fixed here since
it does not affect any ticker in the final slice) was copied to the real local
`~/.alphalens/thematic_scored/2026-08-19.parquet` (legitimate real data, consistent with the
documented Mac-side rsync convention) and OHLCV came from the SAME real run's
`get_default_yfinance_client().cached_daily_ohlcv` side effect, already on disk at
`~/.alphalens/thematic_ohlcv/`.

`scripts/record_golden_brief.py`'s `ASOF` / `SLICE_TICKERS` constants were updated to
`2026-08-19` / `("PSNL", "CRSP", "ABUS", "MRVI", "RDN")` — five single-occurrence tickers chosen
for compositional diversity (§2), sidestepping the `ZG`/`UWMC` duplicate-ticker Cartesian issue
above. The script itself ran unmodified otherwise; stale artifacts from the superseded
2026-05-24 slice (`scored.parquet`, four old cassettes, four old OHLCV files, the old golden
parquet + meta.json) were deleted first so the fixture tree holds only the new recording.

## 2. Composition of the re-cut brief fixture

| ticker | theme | `channel_support_status` | `channel_grounding_status` | `channel_type` | `brief_model_used` | `layer4_weighted_score` |
|---|---|---|---|---|---|---|
| PSNL | cancer_vaccine | suggestive | grounded | category_attention | flash | 1 |
| CRSP | melanoma | not_established | candidate_misfit | none | flash | 1 |
| ABUS | phase_3_trial | suggestive | grounded | category_attention | flash | 2 |
| MRVI | phase_3_trial | suggestive | grounded | supplier_input | flash | 1 |
| RDN | retail_sales | not_established | theme_misroute | none | flash | 2 |

Per-status counts: `established` **0**, `suggestive` **3**, `not_established` **2**.
Per-grounding counts: `grounded` **3**, `candidate_misfit` **1**, `theme_misroute` **1**,
`unknown` **0**.

**All four required kinds were obtained except `established`** — see §4.2. The other three were
obtained, and BOTH non-grounded conditions (`candidate_misfit` and `theme_misroute`, not just
one) are represented, per the operator's explicit ask. All five score
`layer4_weighted_score < 4`, so this recording exercises only the Flash routing path;
`test_golden_brief_replay.py::test_model_routing_by_score` says so explicitly — Pro-path
coverage is not re-verified by this recording (it was covered by the superseded 2026-05-24
slice, whose cassettes are gone).

### 2.1 Reading the projection — does the new contract actually show up

Quoted verbatim from the recorded cassettes (`fixtures/brief_day/cassettes/`, ticker identified
via the rendered `<facts>` block's `ticker:` line):

**A `not_established` row does not assert a benefit — RDN's `tldr`:**

> "RDN surfaced from Broadcom's sharp decline on financing concerns; no company-specific
> cash-flow path from this event to Radian was established."

**CRSP's `tldr`, same shape:**

> "CRISPR Therapeutics surfaced from an mRNA cancer vaccine Phase 3 success for melanoma; no
> company-specific cash-flow path from that event to this company was established."

Neither states a benefit, a mechanism, or a return direction. CRSP's `supply_chain_reasoning`
goes further and names the misroute explicitly: "The event describes a competitor's vaccine
trial result; CRISPR Therapeutics develops gene-edited cell therapies, not mRNA vaccines... The
pairing rests on the theme tag 'melanoma' alone." This is exactly the honest-null-case shape
§5.3 specifies, in real generated prose, not a template.

**A `suggestive` row names the missing link explicitly — MRVI's `tldr`:**

> "A plausible supplier_input channel runs from the Moderna/Merck Phase 3 mRNA cancer vaccine
> success to increased demand for Maravai's TriLink CleanCap and nucleotides, but the event does
> not state that any Maravai products were used in the trial or that Maravai will receive
> associated orders."

The missing link is named ("Maravai's products were used," "associated orders"), not gestured
at — the §5.3 defect case ("some uncertainty") does not appear anywhere in the five rows.

**Guard status confirms `guard_applies` scoping exactly as designed:** the two
`not_established` rows (RDN, CRSP) both stamp `brief_support_guard_status = clean` (the guard
was IN scope, scanned, found nothing to fire); the three `suggestive` + `grounded` rows (PSNL,
ABUS, MRVI) all stamp `not_applicable` (the guard never scanned — `guard_applies` returns False
for a grounded row regardless of level). `brief_support_guard_violations = 0` on every row.

**No adverse-direction `suggestive` row landed in this fixture.** All three `suggestive` rows
here (PSNL/ABUS/MRVI) describe increased demand — a positive channel. Two rows from the
EXPLORATORY (non-recorded) 2026-08-18 run did carry an adverse `suggestive` + `grounded`
channel — `GO`: *"potential negative impact on revenue for small-cap retailers... in the near
term"* and `OLLI`: *"reduced customer demand, leading to lower revenue in the near term"* — and
their prose stated the adverse direction honestly rather than flipping it into benefit language,
which is the correct behaviour §5.4 asks for. Neither ticker made it into the final five-ticker
slice (chosen for support/grounding diversity, not for direction), so this fixture does not
contain a committed example of it — the gap analysis in
`docs/research/grounding_and_prose_honesty_design_2026_08_20.md` §13.3 is confirmed by code
inspection (`support_guard.guard_applies`) and by this uncommitted exploratory data, not by a
row in the recorded golden.

## 3. A production defect the recording caught, and where it actually was

CRSP's first brief-generation draw truncated at the golden's base 2000-token cap
(`finish_reason=length`); the SAME retry ladder the live pipeline uses
(`generator.generate_brief_with_retry`) escalated to 4000 tokens and succeeded. Both cassettes
were correctly recorded (the retry ladder tees every draw), and this is the retry mechanism
working as designed, not a production gap — see
`docs/research/grounding_and_prose_honesty_design_2026_08_20.md` §13.1 for the full read and
the actual defect that was found (a naive cassette-glob helper in four `tests/golden/*.py` eval
files, not the generator). Fixed with a shared, TDD-covered
`tests.golden.replay_client.load_final_answer_cassette_records`; no production code changed, no
extra live call was needed to "clean" the cassette set — deleting the truncated cassette would
have BROKEN `test_golden_brief_replay.py`'s replay of the retry sequence, since a replay
re-derives the SAME escalation path the live recording took and needs both cassettes present.

## 4. Request descriptors and the map row-set

| fixture | stage-A cassette key (v3/v2, unchanged) | stage-A cassette key (v4/v3) |
|---|---|---|
| `quantum_2026_05_24` | `ae60f5beae47e93b57467977cf47a54d8c35d03770c12da2e1ccf749ab669289` | **same** |
| `nvda_ising_2026_04_14` | `98e3d8694d7adb8a7b08a876b04da05021596f1b920c1fcf95ab756b6a2eda49` | **same** |

Confirms the design memo's own claim (§7): stage A's prompt, schema and sampling do not move in
this increment, only the `mapper-freeze-v3 → v4` cohort marker. Both `--llm-only` recordings
still made a genuinely LIVE stage-A call — the guard refuses to reuse a cassette across recording
versions — the descriptor simply happens to hash the same because nothing in the request
changed. Stage-B moved for real: `channel_config_version.schema` `channel-assess-v1 →
channel-assess-v2`, both `prompt_sha` and `schema_sha` moved, `max_output_tokens` unchanged at
4000, `_ASSESS_VOTES` unchanged at 3.

| fixture | rows before | rows after | tickers before | tickers after |
|---|---|---|---|---|
| `quantum_2026_05_24` | 1 | 2 | `RGTI` | `RGTI`, `QUBT` |
| `nvda_ising_2026_04_14` | 3 | 3 | `QBTS`, `QUBT`, `RGTI` | `QBTS`, `QUBT`, `RGTI` |

**`quantum_2026_05_24`'s row set is NOT stable this time** — QUBT is a new row alongside the
kept RGTI. Unlike the 2026-08-19 rebaseline (which found the row set held under a full prompt
rewrite and a new stage), this is a genuinely different result under an UNCHANGED stage-A
request, attributable to DeepSeek's own server-side non-determinism (`map_themes`'s own
docstring already names this: "the 6×/day reruns... must not re-roll the (server-side
non-deterministic) DeepSeek MoE proposal") — confirmed here by the OpenRouter `provider CHANGED`
log lines seen mid-recording (`SiliconFlow → AtlasCloud → Novita` across the batch). This is
data, not a defect: `map_themes_projection`'s own docstring already excludes `llm_confidence`
and prose from the golden precisely because they are not reproducible under replay, and the row
SET itself is evidently in the same category for an identical request at a different point in
time. Read the row-count invariant (never drops) as what continues to hold; do not read "same
tickers" as guaranteed.

Both fixtures' stage-B answers, for the record (not asserted by the golden, per
`map_themes_projection`'s own documented limit — §5 of this note repeats the caveat):

* `quantum_2026_05_24`: `RGTI` and `QUBT` both `suggestive` / `financing_ma` / `grounded`.
* `nvda_ising_2026_04_14`: `QBTS` and `RGTI` `suggestive` / `category_attention` / `grounded`;
  `QUBT` `suggestive` / `customer_demand` / `grounded`.

Schema: 42 → 47 columns on both fixtures — the design memo's 11 → 16 `channel_*` columns (§4.4)
plus `channel_config_version` = 17 `channel_*`-prefixed columns total, confirmed present:
`channel_support_status`, `channel_grounding_status`, `channel_grounding_quote`,
`channel_grounding_reason`, `channel_grounding_agree_n`, `channel_grounding_quote_verbatim`,
`channel_type`, `channel_text`, `channel_evidence`, `channel_falsifier`, `channel_confidence`,
`channel_vote_k`, `channel_vote_valid_n`, `channel_support_dispersion`,
`channel_assessment_outcome`, `channel_assessed_at`, `channel_config_version`.

## 5. Standing caveat, unchanged

With k = 3 the three identical stage-B requests collapse to ONE cassette key, so the replayed
`channel_vote_valid_n`, `channel_support_dispersion` and `channel_grounding_agree_n` are always
the unanimous case. **The golden proves nothing about vote stability or about grounding
agreement**, and a reader must not treat a green replay as evidence on either. This is
unchanged from the 2026-08-19 note and from the design memo §9 risk 1.

## 6. Model, provider, and cost

Model: `deepseek/deepseek-v4-pro` for both map stages (proposal + assessment);
`deepseek/deepseek-v4-flash` for all five brief rows (every ticker scored
`layer4_weighted_score < 4`). OpenRouter served the batch from multiple backends —
`AtlasCloud`/`Novita`/`SiliconFlow` for the map calls, `Novita`/`CoreWeave`/`DeepInfra`/
`DigitalOcean` for the brief calls — read directly off each cassette's recorded `provider`
field, not asserted from memory.

**Exact cost of the 13 cassettes committed to the fixture tree** (summed from each cassette's
OpenRouter `usage.cost`): map_day v4 **$0.0313**, map_day_nvda_ising v3 **$0.0320**, brief_day
**$0.0019** — **$0.0651 total**. This is the reliable number.

**Total session spend was higher.** Two full exploratory `map_themes` days (2026-08-18,
2026-08-19 — roughly 8 themes × 1 stage-A + up to 5 stage-B calls each) were run in scratch
output directories while searching for compositional diversity, plus a third day started and
killed mid-run; none of those cassettes are committed. The shared OpenRouter key's
`usage_daily` figure cannot isolate this session's spend from the VPS's own 6×/day production
cron on the same key and same day, so no single authoritative total exists. A rough estimate
from the per-call costs observed in the committed sample (~$0.005–0.02 per stage-A pro call at
the 8000-token cap, ~$0.0002–0.007 per stage-B call at 4000) against the themes actually
attempted puts total session spend in the **$0.25–0.35** range, of which the $0.0651 above is
the exact, auditable portion.

## 7. Held constant

Map fixtures: every non-LLM surface (Polygon vendor cassette, 10-K cache, Form-4 slice,
market-cap map, catalyst events/news window) is byte-identical to the prior recording;
`frozen_surfaces` in each new `provenance.json` was written by the recorder from the artifacts,
never by hand, and `test_golden_map_provenance.py` re-checks every digest for the current
recording on each run.

Brief fixture: not versioned, so there is nothing to hold constant against — the whole slice was
re-cut, per the design memo's explicit instruction (§7: "re-cut from a post-#1066 day").

## 8. Test-suite maintenance beyond the cassettes

Re-cutting the brief slice broke several `tests/golden/*.py` modules that pinned content
specific to the superseded 2026-05-24 tickers (`DFIN`/`QLYS`/`QUBT`/`MANH`), independent of the
truncation-loader defect in §3:

* `test_golden_brief_faithfulness.py` — `_KNOWN_FACTS` rewritten for the five new tickers;
  `TestBoundaryPins`'s GROUNDED-sign-strip / GROUNDED-after-rounding / DISTORTED /
  gross-overstatement-FABRICATED cases rebuilt against REAL numeric facts pulled from the new
  cassettes (CRSP `-22.1%` off 52w-high, MRVI `72.1%` above MA200, ABUS `-4.6%` off 52w-high, RDN
  `-8.4%` off 52w-high) with the same synthetic-seeded-prose-against-real-facts pattern the
  original tests already used — no cassette content was invented, only the pairing of a
  hand-written test string against a real fact substrate, matching how `test_grounded_sign_strip`
  etc. always worked. `test_manh_negation_guard_does_not_fire` was removed (its invariant is
  fully subsumed by `test_no_characterization_violations_over_golden`, which already iterates
  every cassette).
* `test_financing_claims.py`, `test_fabrication_triage.py`, `test_faithfulness_measurement.py` —
  hardcoded `2026-05-24.parquet` paths and `n_briefs == 4` counts updated to
  `2026-08-19.parquet` / `5`.
* `test_golden_brief_replay.py::test_model_routing_by_score` — rewritten to assert all five
  tickers route Flash (§2), with a comment naming that Pro-path coverage is not re-verified by
  this recording.
* `tests/golden/test_golden_map_provenance.py` — the pending note's "second, subtler miss"
  (`split_cassette_records` misclassifying the OLD `map_day/v3` and `map_day_nvda_ising/v2`
  stage-B cassettes, which carry the pre-rename `channel_status` field, as a second stage-A
  cassette) is real and PERMANENT for those two specific recordings — not something re-recording
  resolves, since they stay on disk unmodified. `_derivable_recording_versions()` excludes
  exactly those two `(fixture, version)` pairs from the four checks that re-classify cassettes
  (`test_every_recording_agrees_with_its_cassette_event_and_descriptor`,
  `test_the_stage_a_cassette_is_the_proposal_call`,
  `test_a_recording_with_stage_b_cassettes_carries_the_block`,
  `test_a_pre_stage_b_recording_keeps_its_own_schema_version`) — their COMPLETENESS is still
  checked by `test_every_recording_carries_a_complete_provenance_file` (unaffected, no cassette
  re-classification). `TestPreGroundingVocabularyRecordingsStillRaiseLoud` pins, as a positive
  control, that `split_cassette_records` still raises `ValueError` for both — the classifier
  itself was NOT relaxed, only the test loop's iteration scope, per the pending note's explicit
  prohibition.

## 9. Verification

Golden suite: `cd apps/alphalens-research && env -u VIRTUAL_ENV uv run --project ../.. python -m
unittest discover -s tests/golden -t .` — **196 tests, all green** (rerun fresh in the
foreground to confirm, independent of any earlier backgrounded run).

Full research suite: `uv run --project . python -m unittest discover -s
apps/alphalens-research/tests -t apps/alphalens-research` — **8161 tests, OK (skipped=19,
pre-existing and unrelated)**.

`ruff check apps/alphalens-pipeline apps/alphalens-research apps/alphalens-django` — all checks
passed. `pyright` (repo root config, the blocking CI gate) — **0 errors** (80 pre-existing
`broker_contract.*` unresolved-import warnings, unrelated to this change, present before it).
