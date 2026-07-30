# Ladder Chart Lead-In Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show at most 20 pre-brief sessions in the ladder replay chart and add a ~3-bar right margin so the entry/TP/stop markers stop colliding with the price scale.

**Architecture:** A new pure helper `trimLeadInBars` in `ladderChart.ts` (unit-tested) slices the bar array before it reaches `series.setData()`; `LadderChart.svelte` replaces `fitContent()` with an explicit visible logical range that ends 3 bars past the last candle. No API, pipeline, or persisted-payload changes.

**Tech Stack:** SvelteKit (Svelte 5 runes), lightweight-charts v5, Vitest, Storybook 10 (`@storybook/addon-svelte-csf`), pnpm.

**Spec:** `docs/superpowers/specs/2026-07-30-ladder-chart-leadin-trim-design.md`

## Global Constraints

- All work happens in the worktree `/Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-leadin-trim` on branch `feature/ladder-chart-leadin-trim`. Never touch the main checkout.
- `LEAD_IN_DISPLAY_SESSIONS = 20` and `RIGHT_OFFSET_BARS = 3` are named constants — no magic numbers at call sites.
- The chart stays static: `handleScroll: false, handleScale: false` must NOT change.
- Code comments, identifiers, docstrings: English only.
- Commits: Conventional Commits + DCO sign-off (`git commit -s`; git user is already "Kamil Pająk" / kamilpajak@users.noreply.github.com).
- Gates run from `apps/web` inside the worktree, NOT the workspace root: `pnpm run check`, unit tests via Vitest, `pnpm run build-storybook`.
- The worktree has no `node_modules` — Task 1 installs them first.

---

### Task 1: `trimLeadInBars` pure helper (TDD)

**Files:**
- Modify: `apps/web/src/lib/components/ladderChart.ts` (append after `briefLineTime`, ~line 117)
- Test: `apps/web/tests/unit/ladderChart.test.ts` (append a new `describe` block)

**Interfaces:**
- Consumes: existing `briefLineTime(bars, briefDate)` from the same module; `ChartBar`/`ChartMarker` from `$lib/types`.
- Produces: `export const LEAD_IN_DISPLAY_SESSIONS = 20` and
  `export function trimLeadInBars(bars: ChartBar[], briefDate: string | null | undefined, markers: ChartMarker[], keep?: number): ChartBar[]`
  — Task 2 imports both from `./ladderChart`.

- [ ] **Step 1: Install worktree dependencies**

```bash
cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-leadin-trim
pnpm install
```

- [ ] **Step 2: Write the failing tests**

Append to `apps/web/tests/unit/ladderChart.test.ts`. The file already imports from `$lib/components/ladderChart` — extend that import with `trimLeadInBars` and `LEAD_IN_DISPLAY_SESSIONS`, and reuse the existing `marker()` factory (defined at the top of the file). Add a local `bar()` factory (the file has none):

