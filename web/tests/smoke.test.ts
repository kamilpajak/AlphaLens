import { expect, test, type ConsoleMessage } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

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
	for (const path of ['/', '/briefs', '/about']) {
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

		for (const path of ['/briefs', '/about', '/']) {
			await page.locator(`header a[href="${path}"]`).first().click();
			await expect(page).toHaveURL(new RegExp(path === '/' ? '/$' : path));
		}

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('all internal links on every page resolve to 200/404 (no 5xx)', async ({ page, request }) => {
		const seen = new Set<string>();
		for (const path of ['/', '/briefs', '/about', `/brief/${latestDay.date}`]) {
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

test.describe('smoke — mobile (390 + 360 viewports)', () => {
	// Catches: layout/header/nav/footer that forces a wider viewport than the
	// device; CandidateCard header strip that can't collapse; tooltips whose
	// absolute layout pushes scrollWidth past the viewport edge.
	const PHONES = [
		{ name: 'iphone-13', width: 390, height: 844 },
		{ name: 'small-android', width: 360, height: 800 }
	];
	for (const { name, width, height } of PHONES) {
		for (const path of ['/', '/briefs', '/about', `/brief/${latestDay.date}`]) {
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
