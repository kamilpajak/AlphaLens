# Golden map-themes replay — LLM cassette re-baseline v1 → v2 (2026-08-03)

**Status:** RECORDED
**Scope:** `apps/alphalens-research/tests/golden/fixtures/map_day/`
**Approved by:** Kamil Pajak

## What this is

The L3 map-themes characterization test
(`tests/golden/test_golden_map_characterization.py`) drives the real
`orchestrator.map_themes` offline over one theme (`quantum_computing`, asof
`2026-05-24`) with six external surfaces frozen. The mapper prompt changed on
`feature/mapper-event-conditioning` (the proposal call now receives the resolved
catalyst event instead of a bare theme slug), so the LLM cassette key changed
and the replay missed — by design, fail-loud.

This note records the re-baseline: **only** the Pro LLM cassette was
re-recorded. Every other input was held byte-for-byte constant.

## Method

`scripts/record_golden_map.py --llm-only` made **one** live DeepSeek call and
served the other five surfaces from the existing frozen fixtures via
`tests/golden/map_fixtures.py::frozen_surfaces` — the same harness the replay
test uses, so the recording and the replay cannot drift.

The five held-constant surfaces, sha256 before and after the re-record:

| surface | file | sha256 before | sha256 after |
|---|---|---|---|
| Polygon press cassette | `cassettes_vendor/6182cfb7….json` | `5b9aee9eed8fef46fde31f7262c6e9ec9dd64a16d47e0e8ef44ec2361de6b64c` | unchanged |
| catalyst events | `events/2026-05-15.parquet` | `33f0ba41de5157fc0a94b0aa5c3bc67a677406fadcbc2455eea1f1bafcdbb148` | unchanged |
| catalyst events | `events/2026-05-18.parquet` | `060749a190bd88ebde47a8f0442cf40f4ce730044e742f500fd787e39e4f9684` | unchanged |
| catalyst events | `events/2026-05-24.parquet` | `9b8ff0b26945749b4519a87e95891eb29616b978ef7f6e269b1b20b0674fa731` | unchanged |
| news window | `news/2026-05-15.parquet` | `b9f56e625648f013aecb9c8c8bfb4f7064332ddc8beb40c632e5e611e7549fe4` | unchanged |
| news window | `news/2026-05-18.parquet` | `2a55e2841e8f2afc6b8d6ba6fe3fb18a90c888c2639cf367a62afaa9e1203ab5` | unchanged |
| news window | `news/2026-05-24.parquet` | `a7d8b78e3b420c30a7986b7e43e361670c069bb60af1a984bcec37c7dfaca3ee` | unchanged |
| 10-K text cache | `tenk_cache/QUBT_2026-03-02.txt` | `74d5975cb3ac13456ed146ec17f32f846c6eaab4834d3020f54852970145fac2` | unchanged |
| 10-K text cache | `tenk_cache/RGTI_2026-03-04.txt` | `480d01ecdeb71ac021a42baad390d875b825fdd60819df58aae05233f46ba49d` | unchanged |
| Form-4 slice | `form4_parquet/transaction_year=2023/compacted.parquet` | `912a0ed3e585a4eab6a8b6e9b904ceba79b5508de657315dfa8fb9c5062d5d30` | unchanged |
| Form-4 slice | `form4_parquet/transaction_year=2024/compacted.parquet` | `f18c044b9725ff3001cc0265149854da8e13231c0a8c8729bd789a11c804a78d` | unchanged |
| Form-4 slice | `form4_parquet/transaction_year=2025/compacted.parquet` | `33085445028dc8c6e59f785b14588e1298f4290ea0f1d223a017439182d586c9` | unchanged |
| Form-4 slice | `form4_parquet/transaction_year=2026/compacted.parquet` | `87761b78ceae1a314f6c96e1117105aaeaa19ef02a73929bcaebd3bc1617f3ed` | unchanged |
| market-cap map | `mcap.json` | `d3f3356aa1fbfec0bfad4d0c87b98311e88ac3da361604ef138ca262cc044b67` | unchanged |