```ts
function tbar(time: string): ChartBar {
	return { time, open: 10, high: 11, low: 9, close: 10.5, volume: 1000 };
}

// Sequential ISO weekdays starting 2026-01-05 (a Monday), skipping weekends —
// enough like real sessions for lexicographic time comparisons.
function sessions(count: number): string[] {
	const out: string[] = [];
	const d = new Date('2026-01-05T00:00:00Z');
	while (out.length < count) {
		const day = d.getUTCDay();
		if (day !== 0 && day !== 6) out.push(d.toISOString().slice(0, 10));
		d.setUTCDate(d.getUTCDate() + 1);
	}
	return out;
}

describe('trimLeadInBars', () => {
	it('keeps exactly LEAD_IN_DISPLAY_SESSIONS bars before the brief session on a long lead-in', () => {
		// 60 sessions; brief on session index 50 → 30 in-window from the brief on.
		const times = sessions(60);
		const bars = times.map(tbar);
		const briefDate = times[50];

		const trimmed = trimLeadInBars(bars, briefDate, []);

		expect(trimmed.length).toBe(LEAD_IN_DISPLAY_SESSIONS + 10);
		expect(trimmed[0].time).toBe(times[50 - LEAD_IN_DISPLAY_SESSIONS]);
		expect(trimmed[trimmed.length - 1].time).toBe(times[59]);
	});

	it('is a no-op when the lead-in is already at or under the display cap', () => {
		const times = sessions(25);
		const bars = times.map(tbar);
		// Brief on index 20 → lead-in exactly 20 sessions: nothing to cut.
		expect(trimLeadInBars(bars, times[20], [])).toBe(bars);
	});

	it('is a no-op when there is no anchor at all (no brief date, no ENTRY marker)', () => {
		const bars = sessions(40).map(tbar);
		expect(trimLeadInBars(bars, null, [])).toBe(bars);
	});

	it('anchors on the brief session for a PLANNED payload (empty marker list)', () => {
		const times = sessions(40);
		const bars = times.map(tbar);
		const trimmed = trimLeadInBars(bars, times[30], []);
		expect(trimmed[0].time).toBe(times[30 - LEAD_IN_DISPLAY_SESSIONS]);
	});

	it('falls back to the first ENTRY marker session when the brief date is missing', () => {
		const times = sessions(40);
		const bars = times.map(tbar);
		const markers = [marker('ENTRY', times[32], 'E1')];
		const trimmed = trimLeadInBars(bars, null, markers);
		expect(trimmed[0].time).toBe(times[32 - LEAD_IN_DISPLAY_SESSIONS]);
	});

	it('snaps a non-trading brief date forward to the next session (briefLineTime semantics)', () => {
		const times = sessions(40);
		const bars = times.map(tbar);
		// A Saturday between times[29] (Fri) and times[30] (Mon): brief snaps to times[30].
		const friday = new Date(`${times[29]}T00:00:00Z`);
		friday.setUTCDate(friday.getUTCDate() + 1);
		const saturday = friday.toISOString().slice(0, 10);
		const trimmed = trimLeadInBars(bars, saturday, []);
		expect(trimmed[0].time).toBe(times[30 - LEAD_IN_DISPLAY_SESSIONS]);
	});

	it('never trims when the anchor is not found in the bars (brief postdates every bar)', () => {
		const times = sessions(40);
		const bars = times.map(tbar);
		expect(trimLeadInBars(bars, '2027-01-01', [])).toBe(bars);
	});
});
```

Note: `sessions()` weekday indices — 2026-01-05 is a Monday, so `times[29]` is a Friday (index 29 = 6th week, day 5); the Saturday-snap test relies on consecutive indices 29/30 spanning a weekend. If the assertion about which weekday `times[29]` is fails, pick the nearest index pair `(i, i+1)` where `new Date(times[i])` is a Friday — the test intent (a brief dated on a non-session day) is what matters, not the exact index.

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd apps/web && pnpm exec vitest run tests/unit/ladderChart.test.ts
```

Expected: FAIL — `trimLeadInBars` / `LEAD_IN_DISPLAY_SESSIONS` are not exported.

- [ ] **Step 4: Implement the helper**

Append to `apps/web/src/lib/components/ladderChart.ts`:

```ts
/** Sessions of pre-brief lead-in kept in the DISPLAYED window. The pipeline
 *  persists up to 90 lead-in sessions (min(90, max(20, 2×hold))) for context,
 *  but on a short hold that squeezes the entire ladder into the right ~10% of
 *  the chart width. 20 sessions ≈ one trading month: enough recent range to
 *  read the setup against, while the trade region keeps most of the width. */
export const LEAD_IN_DISPLAY_SESSIONS = 20;

/** Bars cut down to at most `keep` sessions before the anchor session.
 *
 *  Anchor = the brief (arrival) session when known, else the first ENTRY
 *  marker session — an entry can only postdate the brief, so the fallback
 *  only ever widens the kept lead-in, never cuts into the trade. No anchor
 *  at all (context-only payload: no brief date, no fills) → bars returned
 *  unchanged, same when the anchor is not present in the bars. Trailing
 *  bars are never touched. */
