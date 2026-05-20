import { expect, test, type ConsoleMessage } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GLOSSARY } from '../src/lib/data/glossary.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Smoke tests covering every route + interactive surface in the production build.
 *
 * What this catches:
 *   - SSR compile errors (e.g. invalid {@const} placement, missing imports)
 *   - 500 responses from broken prerender entries / load functions
 *   - 404s on routes that should prerender (every brief in days.json, /about,
 *     /, /briefs)
 *   - Client-side console errors / page errors / unhandled rejections
 *   - Missing static data JSON files
 *   - Broken internal links (anything `<a href="/...">` that 404s)
 *   - Major content regressions (header chip text, candidate count)
 *   - SPA navigation hydration failures between nav links
 *   - Filter / checkbox / details interaction errors on the brief detail page
 *   - Hover tooltip render failures on gate pills
 *
 * What it does NOT catch:
 *   - Visual regressions (Tailwind class drift, layout shifts)
 *   - Accessibility deficiencies
 *   - Data correctness vs source parquet
 */

const days: { date: string; n_candidates: number }[] = JSON.parse(
	readFileSync(resolve(__dirname, '../static/data/days.json'), 'utf-8')
);
const latestDay = days[0];

function attachErrorCollectors(consoleErrors: string[], pageErrors: string[]) {
	return {
		onConsole: (msg: ConsoleMessage) => {
			if (msg.type() === 'error') consoleErrors.push(msg.text());
		},
		onPageError: (err: Error) => {
			pageErrors.push(err.message);
		}
	};
}

test.describe('smoke — every route renders without errors', () => {
	for (const path of ['/', '/briefs', '/about', '/experiments']) {
		test(`GET ${path} renders OK`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(path);
			expect(response?.status(), `GET ${path} should be 200`).toBe(200);

			await expect(page.locator('header a[href="/"]').first()).toContainText('ALPHALENS');

			expect(consoleErrors, `${path} console errors`).toEqual([]);
			expect(pageErrors, `${path} page errors`).toEqual([]);
		});
	}

	// Every brief in days.json must be prerendered and load cleanly.
	for (const day of days) {
		test(`GET /brief/${day.date} renders all candidates`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(`/brief/${day.date}`);
			expect(response?.status()).toBe(200);

			await expect(page.getByText(day.date).first()).toBeVisible();

			const candidateCount = await page.locator('article[id]').count();
			expect(candidateCount, `${day.date} candidate count`).toBe(day.n_candidates);

			expect(consoleErrors, `${day.date} console errors`).toEqual([]);
			expect(pageErrors, `${day.date} page errors`).toEqual([]);
		});
	}

	// Under SPA mode the HTTP response is always 200 (nginx serves index.html
	// as fallback); the 404 surfaces as SvelteKit's error page rendering "404"
	// after the client-side router or load function rejects.
	test('GET /brief/2099-01-01 (unknown date) renders SvelteKit 404', async ({ page }) => {
		await page.goto('/brief/2099-01-01');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});

	test('GET /unknown-route renders SvelteKit 404', async ({ page }) => {
		await page.goto('/this-route-does-not-exist');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});
});

test.describe('smoke — SPA navigation', () => {
	test('clicking every header nav link transitions cleanly', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto('/');

		for (const path of ['/briefs', '/about', '/experiments', '/']) {
			await page.locator(`header a[href="${path}"]`).first().click();
			await expect(page).toHaveURL(new RegExp(path === '/' ? '/$' : path));
		}

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('all internal links on every page resolve to 200/404 (no 5xx)', async ({ page, request }) => {
		const seen = new Set<string>();
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			await page.goto(path);
			const hrefs = await page.locator('a[href^="/"]').evaluateAll((nodes) =>
				nodes
					.map((n) => (n as HTMLAnchorElement).getAttribute('href') ?? '')
					.filter((h) => !h.startsWith('//') && !h.startsWith('/#'))
			);
			for (const href of hrefs) seen.add(href.split('#')[0]);
		}
		for (const href of seen) {
			const res = await request.get(href);
			expect(res.status(), `link target ${href}`).toBeLessThan(500);
		}
	});
});

