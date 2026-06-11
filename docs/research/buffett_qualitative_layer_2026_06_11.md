# Buffett qualitative layer — LLM moat / management / understandability (#506)

**Status:** DRAFT
**Epic:** #500 (Buffett Mode-A lens) · **Ticket:** #506
**Date:** 2026-06-11

## What it does

Adds a QUALITATIVE layer to the Buffett Mode-A lens. For each candidate in a
thematic brief it runs DeepSeek Pro (via the canonical `OpenRouterClient`) over
three 10-K sections plus a block of PRE-COMPUTED numeric facts and CLASSIFIES
three Buffett qualities the quantitative lens cannot derive:

| Code | Quality | Field(s) | Type |
|------|---------|----------|------|
| F0 | Business understandability | `understandable` | bool |
| F3 | Economic moat | `moat_type` (brand / cost / switching_cost / network / regulatory / intangible_other / none), `moat_trend` (widening / stable / narrowing / unclear) | enums |
| F4 | Management candor | `management_candor` (candid / mixed / promotional / unclear) | enum |

Plus a free-text `rationale`.

It feeds the existing Mode-A lens as an OPT-IN `--qualitative` flag on
`alphalens buffett lens`. Off by default → zero LLM cost. When on, it adds
`MOAT / TREND / CANDOR / UNDERSTOOD` columns to the table and parquet.

## Doctrine compliance — numbers injected, LLM emits none

Per the CLAUDE.md "LLM training-cutoff blindness" rule, the LLM NEVER produces a
number. All numbers (trailing + 3-year ROIC, trailing + 3-year operating margin,
the net-buyback flag) are computed upstream in Python by the existing
`BuffettPanel` and INJECTED into the prompt as a labelled `FACTS` block. The LLM
only reasons / classifies over the 10-K text + those injected facts.

Two structural guards:

1. **Output schema has zero numeric fields.** `_QUALITATIVE_RESPONSE_SCHEMA`
   contains only `boolean`, `string`, and enum-`string` properties — no
   `"number"` / `"integer"` anywhere (recursively). A model that wanted to emit
   a number has nowhere to put it. Pinned by
   `tests.test_buffett_qualitative.TestResponseSchemaHasNoNumbers`.
2. **Prompt forbids numeric output.** The prompt tells the model to use the
   provided facts qualitatively but never to estimate or produce new numbers.

The prompt DOES contain the injected numeric facts (e.g. "Trailing ROIC: 17.0%")
— that is correct and intended (the model reasons over real numbers it did not
have to recall). The doctrine guard is on the OUTPUT schema, not the prompt
input.

## Section splitter (scope-limited)

A new pure `tenk_sections.py` carves the full-text 10-K (from
`thematic.verification.tenk_grep.fetch_10k_text`) into Item 1 (Business),
Item 1A (Risk Factors), Item 7 (MD&A) by case-insensitive regex on item
headings, each section bounded by the next item heading and truncated to a
character cap (default 30k). It is pure — no SEC calls, no file I/O. A missing
heading yields `None` for that section; junk / empty text yields all-None and
never crashes.

**Scope:** this layer reasons over a SINGLE latest 10-K's sections. Multi-year
10-K history (richer moat-trend evidence) and competitor 10-K fetching are
DEFERRED to #505.

## Cost

One DeepSeek Pro call per candidate. With three ~30k-char sections the input is
~25-40k tokens; at the post-promo ~$1.74/M input + $3.48/M output rate that is
roughly $0.05-0.10 per ticker. A brief of ~10 candidates costs ~$0.50-1.00 per
`--qualitative` run. Opt-in only — the daily thematic pipeline never triggers
it.

## Fail-soft

Every failure path degrades to a `QualitativeAssessment` with `None` fields
rather than raising:

- all three sections `None` (no fetchable 10-K) → all-None, NO LLM call;
- LLM client can't be built (missing `OPENROUTER_API_KEY`) / call raises → all-None;
- unparseable response → all-None;
- an out-of-vocabulary enum value → `None` for THAT field only (valid neighbours
  survive).

This matches the Mode-A lens stance: a thematic basket of small / recent names
often has no 10-K, and that patchy coverage is the honest "too hard" signal, not
a crash.

## Additive + unwired

Nothing in the daily thematic-build pipeline, systemd, Django, or the SPA runs
this. It is a standalone ad-hoc lens path behind the opt-in flag.

## Files

- `apps/alphalens-pipeline/alphalens_pipeline/buffett/tenk_sections.py` — pure splitter
- `apps/alphalens-pipeline/alphalens_pipeline/buffett/qualitative.py` — LLM classifier
- `apps/alphalens-pipeline/alphalens_cli/commands/buffett.py` — `--qualitative` flag wiring
- tests: `test_buffett_tenk_sections.py`, `test_buffett_qualitative.py`, `test_buffett_cli.py`