Nothing was deleted. The v1 recording moved into its own version directory with
its content intact (`cassettes_llm/v1/a56054c7….json` still hashes to
`9f97d6f7d756418a0be6d9d692be758d39c6bf270ed383a602dc7ef939e93424`,
`golden/v1/projection.json` to
`9216f02cdbfadb64e8066c9acf63d5d4890b62257ce84f674986d437a7479eb0`), and v2 was
added beside it. The fixture descriptor's `current_recording`
(`map_fixtures.QUANTUM_2026_05_24`) selects which one the test loads; the
fixture tree is never globbed for "whatever is on disk".

## 1. What changed in the request descriptor

The cassette key is a sha256 over the whole request — model, prompt text and
sampling config.

| | v1 | v2 |
|---|---|---|
| cassette key | `a56054c7d28c7b15c8696fcb57eb8f592b28602723fcee36cdf1b696f64b33af` | `d944b3c881f63d18b74d4caebe1ee089f7d77d71f163be12eee3a655c297fa8d` |
| model | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-v4-pro` (unchanged) |
| temperature | `0.0` | `0.0` (unchanged) |
| max_tokens | `8000` | `8000` (unchanged) |
| response_format | `{"type": "json_object"}` | unchanged |
| prompt length (`contents`) | 2161 chars | 8778 chars |
| system message length | 891 chars | 1113 chars |
| `prompt_sha` | `308d9c81a82c` | `52b12550f344` |
| `schema_sha` | `05321fb18541` | `ec5d56e9d13a` |
| freeze tag | `mapper-freeze-v1` | `mapper-freeze-v2` |

Only the prompt text and the JSON response schema moved. The model and every
sampling parameter are identical, so the key change is fully attributable to
the prompt/schema change.

### What the model now receives that it did not before

v1 sent one variable — the theme slug:

```
<theme>quantum_computing</theme>
```

v2 sends the resolved catalyst event, fenced and sanitized:

```
<untrusted_event>
theme_tag: "quantum_computing"
event_type: "earnings"
published_at: "2026-05-24"
headline: "Weekend Round-Up: Nvidia's Q1 Triumph, SpaceX's IPO Filing, Musk's
           OpenAI Controversy, Google's AI Leap And More"
companies_named_in_event: NVDA, GOOG, GOOGL, ARM
extracted_implications: "Other AI chip designers could see increased investor
  interest … | Companies developing competing AI models … | Smaller quantum
  computing startups may attract more investment following significant
  government funding in the sector."
