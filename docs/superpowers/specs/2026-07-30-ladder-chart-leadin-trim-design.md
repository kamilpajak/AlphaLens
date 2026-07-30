# Ladder chart — trim lead-in history + right-edge breathing room

**Date:** 2026-07-30
**Status:** APPROVED (user, 2026-07-30)
**Scope:** `apps/web` only — no API, pipeline, or persisted-payload changes.

## Problem

`LadderChart.svelte` renders every bar in `chart_payload_json` and calls
`fitContent()`. The pipeline window is `min(90, max(20, 2×hold))` lead-in
sessions + in-trade sessions + 15 trailing sessions
(`alphalens_pipeline/feedback/ladder_chart.py`). For short holds (e.g. PSN,
5d hold) that yields ~90 sessions of pre-brief history and ~10 sessions of
action, so the entire ladder (entry/e2/e3/tp1-3/stop price lines, E1/TP1
markers) is squeezed into the last ~8% of the chart width and markers collide
with the right price scale.

## Decision (user-selected: option B — hard cut, chart stays static)

1. **Trim lead-in to 20 sessions** — new pure helper in
   `src/lib/components/ladderChart.ts`:
   `trimLeadInBars(bars, briefDate, markers, keep = LEAD_IN_DISPLAY_SESSIONS)`.
   - Anchor = brief session (`briefLineTime(bars, briefDate)`); fallback =
     first `ENTRY` marker session; no anchor (context-only payload) → return
     bars unchanged.
   - Returns bars from `anchor − keep` sessions to the end; if the lead-in is
     already ≤ `keep`, no-op.
   - `LEAD_IN_DISPLAY_SESSIONS = 20` as a named exported constant.
   - Trailing sessions untouched. Markers and price lines need no adjustment:
     all markers sit on/after the brief session; price lines are
     bar-independent.
   - `LadderChart.svelte` feeds `series.setData()` with the trimmed bars.
     Scroll/zoom stay disabled; older history is simply not shown.

2. **Right-edge padding ~3 bars** — replace the bare
   `chart.timeScale().fitContent()` with
   `setVisibleLogicalRange({ from: -0.5, to: bars.length - 1 + RIGHT_OFFSET_BARS })`
   (`RIGHT_OFFSET_BARS = 3`) so E1/TP1/E2·E3 arrows detach from the price
   scale. The ResizeObserver / width-refresh path must re-apply this range,
   not fall back to `fitContent()`.

## Rejected alternatives

- **Option A — `setVisibleRange` + re-enable scroll/zoom:** keeps the full
  90-session history reachable by panning. Rejected by user; chart stays a
  static snapshot.
- **Pipeline-side lead-in reduction:** would require recomputing persisted
  `chart_payload_json` for all rows and loses data for any future deeper view.

## Testing

- Unit tests (`tests/unit/ladderChart.test.ts`) for `trimLeadInBars`:
  long lead-in → exactly 20 pre-anchor sessions kept; short lead-in → no-op;
  no anchor → no-op; PLANNED payload (no ENTRY marker) → brief-date anchor.
- Storybook: add "Long Lead-In (trimmed)" story with a ~90-session fixture to
  `LadderChart.stories.svelte`; visually confirms trim + right offset.
- Gate (run from `apps/web`): `pnpm run check` + `pnpm run build-storybook`.

## Risks

- Overlays (in-trade band, BRIEF dashed line) position via
  `timeToCoordinate()` after `setData` and re-position on visible-range
  change — unaffected by trimming, but verify the deferred-RAF pass still
  lands after `setVisibleLogicalRange`.
- Stories with short fixtures must render identically (trim is a no-op for
  them).
