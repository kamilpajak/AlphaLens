# Brief LLM Truncation Fix — Design

**Status:** DRAFT (awaiting approval before implementation)
**Date:** 2026-07-28
**Authored via:** Workflow (3× understand → 3× propose → 3× judge → synthesis)

## Problem

The thematic `brief` stage LLM (DeepSeek v4-pro via OpenRouter) truncates its JSON
response, leaving the brief content empty. The SvelteKit card then silently falls
back to the raw screener `rationale`, rendered in italic
(`CandidateCard.svelte:357-363`). Found on ticker **XMTR**, brief date 2026-07-27
(1 of 18 cards).

### Confirmed mechanism — reasoning-token exhaustion (deterministic, not transient)

- `finish_reason=truncated` on BOTH the initial attempt (`max_output_tokens=2000`)
  AND the single retry (bumped to 4000) — `generator.py:330-414`.
- The brief JSON schema is only **4 string fields** (tldr ≤200, supply_chain ≤400,
  bear ≤250, catalyst_failure_exit ≤200) ≈ 1050 chars ≈ **300-500 output tokens**;
  `trade_setup` + numerics are computed in Python, not by the LLM.
- A 4000-token truncation on so tiny a schema means the **reasoning/thinking trace**
  (DeepSeek v4-pro is a reasoning model; reasoning tokens count against `max_tokens`)
  eats the budget before the JSON closes. This is **deterministic per prompt** — the
  6×/day reruns re-attempt XMTR (no caching: `orchestrator.py:433-541` overwrites the
  whole parquet each slot) but will keep re-truncating.

## Recommendation — HYBRID

**Reliable headroom ladder (primary) + honest observability (companion) +
reasoning-cap as a probe-gated optional enhancement.**

The primary fix uses only the `max_tokens` lever already wired through the canonical
OpenRouter client — no dependency on any unverified API parameter. The reasoning-cap
lever (bounding the trace) is kept OPTIONAL and gated behind a live probe, because the
workflow could not verify that OpenRouter honors a `reasoning` block for
deepseek-v4-pro (the research agent failed) and Proposal 2's proposed retry cap (4000)
is the exact value already shown to fail.

**Judge scores:** headroom-ladder **78** (approve-with-changes) · reasoning-cap 67
(unverified API dependency) · salvage-only 64 (complement, self-admits it is not a fix).

## Phases (TDD per phase)

### Phase 0 — Measure + verify (GATE, no production change)
Replace guessed ceilings with data; decide if Phase 5 is even viable.
- New opt-in live probe `tests/live/test_openrouter_brief_budget_live.py`
  (`OPENROUTER_LIVE_TEST=1`, non-gating, L4 pattern) — run a real XMTR-class Pro prompt
  at 4000/8000/16000/32000 and record observed `usage.completion_tokens` + `finish_reason`.
- `mcp__zen__apilookup` + one live probe: does deepseek-v4-pro accept a `reasoning`
  block and return NON-EMPTY content when bounded? Result decides Phase 5.
- Set base + ceiling constants from the measured p95/max + safety margin.

### Phase 1 — Expose token usage on the canonical client (enabler)
- `openrouter_client.py::_wrap_response` — add `.usage = payload.get("usage")` to the
  returned namespace (both branches). Additive; no other consumer reads it.

### Phase 2 — Budget headroom + bounded escalation ladder (PRIMARY FIX)
- `generator.py` — bump `_DEFAULT_MAX_OUTPUT_TOKENS` to the measured base; add
  `_MAX_OUTPUT_TOKENS_CEILING`; define an unambiguous doubling ladder (ceiling appears
  exactly once).
- `generate_brief_with_retry` — replace the one-shot 2000→4000 double with the ladder
  for `TRUNCATED` only; EMPTY / EMPTY_CONTENT / LANGUAGE_DRIFT keep their single
  base-cap retry. temperature=0 on every escalation.
- Log `completion_tokens` on the TRUNCATED path so the ceiling stays data-driven.

