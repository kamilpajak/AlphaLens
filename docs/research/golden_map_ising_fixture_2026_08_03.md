# Golden map-themes replay — second fixture: NVIDIA Ising, 2026-04-14

**Status:** RECORDED
**Scope:** `apps/alphalens-research/tests/golden/fixtures/map_day_nvda_ising/`
**Approved by:** Kamil Pajak

## What this is

A SECOND fixture for the L3 map-themes characterization test, added beside the
existing `quantum_computing @ 2026-05-24` one. Nothing was replaced. The two
answer different questions and both are now replayed by
`tests/golden/test_golden_map_characterization.py`:

* `quantum_2026_05_24` exists to isolate the prompt change. It was recorded
  under the old prompt (v1) and re-recorded under the new one (v2) with the
  other five surfaces held byte-identical, so the v1→v2 diff is attributable to
  the prompt alone. See `golden_map_rebaseline_2026_08_03.md`.
* `nvda_ising_2026_04_14` (this note) is the case the event conditioning is
  *about*: NVIDIA announces open models for quantum error correction, names no
  small-cap, and the interesting behaviour is which companies come out of a
  mapper that reads the event rather than the theme word.

## Method

One full six-surface capture:

```
uv run python -m scripts.record_golden_map --fixture nvda_ising_2026_04_14
```

Every surface came from real data already on disk or from the live vendor. No
value in this fixture was hand-written.

| # | surface | how it was captured | fixture file |
|---|---|---|---|
| 1 | Pro LLM (`theme_mapper`) | one live DeepSeek v4 Pro call | `cassettes_llm/v1/ac60e901….json` |
| 2 | Polygon press | live `get_news_range` over the 30-day window, then trimmed to candidate-tagged rows (6178 → 14) | `cassettes_vendor/86674e2e….json` |
| 3 | SEC 10-K | copied from the real text cache `~/.alphalens/thematic_tenk`, selected by the production `_find_cached` at this asof | `tenk_cache/{QBTS,QUBT,RGTI}_*.txt` |
| 4 | yfinance market cap | live PIT lookup (`close(asof) × shares(≤asof)`), teed during the run | `mcap.json` |
| 5 | Form-4 insider | trimmed from `~/.alphalens/form4_parquet` — 84 insider CIKs, 2446 rows over the four classification years | `form4_parquet/transaction_year=*/` |
| 6 | Catalyst event + news | copied from `~/.alphalens/thematic_events/2026-04-14.parquet` and `~/.alphalens/thematic_news/2026-04-14.parquet` | `events/`, `news/` |

The Ising press release is the only theme-tagged event on disk inside the
resolver's 30-day lookback from this asof, so the window is a single date and
the fixture is unambiguous about which event it characterizes.

sha256 of everything committed:

| file | sha256 |
|---|---|
| `cassettes_llm/v1/ac60e901….json` | `df3f07ca6c3959d3cc91dae3205e05fbee67e2eb1d187ba0e0ff401c8f5727d8` |
| `cassettes_vendor/86674e2e….json` | `108868b42ef6cf83f1c328667f5fc201697a17730df25f9edd8b6de776ad646e` |
| `events/2026-04-14.parquet` | `13d33b7e5c79fa87e8b1aaebac9389c0a14bfca2721aa2fef4a6bcaf272eea7a` |
| `news/2026-04-14.parquet` | `82a013772d2f1f5f88d3710a9f9918a5942f0ec0f721b9ff0b8a958ebf123649` |
| `tenk_cache/QBTS_2026-02-26.txt` | `e15c437fcb6a4c562b0836afc99eb6d0e6ab95c22196fd8bc94028fa77a6a992` |
| `tenk_cache/QUBT_2026-03-02.txt` | `74d5975cb3ac13456ed146ec17f32f846c6eaab4834d3020f54852970145fac2` |
| `tenk_cache/RGTI_2026-03-04.txt` | `480d01ecdeb71ac021a42baad390d875b825fdd60819df58aae05233f46ba49d` |
| `form4_parquet/transaction_year=2023/compacted.parquet` | `f0e481118b882ee1f81835de50852fda3b2e024c96f55dfcf5019652b2cd0c3a` |
| `form4_parquet/transaction_year=2024/compacted.parquet` | `cfd868aa2be25b481cbc37faaa5e04a9ea5ad222aa92d31e4a1a05297dbd2d27` |
| `form4_parquet/transaction_year=2025/compacted.parquet` | `0274161d3c1434922fccf0b7fd5525e2f1bbcfcbe9f8f84e7a00c7eaab8cc829` |
| `form4_parquet/transaction_year=2026/compacted.parquet` | `814a5f69e4fa93c2570f8e32e3eb36cf690d9d24c3a30e68811a3fb15de23446` |
| `mcap.json` | `666146864ebc6d36c35a0931cf8ae30227bad431896bd746ce175d841bd4dd66` |
| `golden/v1/projection.json` | `4b268aa24e7b80c32fec761e08e7fd3565b74cc440247bba4c49e2ff614cdca4` |
| `golden/v1/2026-04-14.parquet` | `09457b681cdcae69855df37c41731f5a574241adc7eb92131c0c4c909cb4ae80` |

