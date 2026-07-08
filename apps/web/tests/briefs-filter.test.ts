import { test, expect, type Page } from '@playwright/test';

// The /briefs archive toolbar: free-text search over date+top-theme, a top-theme
// facet (shared LedgerFilterBar), an "N of M" count, clear-all, and URL-sync
// (?q=, ?theme=). The days list comes from /api/v1/days.

const DAYS = [
	{ date: '2026-05-18', n_candidates: 5, n_themes: 3, top_theme: 'ai-infra' },
	{ date: '2026-05-19', n_candidates: 4, n_themes: 2, top_theme: 'ai-infra' },
	{ date: '2026-05-20', n_candidates: 6, n_themes: 4, top_theme: 'ai-infra' },
	{ date: '2026-06-01', n_candidates: 3, n_themes: 2, top_theme: 'high-gas' },
	{ date: '2026-06-02', n_candidates: 7, n_themes: 5, top_theme: 'high-gas' },
	{ date: '2026-06-03', n_candidates: 2, n_themes: 1, top_theme: 'quantum' }
];

async function stub(page: Page) {
	await page.route('**/api/v1/market/status**', (r) =>
		r.fulfill({
			json: {
				is_trading_day: false,
				is_half_day: false,
				is_open_now: false,
				next_open_iso: '2099-01-01T13:30:00+00:00',
				next_close_iso: '2099-01-01T20:00:00+00:00',
				exchange: 'XNYS'
			}
		})
	);
	await page.route('**/api/v1/days**', (r) =>
		r.fulfill({ json: { data: DAYS, meta: { total: DAYS.length, limit: 200, offset: 0 } } })
	);
}

const rows = (page: Page) => page.locator('tbody tr');

test('search narrows the archive and updates count + URL', async ({ page }) => {
	await stub(page);
	await page.goto('/briefs');
	await expect(page.getByTestId('briefs-filter')).toBeVisible();
	await expect(rows(page)).toHaveCount(6);

	// Date search — a month prefix keeps only June days.
	await page.getByTestId('briefs-search').fill('2026-06');
	await expect(rows(page)).toHaveCount(3);
	await expect(page.getByTestId('briefs-match-count')).toContainText('3 of 6');
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('2026-06');

	// Theme substring search.
	await page.getByTestId('briefs-search').fill('quantum');
	await expect(rows(page)).toHaveCount(1);
});

test('a top-theme facet chip filters, clear-all resets, and a ?theme= deep link seeds', async ({
	page
}) => {
	await stub(page);
	await page.goto('/briefs');
	await expect(rows(page)).toHaveCount(6);

	await page.getByTestId('briefs-filter').getByRole('button', { name: /^#ai-infra/ }).click();
	await expect(rows(page)).toHaveCount(3);
	await expect.poll(() => new URL(page.url()).searchParams.get('theme')).toBe('ai-infra');

	await page.getByTestId('briefs-clear-all').click();
	await expect(rows(page)).toHaveCount(6);
	await expect.poll(() => new URL(page.url()).searchParams.has('theme')).toBe(false);

	// Deep link arrives pre-filtered.
	await page.goto('/briefs?theme=high-gas');
	await expect(rows(page)).toHaveCount(2);
});
