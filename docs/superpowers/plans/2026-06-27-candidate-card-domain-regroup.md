# Candidate Card Domain-Regroup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the brief candidate card so every metric lives in the one analytical domain a reader looks for it (Valuation & Quality / Momentum & Technicals / Catalyst & Event / Insider), with each lens score anchoring its domain block and no value rendered twice.

**Architecture:** Pure `apps/web` presentation change. The `SYSTEM.SIGNALS` bar list and the `FUNDAMENTALS` / `TECHNICALS.CONTEXT` grids are dissolved and re-emitted as four domain blocks inside the existing left column. Buffett/O'Neil scores move from meta-bar chips to domain-block headers; the `ExpertPanel` drawer loses the O'Neil numeric grid (now in the Momentum block) but keeps the disagreement scale, Buffett prose, O'Neil flags, and the `SCORER BREAKDOWN`. No pipeline/Django/data-contract/`Candidate`-type change — every value stays sourced from the same field.

**Tech Stack:** SvelteKit 5 (runes), TypeScript, Tailwind, Vitest (unit, node env — pure-function only), Playwright (smoke, real build + `tests/fixtures/api-mock/`).

## Global Constraints

- **Scope = `apps/web` only.** No change to `Candidate` type, `format.ts` helper semantics (one new helper added), data contracts, pipeline, Django, parquet.
- **Preserve the `selection_score` ATR-tilt surfaces** (PRs #673/#675/#676): the meta-bar `extended` chip (`(c.atr_penalty ?? 0) > 0`) stays in the meta bar; the `SCORER BREAKDOWN` block + its four `ExpertPanel` props (`layer4Score / atrPenalty / selectionScore / scorerConfigVersion`) stay unchanged.
- **Manufactured-authority discipline unchanged:** the disagreement band word + colour render ONLY inside the opened drawer; the resting card face never shows a verdict. The `extended` chip carries no number.
- **Atomic tokens never wrap** (CLAUDE.md web rule): dates `YYYY-MM-DD`, math notation (`-0.045%/d`, `+0.2σ`), numeric ranges, tickers, version strings carry `whitespace-nowrap` on their wrapping element.
- **No backward compatibility shims** — solo project; rename/move in one commit.
- **TDD** per repo convention, matched to the harness: pure-function logic → vitest; DOM structure → Playwright smoke.
- **Commit convention:** Conventional Commits, `type(scope): description`. Never mention AI assistance.
- Run all commands from `apps/web/` unless noted. Worktree: `.claude/worktrees/card-domain-regroup`, branch `feature/card-domain-regroup`.

## File Structure

- **Modify** `src/lib/format.ts` — add one pure helper `fcffYieldDisplay`.
- **Modify** `src/lib/components/CandidateCard.svelte` — meta-bar slim; replace the thesis + signals/fundamentals/technicals region with four domain blocks.
- **Modify** `src/lib/components/ExpertPanel.svelte` — drop the O'Neil numeric readout grid; add a one-line pointer to the Momentum block.
- **Modify** `tests/unit/format.test.ts` — unit tests for `fcffYieldDisplay`.
- **Modify** `tests/fixtures/api-mock/days/<latest>.json` — ensure one candidate has `atr_penalty > 0` and a full O'Neil blob (no new candidate rows).
- **Modify** `tests/smoke.test.ts` — add a `card — domain grouping` describe block.

---

### Task 1: `fcffYieldDisplay` merge helper

The fcff-yield row is the one piece of new branching logic: it was duplicated (a `%ile` bar in `SYSTEM.SIGNALS` + a raw `%` row in `FUNDAMENTALS`). The merged Valuation row shows the `%ile` as the bar and the raw `%` as an annotation — each shown only when finite.

**Files:**
- Modify: `src/lib/format.ts` (append near the other `fmt*` helpers)
- Test: `tests/unit/format.test.ts`

**Interfaces:**
- Produces: `fcffYieldDisplay(pctile: number | null | undefined, rawPct: number | null | undefined): { pctileText: string | null; rawText: string | null }` — `pctileText` is `"<n>%ile"` when `pctile` is finite else `null`; `rawText` is `fmtPct(rawPct, 2)` (e.g. `"+5.09%"`) when `rawPct` is finite else `null`. Consumed by `CandidateCard.svelte` Task 3.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/format.test.ts` (add `fcffYieldDisplay` to the existing `../../src/lib/format` import):

```ts
import { fcffYieldDisplay } from '../../src/lib/format';

describe('fcffYieldDisplay (merged valuation fcff row)', () => {
	it('both finite → %ile headline + raw annotation', () => {
		expect(fcffYieldDisplay(31, 5.09)).toEqual({ pctileText: '31%ile', rawText: '+5.09%' });
	});
	it('raw negative keeps its sign', () => {
		expect(fcffYieldDisplay(12, -2.5)).toEqual({ pctileText: '12%ile', rawText: '-2.50%' });
	});
	it('pctile null → no headline, raw still shown', () => {
		expect(fcffYieldDisplay(null, 5.09)).toEqual({ pctileText: null, rawText: '+5.09%' });
	});
	it('raw null → headline only', () => {
		expect(fcffYieldDisplay(31, null)).toEqual({ pctileText: '31%ile', rawText: null });
	});
	it('both null / non-finite → both null', () => {
		expect(fcffYieldDisplay(null, undefined)).toEqual({ pctileText: null, rawText: null });
		expect(fcffYieldDisplay(NaN, NaN)).toEqual({ pctileText: null, rawText: null });
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test:unit -- format`
Expected: FAIL — `fcffYieldDisplay is not a function` / import resolves to `undefined`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/lib/format.ts` (uses the existing `fmtPct` / `fmtPctile` in the same file):

```ts
/**
 * Merged fcff-yield Valuation row. Was duplicated on the card (a sector-%ile
 * bar in SYSTEM.SIGNALS + a raw % in FUNDAMENTALS); the domain regroup shows
 * the %ile as the bar headline and the raw % as an annotation, each only when
 * finite. Both null is the honest empty state (the row renders an em-dash).
 */
export function fcffYieldDisplay(
	pctile: number | null | undefined,
	rawPct: number | null | undefined
): { pctileText: string | null; rawText: string | null } {
	return {
		pctileText: Number.isFinite(pctile) ? `${fmtPctile(pctile)}%ile` : null,
		rawText: Number.isFinite(rawPct) ? fmtPct(rawPct, 2) : null
	};
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm test:unit -- format`
Expected: PASS (all 5 new cases + existing format tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/format.ts tests/unit/format.test.ts
git commit -m "feat(web): add fcffYieldDisplay merge helper for the valuation domain row"
```

---

### Task 2: Smoke fixture readiness

The new smoke assertions (Task 3/4) need one fixture candidate with `atr_penalty > 0` (to assert the `extended` chip survives) and one with a full O'Neil blob (rel-strength + earnings-YoY for the Momentum block). Adjust an existing candidate's fields — do **not** add candidate rows (that breaks the per-day `n_candidates` count assertion).

**Files:**
- Modify: `tests/fixtures/api-mock/days/<latest>.json` (the date is `DAYS_INDEX[0].date`; find it from `tests/fixtures/api-mock/days.json`)

**Interfaces:**
- Produces: a fixture brief whose first `article[id]` card (`data[0]`) has a complete O'Neil blob and whose `data` array contains ≥1 candidate with `atr_penalty > 0`. Consumed by Task 3/4 smoke tests.

- [ ] **Step 1: Inspect the latest fixture**

Run:
```bash
node -e "const f=require('./tests/fixtures/api-mock/days.json'); console.log('latest', f[0].date, 'n', f[0].n_candidates)"
node -e "const d=require('./tests/fixtures/api-mock/days/'+require('./tests/fixtures/api-mock/days.json')[0].date+'.json'); const c=d.data||d.candidates||d; console.log(JSON.stringify((c[0]||c.data?.[0]), null, 1).slice(0,1200))"
```
Note the JSON shape (top-level `data` array of candidates), `data[0]`'s `expert_assessments.oneil` keys, and whether any candidate has `atr_penalty > 0`.

- [ ] **Step 2: Edit the fixture so `data[0]` has a full O'Neil blob + one candidate is penalised**

In `tests/fixtures/api-mock/days/<latest>.json`:
- On `data[0].expert_assessments.oneil`, ensure these keys are present and finite: `oneil_score`, `oneil_pct_off_52w_high`, `oneil_rs_approx_pct` (e.g. `24`), `oneil_ma200_slope_pct_per_day`, `oneil_ma200_distance_pct`, `oneil_earnings_growth_yoy_pct` (e.g. `-3.3`). Keep `data[0].expert_assessments.buffett.buffett_quality_score` finite.
- On `data[0]` (or any one candidate), set `atr_penalty` to a value `> 0` (e.g. `0.5`) and `selection_score` = `layer4_weighted_score - atr_penalty`, leaving `scorer_config_version` as a non-empty string. Verify the row still keeps `layer4_weighted_score` finite.

Do not add or remove array elements.

- [ ] **Step 3: Verify the build still parses the fixture (baseline smoke green)**

Run: `pnpm build && pnpm test -- smoke.test.ts`
Expected: PASS — the per-day candidate count + console-clean assertions still hold (richer fields, same row count). (If `pnpm build` is slow, `pnpm test` runs against the preview build.)

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/api-mock/days/
git commit -m "test(web): enrich latest brief fixture with full O'Neil blob + a penalised candidate"
```

---

### Task 3: CandidateCard domain regroup (meta-bar slim + four domain blocks)

Replace the meta-bar right cluster and the entire thesis + signals/fundamentals/technicals region with the domain-grouped structure. Write the failing smoke assertions first, then restructure until green.

**Files:**
- Modify: `src/lib/components/CandidateCard.svelte`
- Modify: `tests/smoke.test.ts` (new `card — domain grouping` describe block)

**Interfaces:**
- Consumes: `fcffYieldDisplay` (Task 1); the existing derived values `buf`, `oneil`, `buffScore`, `buffTone`, `buffLowCov`, `buffRows`, `oneilScore`, `oneilScoreTone`, `oneilRows`, `insider`, `magic`, `confTone`. The O'Neil-sourced Momentum fields `oneil?.oneil_rs_approx_pct` and `oneil?.oneil_earnings_growth_yoy_pct`.
- Produces: a card DOM with sections labeled `catalyst & event`, `valuation & quality`, `momentum & technicals`, `insider / flow`; Buffett score node inside the valuation block, O'Neil score node inside the momentum block; meta bar without catalyst/buffett/o'neil chips.

- [ ] **Step 1: Write the failing smoke assertions**

Append to `tests/smoke.test.ts` (after the existing `test.describe('smoke — every route renders without errors', …)` block; reuse the module-level `installApiMock` via the existing `test.beforeEach`, `latestDay`, and `attachErrorCollectors`):

```ts
test.describe('card — domain grouping', () => {
	test(`first card on /brief/${latestDay.date} is domain-grouped`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await expect(card).toBeVisible();

		// Domain section headings present.
		for (const heading of [
			'catalyst & event',
			'valuation & quality',
			'momentum & technicals',
			'insider / flow'
		]) {
			await expect(card.getByText(heading, { exact: false })).toBeVisible();
		}

		// Dedup: each of these labels renders exactly once in the card.
		for (const label of ['off 52w high', 'ma200 dist', 'ma200 slope', 'fcff yield']) {
			await expect(card.getByText(label, { exact: false })).toHaveCount(1);
		}

		// Meta bar slimmed: no buffett/o'neil/catalyst chip in the meta row.
		const meta = card.locator('[data-testid="card-meta"]');
		await expect(meta.getByText('buffett', { exact: false })).toHaveCount(0);
		await expect(meta.getByText("o'neil", { exact: false })).toHaveCount(0);
		await expect(meta.getByText('catalyst', { exact: false })).toHaveCount(0);
		await expect(meta.getByText('layer-4', { exact: false })).toBeVisible();

		// Lens scores anchor their domain blocks.
		await expect(
			card.locator('[data-testid="block-valuation"]').getByText('buffett', { exact: false })
		).toBeVisible();
		await expect(
			card.locator('[data-testid="block-momentum"]').getByText("o'neil", { exact: false })
		).toBeVisible();
	});
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- smoke.test.ts -g "domain-grouped"`
Expected: FAIL — current DOM has no `catalyst & event` heading / `data-testid="card-meta"` / domain testids.

- [ ] **Step 3: Slim the meta-bar right cluster**

In `CandidateCard.svelte`, add `data-testid="card-meta"` to the meta-bar wrapper. Find the line:

```svelte
	<div class="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 sm:px-5 py-2 border-b border-grid">
```
Replace with:
```svelte
	<div data-testid="card-meta" class="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 sm:px-5 py-2 border-b border-grid">
```

Then, inside the `<div class="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">` cluster, DELETE the catalyst span and the entire expert-lens `<div class="flex items-center gap-x-4 border-l border-grid pl-4">…</div>` (both `ChipTip`s). Keep the layer-4 span, the `{#if (c.atr_penalty ?? 0) > 0}` extended `ChipTip`, and the conf span. After this edit the cluster contains exactly: layer-4 badge → extended chip (conditional) → conf.

Delete this block (the catalyst span):
```svelte
			<span class="inline-flex items-baseline gap-1.5 whitespace-nowrap">
				<span class="text-[9px] uppercase tracking-widest text-fg-muted">catalyst</span>
				<span class="text-xs font-bold lowercase text-violet">{c.catalyst_event_type ?? '—'}</span>
				<span class="text-[11px] text-fg-muted">{fmtNum(c.catalyst_strength, 2)}</span>
			</span>
```
And delete the whole expert-lens comment + `<div class="flex items-center gap-x-4 border-l border-grid pl-4">` … its closing `</div>` (the two `ChipTip term="buffett quality"` and `ChipTip term="o'neil momentum"` blocks).

- [ ] **Step 4: Replace the left-column body with four domain blocks**

In `CandidateCard.svelte`, replace everything from `<!-- Live equity thesis -->` through the end of the `<!-- Signals | Fundamentals + Technicals -->` grid (i.e. the `<div class="grid grid-cols-1 md:grid-cols-2"> … </div>` that closes just before the `<!-- Expert-panel deep-read … -->` comment) with the markup below. Leave the `<ExpertPanel … />` mount and the right column untouched.

```svelte
			<!-- CATALYST & EVENT — the reason this name surfaced: catalyst strength,
			     the thesis it drives, the source event, and the deterministic typed
			     facts. (Retires the standalone live.equity.thesis heading.) -->
			<div class="px-4 sm:px-5 py-4 border-b border-grid">
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">catalyst &amp; event</div>
				<div class="mb-4">
					<SignalBar
						label={`catalyst${c.catalyst_event_type ? ' · ' + c.catalyst_event_type : ''}`}
						value={c.catalyst_strength != null ? c.catalyst_strength * 100 : null}
						format={(v) => (v / 100).toFixed(2)}
					>
						{#snippet tooltipRich()}
							<span class="block">Layer-4 catalyst-floor score (0–1), combining:</span>
							<BulletList
								items={['news novelty', 'thematic alignment with the source event', 'freshness']}
							/>
							<TooltipNote
								>higher = stronger event-driven setup; <span class="font-bold">below</span> the
								<span class="whitespace-nowrap font-bold">0.55 floor</span> → candidate
								<span class="font-bold">filtered out</span></TooltipNote
							>
						{/snippet}
					</SignalBar>
				</div>
				<blockquote class="border-l-2 border-violet pl-4">
					{#if c.brief_tldr}
						<p class="text-fg text-sm leading-relaxed">{c.brief_tldr}</p>
					{:else}
						<p class="text-fg-dim text-sm leading-relaxed italic">{c.rationale}</p>
					{/if}
				</blockquote>
				<div class="mt-3 flex items-start gap-3 text-[11px]">
					<span class="text-fg-muted whitespace-nowrap">{fmtDate(c.source_event_published_at)}</span>
					<span class="w-px self-stretch bg-grid-strong" aria-hidden="true"></span>
					<a
						href={c.source_event_url}
						target="_blank"
						rel="noreferrer"
						aria-label={`${c.source_event_title ?? 'source event'} (opens in a new tab)`}
						class="inline-flex items-start gap-1 text-cyan hover:text-amber transition-colors underline underline-offset-2 min-w-0"
					>
						<span>{c.source_event_title}</span>
						<ExternalLink class="size-3 flex-shrink-0 mt-0.5" />
					</a>
				</div>
				{#if c.brief_template_id}
					<div class="mt-4 border-t border-grid pt-4">
						<TemplateFacts templateId={c.brief_template_id} facts={c.brief_template_facts} />
					</div>
				{/if}
			</div>

			<!-- Valuation & Quality | Momentum & Technicals — two analytical domains
			     side by side, each anchored by its expert lens score. -->
			<div class="grid grid-cols-1 md:grid-cols-2">
				<!-- VALUATION & QUALITY (Buffett anchors) -->
				<div data-testid="block-valuation" class="px-4 sm:px-5 py-4 md:border-r border-grid">
					<div class="flex flex-wrap items-baseline gap-x-2 gap-y-1 mb-3">
						<div class="text-[10px] uppercase tracking-widest text-cyan">valuation &amp; quality</div>
						{#if c.peer_cohort_level === 'thin'}
							<ChipTip
								term="THIN cohort"
								body="SIC peer cohort too small to compute a meaningful percentile (4-digit + 3-digit fallback both below 8 members). Sector-percentile bars are suppressed (shown as —)."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-red/10 text-red text-[9px] uppercase tracking-widest border border-red/40 cursor-help">thin cohort</span>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'sic3'}
							<ChipTip
								term="SIC-3 cohort"
								body="4-digit SIC cohort was too small; widened to the 3-digit prefix. Percentile computed over a broader peer set — still trustworthy but looser."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-cyan/10 text-cyan text-[9px] uppercase tracking-widest border border-cyan/30 cursor-help">sic-3 cohort</span>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'ff48'}
							<ChipTip
								term="FF-48 cohort"
								body="4-digit + 3-digit SIC cohorts were both too small; widened to the Fama-French 48-industry bucket. Percentile reflects a broader but economically coherent peer set."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-fg-muted/10 text-fg-muted text-[9px] uppercase tracking-widest border border-fg-muted/40 cursor-help">ff-48 cohort</span>
								{/snippet}
							</ChipTip>
						{/if}
					</div>
					<!-- Expert anchor: Buffett value/quality lens. -->
					<ChipTip term="buffett quality">
						{#snippet chip()}
							<div
								class="mb-4 flex items-baseline justify-between gap-2 cursor-help"
								class:opacity-60={buffLowCov}
							>
								<span class="text-[10px] uppercase tracking-widest text-fg-muted">buffett <span class="normal-case text-fg-dim">· value / quality</span></span>
								<span
									class="font-display text-base font-bold leading-none"
									class:text-green={buffTone === 'green'}
									class:text-amber={buffTone === 'amber'}
									class:text-fg-muted={buffTone === 'muted'}
									>{buffScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							</div>
						{/snippet}
						{#snippet bodyRich()}
							<MetricGrid rows={buffRows} align="right" />
							<p class="mt-2 text-center text-[15px] text-fg-dim"><Formula name="margin_of_safety" /></p>
							{#if buffScore === null}
								<p class="mt-1 text-fg-muted">not enough fundamentals to score</p>
							{:else if buffLowCov}
								<p class="mt-1 text-fg-muted">thin data, score down-weighted</p>
							{/if}
						{/snippet}
					</ChipTip>
					<div class="flex flex-col gap-y-4">
						<SignalBar
							label="fcff yield (sector %ile)"
							value={c.fcff_yield_sector_percentile}
							format={(v) => fmtPctile(v) + '%ile'}
							tooltip="Free-cash-flow-to-firm yield = FCFF / EV, ranked within sector. Higher = cheaper on a cash-generation basis. Paradigm #13 scorer (αt 1.18 IS, multi-signal corroboration only)."
						/>
						{#if fcffRaw.rawText}
							<div class="-mt-3 text-[10px] uppercase tracking-widest text-fg-muted">
								raw <span class="text-fg-dim font-bold normal-case whitespace-nowrap">{fcffRaw.rawText}</span>
							</div>
						{/if}
						<SignalBar
							label="valuation composite"
							value={c.valuation_composite_sector_percentile}
							format={(v) => fmtPctile(v) + '%ile'}
						>
							{#snippet tooltipRich()}
								<span class="block">Composite sector-%ile rank across 5 multiples:</span>
								<BulletList items={['PE', 'PS', 'EV/Revenue', 'EV/EBITDA', 'FCF margin']} />
								<TooltipNote>higher = cheaper than sector peers on several multiples at once</TooltipNote>
							{/snippet}
						</SignalBar>
					</div>
					<div class="text-[10px] uppercase tracking-widest text-fg-muted mt-4 mb-2">multiples</div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PE')}>pe</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_pe, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PS')}>ps</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ps, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/REV')}>ev/rev</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_rev, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/EBITDA')}>ev/ebitda</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_ebitda, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('FCF margin')}>fcf margin</JargonTip></dt><dd class="text-fg text-right">{c.valuation_fcf_margin !== null ? fmtPct(c.valuation_fcf_margin * 100) : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ROE')}>roe</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.roe_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('magic formula')}>magic formula</JargonTip></dt><dd class="text-fg text-right">
							{#if magic.mode === 'rank'}
								<span class="text-amber font-bold">#{magic.rank}</span>{#if magic.cohortN !== null}/{magic.cohortN}{/if}
							{:else}
								{magic.label}
							{/if}
						</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('financials age')}>financials age</JargonTip></dt><dd class="text-fg text-right">{c.valuation_financials_age_days != null ? Math.round(c.valuation_financials_age_days) + 'd' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('next earnings')}>next earnings</JargonTip></dt><dd class="text-fg text-right whitespace-nowrap">{fmtDate(c.next_earnings_date)}</dd>
					</dl>
				</div>

				<!-- MOMENTUM & TECHNICALS (O'Neil anchors) -->
				<div data-testid="block-momentum" class="px-4 sm:px-5 py-4 border-t md:border-t-0 border-grid">
					<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">momentum &amp; technicals</div>
					<!-- Expert anchor: O'Neil momentum lens. -->
					<ChipTip term="o'neil momentum">
						{#snippet chip()}
							<div class="mb-4 flex items-baseline justify-between gap-2 cursor-help">
								<span class="text-[10px] uppercase tracking-widest text-fg-muted">o'neil <span class="normal-case text-fg-dim">· momentum</span></span>
								<span
									class="font-display text-base font-bold leading-none"
									class:text-green={oneilScoreTone === 'green'}
									class:text-amber={oneilScoreTone === 'amber'}
									class:text-fg-muted={oneilScoreTone === 'muted'}
									>{oneilScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							</div>
						{/snippet}
						{#snippet bodyRich()}
							<MetricGrid rows={oneilRows} align="right" />
							{#if oneilScore === null}
								<p class="mt-1 text-fg-muted">momentum terms incomplete to score</p>
							{/if}
						{/snippet}
					</ChipTip>
					<div class="flex flex-col gap-y-4">
						<SignalBar
							label="rsi 14d"
							value={c.technical_rsi}
							format={(v) => v.toFixed(0)}
						>
							{#snippet tooltipRich()}
								<span class="block">Relative Strength Index, 14-day:</span>
								<MetricGrid
									rows={[
										{ key: '<30', value: 'oversold (potential reversal)' },
										{ key: '~50', value: 'neutral' },
										{ key: '>70', value: 'overbought (potential pullback)' }
									]}
									class="mt-1"
								/>
							{/snippet}
						</SignalBar>
						<SignalBar
							label="off 52w high"
							value={c.technical_pct_off_52w_high != null ? Math.abs(c.technical_pct_off_52w_high) : null}
							min={0}
							max={95}
							format={(v) => '-' + v.toFixed(1) + '%'}
							inverted
							tooltip="% below the 52-week high. Deeper drawdown = potential reversal OR continuation of secular decline. Pair with MA200 slope to discriminate."
						/>
						<SignalBar
							label="off 52w low"
							value={c.technical_pct_off_52w_low}
							min={0}
							max={200}
							format={(v) => '+' + v.toFixed(1) + '%'}
							tooltip="% above the 52-week low. Larger = stronger recovery from recent bottom."
						/>
						<SignalBar
							label="rel strength (sector %ile)"
							value={oneil?.oneil_rs_approx_pct ?? null}
							format={(v) => fmtPctile(v) + '%ile'}
							tooltip="O'Neil relative-strength rank — the stock's trailing return ranked against peers. Higher = stronger leadership. From the O'Neil momentum lens."
						/>
						<SignalBar
							label="vol z-score"
							value={c.technical_volume_zscore !== null ? Math.abs(c.technical_volume_zscore) : null}
							min={0}
							max={5}
							format={(v) => (c.technical_volume_zscore! >= 0 ? '+' : '-') + v.toFixed(1) + 'σ'}
						>
							{#snippet tooltipRich()}
								<span class="block">20-day volume z-score:</span>
								<MetricGrid
									rows={[
										{ key: '>+2σ', value: 'unusual buying interest (catalyst confirmation)' },
										{ key: '<−2σ', value: 'drying volume (waning thesis)' }
									]}
									class="mt-1"
								/>
							{/snippet}
						</SignalBar>
					</div>
					<div class="text-[10px] uppercase tracking-widest text-fg-muted mt-4 mb-2">trend</div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA50')}>ma50 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma50_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200')}>ma200 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma200_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200 slope')}>ma200 slope</JargonTip></dt><dd class="text-fg text-right whitespace-nowrap">{c.technical_ma200_slope_pct_per_day !== null ? fmtPct(c.technical_ma200_slope_pct_per_day, 3) + '/d' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ATR')}>atr</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_atr_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest">earnings yoy</dt><dd class="text-fg text-right whitespace-nowrap">{fmtPct(oneil?.oneil_earnings_growth_yoy_pct)}</dd>
					</dl>
				</div>
			</div>

			<!-- INSIDER / FLOW — ownership-flow domain (one metric, its own strip). -->
			<div class="px-4 sm:px-5 py-4 border-t border-grid">
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">insider / flow</div>
				{#if insider.mode === 'bar'}
					<SignalBar
						label="insider 90d (sector %ile)"
						value={insider.percentile}
						format={(v) => fmtPctile(v) + '%ile'}
						tooltip="Net opportunistic insider buying ({fmtUsdCompact(insider.netUsd)}) in the last 90 days, ranked within sector — shown only when there is net buying. Cohen-Malloy opportunistic classification; paradigm #11 scorer."
					/>
				{:else}
					<SignalBar
						label="insider 90d"
						value={null}
						placeholder={insider.label}
						tooltip="No net opportunistic insider buying in the last 90 days. The sector percentile is suppressed: a 0/negative dollar signal ranks high only relative to net-selling peers, which is not a buy signal. Cohen-Malloy opportunistic classification."
					/>
				{/if}
			</div>
```

- [ ] **Step 5: Add the `fcffRaw` derived value**

In the `<script>` block of `CandidateCard.svelte`, add `fcffYieldDisplay` to the `$lib/format` import, and add this derived (near the other `$derived` declarations, e.g. after `const magic = …`):

```ts
	// Merged fcff-yield Valuation row: the %ile drives the bar; the raw % is an
	// annotation shown below it (see fcffYieldDisplay). Replaces the old duplicate
	// raw-% row in FUNDAMENTALS.
	const fcffRaw = $derived(fcffYieldDisplay(c.fcff_yield_sector_percentile, c.fcff_yield_pct));
```

(Confirm `SignalBar`, `BulletList`, `TooltipNote`, `JargonTip`, `ChipTip`, `MetricGrid`, `Formula`, `fmtUsdCompact`, `fmtPctile`, `fmtPct`, `fmtNum`, `fmtDate`, `ExternalLink`, `TemplateFacts` are all already imported — they are, from the pre-existing card. `fmtNum` is still used by nothing in the meta bar now; it is still used in the multiples grid, so keep its import. If svelte-check flags an unused import, remove only the genuinely-unused one.)

- [ ] **Step 6: Run smoke + svelte-check to verify green**

Run:
```bash
pnpm check
pnpm test -- smoke.test.ts -g "domain-grouped"
pnpm test -- smoke.test.ts -g "renders all candidates"
```
Expected: `pnpm check` 0 errors; both smoke greps PASS (new structure asserted + per-day count/console-clean still hold).

- [ ] **Step 7: Commit**

```bash
git add src/lib/components/CandidateCard.svelte tests/smoke.test.ts
git commit -m "feat(web): regroup candidate card by analytical domain"
```

---

### Task 4: ExpertPanel — drop O'Neil numeric grid, point to Momentum block

The O'Neil numeric readouts (off-52w-high, rel-strength, ma200 slope/dist, earnings-YoY) now live in the Momentum block. Remove that grid from the drawer; keep the score header, the two audit-flag pillars, the source note, the disagreement scale, the Buffett section, and the `SCORER BREAKDOWN`. Add a one-line pointer so the O'Neil section is not empty when no flags fire.

**Files:**
- Modify: `src/lib/components/ExpertPanel.svelte`
- Modify: `tests/smoke.test.ts` (extend the `card — domain grouping` block with a drawer assertion)

**Interfaces:**
- Consumes: the existing `oneil`, `oneilScore`, `hasOneil`, `sections`, `EXPERT_KIND`, scale + scorer-breakdown logic — all unchanged.
- Produces: an opened drawer whose O'Neil section renders the score header + audit flags (when `=== true`) + source note + a "see Momentum & Technicals" pointer, and NO `oneilReadouts` `<dl>`.

- [ ] **Step 1: Write the failing drawer assertion**

Add to the `card — domain grouping` describe block in `tests/smoke.test.ts`:

```ts
	test(`expert.panel drawer omits the O'Neil numeric grid on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		// Open the drawer.
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const drawer = card.locator('[data-testid="expert-panel-body"]');
		await expect(drawer).toBeVisible();
		// Scorer breakdown preserved.
		await expect(drawer.getByText('scorer breakdown', { exact: false })).toBeVisible();
		// O'Neil numeric readout grid is gone (rel strength now only in the momentum block).
		await expect(drawer.getByText('rel strength', { exact: false })).toHaveCount(0);
		// Pointer to the momentum block present.
		await expect(drawer.getByText('momentum & technicals', { exact: false })).toBeVisible();
	});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- smoke.test.ts -g "omits the O'Neil numeric grid"`
Expected: FAIL — the drawer still renders the `oneilReadouts` `<dl>` (so `rel strength` count is 1, not 0) and has no `data-testid="expert-panel-body"`.

- [ ] **Step 3: Add the drawer body testid**

In `ExpertPanel.svelte`, find the opened-drawer container `{#if open}` → its first child `<div class="mt-3 space-y-4">` and add the testid:
```svelte
			<div data-testid="expert-panel-body" class="mt-3 space-y-4">
```

- [ ] **Step 4: Remove the O'Neil readout grid, add the pointer**

In the O'Neil branch (`{:else}` of `{#if isBuf}`), DELETE the readout `<dl>`:
```svelte
							<dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
								{#each oneilReadouts as r (r.label)}
									<div>
										<dt class="text-[9px] uppercase tracking-widest text-fg-muted">{r.label}</dt>
										<dd class="font-bold text-fg-dim whitespace-nowrap">{r.value}</dd>
									</div>
								{/each}
							</dl>
```
Keep the audit-flag `{#if oneil?.oneil_new_high_split_suspected === true || …}` block and the source `<p>` exactly as-is. Immediately AFTER the source `<p>…numeric-only, no LLM</p>`, add the pointer:
```svelte
						<p class="mt-1 text-[10px] leading-snug text-fg-muted">
							numeric readouts shown in <span class="text-fg-dim">Momentum &amp; Technicals</span>.
						</p>
```
Then DELETE the now-unused `oneilReadouts` `$derived` array from the `<script>` block (the `const oneilReadouts = $derived([...])` that lists off-52w-high / rel-strength / MA200 slope / MA200 dist / earnings-YoY). `fmtPctile` may become unused in this file after the removal — if `pnpm check` flags it, remove it from the import; otherwise leave it.

- [ ] **Step 5: Run to verify green**

Run:
```bash
pnpm check
pnpm test -- smoke.test.ts -g "omits the O'Neil numeric grid"
pnpm test:unit -- expertPanel
```
Expected: `pnpm check` 0 errors; the drawer smoke PASSES; the existing `expertPanel` unit tests still PASS (tone/consensus helpers untouched).

- [ ] **Step 6: Commit**

```bash
git add src/lib/components/ExpertPanel.svelte tests/smoke.test.ts
git commit -m "feat(web): slim expert drawer — O'Neil numerics move to the momentum block"
```

---

### Task 5: Full verification + visual confirmation

**Files:** none changed unless a regression is found.

- [ ] **Step 1: Run the full unit + smoke + check suite**

Run (from `apps/web/`):
```bash
pnpm test:unit
pnpm check
pnpm build
pnpm test
```
Expected: unit 188+ PASS (5 new fcff cases), `check` 0 errors, build succeeds, full Playwright smoke PASS (every route + every fixture brief + the new `card — domain grouping` block, all console-clean).

- [ ] **Step 2: Visual confirmation via Playwright MCP**

Start the dev server (`pnpm dev`, background) and open a brief route. Confirm on a real card:
- meta bar = identity · LAYER-4 · (extended when penalised) · CONF — no catalyst/buffett/o'neil chip;
- a `CATALYST & EVENT` block (catalyst bar + thesis + source link + typed facts);
- `VALUATION & QUALITY` with a Buffett header + fcff (%ile bar + raw annotation) + valuation-composite + multiples grid (fcff appears once);
- `MOMENTUM & TECHNICALS` with an O'Neil header + rsi/off-52w-high/off-52w-low/rel-strength/vol-z bars + trend grid (ma200 dist/slope appear once) + earnings-yoy;
- `INSIDER / FLOW` strip;
- opening `expert.panel` shows the scale + Buffett prose + O'Neil flags/pointer + `SCORER BREAKDOWN`, with no O'Neil numeric grid.
Check the browser console is clean and no atomic token (date, `-0.045%/d`, version string) wraps mid-string.

- [ ] **Step 3: Final state — no extra commit unless a fix was needed**

If Step 1/2 surfaced a fix, commit it with a `fix(web): …` message. Otherwise the feature is complete on the four task commits.

---

## Self-Review

**Spec coverage** — every spec section maps to a task:
- Meta-bar slim (remove catalyst + expert chips, keep extended) → Task 3 Step 3.
- Catalyst & Event block (thesis fold-in + catalyst bar + typed facts) → Task 3 Step 4.
- Valuation & Quality block (Buffett anchor + fcff merge + valuation-composite + multiples) → Task 1 + Task 3 Steps 4–5.
- Momentum & Technicals block (O'Neil anchor + momentum bars + rel-strength + trend grid + earnings-YoY) → Task 3 Step 4.
- Insider / Flow strip → Task 3 Step 4.
- ExpertPanel O'Neil-grid removal + pointer, scale/Buffett/flags/scorer-breakdown preserved → Task 4.
- Dedup (fcff-yield, off-52w-high, ma200 dist/slope, catalyst-strength once) → Task 3 Step 1 assertions + the moves.
- Edge cases (sparse blob `—`, thin cohort suppression, flash-path no typed-facts, null momentum terms) → preserved by reusing the existing null-paths/snippets verbatim; cohort chip moved with its three variants (Task 3 Step 4).
- Preserve atr-tilt surfaces (extended chip, scorer breakdown) → Global Constraints + Task 3 Step 3 + Task 4 (untouched).
- Tests (unit fcff, smoke structure/dedup/meta/drawer) → Tasks 1, 3, 4; full run Task 5.

**Placeholder scan** — no TBD/TODO; every code step shows complete markup; commands have expected output.

**Type consistency** — `fcffYieldDisplay(pctile, rawPct) → {pctileText, rawText}` defined in Task 1, consumed as `fcffRaw.rawText` in Task 3. `data-testid`s (`card-meta`, `block-valuation`, `block-momentum`, `expert-panel-body`) defined where the smoke tests query them. O'Neil-sourced Momentum fields read as `oneil?.oneil_rs_approx_pct` / `oneil?.oneil_earnings_growth_yoy_pct` (the `oneil` derived already exists on the card). `oneilReadouts` removed in Task 4 Step 4 (was only used by the deleted grid).
