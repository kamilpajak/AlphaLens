# Candidate card — domain-grouped data reorganization

**Status:** DRAFT (awaiting user review)
**Date:** 2026-06-27
**Scope:** `apps/web` only — presentation reorganization of the brief candidate card. No
pipeline / Django / data-contract changes. Every value already present on the card stays
sourced from the same `Candidate` fields; this PR only moves where each is rendered.

## Goal

Regroup the candidate card so every metric lives in the one analytical domain a reader
would look for it, and so no value is shown twice. Today the card splits metrics by
*presentation format* (percentile bars in `SYSTEM.SIGNALS` vs raw numbers in
`FUNDAMENTALS` / `TECHNICALS.CONTEXT`), which scatters a single question ("is this cheap?",
"what's the momentum?") across two or three places and duplicates several values.

This is a **logical-ordering / de-duplication** change, not a feature change. No new metric
is computed; a few are relocated, several duplicates are collapsed to a single render site.

## Decisions locked with the user

1. **Organizing principle = by analytical domain.** Metrics cluster by meaning
   (Valuation & Quality, Momentum & Technicals, Catalyst & Event, Insider / Flow), not by
   whether they render as a bar or a grid row.
2. **Expert lenses anchor their domain block.** Buffett score becomes the header of the
   Valuation & Quality block; O'Neil score the header of Momentum & Technicals. The unified
   `ExpertPanel` drawer shrinks to the cross-domain *disagreement scale* + Buffett's
   qualitative rationale + O'Neil audit flags. The meta-bar Buffett / O'Neil chips are
   **removed** (the domain headers now carry the scores — no triple display).

## Current state (what exists today)

Source: `apps/web/src/lib/components/CandidateCard.svelte`,
`ExpertPanel.svelte`, `TradeSetup.svelte`, `TemplateFacts.svelte`.

Eight data zones:

- **Header** — rank · ticker · company · `#theme` · `[reversal]` · gates (TENK/PRESS/INSIDER)
- **Meta bar** — sector/industry · mcap ‖ LAYER-4 · CONF · CATALYST type+strength ‖ BUFFETT · O'NEIL
- **Thesis** — `brief_tldr` (or `rationale`) + source-event date/link
- **SYSTEM.SIGNALS** (`SignalBar`s) — insider-90d · fcff-yield %ile · valuation-composite %ile ·
  catalyst-strength · rsi-14d · off-52w-high · off-52w-low · vol-z-score
- **FUNDAMENTALS** (`<dl>` grid) — PE · PS · EV/REV · EV/EBITDA · FCF-margin · ROE ·
  fcff-yield (raw) · magic-formula · financials-age · next-earnings
- **TECHNICALS.CONTEXT** (`<dl>` grid) — ma50-dist · ma200-dist · ma200-slope · atr
- **EXPERT.PANEL** (drawer) — disagreement scale + Buffett qual (pillars + rationale) +
  O'Neil numeric readouts (off-52w-high · rel-strength · ma200-slope · ma200-dist · earnings-YoY) + flags
- **Right col / footer** — Trade setup · Typed facts · Supply-chain · Bear · Catalyst-failure-exit

### Duplicates today

| Value | Appears in |
|---|---|
| catalyst strength (0.64) | meta bar **and** SYSTEM.SIGNALS |
| fcff yield | SYSTEM.SIGNALS (%ile) **and** FUNDAMENTALS (raw +5.09%) — both labeled "fcff yield" |
| off-52w-high (−24.9%) | SYSTEM.SIGNALS bar **and** O'Neil drawer readout |
| ma200-dist (−15.6%) | TECHNICALS.CONTEXT **and** O'Neil drawer readout |
| ma200-slope | TECHNICALS.CONTEXT **and** O'Neil drawer readout |
| Buffett / O'Neil score | meta-bar chip **and** ExpertPanel drawer (intentional chip→drawer) |

`valuation composite` is the sector-percentile roll-up of the same five multiples
(PE / PS / EV-Rev / EV-EBITDA / FCF-margin) that sit in FUNDAMENTALS — not a duplicate value,
but a headline that belongs next to its breakdown.

## Target layout