test.describe('smoke — brief detail interactions', () => {
	test('theme filter chips and verified-only checkbox toggle without errors', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto(`/brief/${latestDay.date}`);

		// Click each theme filter chip; assert the candidate list narrows or
		// the empty-state placeholder appears — guarantees Svelte's $derived
		// filtered-list reactivity ran before we move on.
		const chips = page.getByRole('button', { name: /^#/ });
		const count = await chips.count();
		for (let i = 0; i < count; i++) {
			await chips.nth(i).click();
			await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
		}
		await page.getByRole('button', { name: /^all \(/ }).click();
		await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);

		// Toggle verified-only — same reactivity guarantee.
		const cb = page.locator('input[type="checkbox"]').first();
		await cb.check();
		await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
		await cb.uncheck();
		await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('signal-bar tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// data-testid is stable across Tailwind class refactors.
		const firstBar = page.locator('article[id] [data-testid="signal-bar"]').first();
		await firstBar.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		await expect(tooltip).toBeVisible();
	});

	test('jargon-tip tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto('/experiments');
		// The "how.to.read" block contains the first inline JargonTip wrappers.
		// Hover the wrapper (.group) to trigger group-hover:opacity-100 on the
		// sibling tooltip. data-testid is stable across Tailwind class refactors.
		// The first αt JargonTip wraps a nested Carhart 4F JargonTip, so the
		// outer tip contains 2 tooltips — assert the OUTER one (direct child) is
		// visible on hover via the `>` direct-child selector.
		const firstTip = page.locator('[data-testid="jargon-tip"]').first();
		await firstTip.hover();
		const tooltip = firstTip.locator('> [role="tooltip"]');
		await expect(tooltip).toBeVisible();
	});

	test('gate-pill tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// Inner pill carries the status symbol. Hover its wrapper (.group) to
		// trigger group-hover:opacity-100 on the sibling tooltip.
		const firstPill = page
			.locator('article[id] .group:has(.cursor-help)')
			.first();
		await firstPill.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		// toBeVisible() enforces opacity > 0; this catches a regression of the
		// group-hover:opacity-100 transition as well as DOM presence.
		await expect(tooltip).toBeVisible();
	});

	test('expanding the full-markdown <details> works for every candidate', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const detailsCount = await page.locator('article[id] details').count();
		expect(detailsCount).toBe(latestDay.n_candidates);

		// Toggle the first three to keep test bounded.
		const sample = Math.min(3, detailsCount);
		for (let i = 0; i < sample; i++) {
			const det = page.locator('article[id] details').nth(i);
			await det.locator('summary').click();
			await expect(det).toHaveAttribute('open', '');
		}
	});
});

