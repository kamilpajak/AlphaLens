# Daily-brief jargon removal — implementation design

## 1. Root cause

`_format_facts_block` in `apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/prompts.py` injects internal pipeline-stage labels (`Phase C`, `Phase D`, `weighted_score`, `verified gates: tenk,press,insider`) into the `<facts>` block sent to DeepSeek; the model echoes those labels verbatim into reader-facing brief prose. Fix = relabel the fact labels to reader-neutral text (source-side), which removes the tokens the model copies from.

## 2. Relabel map

All edits in `apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/prompts.py`. Each new label was verified accurate against the metric source.

| file:line | current verbatim | new reader-neutral text |
|---|---|---|
| prompts.py:160 | `f"weighted_score: {facts['weighted_score']}/5 (Phase D signal alignment)\n"` | `f"composite signal score: {facts['weighted_score']}/5 (1 = weak alignment, 5 = strong alignment across catalyst, cash-flow/valuation, value-or-reversal, and momentum signals; not a buy rating)\n"` |
| prompts.py:161 | `f"Phase C rationale: {facts.get('rationale', '')}\n"` | `f"theme-fit rationale: {facts.get('rationale', '')}\n"` |
| prompts.py:162 | `f"verified gates: {facts.get('gates_passed_str', '')}\n"` | `f"corroborating evidence checks passed: {_format_gates_passed(facts.get('gates_passed_str', ''))}\n"` |
| prompts.py:164 | `f"Phase D signals:\n"` | `f"quantitative signals:\n"` |

**Line-160 accuracy note (load-bearing):** `compose_weighted_score` (scorer.py:138-172) combines exactly four inputs — `catalyst_floor` (0-2), `fcff_positive`, `val_or_reversal` (magic-formula OR deep-drawdown reversal), `technicals_positive` — and clips to [1,5]. **Insider is intentionally HELD OUT** (scorer.py:156-161, :425). Do NOT name insider as a component. The `/5` scale is correct — keep it.

**Line-162 gate-token map** — add at module level in prompts.py (one-file change; leave `orchestrator.py:286` / `GATE_NAMES` and the raw `gates_passed_str` telemetry column untouched):

```python
_GATE_READER_PHRASES = {
    "tenk": "10-K filing mentions the theme",
    "press": "recent press coverage of the theme",
    "insider": "recent insider buying",
}

def _format_gates_passed(gates_passed_str: str) -> str:
    tokens = [t.strip() for t in (gates_passed_str or "").split(",") if t.strip()]
    return ", ".join(_GATE_READER_PHRASES.get(t, t) for t in tokens)
```

Renders e.g. `corroborating evidence checks passed: 10-K filing mentions the theme, recent press coverage of the theme`. Unrecognised tokens pass through verbatim (`.get(t, t)`); empty case (unreachable — orchestrator filters to verified=True) renders cleanly with a trailing space.

## 3. Stale-label fixes (same PR — same cassette re-record)

| file:line | current | corrected | why |
|---|---|---|---|
| prompts.py:169 | `f"- insider opportunistic buys (90d): {ins_str},"` | `f"- insider opportunistic buys (180d, buy-only): {ins_str},"` | Value is `insider_score_usd` from insider-v2 (`INSIDER_SIGNAL_VERSION="insider-v2-buyonly-180d-withinbuyers"`, `LOOKBACK_DAYS=180`): window is 180d AND metric is buy-only, not net buy−sell. Keep trailing comma + following percentile line. |
| prompts.py:165-168 | stale-label `# NOTE` comment | delete | The NOTE documents exactly the staleness this PR fixes; remove once fixed. |

**Out of scope (docstring-only, separate hygiene change, NO cassette impact):** `catalyst_signals.py:155` has TWO stale `0.25`s on one line — fix BOTH: `Moderate (≥0.25) → +1. Weak (<0.25) → 0` becomes `≥0.45` / `<0.45` (live `_FLOOR_MODERATE_THRESHOLD = 0.45`). Also `scorer.py:154` `moderate (≥0.25)` → `(≥0.45)`. These never reach the prompt — do NOT bundle into the cassette PR.

## 4. Output guard — YAGNI, do NOT ship the runtime retry

The relabel removes the echo source deterministically (the model cannot copy a token no longer in its input). A runtime retry-guard adds a retry round-trip cost, a clinical false-positive surface, and retry-cassette maintenance to guard a near-impossible residual. **Decision: do not ship the `BriefErrorKind.PHASE_ECHO` retry-guard.**

Instead ship a **test-only, zero-runtime-cost regression assertion** in the golden replay test, asserting no brief output field matches the internal-phase pattern. Use the **descriptor-anchored** regex (NOT bare `\bPhase\s+[A-E]\b` — that false-positives on biotech "Phase B clinical trial"):

```python
_INTERNAL_PHASE_RE = re.compile(
    r"\bPhase\s+[A-E]\b\s+(?:signal|signals|rationale|score|scored|alignment)",
    re.IGNORECASE,
)
```

Proven: matches all 5 internal phrases ("Phase D signal alignment", "Phase C rationale", "Phase D signals", …) and misses all clinical forms — "phase 3 trial", "phase III", "phase 1/2" (excluded by `[A-E]`) AND lettered "Phase B clinical trial" / "Phase A/B crossover" (excluded by the descriptor anchor). Keep the regex in the design memo so the runtime guard (mirroring `generator.py` `_contains_cjk` at :80-89 + the LANGUAGE_DRIFT retry at :262-264, :359) is a 30-min add IF post-relabel monitoring ever shows real residual echo.