</untrusted_event>
```

New inputs: the headline, the event type, the publication date, the resolved
entity list, and the extraction stage's body read-outs. The theme slug is
demoted to secondary routing context.

New instructions, in order of expected effect on output:

1. **The minimum candidate count is gone.** v1 said "Output 5 to 15 candidates";
   v2 says "between 0 and 15 … there is no minimum … an empty answer is a
   correct answer". The ceiling was deliberately left at 15 so a drop in volume
   is attributable to the removed floor and the channel test, not to a narrower
   cap.
2. **Every candidate must state a transmission channel** — a chain of at least
   two links from an event fact to a named line of that company's economics.
   `transmission_channel` is a required response field, and `_normalize` drops
   any candidate that omits it.
3. **A direction test**: the channel must move the company's economics
   favourably; a company the event harms is dropped.
4. **A materiality test**: no plausible 12-month effect means drop.
5. **Explicit anti-patterns**: shared vocabulary with the theme tag, "well-known
   name in a loosely related sector", "more attention to X", and chains of three
   or more speculative hops are all named as non-channels.
6. **`event_read`** (one-sentence factual read of the event) and
   `no_candidates_reason` are new response fields.
7. **`search_keywords` was re-scoped** from "the theme" to "this line of
   business", with an explicit instruction not to return the event's proper
   nouns.
8. **A prompt-injection fence** around the untrusted block, backed by code-side
   sanitization in `theme_mapper._sanitize`.

## 2. Old vs new projection

| | v1 | v2 |
|---|---|---|
| candidates proposed by the model | 10 | 3 |
| proposed tickers | IONQ, RGTI, QBTS, QUBT, ARQQ, IBM, GOOGL, MSFT, HON, INTC | IONQ, RGTI, QBTS |
| inside the 500M–10B bracket | QUBT, RGTI | RGTI |
| `row_count` | 2 | 1 |
| `tickers` | `["QUBT", "RGTI"]` | `["RGTI"]` |
| columns | 25 | 26 (`transmission_channel` added) |

Per-row gate outcomes:

| ticker | recording | verified | gates passed | gates failed | gates unknown | mcap bucket | catalyst | llm_confidence |
|---|---|---|---|---|---|---|---|---|
| QUBT | v1 | true | `tenk,press` | `insider` | — | 1B-3B | yes | 0.85 |
| RGTI | v1 | true | `tenk,press` | `insider` | — | 3B-10B | yes | 0.95 |
| RGTI | v2 | true | `tenk,press` | `insider` | — | 3B-10B | yes | 0.50 |
| QUBT | v2 | *not proposed* | — | — | — | — | — | — |

The gate machinery behaves identically on the row that survives in both: same
two gates pass, the same gate fails, nothing is unknown, the same catalyst is
attached, the same mcap bucket. What moved is which names reached the gates,
and the model's self-reported confidence in them.

`search_keywords` also moved, from theme vocabulary to event-plus-domain
vocabulary:

* v1: `quantum computing, qubit, quantum annealing, trapped-ion, superconducting
  qubit, quantum hardware, quantum software, quantum processor, quantum
  algorithm, quantum encryption`
* v2: `quantum computing, government funding, quantum computer, quantum
  processor, trapped ion, superconducting qubit, quantum cloud, quantum
  algorithm, quantum as a service, quantum advantage`

Note `government funding` — a phrase drawn from the event, not the theme. It did
not change any gate verdict on this fixture, but it is the kind of drift the
re-scoped keyword instruction is meant to bound and is worth watching.

### Where the two candidates went

Both drops are mechanical and neither is a fixture artifact:

* **IONQ ($23.75B) and QBTS ($10.89B)** were proposed in both recordings and
  dropped by the real market-cap bracket filter (`DEFAULT_MCAP_RANGE` =
  500M–10B) in both.
* **QUBT** was proposed by v1 (confidence 0.85) and **not proposed at all** by
  v2. Its market cap ($2.78B) is inside the bracket and its 10-K text is in the
  frozen cache, so nothing downstream removed it — the model simply did not name
  it.

All three v2 tickers are present in the frozen `mcap.json`, so the frozen
market-cap map did not starve this run. It would have if the model had named a
ticker outside the ten v1 tickers: an unknown ticker gets `None` and is dropped
by the bracket filter. That bound is a known and accepted property of holding
the surfaces constant — a wider ticker universe needs a deliberate full
re-capture, which would move several variables at once and is out of scope here.

## 3. Plain-language reading of this one case

The event behind this fixture is a Benzinga weekend round-up whose headline is
about Nvidia earnings, a SpaceX IPO filing, an OpenAI controversy and Google AI.
Quantum computing appears only in the body, distilled by the extraction stage
into "significant government funding" for the sector.

Under v1 the model saw the word `quantum_computing` and produced a
ten-name industry roster, ordered by how central quantum is to each business:
the four pure-plays, then IBM, Alphabet, Microsoft, Honeywell, Intel. That is a
"who works in this field" answer.

Under v2 the model wrote out what happened, latched onto the government-funding
implication, and returned three names — each with a stated chain from
"government funding for quantum computing" to that company's contract revenue.
The mega-caps disappeared: the same funding line does not move Alphabet or
Microsoft revenue. It also returned lower confidence (0.5/0.5/0.4 versus
0.95/0.95/0.9/0.85), which reads as the model pricing a specific causal claim
rather than a category membership.

On this one case the change did what it was designed to do: fewer names, each
attached to a stated mechanism, and the loosely-related large caps gone. QUBT —
a quantum pure-play inside the bracket that v1 kept and v2 did not name — is the
cost side of the same trade.

## 4. What this evidence does and does not support

**This is a characterization re-baseline, not evidence that the new behaviour is
better.** The golden replay answers one question — did this execution differ
from the approved one — and it now has a new approved execution to compare
against. It does not and cannot answer whether v2 picks better companies.

Two limits, stated plainly:

* **One recorded response is one sample of a stochastic generator.** Temperature
  is 0.0, which makes the *replay* deterministic, but not the recording: the
  same prompt and the same event run 15 times returned one target ticker 6 out
  of 15 times. A different draw on 2026-08-03 would have produced a different
  cassette and a different golden, and QUBT's absence could well flip back on a
  re-run. Nothing here should be read as "v2 drops QUBT".
* **N = 1 event, 1 theme, 1 date.** The drop from ten candidates to three, and
  the disappearance of the mega-caps, are a single observation on a fixture that
  was never selected to be representative.

What a green replay does support: the deterministic downstream machinery —
prompt rendering, response parsing, `_normalize` and its channel requirement,
the three wired verification gates (`orchestrator.GATE_NAMES`), the mcap
bracket, the emitted schema — handles the new response shape the approved way,
and the new `transmission_channel` column reaches the parquet.

Prohibition and invariant assertions ("the article subject must not be a
beneficiary", "a candidate must state a transmission channel that is true") are
deliberately **not** added here. The first is false for acquisitions and product
launches where the subject is a legitimate beneficiary; the second, as a test,
would only check that an explanation is present, not that it is correct. Both
are under-specified and are deferred to their own issue. Freezing an unvalidated
hypothesis into a characterization test would make the test assert something it
cannot see.

## 5. Fixture provenance

| field | value |
|---|---|
| fixture | `quantum_2026_05_24` (`tests/golden/map_fixtures.py`) |
| recording version | `v2` (the descriptor's `current_recording`) |
| recorded | 2026-08-03 |
| recorded by | `scripts/record_golden_map.py --fixture quantum_2026_05_24 --llm-only` (one live call) |
| theme / asof | `quantum_computing` / `2026-05-24` (unchanged from v1) |
| model | `deepseek/deepseek-v4-pro` |
| sampling | `temperature=0.0`, `max_tokens=8000`, `response_format={"type": "json_object"}` |
| cassette key | `d944b3c881f63d18b74d4caebe1ee089f7d77d71f163be12eee3a655c297fa8d` |
| `mapper_config_version` | `{"block_tag":"untrusted_event","implications_max":5,"max_candidates":15,"max_output_tokens":8000,"mcap_range":[500000000,10000000000],"model":"deepseek/deepseek-v4-pro","prompt_sha":"52b12550f344","schema":"mapper-freeze-v2","schema_sha":"ec5d56e9d13a","temperature":0.0}` |
| superseded recording | `v1`, recorded 2026-06-01 (PR #369), `mapper-freeze-v1`, `prompt_sha=308d9c81a82c`, key `a56054c7…` — kept under `cassettes_llm/v1/` + `golden/v1/` |
| approved by | Kamil Pajak |

Note on the v1 artifacts: `golden/v1/2026-05-24.parquet` predates the
`mapper_config_version` freeze column, so the v1 config version above is derived
from the `origin/main` code rather than read off that parquet. The v1 projection
is the authoritative v1 record.

### Machine-readable form

This table is the prose companion to
`golden/v2/provenance.json`, which carries the same facts in machine-readable
form plus a sha256 of every frozen surface. `golden/v1/provenance.json` exists
beside it for the superseded recording; both were written on 2026-08-03, so v1
carries `recorded_date` 2026-06-01 with `provenance_written` 2026-08-03 and a
`notes` field saying which of its fields were reconstructed rather than read off
an artifact (the config version above being the one).

`tests/golden/test_golden_map_provenance.py` refuses a recording without a
complete file, and re-reads the model, sampling, cassette key and event from the
artifacts so the document cannot quietly disagree with them.
