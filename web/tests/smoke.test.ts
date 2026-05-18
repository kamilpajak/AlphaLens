import { expect, test, type ConsoleMessage } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Smoke tests covering every route in the production build.
 *
 * What this catches:
 *   - SSR compile errors (e.g. invalid `{@const}` placement, missing imports)
 *   - 500 responses from broken prerender entries / load functions
 *   - Client-side console errors / page errors
 *   - Missing static data JSON
 *   - Major content regressions (header chip text, candidate count)
 *
 * What it does NOT catch:
 *   - Visual regressions (Tailwind class drift, layout shifts)
 *   - Tooltip / interaction-only bugs (no hover assertions yet)
 *   - Accessibility deficiencies
 *   - Data correctness vs source parquet
 *
 * Add more focused tests as failure modes surface.
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

			// Layout always present (brand link in top-bar).
			await expect(page.locator('header a[href="/"]').first()).toContainText('ALPHALENS');

			expect(consoleErrors, `${path} console errors`).toEqual([]);
			expect(pageErrors, `${path} page errors`).toEqual([]);
		});
	}

	test(`GET /brief/${latestDay.date} renders all candidates`, async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		const response = await page.goto(`/brief/${latestDay.date}`);
		expect(response?.status()).toBe(200);

		// Header chips reflect the loaded brief.
		await expect(page.getByText(latestDay.date).first()).toBeVisible();
		await expect(page.getByText(`${latestDay.n_candidates}`, { exact: false }).first()).toBeVisible();

		// At least one candidate article is rendered.
		const candidateCount = await page.locator('article[id]').count();
		expect(candidateCount).toBe(latestDay.n_candidates);

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('GET /brief/2099-01-01 (unknown date) returns 404', async ({ page }) => {
		const response = await page.goto('/brief/2099-01-01', { waitUntil: 'domcontentloaded' });
		expect(response?.status()).toBe(404);
	});
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
