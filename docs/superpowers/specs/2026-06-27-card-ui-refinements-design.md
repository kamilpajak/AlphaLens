# Candidate card — three UI/logic refinements (lens scale, Buffett symmetry, TTL)

**Status:** DRAFT (awaiting user review)
**Date:** 2026-06-27
**Scope:** `apps/web` only. Follow-up to the domain-regroup PR (#682, `feature/card-domain-regroup`);
this branch (`feature/card-ui-refinements`) is **stacked on it** because the changes operate on
the regrouped card. No `Candidate`-type / data-contract / pipeline / Django change.

## Goal

Three independent refinements found by reviewing the rendered domain-grouped card (the `FOUR`
fixture):

1. **#2 — LENS SCORE scale** in the `ExpertPanel` drawer: the two lens labels collide at small
   gaps (`O'Neil 55Buffett 62` with no space), and the marker dots overflow the track at scores
   0/100 (a pre-existing zen MEDIUM finding from PR-8b).
2. **#5 — Buffett card symmetry**: when a candidate has a numeric Buffett score but no
   *qualitative* data (e.g. `FOUR`: 62/100, no moat/rationale), the drawer drops the Buffett card
   entirely — so the disagreement scale shows `Buffett 62` but no Buffett card below it, while
   O'Neil gets a full card. Asymmetric and confusing. The empty state should also name the gate
   dependency that blocks the qualitative read (the 10-K / `tenk` gate).
3. **#6 — TTL chip**: `TradeSetup` shows `ttl: N days` whenever `order_ttl_days != null`, even on
   `NO STRUCTURED LADDER` (no order to live for N days). Logically contradictory.

These came out of a design review; gates (`tenk`/`press`/`insider`/`etf`) and the expert lenses
were confirmed to be **distinct categories** (evidence-provenance vs scored verdict) and stay
visually separate — #5 only *references* the gate as the cause of an absent qualitative read, it
does not merge the two.

## Current behaviour (what exists today)

Files: `apps/web/src/lib/components/ExpertPanel.svelte`, `TradeSetup.svelte`, `CandidateCard.svelte`.

### #2 — scale (ExpertPanel)
Inside `{#if showScale}` the track is a single `<div class="relative mt-2 mb-7 h-1.5 rounded-full
bg-grid">`. It contains: a gap-fill span, the Buffett dot + Buffett label, the O'Neil dot + O'Neil
label. Both labels use `class="absolute top-full mt-1 …"` with `style="left: {score}%; transform:
{labelShift(score)}"`. Because both labels are anchored *below* the track at their own `left%`,
two close scores render overlapping text. The dots use `left: {score}%` with `-translate-x-1/2`;
the track has no `overflow-hidden`, so a dot at 0% or 100% draws ~6px outside the track.
`labelShift(p)` = `translateX(-clamp(0,p,100)%)` already keeps label *text* in horizontal bounds.

### #5 — Buffett section (ExpertPanel)
- `hasBuffQual` = true when any of moat_type / qualitative_rationale / understandable /
  moat_trend / management_candor is present.
- `buffScore` = `Math.round(buffett_quality_score)` when finite, else null. This is the cheap
  *numeric* score (owner-earnings yield / ROIC / margin-of-safety) — computed WITHOUT the 10-K.
- `sections = ['buffett','oneil'].filter(id => id === 'buffett' ? hasBuffQual : id === 'oneil' ?
  hasOneil : false)`. So Buffett is dropped whenever `!hasBuffQual`, even if `buffScore` exists.
- In the `{#each sections}` loop, the Buffett branch (`{#if isBuf}`) renders the pillars +
  rationale blockquote + scuttlebutt/classified footnote. There is no else/empty state because the
  filter guarantees `hasBuffQual` when the branch runs.

### #6 — TTL chip (TradeSetup)
- `hasStructure = setup != null && setup.status === 'OK' && setup.entry_tiers.length > 0`.
- The header renders `{#if setup?.order_ttl_days != null}<span>ttl: {order_ttl_days} days</span>`
  — independent of `hasStructure`. The `{#if !hasStructure}` branch below shows the
  `NO STRUCTURED LADDER` box, so TTL + "no ladder" can show together.

### Gate identifiers (for #5)
`c.gates_passed` / `c.gates_failed` / `c.gates_unknown` are string arrays. The 10-K gate id is
`tenk` (lowercase). Per `GatePill` semantics: `tenk` in passed = "keywords found in 10-K"; in
failed = "10-K exists but no keyword match"; in unknown = "no 10-K available". So **the 10-K
exists** iff `tenk ∈ passed ∪ failed`.

## Design

### #2 — Restructure the scale into three stacked rows

Replace the single track div with a 3-row vertical stack:

```
LENS SCORE                                    0–100
                    Buffett 62        ← row 1: Buffett label, positioned at left:{buffScore}%
   ───────────●━━●────────────────    ← row 2: track (overflow-hidden) — gap-fill + both dots
                 O'Neil 55            ← row 3: O'Neil label, positioned at left:{oneilScore}%
```

- **Row 2 (track)** gets `overflow-hidden` so a dot at 0%/100% is clipped to a clean half-dot at
  the edge instead of overflowing (fixes the zen MEDIUM). The gap-fill span + both dots are the
  only children — labels move OUT so `overflow-hidden` does not clip them.
- **Row 1** holds only the Buffett label; **row 3** holds only the O'Neil label. Each is an
  `absolute` span at `left: {score}%` with the existing `labelShift(score)` transform (keeps text
  within horizontal bounds). Buffett always above, O'Neil always below → the two labels are on
  different vertical rows and **can never collide horizontally**, regardless of the gap. No
  threshold / collision-detection logic.
- Label tone colours (`toneText(buffT)` / `toneText(oneilT)`) and the dot tone fills are unchanged.
- The wrapper keeps the same total vertical footprint feel (one label-height above, the thin track,
  one label-height below) — net height ≈ unchanged (the old `mb-7` below-track space is now split
  into an above row + a below row).

Rejected alternatives: (B) threshold-stack only when close — adds a magic number and still needs
the above/below machinery; (C) merge to one `O'Neil 55 · Buffett 62` label when close — loses the
positional read (which score sits where on the track). The always-stacked layout is deterministic
and solves the dot-overflow in the same edit.

### #5 — Render a minimal Buffett card when a numeric score exists

- **Section inclusion:** change the filter so Buffett is included when it has *either* qual or a
  numeric score: `id === 'buffett' ? (hasBuffQual || buffScore !== null) : …`. (O'Neil arm
  unchanged.) This makes the Buffett card render its score header (the same header the loop already
  builds), restoring symmetry with O'Neil on the scale.
- **Empty state:** in the Buffett branch, wrap the existing pillars+rationale+footnote in
  `{#if hasBuffQual}`. Add an `{:else}` that renders a single muted line naming the gate dependency:
  - `tenkAvailable` → `"numeric score only — qualitative read not computed"`
  - `!tenkAvailable` → `"numeric score only — no 10-K for a qualitative read"`
  (Phrasing: lead with what IS known — the numeric score is already in the header — then the reason
  the qual is missing. Tone = `text-fg-muted`, matching the O'Neil source note styling.)
- **New prop:** `ExpertPanel` gains `tenkAvailable?: boolean | null`. `CandidateCard` passes it,
  derived as `c.gates_passed.includes('tenk') || c.gates_failed.includes('tenk')`. When the prop is
  absent/null (no gate data), default the empty-state copy to the neutral "not computed" form (do
  not assert "no 10-K" without evidence).
- `hasContent` already includes `hasBuffQual`; it must ALSO surface the drawer when only a numeric
  Buffett score exists. Update: `hasContent = hasBuffQual || hasOneil || spread !== null ||
  hasScoreBreakdown || buffScore !== null`. (Without this, a card with only a numeric Buffett score
  and nothing else would never offer the drawer — though in practice `hasScoreBreakdown`/`spread`
  usually co-occur. Add it for correctness.)

This keeps gates and lenses categorically separate (gates remain the header provenance strip) while
making the lens's empty state *point at* the gate that blocks it — the dependency the user asked to
surface.

### #6 — Gate the TTL chip on structure

Change the header condition to `{#if hasStructure && setup?.order_ttl_days != null}`. When there is
no structured ladder, the TTL chip is hidden (nothing to expire). No other change to TradeSetup.

## Edge cases

- **Both lenses null** — scale already gated on `showScale` (finite spread && both scored); the
  3-row restructure is inside that guard, so a no-scale card is unaffected.
- **Buffett numeric but O'Neil absent** — scale needs both scored, so it won't render; the Buffett
  card still renders via the new `sections` rule with its empty-state. Symmetric-enough (O'Neil
  simply has no card because it has no data — that is honest, not asymmetric).
- **Buffett fully absent (no score, no qual)** — `buffScore === null && !hasBuffQual` → Buffett not
  in `sections` (unchanged behaviour). No empty card for a lens that never ran.
- **`tenkAvailable` unknown** — neutral "not computed" copy; never claims "no 10-K" without the gate
  saying so.
- **TTL with structure** — unchanged (chip shows on a real ladder).

## Testing

Matched to the harness (vitest = pure-function/node; Playwright smoke = DOM).

- **Unit (vitest):** extract the two bits of new logic as pure functions and test them:
  - `tenkAvailable(passed, failed)` → boolean (in `$lib/format` or a small helper) — cases: tenk in
    passed, tenk in failed, tenk only in unknown, tenk absent.
  - the Buffett-card inclusion predicate `showsBuffettCard(hasBuffQual, buffScore)` mirrored as a
    pure function (like the existing `candidateCard.test.ts` mirrors) — cases: qual only, score
    only, both, neither.
- **Playwright smoke** (`tests/smoke.test.ts`, fixture `FOUR` = numeric Buffett 62 + no qual,
  `tenk` passed, no ladder):
  - **#2** open the drawer; assert the Buffett label and O'Neil label are both present and the
    `[data-testid="expert-panel-body"]` scale region renders both `Buffett 62` and `O'Neil 55`
    (the stacked layout keeps both texts queryable; the old overlap concatenated them but both were
    still in DOM — so add a structural assertion that the two labels are in *separate* row elements,
    e.g. distinct testids `lens-label-buffett` / `lens-label-oneil`).
  - **#5** assert the drawer renders a Buffett card (score `62`) WITH the empty-state line
    ("qualitative read not computed") and NO Buffett pillars (`moat` etc. absent) for `FOUR`.
  - **#6** assert the first card (FOUR, no ladder) shows `NO STRUCTURED LADDER` and does NOT show a
    `ttl:` chip. (If no existing fixture has a structured ladder to assert the positive case, add a
    positive assertion only if one exists; otherwise document the gap.)
- Existing unit + smoke suites stay green (191 unit, 122 smoke at branch base).
- Visual confirmation via the local preview (`localhost:8799`, `FOUR`) before opening the PR.

## Rollout

Stacked PR on `feature/card-domain-regroup`. Base = `feature/card-domain-regroup` while #682 is
open; rebase onto `main` once #682 merges. DCO sign-off on every commit. Zen `deepseek/deepseek-v4-pro`
(thinking=high) pre-merge review per repo convention. CF Pages auto-deploys after merge to main.

## Added after the initial review (#10 — dot-separated domain headers)

- **#10** The three new domain headers used ` & ` (`catalyst & event` …), breaking the card's
  cyan-header convention (`live.equity.thesis`, `trade.execution.setup`, `supply.chain` — all
  dot-separated WORD.WORD). Renamed to `catalyst.event` / `valuation.quality` /
  `momentum.technicals`; smoke heading assertions updated. (The drawer's prose pointer
  "Momentum & Technicals" stays — it's a readable sentence, not a stylized header.)

## Added after the initial review (#9 — catalyst label + correct tooltip)

- **#9** Catalyst row: humanise the event type + fix a wrong tooltip (added 2026-06-28). The
  `CATALYST & EVENT` bar showed the raw enum (`M_AND_A`) and a tooltip that was wrong on three
  counts vs the pipeline (`compute_catalyst_strength` = 0.4·event-type-tier + 0.4·confidence +
  0.2·second-order-implications; `catalyst_floor` = a 0/+1/+2 cohort-score lift at 0.45/0.70):
  it claimed the inputs were "news novelty / thematic alignment / freshness", that there was a
  "0.55 floor", and that a sub-floor catalyst is "filtered out". A user caught it via a 0.52
  catalyst that was NOT filtered (correct: 0.52 ≥ 0.45 → +1 lift; selection is the OR-gate
  funnel, not a catalyst cutoff). Fix: new `catalystLabel(eventType)` helper (`m_and_a` → "M&A",
  `ipo` → "IPO", else underscores→spaces; null when absent so the suffix drops) + a corrected
  tooltip describing the real inputs and the lift mechanism (≥0.45 → +1, ≥0.70 → +2; weak adds
  no lift but does not drop the name). Helper unit-tested. The bug pre-dates #682 (carried from
  the original SYSTEM.SIGNALS catalyst bar).

## Added after the initial review (#8 — SCORER BREAKDOWN → badge tooltip)

- **#8** SCORER BREAKDOWN moved from the `ExpertPanel` drawer into the **score-badge tooltip**
  (added 2026-06-28). Once the badge became `selection_score` (#7), the derivation belongs on
  the badge itself, and the breakdown was a poor fit inside the expert-lens drawer (it was
  bolted on there by the atr-tilt PRs). The badge now wraps in a `ChipTip` whose tooltip shows
  `layer-4 → atr penalty (only when > 0) → selection score`, the `scorer_config_version`, and
  the `suggestive — not yet validated` caveat (all hover-only). `ExpertPanel` loses the whole
  `{#if hasScoreBreakdown}` section, the `hasScoreBreakdown` predicate, and its four scorer
  props (`layer4Score / atrPenalty / selectionScore / scorerConfigVersion`) — it is now purely
  the expert-lens consensus drawer. The `hasScoreBreakdown` unit mirror is removed; smoke
  asserts the breakdown is gone from the drawer and present in the badge tooltip. The `score`
  badge + tooltip read `c.*` directly, so no data plumbing through `ExpertPanel`.

## Added after the initial review (#7 — meta-bar headline badge)

- **#7** Meta-bar badge → **selection_score** (added 2026-06-28). The brief is ranked by
  `selection_score` (= `layer4_weighted_score − atr_penalty`; pipeline `orchestrator.py` sorts
  on it, `rank_in_day` follows), but the filled amber badge next to "RANK" showed the raw
  `layer4_weighted_score` — the input, not the operative ranking number. A live audit (39
  briefs / 437 candidates) found `atr_penalty > 0` in 6 cases (~1.4%) where the two diverge
  materially (e.g. SNAP `layer4=1 → selection=0.49`, SABR `2 → 1.49`); there the old badge
  **overstated** the card's standing. Fix: the badge renders `selectionBadge(c.selection_score,
  c.layer4_weighted_score)` and is relabelled `score`. `layer4` + the ATR penalty stay in the
  drawer's SCORER BREAKDOWN; the `extended` chip continues to flag a non-zero penalty. Helper
  `selectionBadge` (integer-valued → no decimals `3.0→"3"`; fractional → 2 dp; falls back to
  layer4, then `—`). New unit tests + smoke updated (meta shows `score`, not `layer-4`).
  Validation note: `selection_score` is "suggestive — not yet validated", but it already drives
  rank, so naming the operative number is more honest than hiding it behind layer4.

## Added after the initial review (#3)

- **#3** Insider/Flow → **compact one-line row** (added 2026-06-27). A live audit of 39
  briefs / 431 candidates found net opportunistic buying exactly **once** (GME 2026-06-16,
  $114k, 100%ile), so the full strip rendered an always-empty bar ~99.8% of the time and
  duplicated the header `✗ INSIDER` gate. Replaced the cyan-headed section + `SignalBar` with
  a single muted row (`insider 90d` label + value): no-buys/selling → muted text; the rare
  net-buying → `<pctile>%ile · <$>` in amber so it stands out. Tooltip kept (term
  `insider buying (90d)` so it doesn't echo the row label). The `INSIDER / FLOW` heading is
  retired; smoke updated.

## Out of scope (deferred from the same review)

- **#1** empty right column when no ladder (layout rebalance) — larger refactor, separate effort.
- **#4** bar-semantics consistency (percentile vs signed-magnitude) — a design decision, separate.