These are the hashes AFTER the pre-commit `end-of-file-fixer` hook, which
appends a single trailing newline to the text and JSON files (the parquets are
untouched). Re-running the recorder produces files one byte shorter until that
hook runs, so compare hashes post-commit, not straight off the recorder. The
newline changes no behaviour: the replay was re-run after the hook and the
projection is unchanged.

Cross-check on the 10-K freeze: `tenk_cache/QUBT_2026-03-02.txt` and
`tenk_cache/RGTI_2026-03-04.txt` hash identically to the copies of the same two
filings already committed under `fixtures/map_day/tenk_cache/`. Both fixtures
selected the same real filing text out of `~/.alphalens/thematic_tenk` for these
tickers, independently, which is what a PIT-correct selector should do.

The `quantum_2026_05_24` tree was not touched by this capture (it has its own
directory; `git status` shows no change under `fixtures/map_day/`).

## 1. The request

| field | value |
|---|---|
| cassette key | `ac60e901cec1e2f621b0e2a82ce30991ea0bd84db3a2ef08af00812059b20d8b` |
| model | `deepseek/deepseek-v4-pro` |
| sampling | `temperature=0.0`, `max_tokens=8000`, `response_format={"type": "json_object"}` |
| prompt length (`contents`) | 8456 chars (system message 1113) |
| `prompt_sha` / `schema_sha` | `52b12550f344` / `ec5d56e9d13a` |
| freeze tag | `mapper-freeze-v2` |

Same prompt build and same sampling config as the `quantum_2026_05_24` v2
recording — only the event inside the fenced block differs:

```
<untrusted_event>
theme_tag: "quantum_computing"
event_type: "product_launch"
published_at: "2026-04-14"
headline: "NVIDIA Launches Ising, the Worlds First Open AI Models to Accelerate
           the Path to Useful Quantum Computers"
companies_named_in_event: NVDA
extracted_implications: "Quantum hardware vendors may benefit from Ising tooling"
</untrusted_event>
```

## 2. The recorded projection

The model proposed five candidates; the real market-cap bracket filter
(`DEFAULT_MCAP_RANGE` = 500M–10B, PIT at 2026-04-14) kept three.

| ticker | proposed | PIT market cap | in bracket | kept |
|---|---|---|---|---|
| NVDA | 0.60 | $4,776.18B | no | dropped |
| IONQ | 0.50 | $13.15B | no | dropped |
| QBTS | 0.60 | $6.28B | yes | kept |
| RGTI | 0.50 | $5.60B | yes | kept |
| QUBT | 0.40 | $1.82B | yes | kept |

`row_count` 3, `columns` 26, `tickers` `["QBTS", "QUBT", "RGTI"]`. Per-row gate
outcome, identical across the three:

| ticker | verified | gates passed | gates failed | gates unknown | mcap bucket | catalyst |
|---|---|---|---|---|---|---|
| QBTS | true | `tenk,press` | `insider` | — | 3B-10B | yes |
| QUBT | true | `tenk,press` | `insider` | — | 1B-3B | yes |
| RGTI | true | `tenk,press` | `insider` | — | 3B-10B | yes |

Every gate reached a decisive verdict for every row, which is what proves the
frozen 10-K text, the trimmed press cassette and the Form-4 slice actually fed
the real gate code rather than degrading to "unknown".