```
┌─ HEADER ───────────────────────────────────────────────────────────────┐
│ RANK 02 OF 12  FCN  FTI Consulting  #class_action      ✓TENK ✗PRESS ✗INS │
├─ META ─────────────────────────────────────────────────────────────────┤
│ SERVICES / MANAGEMENT CONSULTING · mcap $4.23B   ‖   LAYER-4 2 · CONF 4/5│
└────────────────────────────────────────────────────────────────────────┘
LEFT (lg:col-span-7)                            │ RIGHT (lg:col-span-5)
                                                │
── CATALYST & EVENT ──────────────────────────  │ ── TRADE.EXECUTION.SETUP ──
 catalyst  litigation  ▓▓▓▓▓▓░░ 0.64 (floor .55)│  (unchanged: sizing, stops,
 "FCN's e-discovery … offset the catalyst."     │   entry tiering, take-profit)
 2026-06-25  Bronstein… Class Action Filed ↗    │
 ┌ typed.facts (financing announcement) ──────┐ │
 │ announced 2026-06-05 · raise $370.0M       │ │
 └────────────────────────────────────────────┘ │
                                                │
┌ VALUATION & QUALITY ───┬ MOMENTUM & TECHNICALS ┐
│ BUFFETT 19/100         │ O'NEIL 6/100          │
│  value / quality       │  momentum             │
│ fcff yield ▓▓▓ 31%ile  │ rsi 14d      ▓▓▓ 31   │
│   (+5.09% raw)         │ off 52w high ▓▓░ -24.9%│
│ valuation  ▓▓▓▓ 49%ile │ off 52w low  ░  +0.0% │
│   composite            │ rel strength ▓▓ 24%ile│
│ ─ multiples ─          │ vol z-score  ▓  +0.2σ │
│ PE 16.8    PS 1.2      │ ─ trend ─             │
│ EV/REV 1.3 EV/EBITDA…  │ ma50 dist   -13.0%    │
│ FCF mgn 6.6% ROE 16.0% │ ma200 dist  -15.6%    │
│ magic #2/3             │ ma200 slope -0.045%/d │
│ fin.age 56d            │ atr         +4.1%     │
│ next earn 2026-07-30   │ earnings YoY -3.3%    │
└────────────────────────┴───────────────────────┘
── INSIDER / FLOW ───────────────────────────────
 insider 90d   NO BUYS
▸ EXPERT.PANEL → disagreement scale (Buffett 19 ─gap 13─ O'Neil 6)
                 + Buffett rationale / moat-trend-candor + O'Neil audit flags

── SUPPLY.CHAIN ───┬── BEAR.CASE ───┬── CATALYST.FAILURE.EXIT ──  (unchanged)
```

## Exact moves & de-dups

| # | Change | From → To | Rationale |
|---|---|---|---|
| 1 | `SYSTEM.SIGNALS` heading dissolved | bars distributed to domains | grouped by presentation, not meaning |
| 2 | fcff-yield %ile + valuation-composite bars | → **Valuation & Quality** | valuation domain |
| 3 | rsi · off-52w-high · off-52w-low · vol-z · rel-strength bars | → **Momentum & Technicals** | momentum domain |
| 4 | catalyst-strength bar | → **Catalyst & Event** block | event domain; removes meta-bar duplicate |
| 5 | insider-90d | → **Insider / Flow** strip | own domain (ownership/flow) |
| 6 | fcff-yield collapsed 2 rows → 1 | bar (%ile) with raw value annotated | was duplicated, both labeled identically |
| 7 | Buffett score | meta chip → **Valuation block header** | anchors domain; removes meta dup |
| 8 | O'Neil score | meta chip → **Momentum block header** | anchors domain; removes meta dup |
| 9 | O'Neil drawer readouts (off-52w-high, ma200 dist/slope, earnings-YoY, rel-strength) | → Momentum block, **removed from drawer** | were O'Neil↔technicals duplicates |
| 10 | typed.facts (financing announcement) | right col → **Catalyst & Event** | deterministic event evidence belongs with catalyst |
| 11 | meta-bar catalyst type+strength | → Catalyst block | event domain; meta bar slims to identity + ordering signals |

Three judgment calls (confirmed with user):
- `earnings YoY` → Momentum block (O'Neil's growth input), not Valuation.
- `typed.facts` → Catalyst block; right column becomes purely trade execution.
- Insider kept as its own thin strip (one metric, distinct domain).

## Component-level design