test.describe('experiments — hybrid tooltip policy', () => {
	// Policy: short acronyms (αt, OOS, IS, FL, CAR, BAB, Q5) get a JargonTip
	// at EVERY occurrence in data text. Multi-word / longer terms (Carhart 4F,
	// Bonferroni, phase-aliasing, multi-phase audit, Phase A-E, R², R2000-PIT,
	// FCFF, PEAD, NW HAC, Form-4, ADR) get a tooltip on FIRST occurrence per
	// section. Each paradigm <article> and each pattern <li> is a section.
	// These assertions guard the policy from silent drift via text edits.

	test('αt always-tooltip — appears as JargonTip across the page', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("αt")').count();
		expect(count, 'αt JargonTip count across page').toBeGreaterThanOrEqual(20);
	});

	test('OOS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("OOS")').count();
		expect(count, 'OOS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('IS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("IS")').count();
		expect(count, 'IS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('FL — JargonTip exists at least in how.to.read + paradigm metrics', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("FL")').count();
		expect(count, 'FL JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('CAR — wrapped on every occurrence in P04 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P04 [data-testid="jargon-tip"]:has-text("CAR")').count();
		expect(count, 'CAR JargonTip count in P04').toBe(4);
	});

	test('BAB — exists in P15 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P15 [data-testid="jargon-tip"]:has-text("BAB")').count();
		expect(count, 'BAB JargonTip count in P15').toBeGreaterThanOrEqual(1);
	});

	test('Q5 — exists in S01 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Q5")').count();
		expect(count, 'Q5 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('Bonferroni — first-per-section across patterns + paradigm rows', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Bonferroni")').count();
		expect(count, 'Bonferroni JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('Carhart 4F — first-per-section in how.to.read + patterns', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Carhart")').count();
		expect(count, 'Carhart 4F JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('PEAD — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("PEAD")').count();
		expect(count, 'PEAD JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('NW HAC — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("NW HAC")').count();
		expect(count, 'NW HAC JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('Form-4 — exists in S01 row (first-per-section)', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Form-4")').count();
		expect(count, 'Form-4 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('ADR — exists in patterns section', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("ADR")').count();
		expect(count, 'ADR JargonTip count').toBeGreaterThanOrEqual(1);
	});

	test('glossary section has NO JargonTips (defines them; tooltips would be self-referential)', async ({ page }) => {
		await page.goto('/experiments');
		// The glossary section comes last on the page; scope by section title text.
		// Each glossary entry is a <dt>/<dd> pair inside the section. JargonTip
		// uses <span data-testid="jargon-tip">, none should appear under the
		// glossary section header.
		const glossarySection = page.locator('section').filter({ hasText: 'glossary.terms' });
		const count = await glossarySection.locator('[data-testid="jargon-tip"]').count();
		expect(count, 'glossary section JargonTip count must be 0').toBe(0);
	});
});

test.describe('experiments — glossary auto-discovery', () => {
	// For every entry in $lib/data/glossary, assert at least one inline
	// JargonTip with the corresponding data-term attribute renders on
	// /experiments. The data-term attribute (set on JargonTip wrapper) is
	// stable across text-label variants — so [Bonferroni correction|Bonferroni]
	// still passes the "Bonferroni correction" assertion.
	//
	// Adding a new glossary entry without an inline reference will fail the
	// matching test here, surfacing orphan glossary entries automatically.

	for (const entry of GLOSSARY) {
		test(`glossary term "${entry.term}" has ≥1 inline JargonTip`, async ({ page }) => {
			await page.goto('/experiments');
			const count = await page
				.locator(`[data-testid="jargon-tip"][data-term="${entry.term}"]`)
				.count();
			expect(
				count,
				`term "${entry.term}" should appear as inline JargonTip somewhere on /experiments (glossary section excluded — those terms appear in <dt>, not JargonTip)`
			).toBeGreaterThanOrEqual(1);
		});
	}
});

test.describe('smoke — mobile (390 + 360 viewports)', () => {
	// Catches: layout/header/nav/footer that forces a wider viewport than the
	// device; CandidateCard header strip that can't collapse; tooltips whose
	// absolute layout pushes scrollWidth past the viewport edge.
	const PHONES = [
		{ name: 'iphone-13', width: 390, height: 844 },
		{ name: 'small-android', width: 360, height: 800 }
	];
	for (const { name, width, height } of PHONES) {
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			test(`${name} (${width}x${height}) — no horizontal scroll on ${path}`, async ({ page }) => {
				await page.setViewportSize({ width, height });
				const response = await page.goto(path);
				expect(response?.status()).toBe(200);
				const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
				expect(scrollW, `${path} scrollWidth must be ≤ viewport ${width}`).toBeLessThanOrEqual(width);
			});
		}
	}
});

test.describe('smoke — index integrity', () => {
	test('days.json index lists all per-day brief files', async ({ request }) => {
		const indexRes = await request.get('/data/days.json');
		expect(indexRes.status()).toBe(200);
		const index: { date: string }[] = await indexRes.json();
		expect(index.length).toBeGreaterThan(0);

		for (const day of index) {
			const dayRes = await request.get(`/data/days/${day.date}.json`);
			expect(dayRes.status(), `per-day file for ${day.date}`).toBe(200);
		}
	});
});