## 5. Golden cassette impact — the critical step

**What breaks:** the cassette key is `sha256({model, contents, config})` where `contents` is the full prompt including `_format_facts_block` (`replay_client.py:67-75`). Editing any of lines 160/161/162/164/169 changes `contents` → key miss → `CassetteMissError` (fail-loud, `fail_on_miss=True`). All **4 cassettes** under `apps/alphalens-research/tests/golden/fixtures/brief_day/cassettes/` go stale, and **all 5 tests** in `test_golden_brief_replay.py` error (each calls `_replay_briefs`, which throws on the first miss). No test asserts the jargon text; `projection.json` excludes LLM prose, so expect it **UNCHANGED** after re-record (a non-empty projection diff signals an accidental routing/schema change, not a wording change).

**Re-record COSTS live DeepSeek calls** (4 tickers: DFIN/QLYS → Pro, QUBT/MANH → Flash; Pro has no free tier). It is NOT a deterministic regenerate.

**Procedure (the critical test step):**
1. Apply the prompts.py edits (§2 + §3).
2. `just test-golden` → expect 5 `CassetteMissError` failures (confirms the pin caught the change).
3. Ensure local data exists: `~/.alphalens/thematic_scored/2026-05-24.parquet` + `~/.alphalens/thematic_ohlcv/{DFIN,QLYS,QUBT,MANH}_2026-05-24.parquet` (rsync from VPS if absent — re-record reads them).
4. Re-record: `OPENROUTER_API_KEY=... uv run python -m scripts.record_golden_brief` **run from `apps/alphalens-research`**.
5. `just test-golden` → all 5 green.
6. PR-diff: 4 new cassette files (new sha filenames, `contents` without jargon) + verify `projection.json` UNCHANGED.

## 6. Other jargon sources

- **`rationale` value (map-themes / Phase C origin) — CLEAN, do NOT touch.** The theme_mapper prompt asks for "one to two sentences, factual, no marketing tone"; the value carries no phase vocabulary. Only the brief-prompt LABEL "Phase C rationale:" leaks — fixed at line 161. No `theme_mapper.py` change, no map-themes fixture re-record.
- **`template_id:` + "TYPED-FACT CITATION CONTRACT" block (`_format_template_facts_block`, prompts.py:56, 67-71) — in-scope-now, but currently dormant (no cassette exercises it; brief_day scored.parquet has no `catalyst_template_facts_json` column).** Two edits, NO re-record needed today:
  - Line 56: `lines = [f"template_id: {template_id}"]` → `lines: list[str] = []` (template_id stays as `brief_template_id` provenance column; no longer model-visible).
  - Lines 67-71: reword to reader-neutral but **keep the "do not re-derive from `<facts>` numerics" clause**: `"TYPED-FACT CITATION CONTRACT: every value above was extracted directly\nfrom the source document. Quote these values exactly in the brief — do\nnot paraphrase, round, convert units, or re-derive them from the\n<facts> numerics.\n"`
- **Docstrings naming Phase C/D (prompts.py:3, :9; generator.py:179) — non-leaking (not in prompt contents), optional cosmetic sweep.** Update to "score stage" wording for internal consistency; no cassette impact. Can ride the same PR or a follow-up.

## 7. PR shape + task breakdown (dependency order)

Single PR (one cassette re-record amortises all prompt edits):

1. Add `_GATE_READER_PHRASES` + `_format_gates_passed` helper to prompts.py.
2. Relabel prompts.py:160, :161, :162, :164 per §2; delete stale NOTE (:165-168); fix insider window :169 per §3.
3. Edit `_format_template_facts_block` (:56 drop template_id line, :67-71 reword contract) per §6.
4. (optional, same PR) docstring sweep prompts.py:3/:9 + generator.py:179.
5. Add the test-only `_INTERNAL_PHASE_RE` regression assertion to `test_golden_brief_replay.py` (§4).
6. **Re-record cassettes** (§5 procedure) + commit 4 new cassettes; verify `projection.json` unchanged.
7. Run full golden suite green; zen pre-merge review (touches shared prompt surface) with `deepseek/deepseek-v4-pro` before merge.

**Separate small PR (no cassette dep):** catalyst threshold docstring fixes (`catalyst_signals.py:155` both `0.25`s, `scorer.py:154`) per §3.

## 8. Risks / out-of-scope — do NOT change

- **Keep `/5` scale** (line 160) — correct (clipped [1,5] in scorer.py:172).
- **Do NOT name insider as a weighted_score component** — it is held out of the score by design.
- **Keep as-is (finance shorthand, not jargon):** `technicals:` RSI/MA50/MA200/ATR/volZ (:180); `durability (Buffett quant)` ROIC/owner-earnings/DCF (:103-106) — "Buffett" is intentional brand/lens language surfaced on the card by design; FCFF/valuation/52w/MA200 bullets (:170-184).
- **Do NOT touch the `rationale` VALUE** — reader content; only its label is jargon.
- **Do NOT ship the runtime PHASE_ECHO retry-guard** (YAGNI, §4).
- **Do NOT rename pipeline stages** (`Phase C`/`Phase D` as code identifiers / stage names stay — only reader-facing labels change).
- **The `/experiments` glossary "Phase A/B/C/D/E" entry is intentional educational content — leave it.** Same for any user-facing docs explaining the methodology stages.
- **Do NOT use the bare `\bPhase\s+[A-E]\b` regex anywhere** — false-positives on biotech lettered-phase prose (project has a GDELT biotech theme).