### `CandidateCard.svelte`

- **Meta bar** — drop the `ChipTip` Buffett + O'Neil cluster and the catalyst type+strength
  span. Keep identity cluster (sector/industry · mcap) and the right-side LAYER-4 + CONF.
- **New `CATALYST & EVENT` block** (left column, above the two domain columns). The standalone
  `LIVE.EQUITY.THESIS` heading is **retired**; its content (the `brief_tldr` blockquote + the
  source-event date/link) **folds into this block** as the catalyst's narrative. Block order:
  a labeled catalyst-strength `SignalBar` (type label + 0.64 + the 0.55-floor note its current
  tooltip already carries) → `brief_tldr` blockquote → source date/link → `TemplateFacts`
  (rendered here when `brief_template_id` present). Net: thesis + catalyst + event evidence
  read as one event-domain unit instead of three separate zones.
- **`VALUATION & QUALITY` block** (left sub-column) — header row: `BUFFETT <score>/100 ·
  value / quality` (reuse `buffettTone`; the chip's hover content moves to a header tooltip).
  Then: fcff-yield bar (one row, %ile + raw annotation), valuation-composite bar, a
  `─ multiples ─` divider, the existing `<dl>` of PE/PS/EV-Rev/EV-EBITDA/FCF-margin/ROE +
  magic-formula + financials-age + next-earnings.
- **`MOMENTUM & TECHNICALS` block** (right sub-column of the 2-up) — header row: `O'NEIL
  <score>/100 · momentum` (reuse `oneilTone`). Then bars: rsi · off-52w-high · off-52w-low ·
  rel-strength · vol-z; a `─ trend ─` divider; `<dl>` of ma50-dist · ma200-dist · ma200-slope ·
  atr · earnings-YoY.
- **`INSIDER / FLOW` strip** — the existing insider `SignalBar` (bar or muted-placeholder
  modes from `insiderDisplay`) as a thin full-width row.
- **Thin-cohort chip** — currently sits beside the `SYSTEM.SIGNALS` heading. Move it beside
  the `VALUATION & QUALITY` header (the percentile bars it qualifies now live in the two
  domain blocks); the same chip qualifies both blocks' `%ile` readouts.

### `ExpertPanel.svelte`

- Keep the collapsed trigger + the disagreement scale (Buffett ─gap─ O'Neil) exactly as is —
  it reads the persisted `panel.expert_spread` (manufactured-authority guard unchanged: band
  word + colour stay drawer-only).
- **Buffett section** — unchanged (pillars + rationale + scuttlebutt/classified footnote).
- **O'Neil section** — drop the `oneilReadouts` `<dl>` (now in the Momentum block). Keep the
  two audit-flag `ExpertPillar`s (`split-suspected`, `near-zero base`) and the source note.
  If neither flag is present, the O'Neil section renders only the score header + source note;
  add a one-line pointer: "numeric readouts shown in Momentum & Technicals."
- `hasOneil` predicate stays so the drawer still offers an O'Neil section when flags exist.

### Recently-merged features to PRESERVE (do not drop in the reorg)

The card already carries the `selection_score` ATR-tilt surfaces (PRs #673/#675/#676). They
are orthogonal to this reorg and **stay exactly as-is**:

- **Meta-bar `extended` chip** — rendered when `(c.atr_penalty ?? 0) > 0` (tone-neutral, no
  number on the face). It is an ordering/selection flag tied to LAYER-4, so it **stays in the
  meta bar** alongside LAYER-4 + CONF (it is NOT one of the removed catalyst / expert chips).
- **`SCORER BREAKDOWN` section in the ExpertPanel drawer** — layer-4 → atr-penalty →
  selection-score → `scorer_config_version` → "suggestive — not yet validated". Gated on
  `hasScoreBreakdown`. The `ExpertPanel` props `layer4Score / atrPenalty / selectionScore /
  scorerConfigVersion` and this block **stay unchanged**. (This is why the drawer is NOT
  empty after the O'Neil readout grid moves out — the scale + Buffett prose + O'Neil flags +
  scorer-breakdown remain.)

So the meta bar after the reorg = identity cluster (sector/industry · mcap) ‖ LAYER-4 ·
[`extended` when penalised] · CONF. Removed from meta: catalyst type+strength, Buffett chip,
O'Neil chip.

### Unchanged

`TradeSetup.svelte`, `TemplateFacts.svelte` (only its mount point moves), the narrative
footer (supply-chain / bear / exit), header, gates, the `Candidate` type, all `format.ts`
helpers, all data contracts.

## Edge cases (must preserve current behavior)

- **Sparse blob** — Buffett/O'Neil scores absent → header shows `—` (same null-paths as the
  chips today). Block still renders its other metrics.
- **Thin / sic-3 / ff-48 cohort** — percentile bars suppressed to `—`; the cohort chip moves
  to the Valuation header but the suppression logic is unchanged.
- **NO_STRUCTURE trade setup** — right column behavior unchanged.
- **Flash-path candidate** (`brief_template_id` empty) — `TemplateFacts` not rendered; the
  Catalyst block still shows catalyst bar + thesis + source link.
- **Missing momentum terms** — O'Neil score `—`; the audit flags / split-suspected handling
  is unchanged (those reasons explain a withheld score).
- **fcff-yield** — when the %ile is null but raw exists (or vice-versa), the merged row shows
  whichever is present; never blank both if one exists.

## Testing

TDD per repo convention, but matched to the actual harness:

- **Unit suite** (`pnpm test:unit`, vitest, **node env, no DOM**) — pure-function mirrors of
  template guards (see `tests/unit/candidateCard.test.ts`, `expertPanel.test.ts`). Use this
  layer only for extractable logic: the merged **fcff-yield display** helper (the one piece of
  new branching logic — "show %ile + raw, whichever present"). Keep the existing
  `showsExtendedChip` / `hasScoreBreakdown` / tone-helper tests green (they cover preserved
  features and must not regress).
- **Playwright smoke** (`pnpm test`, real built app + `tests/fixtures/api-mock/` fixtures) —
  this is where DOM structure is asserted. Add a `card — domain grouping` describe block in
  `tests/smoke.test.ts` (or a sibling `tests/card-domain.test.ts`) that loads the latest
  fixture brief and asserts on the **first `article[id]`** card:
  - **Dedup** — each label `off 52w high`, `ma200 dist`, `ma200 slope`, `catalyst strength`,
    `fcff yield` appears **exactly once** in the card (`locator.count()` regression guard).
  - **Domain placement** — the `BUFFETT <n>` score node is inside the `VALUATION & QUALITY`
    block; `O'NEIL <n>` inside `MOMENTUM & TECHNICALS`.
  - **Meta-bar slimming** — the meta bar contains LAYER-4 + CONF but **no** Buffett / O'Neil /
    catalyst chip text; the `extended` chip still appears for a fixture row with `atr_penalty>0`.
  - **Drawer** — opening `expert.panel` shows the disagreement scale + `SCORER BREAKDOWN` but
    the O'Neil section no longer renders the numeric readout grid.
  - Existing smoke guarantees (per-day candidate count, console-clean) must stay green —
    fixture day files may need the new structure's labels but **not** new candidate rows.
- **Storybook** — no change. There is no `CandidateCard` / `ExpertPanel` story (the catalog
  covers only primitives: `SignalBar`, `ChipTip`, `ExpertPillar`, …). `SignalBar`'s API is
  unchanged by this reorg, so its story stays valid.
- **Visual confirmation** via Playwright MCP against `pnpm dev` before opening the PR.

Fixture note: the three `tests/fixtures/api-mock/days/*.json` already carry candidates with
Buffett/O'Neil blobs; confirm at least one fixture candidate has `atr_penalty > 0` (for the
`extended`-chip assertion) and one has a full O'Neil blob (for the momentum-block assertions).
If not present, extend an existing fixture candidate's fields (do not add new candidate rows,
which would break the count assertions).

## Out of scope

- No change to which metrics are *computed* or to selection / ordering. `selection_score` /
  `layer4_weighted_score` ordering is untouched (display reorg only).
- No new metrics added beyond surfacing `rel strength` as a Momentum bar (it already exists
  in the O'Neil blob; this just gives it a domain home instead of a drawer row).
- No pipeline, Django, parquet, or `Candidate`-type change.

## Rollout

Single `apps/web` PR. Zen pre-merge review (frontend) per repo convention. CF Pages
auto-deploys from `main` after merge — no VPS image rebuild needed for the SPA.
