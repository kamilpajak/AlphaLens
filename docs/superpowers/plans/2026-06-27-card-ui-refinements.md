# Card UI/Logic Refinements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three small `apps/web` refinements to the (already-regrouped) candidate card: collision-proof LENS SCORE scale, a symmetric minimal Buffett drawer card when only a numeric score exists, and hide the trade TTL chip when there is no structured ladder.

**Architecture:** Pure presentation change in three Svelte components plus one tiny pure helper. `ExpertPanel.svelte` gets the scale restructure (#2) and the Buffett-symmetry empty state (#5); `CandidateCard.svelte` passes a new `tenkAvailable` prop (#5); `TradeSetup.svelte` gates its TTL chip on structure (#6). No `Candidate`-type / data-contract / pipeline / Django change.

**Tech Stack:** SvelteKit 5 (runes), TypeScript, Tailwind, Vitest (unit, node env — pure-function only), Playwright (smoke, real build + `tests/fixtures/api-mock/`).

## Global Constraints

- **Scope = `apps/web` only.** No `Candidate` type / data-contract / pipeline / Django change.
- **Stacked branch:** `feature/card-ui-refinements` is branched off `feature/card-domain-regroup` (PR #682). It builds on the regrouped card. DCO **sign-off required on every commit** (`git commit -s`).
- **Manufactured-authority discipline unchanged:** the disagreement band word/colour stays inside the opened drawer only.
- **Gates ≠ lenses:** gates remain the header provenance strip; #5 only *references* the `tenk` gate as the cause of an absent qualitative read — it does not move or merge gates.
- **Atomic tokens never wrap** (CLAUDE.md): dates / math notation / version strings / tickers carry `whitespace-nowrap`.
- **The 10-K gate id is `tenk`** (lowercase). The 10-K exists iff `tenk ∈ gates_passed ∪ gates_failed`.
- **TDD** matched to the harness: pure-function logic → vitest; DOM → Playwright smoke.
- **Conventional Commits**, `type(scope): description`, no AI-assistance mentions.
- Run all commands from `apps/web/`. Worktree: `.claude/worktrees/card-refinements`.

## File Structure

- **Modify** `src/lib/format.ts` — add pure helper `tenkAvailable(gatesPassed, gatesFailed)`.
- **Modify** `src/lib/components/ExpertPanel.svelte` — add `tenkAvailable` prop; widen `sections` to include Buffett on a numeric score; add the Buffett empty state; restructure the LENS SCORE scale into 3 stacked rows.
- **Modify** `src/lib/components/CandidateCard.svelte` — pass `tenkAvailable={…}` to `<ExpertPanel>`.
- **Modify** `src/lib/components/TradeSetup.svelte` — gate the TTL chip on `hasStructure`.
- **Modify** `tests/unit/format.test.ts` — `tenkAvailable` cases.
- **Modify** `tests/unit/expertPanel.test.ts` — mirror the Buffett-card inclusion predicate.
- **Modify** `tests/smoke.test.ts` — drawer/scale/TTL assertions on the `FOUR` fixture.

---

### Task 1: `tenkAvailable` pure helper

**Files:**
- Modify: `src/lib/format.ts` (append near the other helpers)
- Test: `tests/unit/format.test.ts`

**Interfaces:**
- Produces: `tenkAvailable(gatesPassed: string[] | null | undefined, gatesFailed: string[] | null | undefined): boolean` — true iff `'tenk'` is in either array (the 10-K exists whether keywords matched or not). Consumed by `CandidateCard.svelte` (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/format.test.ts` (add `tenkAvailable` to the existing `../../src/lib/format` import):

```ts
import { tenkAvailable } from '../../src/lib/format';

describe('tenkAvailable (10-K presence from gate arrays)', () => {
	it('true when tenk passed (keywords matched)', () => {
		expect(tenkAvailable(['tenk'], ['press'])).toBe(true);
	});
	it('true when tenk failed (10-K exists, no keyword match)', () => {
		expect(tenkAvailable(['press'], ['tenk'])).toBe(true);
	});
	it('false when tenk only unknown / absent from both arrays', () => {
		expect(tenkAvailable(['press'], ['insider'])).toBe(false);
	});
	it('false / safe on null / undefined inputs', () => {
		expect(tenkAvailable(null, undefined)).toBe(false);
		expect(tenkAvailable(undefined, null)).toBe(false);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test:unit -- format`
Expected: FAIL — `tenkAvailable is not a function`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/lib/format.ts`:

```ts
/**
 * Whether a 10-K exists for the ticker, read from the gate arrays. The `tenk`
 * gate is `passed` when theme keywords matched the 10-K and `failed` when the
 * 10-K exists but no keyword hit — both mean the filing is available; only
 * `unknown` (absent from both) means no 10-K. Used to explain an absent Buffett
 * qualitative read (which reads the 10-K).
 */
export function tenkAvailable(
	gatesPassed: string[] | null | undefined,
	gatesFailed: string[] | null | undefined
): boolean {
	return Boolean(gatesPassed?.includes('tenk') || gatesFailed?.includes('tenk'));
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm test:unit -- format`
Expected: PASS (4 new cases + existing).

- [ ] **Step 5: Commit**

```bash
git add src/lib/format.ts tests/unit/format.test.ts
git commit -s -m "feat(web): add tenkAvailable gate-presence helper"
```

---

### Task 2: Buffett drawer symmetry (#5)

When a candidate has a numeric Buffett score but no qualitative data, render the Buffett card (score header + an empty-state line that names the 10-K dependency) so the drawer is symmetric with O'Neil.

**Files:**
- Modify: `src/lib/components/ExpertPanel.svelte`
- Modify: `src/lib/components/CandidateCard.svelte`
- Test: `tests/unit/expertPanel.test.ts`, `tests/smoke.test.ts`

**Interfaces:**
- Consumes: `tenkAvailable` (Task 1); existing `buffScore`, `hasBuffQual`, `hasOneil`, `spread`, `hasScoreBreakdown`, `sections`, `buffPillars`.
- Produces: `ExpertPanel` prop `tenkAvailable?: boolean | null`; a Buffett card that renders on `hasBuffQual || buffScore !== null`.

- [ ] **Step 1: Write the failing unit test (inclusion predicate mirror)**

Append to `tests/unit/expertPanel.test.ts`:

```ts
// Mirrors the ExpertPanel `sections` Buffett-arm rule: the Buffett card shows
// when it has qualitative data OR a numeric score (symmetry with O'Neil).
function showsBuffettCard(hasBuffQual: boolean, buffScore: number | null): boolean {
	return hasBuffQual || buffScore !== null;
}

describe('Buffett card inclusion (symmetry)', () => {
	it('shows on qual only', () => {
		expect(showsBuffettCard(true, null)).toBe(true);
	});
	it('shows on numeric score only', () => {
		expect(showsBuffettCard(false, 62)).toBe(true);
	});
	it('shows on both', () => {
		expect(showsBuffettCard(true, 62)).toBe(true);
	});
	it('hidden when neither', () => {
		expect(showsBuffettCard(false, null)).toBe(false);
	});
});
```

- [ ] **Step 2: Run to verify it passes immediately (pure mirror)**

Run: `pnpm test:unit -- expertPanel`
Expected: PASS. (This is a regression-pinning mirror; it documents the rule Step 3 implements in the template. It passes on its own — its value is catching a future drift of the rule.)

- [ ] **Step 3: Add the `tenkAvailable` prop to ExpertPanel**

In `src/lib/components/ExpertPanel.svelte`, find the `interface Props` block:

```ts
	interface Props {
		assessments: ExpertAssessments | null | undefined;
		layer4Score?: number | null;
		atrPenalty?: number | null;
		selectionScore?: number | null;
		scorerConfigVersion?: string | null;
	}
	let { assessments, layer4Score, atrPenalty, selectionScore, scorerConfigVersion }: Props =
		$props();
```
Replace with (add `tenkAvailable`):

```ts
	interface Props {
		assessments: ExpertAssessments | null | undefined;
		layer4Score?: number | null;
		atrPenalty?: number | null;
		selectionScore?: number | null;
		scorerConfigVersion?: string | null;
		/** Whether a 10-K exists (from the tenk gate) — explains an absent Buffett
		 *  qualitative read. */
		tenkAvailable?: boolean | null;
	}
	let {
		assessments,
		layer4Score,
		atrPenalty,
		selectionScore,
		scorerConfigVersion,
		tenkAvailable
	}: Props = $props();
```

- [ ] **Step 4: Widen `sections` + `hasContent`**

In the same file, find:

```ts
	const hasContent = $derived(hasBuffQual || hasOneil || spread !== null || hasScoreBreakdown);
```
Replace with (also surface the drawer when only a numeric Buffett score exists):

```ts
	const hasContent = $derived(
		hasBuffQual || hasOneil || spread !== null || hasScoreBreakdown || buffScore !== null
	);
```

Then find:

```ts
	const sections = $derived(
		['buffett', 'oneil'].filter((id) =>
			id === 'buffett' ? hasBuffQual : id === 'oneil' ? hasOneil : false
		)
	);
```
Replace with (Buffett shows on qual OR a numeric score):

```ts
	const sections = $derived(
		['buffett', 'oneil'].filter((id) =>
			id === 'buffett' ? hasBuffQual || buffScore !== null : id === 'oneil' ? hasOneil : false
		)
	);
```

- [ ] **Step 5: Add the Buffett empty state**

In the per-expert loop's Buffett branch, the current structure is `{#if isBuf}` → pillars `<div>` + rationale `{#if}` + footnote `<div>`, then `{:else}` → O'Neil content. Wrap the three Buffett children in `{#if hasBuffQual}` and add an `{:else}` empty state. Find:

```svelte
						{#if isBuf}
							<div class="mt-3 flex flex-wrap gap-2">
								{#each buffPillars as pillar (pillar.label)}
									<ExpertPillar label={pillar.label} value={pillar.value} tone={pillar.tone} body={pillar.body} />
								{/each}
							</div>
							{#if buf?.buffett_qualitative_rationale}
								<blockquote class="mt-3 border-l-2 border-cyan pl-4">
									<p class="text-fg-dim text-xs leading-relaxed">{buf.buffett_qualitative_rationale}</p>
								</blockquote>
							{/if}
							<div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] leading-snug text-fg-muted">
								{#if buf?.buffett_used_scuttlebutt}
									<span class="text-amber whitespace-nowrap">scuttlebutt: web-grounded, unverified</span>
								{/if}
								{#if buf?.buffett_qual_computed_at}
									<span class="whitespace-nowrap">classified {fmtDate(buf.buffett_qual_computed_at)}</span>
								{/if}
							</div>
						{:else}
```
Replace the `{#if isBuf}` … up to (not including) the `{:else}` with:

```svelte
						{#if isBuf}
							{#if hasBuffQual}
								<div class="mt-3 flex flex-wrap gap-2">
									{#each buffPillars as pillar (pillar.label)}
										<ExpertPillar label={pillar.label} value={pillar.value} tone={pillar.tone} body={pillar.body} />
									{/each}
								</div>
								{#if buf?.buffett_qualitative_rationale}
									<blockquote class="mt-3 border-l-2 border-cyan pl-4">
										<p class="text-fg-dim text-xs leading-relaxed">{buf.buffett_qualitative_rationale}</p>
									</blockquote>
								{/if}
								<div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] leading-snug text-fg-muted">
									{#if buf?.buffett_used_scuttlebutt}
										<span class="text-amber whitespace-nowrap">scuttlebutt: web-grounded, unverified</span>
									{/if}
									{#if buf?.buffett_qual_computed_at}
										<span class="whitespace-nowrap">classified {fmtDate(buf.buffett_qual_computed_at)}</span>
									{/if}
								</div>
							{:else}
								<p class="mt-3 text-[10px] leading-snug text-fg-muted">
									numeric score only —
									<span class="text-fg-dim"
										>{tenkAvailable
											? 'qualitative read not computed'
											: 'no 10-K for a qualitative read'}</span
									>
								</p>
							{/if}
						{:else}
```

(Leave the `{:else}` O'Neil branch and everything after it unchanged. Verify the tag nesting is balanced: you added one `{#if hasBuffQual}…{:else}…{/if}` strictly inside the existing `{#if isBuf}` arm.)

- [ ] **Step 6: Pass `tenkAvailable` from CandidateCard**

In `src/lib/components/CandidateCard.svelte`, add `tenkAvailable` to the `$lib/format` import list, then find the `<ExpertPanel … />` mount:

```svelte
			<ExpertPanel
				assessments={c.expert_assessments}
				layer4Score={c.layer4_weighted_score}
				atrPenalty={c.atr_penalty}
				selectionScore={c.selection_score}
				scorerConfigVersion={c.scorer_config_version}
			/>
```
Replace with (add the prop):

```svelte
			<ExpertPanel
				assessments={c.expert_assessments}
				layer4Score={c.layer4_weighted_score}
				atrPenalty={c.atr_penalty}
				selectionScore={c.selection_score}
				scorerConfigVersion={c.scorer_config_version}
				tenkAvailable={tenkAvailable(c.gates_passed, c.gates_failed)}
			/>
```

- [ ] **Step 7: Write the failing smoke assertion**

Add to the `card — domain grouping` describe block in `tests/smoke.test.ts` (fixture `FOUR` = Buffett 62 numeric, no qual, `tenk` passed):

```ts
	test(`buffett drawer card is symmetric (score + empty state, no pillars) on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const drawer = card.locator('[data-testid="expert-panel-body"]');
		await expect(drawer).toBeVisible();
		// The Buffett card renders its empty state (numeric score, no qual).
		await expect(drawer.getByText('qualitative read not computed', { exact: false })).toBeVisible();
		// No qualitative pillars for this name.
		await expect(drawer.getByText('moat', { exact: false })).toHaveCount(0);
	});
```

- [ ] **Step 8: Run to verify it fails, then passes after the edits**

Run: `pnpm test -- smoke.test.ts -g "symmetric"`
Expected: with Steps 3–6 applied, PASS. (If you run it before the edits it FAILS: the Buffett card is absent so the empty-state text is missing.)

- [ ] **Step 9: Verify full check + suites**

Run:
```bash
pnpm check
pnpm test:unit -- expertPanel
pnpm test -- smoke.test.ts -g "domain-grouped|omits the O'Neil|symmetric"
```
Expected: `pnpm check` 0 errors; unit pass; smoke greps pass.

- [ ] **Step 10: Commit**

```bash
git add src/lib/components/ExpertPanel.svelte src/lib/components/CandidateCard.svelte tests/unit/expertPanel.test.ts tests/smoke.test.ts
git commit -s -m "feat(web): symmetric buffett drawer card with gate-aware empty state"
```

---

### Task 3: LENS SCORE scale — stacked labels + clipped dots (#2)

**Files:**
- Modify: `src/lib/components/ExpertPanel.svelte`
- Test: `tests/smoke.test.ts`

**Interfaces:**
- Consumes: existing `buffScore`, `oneilScore`, `bandTone`, `buffT`, `oneilT`, `gapLeft`, `gapWidth`, `toneDot`, `toneText`, `labelShift`.
- Produces: a 3-row scale with `data-testid="lens-label-buffett"` and `data-testid="lens-label-oneil"` in separate rows; the track has `overflow-hidden`.

- [ ] **Step 1: Write the failing smoke assertion**

Add to the `card — domain grouping` describe block in `tests/smoke.test.ts`:

```ts
	test(`lens-score labels are stacked in separate rows on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const buf = card.locator('[data-testid="lens-label-buffett"]');
		const oneil = card.locator('[data-testid="lens-label-oneil"]');
		await expect(buf).toBeVisible();
		await expect(oneil).toBeVisible();
		await expect(buf).toContainText('Buffett');
		await expect(oneil).toContainText("O'Neil");
		// Stacked, not overlapping: the buffett label sits above the o'neil label.
		const b = await buf.boundingBox();
		const o = await oneil.boundingBox();
		expect(b && o && b.y < o.y).toBeTruthy();
	});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- smoke.test.ts -g "stacked in separate rows"`
Expected: FAIL — no `lens-label-buffett` / `lens-label-oneil` testids exist yet.

- [ ] **Step 3: Restructure the scale block**

In `src/lib/components/ExpertPanel.svelte`, find the track block (inside `{#if showScale}`, after the `lens score` / `0–100` header row):

```svelte
						<!-- track -->
						<div class="relative mt-2 mb-7 h-1.5 rounded-full bg-grid" aria-hidden="true">
							<span
								class="absolute top-0 h-1.5 rounded-full"
								class:bg-green={bandTone === 'green'}
								class:bg-amber={bandTone === 'amber'}
								class:bg-red={bandTone === 'red'}
								style="left: {gapLeft}%; width: {gapWidth}%; opacity: 0.22"
							></span>
							<!-- Buffett marker + label -->
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									buffT
								)}"
								style="left: {buffScore}%"
							></span>
							<span
								class="absolute top-full mt-1 text-[9px] whitespace-nowrap {toneText(buffT)}"
								style="left: {buffScore}%; transform: {labelShift(buffScore!)}"
							>
								Buffett {buffScore}
							</span>
							<!-- O'Neil marker + label -->
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									oneilT
								)}"
								style="left: {oneilScore}%"
							></span>
							<span
								class="absolute top-full mt-1 text-[9px] whitespace-nowrap {toneText(oneilT)}"
								style="left: {oneilScore}%; transform: {labelShift(oneilScore!)}"
							>
								O'Neil {oneilScore}
							</span>
						</div>
```
Replace the whole block with a 3-row stack (Buffett label row → track → O'Neil label row):

```svelte
						<!-- Buffett label row (above the track) -->
						<div class="relative mt-2 h-3.5">
							<span
								data-testid="lens-label-buffett"
								class="absolute bottom-0 text-[9px] whitespace-nowrap {toneText(buffT)}"
								style="left: {buffScore}%; transform: {labelShift(buffScore!)}"
							>
								Buffett {buffScore}
							</span>
						</div>
						<!-- track (overflow-hidden clips the dots cleanly at 0/100) -->
						<div class="relative h-1.5 overflow-hidden rounded-full bg-grid" aria-hidden="true">
							<span
								class="absolute top-0 h-1.5 rounded-full"
								class:bg-green={bandTone === 'green'}
								class:bg-amber={bandTone === 'amber'}
								class:bg-red={bandTone === 'red'}
								style="left: {gapLeft}%; width: {gapWidth}%; opacity: 0.22"
							></span>
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									buffT
								)}"
								style="left: {buffScore}%"
							></span>
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									oneilT
								)}"
								style="left: {oneilScore}%"
							></span>
						</div>
						<!-- O'Neil label row (below the track) -->
						<div class="relative mb-1 h-3.5">
							<span
								data-testid="lens-label-oneil"
								class="absolute top-0 text-[9px] whitespace-nowrap {toneText(oneilT)}"
								style="left: {oneilScore}%; transform: {labelShift(oneilScore!)}"
							>
								O'Neil {oneilScore}
							</span>
						</div>
```

(The track no longer carries the dots' overflow risk; the two labels are in their own rows so `overflow-hidden` on the track never clips them. `labelShift` keeps each label's text within the horizontal bounds; vertical separation removes the collision.)

- [ ] **Step 4: Run to verify the smoke passes + no regression**

Run:
```bash
pnpm check
pnpm test -- smoke.test.ts -g "stacked in separate rows|domain-grouped|omits the O'Neil|symmetric"
```
Expected: `pnpm check` 0 errors; all greps PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/components/ExpertPanel.svelte tests/smoke.test.ts
git commit -s -m "fix(web): stack lens-score labels + clip dots so close scores don't collide"
```

---

### Task 4: Hide TTL chip when no ladder (#6)

**Files:**
- Modify: `src/lib/components/TradeSetup.svelte`
- Test: `tests/smoke.test.ts`

**Interfaces:**
- Consumes: existing `hasStructure`, `setup`.
- Produces: the `ttl: N days` chip renders only when `hasStructure` is true.

- [ ] **Step 1: Write the failing smoke assertion**

Add to the `card — domain grouping` describe block in `tests/smoke.test.ts` (the `FOUR` first card has `NO STRUCTURED LADDER`):

```ts
	test(`no TTL chip when there is no structured ladder on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const setup = page.locator('article[id]').first().locator('[data-testid="trade-setup"]');
		await expect(setup.getByText('no structured ladder', { exact: false })).toBeVisible();
		await expect(setup.getByText('ttl:', { exact: false })).toHaveCount(0);
	});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- smoke.test.ts -g "no TTL chip"`
Expected: FAIL — the `FOUR` setup has `order_ttl_days` set, so the chip currently renders despite no ladder (`ttl:` count is 1, not 0).

- [ ] **Step 3: Gate the TTL chip on structure**

In `src/lib/components/TradeSetup.svelte`, find:

```svelte
		{#if setup?.order_ttl_days != null}
			<span
				class="px-2 py-0.5 border border-grid-strong text-[9px] uppercase tracking-widest text-fg-muted whitespace-nowrap"
			>
				ttl: {setup.order_ttl_days} days
			</span>
		{/if}
```
Replace the condition with (TTL only makes sense with a live ladder):

```svelte
		{#if hasStructure && setup?.order_ttl_days != null}
			<span
				class="px-2 py-0.5 border border-grid-strong text-[9px] uppercase tracking-widest text-fg-muted whitespace-nowrap"
			>
				ttl: {setup.order_ttl_days} days
			</span>
		{/if}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- smoke.test.ts -g "no TTL chip"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/components/TradeSetup.svelte tests/smoke.test.ts
git commit -s -m "fix(web): hide trade TTL chip when there is no structured ladder"
```

---

### Task 5: Full verification + visual

**Files:** none changed unless a regression is found.

- [ ] **Step 1: Run the full suite**

Run (from `apps/web/`):
```bash
pnpm test:unit
pnpm check
pnpm build
pnpm test
```
Expected: unit all pass (Task 1 + Task 2 additions), `check` 0 errors, build OK, full Playwright smoke pass (the three new `card — domain grouping` tests + everything else, console-clean).

- [ ] **Step 2: Visual confirmation**

On `FOUR` (local preview or a fresh headless screenshot of the first card + opened drawer): confirm
- the LENS SCORE scale shows `Buffett 62` above the track and `O'Neil 55` below (no overlap), dots clean at the edges;
- the drawer shows a Buffett card with `62/100` + "numeric score only — qualitative read not computed" and no moat/trend pillars;
- the right column shows `NO STRUCTURED LADDER` with **no** `TTL:` chip.

- [ ] **Step 3: No extra commit unless a fix was needed.**

---

## Self-Review

**Spec coverage:**
- #2 scale restructure (stacked labels + overflow-hidden) → Task 3.
- #5 Buffett symmetry (sections include + empty state + `tenkAvailable` prop + gate-aware copy + `hasContent`) → Task 1 (helper) + Task 2.
- #6 TTL gated on structure → Task 4.
- Gate dependency surfaced, gates not merged → Task 2 Step 5 copy (`tenkAvailable` branch).
- Tests (unit `tenkAvailable` + inclusion mirror; smoke scale/symmetry/TTL) → Tasks 1–4; full run Task 5.
- Edge cases (both lenses null → scale gated; buffett fully absent → not in sections; tenk unknown → neutral copy) → covered by the `showScale`/`sections`/`tenkAvailable` logic.

**Placeholder scan:** no TBD/TODO; every code step shows complete markup; commands have expected output.

**Type consistency:** `tenkAvailable(gatesPassed, gatesFailed): boolean` defined in Task 1, consumed in Task 2 Step 6 with `c.gates_passed, c.gates_failed`. The `ExpertPanel` prop `tenkAvailable?: boolean | null` (Task 2 Step 3) matches the value passed (Task 2 Step 6). `sections` Buffett rule `hasBuffQual || buffScore !== null` (Task 2 Step 4) matches the `showsBuffettCard` mirror (Task 2 Step 1). Testids `lens-label-buffett` / `lens-label-oneil` defined (Task 3 Step 3) match the smoke queries (Task 3 Step 1).
