# Golden map-themes replay — re-baseline across the channel-as-feature change (2026-08-19)

**Status:** RECORDED
**Scope:** `apps/alphalens-research/tests/golden/fixtures/map_day/` (v2 → v3) and
`apps/alphalens-research/tests/golden/fixtures/map_day_nvda_ising/` (v1 → v2)
**Approved by:** Kamil Pajak
**Change under test:** `feature/channel-as-feature` — the stage-A proposal prompt stops
asking for a transmission channel, and a per-candidate stage-B channel assessment is added
after the market-cap bracket. Design: `docs/research/channel_as_feature_design_2026_08_19.md`.

## What this is

The L3 map-themes characterization test
(`tests/golden/test_golden_map_characterization.py`) drives the real
`orchestrator.map_themes` offline with six external surfaces frozen. Two things moved this
time, and both change the cassette key on purpose:

* the stage-A prompt was rewritten (the proposal call no longer requires a channel and is
  told to order most-direct-first), so its request descriptor differs;
* stage B is a NEW request the previous recordings never made.

The replay therefore missed, loudly, on both fixtures — the intended fail-loud behaviour of
`replay_client.cassette_key`, not a defect. This note records the re-baseline. **Only the
Pro LLM cassettes were re-recorded**; every other input was held byte-for-byte constant,
and the previous recordings were left in place beside the new ones.

## Method

`scripts/record_golden_map.py --fixture NAME --llm-only`, once per fixture. The five
non-LLM surfaces were served from the already-frozen fixtures via
`tests/golden/map_fixtures.py::frozen_surfaces` — the same harness the replay test uses, so
the recording and the replay cannot drift. Stage B goes through that same replay/recording
client (`frozen_surfaces` patches `orchestrator._init_pro_client`, and
`_assess_channels_for_theme` forwards the client into `channel_assessor.assess_candidates`),
so no live call escaped the recorder.

Nothing was deleted. `map_day/cassettes_llm/v2` + `golden/v2` and
`map_day_nvda_ising/cassettes_llm/v1` + `golden/v1` are untouched; the new recordings were
added beside them, and `map_fixtures.py::current_recording` selects which one the test
loads.

## 1. Request descriptors

| fixture | stage-A cassette key (before) | stage-A cassette key (after) |
|---|---|---|
| `quantum_2026_05_24` | `d944b3c881f63d18b74d4caebe1ee089f7d77d71f163be12eee3a655c297fa8d` | `ae60f5beae47e93b57467977cf47a54d8c35d03770c12da2e1ccf749ab669289` |
| `nvda_ising_2026_04_14` | `ac60e901cec1e2f621b0e2a82ce30991ea0bd84db3a2ef08af00812059b20d8b` | `98e3d8694d7adb8a7b08a876b04da05021596f1b920c1fcf95ab756b6a2eda49` |

Stage-A model and sampling are unchanged (`deepseek/deepseek-v4-pro`, temperature 0.0,
`max_tokens` 8000); only the prompt text moved. Stage B is a second request shape:
same model, temperature 0.0, `max_tokens` 4000, its own response schema. Freeze tokens:
`mapper-freeze-v2` → `mapper-freeze-v3`, with the nested `channel-assess-v1` token.

## 2. Result of the replay

| fixture | rows before | rows after | tickers before | tickers after |
|---|---|---|---|---|
| `quantum_2026_05_24` | 1 | 1 | `RGTI` | `RGTI` |
| `nvda_ising_2026_04_14` | 3 | 3 | `QBTS`, `QUBT`, `RGTI` | `QBTS`, `QUBT`, `RGTI` |

**The row set did not move.** That is the result worth reading: the stage-A prompt was
rewritten and a whole new stage was added, and the same names still ship. It is the
never-drops invariant holding on a real recorded case rather than on a stub.

Schema: 26 → 42 columns. Added the eleven `channel_*` columns plus
`channel_config_version` and the five `shadow_strict_*` columns; removed
`transmission_channel` (its free text now lives in `channel_text` with a real status beside
it, no alias, no shim).

Stage-B answers in the new recordings, for the record — **not** asserted by the golden, see
§3:

* `quantum_2026_05_24` / `RGTI`: `partial`, `financing_ma`; theme shadow `refuse`
  (0 verified of 1 answered, 0 failed).
* `nvda_ising_2026_04_14`: `RGTI` and `QBTS` `verified` / `customer_demand`, `QUBT`
  `partial` / `category_attention`; theme shadow `keep` (2 of 3 answered, 0 failed).

## 3. What this golden CANNOT tell you (read this before trusting a green replay)

The cassette key is a sha256 over the whole request descriptor, and the `_ASSESS_VOTES = 3`
draws of one candidate are **identical requests**. They collapse to ONE cassette file
(`RecordingOpenRouter` writes `{key}.json`, last write wins), and the replay then serves
that single body to all three draws.

This is measured, not hypothetical. In the `nvda_ising_2026_04_14` v2 capture the live
draws for `QBTS` and `RGTI` split 2 `verified` + 1 `partial` (median `verified`,
`channel_vote_dispersion = 1`), while a replay of the same cassette returns a unanimous
`partial`. Every vote-derived field is therefore **unreproducible under replay** whenever
the live draws disagreed.

Consequences, all deliberate:

* No `channel_*` field is in the golden projection's per-row exemplar. Asserting one would
  pin an artefact of replay as if it were a measurement. The docstring of
  `tests/golden/projection.py::map_themes_projection` states this at the code.
* The committed `golden/<version>/<asof>.parquet` is the LIVE recording's output and does
  carry `dispersion = 1` rows that a replay does not reproduce. It is kept for provenance
  and for diffing captures, not as a replay expectation.
* A green replay evidences: the stage is wired, the schema is what the design memo says,
  the row set survives the change, and the gate verdicts did not move. It evidences
  **nothing** about whether the k-draw voting is stable. Vote stability is a live
  measurement — forward pre-registration
  (`docs/research/channel_feature_forward_prereg_2026_08_19.md`) §5 descriptive 5.

## 4. Provenance-document changes that came with this

map-themes stopped being a one-call stage, so the provenance machinery had to stop assuming
one cassette per recording (`cassette_record` raised `ValueError: expected exactly 1 LLM
cassette ... found 3`). `tests/golden/map_provenance.py` now:

* splits a recording's cassettes into the ONE stage-A proposal call and the N stage-B
  assessment calls, discriminated on the rendered response schema in the synthesised system
  message (`response_format` is the bare `{"type": "json_object"}` on this client);
* adds a `stage_b` block — cassette keys, model, sampling, system-message sha, and the
  vote-collapse caveat above — under schema version **3**;
* keeps pre-stage-B recordings at schema version **2**. The expected version is derived
  from the ARTIFACTS, not from the module's newest constant: back-stamping an older
  recording to v3 with an empty `stage_b` block would assert a stage it never ran.

`scripts/record_golden_map.py` also had to pass the now-required
`channel_config_version` into `mapper_config_version` — without it the recorder crashed
after making its live calls, which is how the gap was found.

Positive controls for all of the above are in
`tests/golden/test_golden_map_provenance.py::TestStageBIsDocumented`.

## 5. Held constant

Every non-LLM surface of both fixtures is byte-identical to before this re-baseline; the
per-recording `frozen_surfaces` manifest in each `provenance.json` records the sha256 of
each one at capture time, and `test_golden_map_provenance.py` re-checks every digest for
the current recording on each run. The `nvda_ising_2026_04_14` fixture keeps its
hand-authored catalyst-window disclosure (`seeded_surfaces`), copied into the new
recording's provenance by the recorder rather than by hand.