export function trimLeadInBars(
	bars: ChartBar[],
	briefDate: string | null | undefined,
	markers: ChartMarker[],
	keep: number = LEAD_IN_DISPLAY_SESSIONS
): ChartBar[] {
	const anchor =
		briefLineTime(bars, briefDate) ?? markers.find((m) => m.kind === 'ENTRY')?.time ?? null;
	if (anchor == null) return bars;
	const anchorIdx = bars.findIndex((b) => b.time >= anchor);
	// Covers anchorIdx === -1 (anchor past every bar) and short lead-ins alike.
	if (anchorIdx <= keep) return bars;
	return bars.slice(anchorIdx - keep);
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd apps/web && pnpm exec vitest run tests/unit/ladderChart.test.ts
```

Expected: PASS (all pre-existing `describe` blocks too).

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/lib/components/ladderChart.ts apps/web/tests/unit/ladderChart.test.ts
git commit -s -m "feat(web): add trimLeadInBars helper capping displayed ladder lead-in at 20 sessions"
```

---

### Task 2: Wire trim + right offset into LadderChart.svelte, add Storybook story

**Files:**
- Modify: `apps/web/src/lib/components/LadderChart.svelte` (imports ~line 38-43; candles ~line 203-210; `fitContent` line 294; ResizeObserver ~line 377-383)
- Modify: `apps/web/src/lib/components/LadderChart.stories.svelte` (new payload builder + one new `<Story>`)

**Interfaces:**
- Consumes: `trimLeadInBars` from `./ladderChart` (Task 1 signature: `(bars, briefDate, markers, keep?) => ChartBar[]`).
- Produces: nothing consumed by later tasks (Task 3 is process only).

- [ ] **Step 1: Trim the bars fed to the candle series**

In `LadderChart.svelte`, extend the existing import from `./ladderChart`:

```ts
import {
	briefLineTime,
	collapseEntryMarkers,
	deeperEntryTierLines,
	finalExitMarkerTime,
	trimLeadInBars
} from './ladderChart';
```

Replace the candles block (currently `payload.bars.map(...)` at ~line 203):

```ts
// Display window: cut the persisted lead-in (up to 90 sessions) down to
// LEAD_IN_DISPLAY_SESSIONS before the brief so the trade region — not the
// pre-brief drift — owns the chart width. Everything else (band, brief
// line, in-trade counts) keeps reading payload.bars; the trim only affects
// what the candle series draws, and the trimmed window always contains the
// brief session onward, so every marker/overlay anchor stays on-chart.
const displayBars = trimLeadInBars(payload.bars, payload.brief_date, payload.markers);
const candles: CandlestickData<Time>[] = displayBars.map((b) => ({
	time: b.time as Time,
	open: b.open,
	high: b.high,
	low: b.low,
	close: b.close
}));
series.setData(candles);
```

- [ ] **Step 2: Replace `fitContent()` with an offset logical range**

Add the constant next to `COLOR` (~line 66, script top level):

```ts
// Empty bar slots kept right of the last candle so the E1/TP1 marker arrows
// and the last candle do not butt against the price-scale labels.
const RIGHT_OFFSET_BARS = 3;
```

Replace line 294 (`chart.timeScale().fitContent();`) with:

```ts
// Static replay viewport with breathing room: fitContent() would butt the
// last bar against the price scale, colliding marker arrows with the axis
// labels. Bar 0 starts at logical -0.5; ending RIGHT_OFFSET_BARS past the
// last bar keeps the whole window visible plus a small right margin.
const applyViewport = () => {
	chart?.timeScale().setVisibleLogicalRange({
		from: -0.5,
		to: candles.length - 0.5 + RIGHT_OFFSET_BARS
	});
};
applyViewport();
timeScale = chart.timeScale();
```

(The `timeScale = chart.timeScale();` line already exists right after — keep exactly one.)

- [ ] **Step 3: Re-apply the viewport on resize**

In the ResizeObserver callback (~line 377), add `applyViewport()` before `updateOverlays()`:

```ts
resizeObserver = new ResizeObserver(() => {
	if (chart && chartContainer) {
		chart.applyOptions({ width: chartContainer.clientWidth });
		// Width changes must not drift the viewport — re-pin the logical
		// range (never fitContent) before repositioning the overlays.
		applyViewport();
		updateOverlays();
	}
});
```

- [ ] **Step 4: Run the type/lint gate**

```bash
cd apps/web && pnpm run check
```

Expected: 0 errors, 0 warnings (same count as before the change).

- [ ] **Step 5: Add the "Long Lead-In (trimmed)" story**

In `LadderChart.stories.svelte` module script, after `PLANNED_PAYLOAD`, add a builder + payload (reuses the file's existing `bar()`/`marker()` factories):

```ts
// LONG LEAD-IN: the pipeline persists up to 90 pre-brief sessions for a
// short hold. The component must display only the last 20 of them — the
// brief line should sit ~20 bars from the left edge with the trade filling
// the rest, plus an empty right margin (RIGHT_OFFSET_BARS) before the axis.
function leadInBars(startISO: string, count: number, base: number): ChartBar[] {
	const out: ChartBar[] = [];
	const d = new Date(`${startISO}T00:00:00Z`);
	while (out.length < count) {
		const day = d.getUTCDay();
		if (day !== 0 && day !== 6) {
			out.push(bar(d.toISOString().slice(0, 10), base + Math.sin(out.length / 7) * 4));
		}
		d.setUTCDate(d.getUTCDate() + 1);
	}
	return out;
}

// 90 weekday sessions starting 2026-02-02 end well before the CLOSED trade's
// first bar (2026-06-10) — same trade shape as CLOSED_PAYLOAD, longer tail.
const LONG_LEADIN_PAYLOAD: ChartPayload = {
	...CLOSED_PAYLOAD,
	ticker: 'PSN',
	bars: [...leadInBars('2026-02-02', 90, 116), ...CLOSED_PAYLOAD.bars]
};
```

Then add one story following the file's existing `<Story>` pattern (name + args, no `play` needed — the trim itself is pinned by unit tests; this story is the visual spec):

```svelte
<Story name="Long Lead-In (trimmed)" args={{ payload: LONG_LEADIN_PAYLOAD }} />
```

- [ ] **Step 6: Run the Storybook gate**

```bash
cd apps/web && pnpm run check && pnpm run build-storybook
```

Expected: both pass.

- [ ] **Step 7: Visual sanity check in Storybook**

Run `pnpm run storybook` (from `apps/web`), open LadderChart → "Long Lead-In (trimmed)" and verify: (a) roughly 30 candles visible, not 100; (b) the dashed BRIEF line sits ~2/3 left of center; (c) a visible empty margin between the last candle and the price scale; (d) the green in-trade band and price-line labels render as before. Also spot-check "Closed Trade" and "Planned Not Triggered" (short fixtures — must look unchanged). Then stop the dev server.

- [ ] **Step 8: Run the full web unit-test suite**

```bash
cd apps/web && pnpm exec vitest run
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add apps/web/src/lib/components/LadderChart.svelte apps/web/src/lib/components/LadderChart.stories.svelte
git commit -s -m "feat(web): trim ladder-chart lead-in to 20 sessions and add right-edge bar offset"
```

---

### Task 3: Push branch and open the PR

**Files:** none (process only).

**Interfaces:**
- Consumes: commits from Tasks 1-2 on `feature/ladder-chart-leadin-trim`.
- Produces: an open PR on `kamilpajak/AlphaLens` (zen pre-merge review happens afterwards, orchestrator-side).

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feature/ladder-chart-leadin-trim
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --repo kamilpajak/AlphaLens \
  --title "feat(web): declutter ladder chart — trim lead-in to 20 sessions + right-edge offset" \
  --body "$(cat <<'EOF'
Feature: cap the ladder replay chart's displayed pre-brief history at 20 sessions and add a 3-bar right margin.

## How it works today / problem
- The pipeline persists up to 90 lead-in sessions in `chart_payload_json`; the chart draws all of them and calls `fitContent()`.
- On short holds the whole ladder (entry/e2/e3/tp/stop lines, E1/TP1 markers) is squeezed into the last ~8% of the width and marker arrows collide with the price-scale labels.

## Goal
- The trade region — not the pre-brief drift — should own the chart width. The pre-entry history carries little information for the viewer.

## How
- New pure helper `trimLeadInBars` (in `ladderChart.ts`): keeps at most `LEAD_IN_DISPLAY_SESSIONS = 20` bars before the brief session (fallback anchor: first ENTRY marker; no anchor → no-op). Only the candle series input is trimmed; overlays still read the full payload.
- `fitContent()` replaced with `setVisibleLogicalRange` ending `RIGHT_OFFSET_BARS = 3` slots past the last bar; re-applied on resize.
- Chart stays static (scroll/zoom remain disabled) per the approved design (option B — hard cut).

Design spec: `docs/superpowers/specs/2026-07-30-ladder-chart-leadin-trim-design.md`

## Test plan
- [ ] `pnpm exec vitest run` (new `trimLeadInBars` describe block + existing suites)
- [ ] `pnpm run check`
- [ ] `pnpm run build-storybook`
- [ ] Manual: Storybook "Long Lead-In (trimmed)" story + short-fixture stories unchanged
- Not covered: no DOM/canvas render test exists for this component (pre-existing gap); trim behaviour is pinned at the pure-helper level.

## Known issues / behaviour notes
- The persisted 90-session lead-in is still fetched and parsed — only the display is trimmed. If we later want a "show full history" affordance, the data is already in the payload (rejected option A kept the history reachable via pan).
- Payloads with no brief date and no ENTRY marker (context-only) are intentionally left untrimmed.
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

Return the PR number/URL to the orchestrator. Zen pre-merge codereview + CI watch happen orchestrator-side after this task.