### Phase 3 — Honest failure surface + observability (salvage DROPPED)
Salvage of the partial JSON is inert here (reasoning exhaustion → empty content, nothing
to recover) and is dropped. Keep the honesty + metric halves:
- `generate_brief_with_retry` → return `tuple[dict|None, BriefErrorKind]` (terminal kind).
- `orchestrator.py` — derive `brief_status` (ok/unavailable) + `brief_error_kind` per
  row; add to the row dict + `_EMPTY_OUT_COLUMNS`; count `n_failed`; sidecar.
- `thematic.py brief` — emit `alphalens_thematic_brief_status_total{status="unavailable"}`
  + a best-effort gated Telegram note.
- Prometheus `AlphalensThematicBriefTruncations` rule — SEQUENCED after the fix, gated
  on a ratio/threshold (not >0 from day one), hand-synced + `HUP`.

### Phase 4 — SPA honest-flag carry-through (larger blast radius; recommended)
Carry `brief_status`/`brief_error_kind` across parquet → Django → SPA (avoid the
`options_*` parquet-edge drop):
- Django ingest + model (+ migration) + serializer (null-safe).
- `CandidateCard.svelte` — replace the silent `{:else}` italic-rationale with an
  explicit muted "brief unavailable — LLM response truncated" label above the (still
  clearly-labeled) rationale. New `.stories.svelte` state bound to a real fixture.

### Phase 5 — OPTIONAL reasoning-cap (ONLY if Phase 0 probe passes)
- `openrouter_client.py` — `OpenRouterConfig.reasoning: dict|None`; emit `body["reasoning"]`
  only when set (omitted otherwise → extract/mapper byte-identical).
- `generator.py` — gate on `model==PRO_MODEL`; on the TRUNCATED retry pass
  `reasoning={"effort":"low"}` PAIRED with a cap strictly > 4000. Never load-bearing.

## Rollout
TDD per phase; research tests `unittest.TestCase`. Gate: `uv run python -m unittest
discover -s apps/alphalens-research/tests -t apps/alphalens-research` (+ apps/web
`pnpm run check` + `build-storybook` and Django tests if Phase 4). Keep golden-replay
+ property + max_output_tokens-propagation suites green. Zen pre-MERGE
(deepseek-v4-pro, thinking=high), one combined pass. Deploy = **VPS-LOCAL pipeline
image rebuild** (`deploy/docker/Dockerfile.pipeline`, NOT GHCR); Phase 4 also publishes
the Django image via CI. Validate: `alphalens thematic brief --date 2026-07-27` re-run
(overwrites the whole date parquet) → confirm XMTR gets a non-empty `brief_tldr`.

## Risks
- Ceilings stay guesses if Phase 0 is skipped → wasted escalation or silent re-degrade.
  Mitigation: Phase 0 gate.
- Mocked tests prove plumbing, not that a real prompt fits under the new ceiling — only
  the Phase 0 live probe proves the fix.
- A persistently-stuck ticker (trace > ceiling) runs the full ladder AND is alert-eligible
  each of the 6 daily slots. Mitigation: per-(asof,ticker) alert dedup.
- `generate_brief_with_retry` signature change ripples into `_brief_for_row` + existing
  tests (mechanical, wide churn).
- Phase 4 cross-boundary blast radius (Django migration + SPA + Storybook).
- Cost: ~zero on the healthy path (OpenRouter bills tokens generated, not the cap); a
  stuck ticker pays up to `len(ladder)` escalating generations per slot (bounded).

## Open questions (need user decision)
1. **Measure-first vs provisional constants** → recommend measure-first (Phase 0).
2. **Adopt Phase 5 (reasoning-cap)?** → only if the Phase 0 probe confirms it works.
3. **Phase 4 (SPA honest badge) same series, or pipeline-side (Phases 1-3) first + SPA
   fast-follow?** → coherent honesty story vs smaller review surface.
4. **`brief --force <ticker>` escape hatch, or rely on the full `--date` re-run?** → the
   full re-run suffices; single-ticker is convenience.
5. **Alert policy** — Telegram + Prometheus, or Prometheus-only; threshold; sequenced
   after the fix so it does not fire from day one.
6. **Cost control** — should a ticker recorded as ceiling-failed for an asof SKIP
   re-escalation on later same-day slots, or is re-paying the bounded ladder each slot OK?
