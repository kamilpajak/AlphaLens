/**
 * Real-wire smoke test against a running Django backend.
 *
 * Default behaviour: skipped. The existing `smoke.test.ts` covers UI
 * rendering hermetically via `page.route()` mocks; this test exists for
 * the F6 cutover moment when we need to verify "the new backend actually
 * serves what the frontend asks for", without the route interceptor in
 * the loop.
 *
 * To run it (manual, F6 / F7 verification):
 *
 *   1. Start Django:    cd apps/alphalens-django && uv run python manage.py runserver 8000
 *   2. Populate DB:     uv run python manage.py rebuild_briefs_cache --briefs-dir ~/.alphalens/thematic_briefs
 *   3. Run Playwright:  cd web && VITE_API_TARGET=http://127.0.0.1:8000 DJANGO_SMOKE=1 pnpm test django-smoke
 *
 * The `VITE_API_TARGET` env is consumed by vite.config.ts to route the
 * preview server's `/api/*` proxy to Django instead of the legacy FastAPI
 * container, so the SPA's same-origin fetches reach the right backend.
 */

import { expect, test, type Page } from '@playwright/test';

const enabled = process.env.DJANGO_SMOKE === '1';

test.describe('Django backend smoke', () => {
	test.skip(!enabled, 'Set DJANGO_SMOKE=1 to enable (requires a running Django app)');

	test('dashboard loads days index and latest brief', async ({ page }) => {
		const consoleErrors = collectConsoleErrors(page);
		await page.goto('/');
		// Page renders without going into SvelteKit's error boundary.
		await expect(page.getByRole('main')).toBeVisible();
		// No client-side errors during initial load (transient network
		// failures would surface here even if the SPA gracefully degrades
		// to the empty state).
		expect(consoleErrors).toEqual([]);
	});

	test('GET /api/v1/days returns envelope shape', async ({ request }) => {
		// `request` uses APIRequestContext which bypasses page.route() — that
		// is the point here: we exercise the actual Django route through the
		// preview's proxy. (Recorded in MEMORY as
		// `feedback_playwright_page_request_bypasses_route`.)
		const resp = await request.get('/api/v1/days?limit=5');
		expect(resp.ok()).toBeTruthy();
		const body = await resp.json();
		expect(body).toHaveProperty('data');
		expect(body).toHaveProperty('meta');
		expect(body.meta).toMatchObject({
			total: expect.any(Number),
			limit: expect.any(Number),
			offset: expect.any(Number)
		});
	});

	test('GET /api/v1/stats has top-level counters', async ({ request }) => {
		const resp = await request.get('/api/v1/stats');
		expect(resp.ok()).toBeTruthy();
		const body = await resp.json();
		for (const key of [
			'n_days',
			'n_candidates',
			'n_themes',
			'earliest_date',
			'latest_date',
			'top_themes'
		]) {
			expect(body).toHaveProperty(key);
		}
	});
});

function collectConsoleErrors(page: Page): string[] {
	const errors: string[] = [];
	page.on('console', (msg) => {
		if (msg.type() === 'error') errors.push(msg.text());
	});
	return errors;
}