`search_keywords`: `quantum computing, quantum hardware, quantum processor,
qubit, quantum simulation, trapped ion, superconducting qubit, quantum
annealing, quantum cloud, error correction`.

## 3. Plain-language reading of this one draw

The article is an NVIDIA product launch. It names one public company — NVIDIA
itself — plus a long list of private labs and universities. The extraction stage
distilled one implication: "quantum hardware vendors may benefit from Ising
tooling".

In this recorded draw the model returned NVIDIA (the subject) and four quantum
hardware names, three of which the article never mentions. Each came with a
stated chain: for D-Wave, that Ising models address the annealer's native
problem class; for Rigetti, that the tooling lowers the cost of qubit
optimisation and error correction; for Quantum Computing Inc., that the whole
ecosystem iterates faster. The bracket filter then removed NVIDIA and IonQ on
size alone.

**Read that paragraph as a description of one sample, not a finding.** See §4.

## 4. What this evidence does and does not support

**This is a characterization fixture, not evidence about model quality.** It
answers "did this execution differ from the approved one" and nothing else.

* **One recorded response is one sample of a stochastic generator.**
  Temperature 0.0 makes the *replay* deterministic, not the recording: the same
  prompt and the same event, run 15 times, returned one target ticker 6 times
  out of 15. A capture made an hour later could contain a different candidate
  set, and the golden would then be that set. Nothing here supports "the mapper
  surfaces unnamed quantum companies from this event" — that is a claim about a
  distribution, and this fixture holds a single draw.
* **No assertion in the test names any ticker.** The test compares the whole
  projection against `golden/v1/projection.json`. There is deliberately no
  "QUBT must appear" assertion: at a measured 6/15 hit rate it would be flaky by
  construction and would dress a sampling artifact up as a specification.
* **N = 1 event, 1 theme, 1 date, 1 draw.**

What a green replay does support: the deterministic downstream machinery —
prompt rendering, response parsing, `_normalize` and its transmission-channel
requirement, the three verification gates, the PIT market-cap bracket, the
emitted schema — handles this recorded response the approved way.

Prohibition and invariant assertions ("the article subject must not be a
beneficiary", "a candidate must state a transmission channel that is true") are
deliberately **not** added here either. This fixture is in fact a good example
of why the first is under-specified: the model proposed NVIDIA, the subject of
the article, and for a product launch the subject can be a legitimate
beneficiary — a blanket prohibition would score that as a defect without
looking at the economics. Both assertions remain deferred to their own issue.

## 5. Fixture provenance

| field | value |
|---|---|
| fixture | `nvda_ising_2026_04_14` (`tests/golden/map_fixtures.py`) |
| recording version | `v1` (first recording of this case) |
| recorded | 2026-08-03 |
| recorded by | `scripts/record_golden_map.py --fixture nvda_ising_2026_04_14` |
| theme / asof | `quantum_computing` / `2026-04-14` |
| event | `nvda_ising_2026_04_14`, NVIDIA press release, published 2026-04-14 13:00 UTC |
| model | `deepseek/deepseek-v4-pro` |
| sampling | `temperature=0.0`, `max_tokens=8000`, `response_format={"type": "json_object"}` |
| cassette key | `ac60e901cec1e2f621b0e2a82ce30991ea0bd84db3a2ef08af00812059b20d8b` |
| `mapper_config_version` | `{"block_tag":"untrusted_event","implications_max":5,"max_candidates":15,"max_output_tokens":8000,"mcap_range":[500000000,10000000000],"model":"deepseek/deepseek-v4-pro","prompt_sha":"52b12550f344","schema":"mapper-freeze-v2","schema_sha":"ec5d56e9d13a","temperature":0.0}` |
| approved by | Kamil Pajak |

The same facts, plus a sha256 of every frozen surface, are committed in
machine-readable form as `golden/v1/provenance.json` and checked by
`tests/golden/test_golden_map_provenance.py`.

Re-recording this fixture bumps `current_recording` on its descriptor and adds a
new version directory beside `v1`; the recorder refuses to write into a version
that already holds a cassette, and writes the new recording's provenance file
last, after every frozen surface is on disk.
